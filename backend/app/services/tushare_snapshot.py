from __future__ import annotations

"""
基于 TuShare 的每日截面快照服务：
- 市值：float_mv / total_mv
- 估值：pe_ttm / pb
- 状态：is_suspended / is_st / is_limit_up / is_limit_down

注意：本模块只负责从 TuShare 拉取并在进程内缓存，不直接落库。
"""

import logging
from dataclasses import asdict, dataclass
import json
from datetime import date, timedelta
import os
from pathlib import Path
import time
import threading
from typing import Dict, Optional, Tuple
import uuid

import pandas as pd
try:
    import redis
except Exception:  # pragma: no cover - 运行环境未安装 redis 时兜底
    redis = None

from app.core.trade_calendar import get_last_trade_date, get_last_trade_date_str
from app.data_platform.db import _conn
from app.integrations.tushare_client import get_tushare_client

logger = logging.getLogger("quant.tushare_snapshot")
def _resolve_recent_trade_date(d: date) -> str:
    """计算最近交易日（周末/节假日/当日 15:00 前 -> 前一交易日）。"""
    return get_last_trade_date_str(d, for_data_availability=True)


def _daily_basic_with_timeout(client, trade_date: str) -> pd.DataFrame:
    """
    严格使用 TuShare Pro daily_basic（遵循 ts.pro_api + _DataApi__token/_DataApi__http_url）。
    """
    fields = "ts_code,trade_date,close,total_mv,circ_mv,turnover_rate,pe_ttm,pb"
    try:
        df = client.daily_basic_resilient(trade_date=trade_date, ts_code="", fields=fields)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"daily_basic failed for {trade_date}: {str(e)[:160]}") from e


@dataclass
class DailySnapshotRow:
    trade_date: str  # YYYYMMDD
    ts_code: str
    symbol: str  # 6 位代码
    name: str
    close: float
    total_mv: float
    float_mv: float
    turnover_rate: float
    pe_ttm: float
    pb: float
    is_st: bool
    is_suspended: bool
    is_limit_up: bool
    is_limit_down: bool


_snapshot_cache: Dict[str, Dict[str, DailySnapshotRow]] = {}
_snapshot_lock = threading.Lock()
_SNAPSHOT_DISK_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "tushare_snapshot"
_LOCK_WAIT_SEC = 20.0
_LOCK_STALE_SEC = 60.0
_LOCK_POLL_SEC = 0.2
_REDIS_LOCK_TTL_SEC = 30
_REDIS_CACHE_TTL_SEC = 6 * 60 * 60
_REDIS_EMPTY_CACHE_TTL_SEC = 5 * 60
_DISK_EMPTY_CACHE_TTL_SEC = 5 * 60
_redis_client = None
_redis_disabled = False
_SNAPSHOT_FAIL_BACKOFF_SEC = 60.0
_snapshot_fail_until: Dict[str, float] = {}


def _normalize_symbol_from_ts_code(ts_code: str) -> str:
    """
    将 ts_code（如 000001.SZ）转换为 6 位代码（000001），与系统中其它地方保持一致。
    """
    if not ts_code:
        return ""
    return str(ts_code).split(".")[0].zfill(6)


def _redis_lock_key(trade_date: str) -> str:
    return f"quant:tushare_snapshot:lock:{trade_date}"


def _redis_cache_key(trade_date: str) -> str:
    return f"quant:tushare_snapshot:data:{trade_date}"


def _get_redis_client():
    global _redis_client, _redis_disabled
    if _redis_client is not None or _redis_disabled:
        return _redis_client
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        return None
    if redis is None:
        logger.warning("TuShare 快照: 配置了 REDIS_URL 但 redis 依赖不可用，将回退本地锁")
        _redis_disabled = True
        return None
    try:
        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        client.ping()
        _redis_client = client
        logger.info("TuShare 快照: 已启用 Redis 分布式锁")
        return _redis_client
    except Exception as e:
        logger.warning("TuShare 快照: Redis 不可用（%s），将回退本地锁", str(e))
        _redis_disabled = True
        return None


def _load_snapshot_from_redis(trade_date: str) -> Optional[Dict[str, DailySnapshotRow]]:
    client = _get_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_redis_cache_key(trade_date))
        if not raw:
            return None
        payload = json.loads(raw)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        out: Dict[str, DailySnapshotRow] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            row = DailySnapshotRow(**item)
            out[row.symbol] = row
        return out
    except Exception as e:
        logger.warning("TuShare 快照: Redis 缓存读取失败，将忽略（trade_date=%s, err=%s）", trade_date, str(e))
        return None


def _save_snapshot_to_redis(trade_date: str, data: Dict[str, DailySnapshotRow]) -> None:
    client = _get_redis_client()
    if client is None:
        return
    try:
        payload = {
            "trade_date": trade_date,
            "rows": [asdict(v) for v in data.values()],
        }
        ttl = _REDIS_EMPTY_CACHE_TTL_SEC if not data else _REDIS_CACHE_TTL_SEC
        client.set(_redis_cache_key(trade_date), json.dumps(payload, ensure_ascii=False), ex=ttl)
    except Exception as e:
        logger.warning("TuShare 快照: Redis 缓存写入失败，将忽略（trade_date=%s, err=%s）", trade_date, str(e))


def _acquire_redis_lock(trade_date: str) -> Optional[str]:
    client = _get_redis_client()
    if client is None:
        return None
    token = uuid.uuid4().hex
    try:
        ok = client.set(_redis_lock_key(trade_date), token, nx=True, ex=_REDIS_LOCK_TTL_SEC)
        return token if ok else None
    except Exception as e:
        logger.warning("TuShare 快照: Redis 加锁失败，将回退本地锁（trade_date=%s, err=%s）", trade_date, str(e))
        return None


def _release_redis_lock(trade_date: str, token: Optional[str]) -> None:
    if not token:
        return
    client = _get_redis_client()
    if client is None:
        return
    lua = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""
    try:
        client.eval(lua, 1, _redis_lock_key(trade_date), token)
    except Exception:
        pass


def _ensure_disk_cache_dir() -> None:
    _SNAPSHOT_DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _snapshot_cache_file(trade_date: str) -> Path:
    return _SNAPSHOT_DISK_CACHE_DIR / f"{trade_date}.json"


def get_latest_cached_snapshot_date(*, on_or_before: str | None = None) -> Optional[str]:
    """
    返回磁盘快照缓存中已有的最新交易日 YYYYMMDD；无有效缓存时返回 None。
    指定 on_or_before 时只允许回退到目标日或更早，防止历史研究读取未来截面。
    """
    if not _SNAPSHOT_DISK_CACHE_DIR.exists():
        return None
    latest: Optional[str] = None
    for p in _SNAPSHOT_DISK_CACHE_DIR.glob("*.json"):
        if ".tmp." in p.name or p.name.endswith(".lock"):
            continue
        stem = p.stem
        if len(stem) == 8 and stem.isdigit():
            if on_or_before and stem > str(on_or_before).replace("-", "")[:8]:
                continue
            if latest is None or stem > latest:
                latest = stem
    return latest


def _snapshot_lock_file(trade_date: str) -> Path:
    return _SNAPSHOT_DISK_CACHE_DIR / f"{trade_date}.lock"


def _load_snapshot_from_disk(trade_date: str) -> Optional[Dict[str, DailySnapshotRow]]:
    """
    读取磁盘快照缓存（跨进程共享）：
    - 返回 None：无缓存或损坏
    - 返回 {}：缓存存在但快照不可用（降级结果）
    """
    p = _snapshot_cache_file(trade_date)
    if not p.exists():
        return None


def _load_snapshot_from_postgres(trade_date: str) -> Optional[Dict[str, DailySnapshotRow]]:
    """Read the normalized daily snapshot from PostgreSQL, our durable store."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT ts_code,trade_date,symbol,name,close,total_mv,circ_mv,turnover_rate,
                          pe_ttm,pb,is_suspended,up_limit,down_limit
                   FROM dc_stock_daily_snapshot WHERE trade_date=?""",
                (trade_date,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL snapshot read failed trade_date=%s: %s", trade_date, str(exc)[:160])
        return None
    if not rows:
        return None
    out: Dict[str, DailySnapshotRow] = {}
    for raw in rows:
        row = dict(raw)
        symbol = _normalize_symbol_from_ts_code(str(row.get("symbol") or row.get("ts_code") or ""))
        if not symbol:
            continue
        close = float(row.get("close") or 0)
        up_limit = float(row.get("up_limit") or 0)
        down_limit = float(row.get("down_limit") or 0)
        item = DailySnapshotRow(
            trade_date=str(row.get("trade_date") or trade_date), ts_code=str(row.get("ts_code") or ""),
            symbol=symbol, name=str(row.get("name") or ""), close=close,
            total_mv=float(row.get("total_mv") or 0), float_mv=float(row.get("circ_mv") or 0),
            turnover_rate=float(row.get("turnover_rate") or 0), pe_ttm=float(row.get("pe_ttm") or 0),
            pb=float(row.get("pb") or 0), is_st=_infer_is_st(str(row.get("name") or "")),
            is_suspended=bool(row.get("is_suspended")),
            is_limit_up=up_limit > 0 and close >= up_limit * 0.9999,
            is_limit_down=down_limit > 0 and close <= down_limit * 1.0001,
        )
        out[symbol] = item
    return out or None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        if len(rows) == 0:
            # 空快照仅短时间有效，避免一次异常后长期“粘住”导致持续跳过市值/估值过滤。
            try:
                age = time.time() - p.stat().st_mtime
                if age > _DISK_EMPTY_CACHE_TTL_SEC:
                    return None
            except OSError:
                return None
        out: Dict[str, DailySnapshotRow] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            row = DailySnapshotRow(**item)
            out[row.symbol] = row
        return out
    except Exception:
        logger.warning("TuShare 快照: 磁盘缓存读取失败，将忽略并重拉（trade_date=%s）", trade_date)
        return None


def _save_snapshot_to_disk(trade_date: str, data: Dict[str, DailySnapshotRow]) -> None:
    _ensure_disk_cache_dir()
    p = _snapshot_cache_file(trade_date)
    tmp = _SNAPSHOT_DISK_CACHE_DIR / f"{trade_date}.json.tmp.{os.getpid()}"
    payload = {
        "trade_date": trade_date,
        "rows": [asdict(v) for v in data.values()],
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _load_existing_non_empty_snapshot(trade_date: str) -> Optional[Dict[str, DailySnapshotRow]]:
    """
    读取已有的非空快照（内存/Redis/磁盘），用于远端失败时避免把有效缓存覆盖为空。
    """
    mem = _snapshot_cache.get(trade_date)
    if mem:
        return mem
    redis_cached = _load_snapshot_from_redis(trade_date)
    if redis_cached:
        return redis_cached
    disk_cached = _load_snapshot_from_disk(trade_date)
    if disk_cached:
        return disk_cached
    return None


def _try_acquire_file_lock(trade_date: str) -> Tuple[Optional[int], Path]:
    """
    跨进程锁：用 O_EXCL 创建 lock 文件，成功返回 fd；失败返回 None。
    """
    _ensure_disk_cache_dir()
    lock_path = _snapshot_lock_file(trade_date)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        return fd, lock_path
    except FileExistsError:
        return None, lock_path


def _release_file_lock(fd: Optional[int], lock_path: Optional[Path]) -> None:
    # 只有拿到 fd 才表示当前进程确实持有锁，避免误删他人锁文件。
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    if lock_path is not None:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _infer_is_st(name: str) -> bool:
    """
    根据股票名称粗略判断是否 ST/*ST。
    """
    if not name:
        return False
    n = name.upper()
    return n.startswith("ST") or n.startswith("*ST")


def _build_snapshot_for_date(trade_date: str) -> Dict[str, DailySnapshotRow]:
    """
    实际从 TuShare 拉取并构建某个交易日的快照字典：symbol(6位) -> DailySnapshotRow。
    """
    client = get_tushare_client()

    # 基础估值/市值/换手
    basic_df = _daily_basic_with_timeout(client, trade_date)
    if basic_df.empty:
        return {}

    # 当日涨跌停价
    limit_df = client.stk_limit(
        trade_date=trade_date,
        fields="ts_code,trade_date,up_limit,down_limit",
    )
    limit_map: Dict[str, Dict[str, float]] = {}
    if not limit_df.empty:
        for _, row in limit_df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            limit_map[ts_code] = {
                "up": float(row.get("up_limit", 0) or 0),
                "down": float(row.get("down_limit", 0) or 0),
            }

    # 停牌列表
    susp_df = client.suspend_d(trade_date=trade_date)
    suspended_set = set()
    if not susp_df.empty:
        suspended_set = set(str(x) for x in susp_df["ts_code"].dropna().tolist())

    # 基础信息（名称 + symbol，用来识别 ST）
    stock_df = client.stock_basic(
        list_status="L",
        fields="ts_code,symbol,name",
    )
    name_map: Dict[str, Dict[str, str]] = {}
    if not stock_df.empty:
        for _, row in stock_df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            name_map[ts_code] = {
                "symbol": str(row.get("symbol", "")).zfill(6),
                "name": str(row.get("name", "")),
            }

    out: Dict[str, DailySnapshotRow] = {}
    for _, row in basic_df.iterrows():
        ts_code = str(row.get("ts_code", ""))
        symbol = _normalize_symbol_from_ts_code(ts_code)
        if not symbol:
            continue
        close = float(row.get("close", 0) or 0)
        total_mv = float(row.get("total_mv", 0) or 0)
        float_mv = float(row.get("circ_mv", 0) or 0)
        turnover_rate = float(row.get("turnover_rate", 0) or 0)
        pe_ttm = float(row.get("pe_ttm", 0) or 0)
        pb = float(row.get("pb", 0) or 0)

        info = name_map.get(ts_code, {})
        name = info.get("name", "")
        symbol_from_basic = info.get("symbol") or symbol

        is_st = _infer_is_st(name)
        is_suspended = ts_code in suspended_set

        lim = limit_map.get(ts_code, {})
        up_limit = float(lim.get("up", 0) or 0)
        down_limit = float(lim.get("down", 0) or 0)
        is_limit_up = up_limit > 0 and close >= up_limit * 0.9999
        is_limit_down = down_limit > 0 and close <= down_limit * 1.0001

        actual_trade_date = str(row.get("trade_date", trade_date) or trade_date)
        if len(actual_trade_date) != 8 or not actual_trade_date.isdigit():
            actual_trade_date = trade_date
        row_obj = DailySnapshotRow(
            trade_date=actual_trade_date,
            ts_code=ts_code,
            symbol=symbol_from_basic,
            name=name,
            close=close,
            total_mv=total_mv,
            float_mv=float_mv,
            turnover_rate=turnover_rate,
            pe_ttm=pe_ttm,
            pb=pb,
            is_st=is_st,
            is_suspended=is_suspended,
            is_limit_up=is_limit_up,
            is_limit_down=is_limit_down,
        )
        out[row_obj.symbol] = row_obj

    return out


def get_daily_snapshot(
    trade_date: Optional[str] = None,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> Dict[str, DailySnapshotRow]:
    """
    获取某个交易日的快照字典（symbol -> DailySnapshotRow），结果按 trade_date 缓存。
    trade_date 默认为今日；若当日无数据（如周末、节假日），自动向前取最近有数据的交易日（最多 3 天）。
    force_refresh=True 时忽略已有缓存/退避窗，强制发起一次拉取（用于初始化预热）。
    cache_only=True 时仅读缓存，无缓存则返回 {} 且不请求 TuShare（业务侧在非 init 路径传 True）。
    """
    if trade_date is None:
        trade_date = _resolve_recent_trade_date(date.today())
    elif len(str(trade_date)) == 8:
        # 调用方常传 today 字符串，这里统一做“周末 -> 周五”修正，避免先查周末再回退。
        try:
            d = date(int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:8]))
            trade_date = _resolve_recent_trade_date(d)
        except (ValueError, IndexError):
            pass
    if cache_only:
        # 仅缓存模式：只读内存/Redis/磁盘，无缓存则返回 {}，不请求 TuShare。
        if trade_date in _snapshot_cache:
            return _snapshot_cache[trade_date]
        postgres_cached = _load_snapshot_from_postgres(trade_date)
        if postgres_cached is not None:
            _snapshot_cache[trade_date] = postgres_cached
            return postgres_cached
        redis_cached = _load_snapshot_from_redis(trade_date)
        if redis_cached is not None:
            _snapshot_cache[trade_date] = redis_cached
            return redis_cached
        disk_cached = _load_snapshot_from_disk(trade_date)
        if disk_cached is not None:
            _snapshot_cache[trade_date] = disk_cached
            return disk_cached
        logger.info(
            "TuShare 快照: cache_only=True 且无缓存（trade_date=%s），返回空；请先执行初始化 POST /api/init",
            trade_date,
        )
        return {}
    if not force_refresh:
        now = time.time()
        fail_until = _snapshot_fail_until.get(trade_date, 0.0)
        if fail_until > now:
            # 失败退避窗：短时间内不再重复打远端，避免并发雪崩重试导致日志刷屏与上游超时。
            return _snapshot_cache.get(trade_date, {})
        if trade_date in _snapshot_cache:
            return _snapshot_cache[trade_date]
        postgres_cached = _load_snapshot_from_postgres(trade_date)
        if postgres_cached is not None:
            _snapshot_cache[trade_date] = postgres_cached
            return postgres_cached
        redis_cached = _load_snapshot_from_redis(trade_date)
        if redis_cached is not None:
            _snapshot_cache[trade_date] = redis_cached
            return redis_cached
        disk_cached = _load_snapshot_from_disk(trade_date)
        if disk_cached is not None:
            _snapshot_cache[trade_date] = disk_cached
            return disk_cached
    # 单飞：同一 trade_date 只允许一个线程真正发起远端请求，避免并发击穿镜像站导致超时。
    with _snapshot_lock:
        if not force_refresh:
            if trade_date in _snapshot_cache:
                return _snapshot_cache[trade_date]
            postgres_cached = _load_snapshot_from_postgres(trade_date)
            if postgres_cached is not None:
                _snapshot_cache[trade_date] = postgres_cached
                return postgres_cached
            redis_cached = _load_snapshot_from_redis(trade_date)
            if redis_cached is not None:
                _snapshot_cache[trade_date] = redis_cached
                return redis_cached
            disk_cached = _load_snapshot_from_disk(trade_date)
            if disk_cached is not None:
                _snapshot_cache[trade_date] = disk_cached
                return disk_cached
        redis_lock_token = _acquire_redis_lock(trade_date)
        if redis_lock_token is None and _get_redis_client() is not None:
            wait_start = time.time()
            while time.time() - wait_start <= _LOCK_WAIT_SEC:
                redis_cached = _load_snapshot_from_redis(trade_date)
                if redis_cached is not None:
                    _snapshot_cache[trade_date] = redis_cached
                    return redis_cached
                time.sleep(_LOCK_POLL_SEC)
            logger.warning(
                "TuShare 快照: 等待 Redis 分布式锁超时（trade_date=%s），将回退本地锁",
                trade_date,
            )
        # 单机多进程单飞：一个进程拉取，其它进程等待并复用磁盘缓存。
        lock_fd: Optional[int] = None
        lock_path: Optional[Path] = None
        if redis_lock_token is None:
            wait_start = time.time()
            while True:
                lock_fd, lock_path = _try_acquire_file_lock(trade_date)
                if lock_fd is not None:
                    break
                cached = _load_snapshot_from_disk(trade_date)
                if cached is not None:
                    _snapshot_cache[trade_date] = cached
                    return cached
                if lock_path and lock_path.exists():
                    try:
                        age = time.time() - lock_path.stat().st_mtime
                        if age > _LOCK_STALE_SEC:
                            logger.warning(
                                "TuShare 快照: 检测到陈旧锁，尝试清理（trade_date=%s, age=%.1fs）",
                                trade_date,
                                age,
                            )
                            lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                if time.time() - wait_start > _LOCK_WAIT_SEC:
                    logger.warning(
                        "TuShare 快照: 等待本地锁超时（trade_date=%s），当前进程将继续拉取",
                        trade_date,
                    )
                    break
                time.sleep(_LOCK_POLL_SEC)
        logger.info("TuShare 快照: 拉取 trade_date=%s（市值/估值过滤依赖）", trade_date)
        try:
            try:
                data = _build_snapshot_for_date(trade_date)
            except RuntimeError as e:
                # daily_basic 链路失败（网络超时/服务端错误/SDK 兜底失败）：快速降级，不阻塞主流程。
                if "daily_basic" in str(e):
                    existing = _load_existing_non_empty_snapshot(trade_date)
                    if existing:
                        logger.warning(
                            "TuShare 快照不可用：%s；复用已有快照（trade_date=%s, rows=%d）",
                            str(e),
                            trade_date,
                            len(existing),
                        )
                        _snapshot_cache[trade_date] = existing
                        _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
                        return existing
                    logger.warning("TuShare 快照不可用：%s；将跳过市值/估值过滤", str(e))
                    _snapshot_cache[trade_date] = {}
                    _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
                    _save_snapshot_to_redis(trade_date, {})
                    _save_snapshot_to_disk(trade_date, {})
                    return {}
                raise
            if not data and trade_date == get_last_trade_date_str(for_data_availability=True):
                for d in range(1, 4):
                    fallback = (get_last_trade_date(for_data_availability=True) - timedelta(days=d)).strftime("%Y%m%d")
                    logger.info("TuShare 快照: 当日无数据，尝试 %s", fallback)
                    data = _build_snapshot_for_date(fallback)
                    if data:
                        _snapshot_cache[trade_date] = data
                        _save_snapshot_to_redis(trade_date, data)
                        _save_snapshot_to_disk(trade_date, data)
                        logger.info("TuShare 快照: 完成，共 %d 条（来自 %s）", len(data), fallback)
                        return data
            persist_trade_date = trade_date
            if data:
                first_row = next(iter(data.values()))
                if first_row.trade_date and first_row.trade_date != trade_date:
                    # TuShare 在非交易时段可能返回上一交易日数据；此时按真实交易日落缓存，避免出现“未来日期文件名”。
                    persist_trade_date = first_row.trade_date
                    logger.info(
                        "TuShare 快照: 请求 %s 实际返回 %s，按 %s 落缓存（共 %d 条）",
                        trade_date,
                        first_row.trade_date,
                        persist_trade_date,
                        len(data),
                    )
                else:
                    logger.info("TuShare 快照: 完成，共 %d 条", len(data))

            _snapshot_cache[persist_trade_date] = data
            # 兼容调用方：请求 key 也指向同一份数据（仅内存别名，不再写磁盘）
            _snapshot_cache[trade_date] = data
            _snapshot_fail_until.pop(trade_date, None)
            _save_snapshot_to_redis(persist_trade_date, data)
            _save_snapshot_to_disk(persist_trade_date, data)
            return data
        except Exception as e:
            existing = _load_existing_non_empty_snapshot(trade_date)
            if existing:
                logger.warning(
                    "TuShare 快照异常：%s；复用已有快照（trade_date=%s, rows=%d）",
                    str(e),
                    trade_date,
                    len(existing),
                )
                _snapshot_cache[trade_date] = existing
                _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
                return existing
            logger.warning(
                "TuShare 快照异常：%s；将在 %.0fs 内跳过市值/估值过滤",
                str(e),
                _SNAPSHOT_FAIL_BACKOFF_SEC,
            )
            _snapshot_cache[trade_date] = {}
            _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
            _save_snapshot_to_redis(trade_date, {})
            _save_snapshot_to_disk(trade_date, {})
            return {}
        finally:
            _release_redis_lock(trade_date, redis_lock_token)
            _release_file_lock(lock_fd, lock_path)

from __future__ import annotations

"""
基于规范数据平台的每日截面快照服务：
- 市值：float_mv / total_mv
- 估值：pe_ttm / pb
- 状态：is_suspended / is_st / is_limit_up / is_limit_down

注意：本模块只读取数据平台已留痕的规范快照；需要刷新时委托数据
平台执行多源发现和全量同步，绝不在业务进程直连某个行情源。
"""

import logging
from dataclasses import asdict, dataclass
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
import os
from pathlib import Path
import time
import threading
from typing import Dict, Optional, Tuple
import uuid

try:
    import redis
except Exception:  # pragma: no cover - 运行环境未安装 redis 时兜底
    redis = None

from app.core.trade_calendar import get_last_trade_date_str
from app.core.runtime_paths import backend_cache_root
from app.data_platform.db import _conn

logger = logging.getLogger("quant.tushare_snapshot")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
def _resolve_recent_trade_date(d: date) -> str:
    """计算最近交易日（周末/节假日/当日 15:00 前 -> 前一交易日）。"""
    return get_last_trade_date_str(d, for_data_availability=True)


class DailySnapshotUnavailableError(RuntimeError):
    """Exact-date normalized market snapshot is not available."""


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
_SNAPSHOT_LOCK_DIR = backend_cache_root() / "tushare_snapshot"
_LOCK_WAIT_SEC = 20.0
_LOCK_STALE_SEC = 60.0
_LOCK_POLL_SEC = 0.2
_REDIS_LOCK_TTL_SEC = 30
_REDIS_CACHE_TTL_SEC = 6 * 60 * 60
_REDIS_EMPTY_CACHE_TTL_SEC = 5 * 60
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


def _ensure_snapshot_lock_dir() -> None:
    _SNAPSHOT_LOCK_DIR.mkdir(parents=True, exist_ok=True)


def _snapshot_lock_file(trade_date: str) -> Path:
    return _SNAPSHOT_LOCK_DIR / f"{trade_date}.lock"


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


def _load_existing_non_empty_snapshot(trade_date: str) -> Optional[Dict[str, DailySnapshotRow]]:
    """
    读取已有的非空规范快照，用于刷新失败时避免把已验证的精确日期数据覆盖为空。
    """
    mem = _snapshot_cache.get(trade_date)
    if mem:
        return mem
    return _load_snapshot_from_postgres(trade_date)


def _try_acquire_file_lock(trade_date: str) -> Tuple[Optional[int], Path]:
    """
    跨进程锁：用 O_EXCL 创建 lock 文件，成功返回 fd；失败返回 None。
    """
    _ensure_snapshot_lock_dir()
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
    """Refresh one exact trading session through the data platform only.

    ``sync_market_bundle`` owns provider discovery, bounded hedging, raw
    evidence and coverage validation.  The business-layer snapshot must not
    reconstruct a date from a direct provider response, because that bypasses
    both source provenance and the decision-data contract.
    """
    from app.data_platform.services.data_center import (
        build_stock_daily_snapshot,
        sync_market_bundle,
        sync_stock_basic,
    )

    master = sync_stock_basic()
    bundle = sync_market_bundle(trade_date)
    if not bundle.get("ok"):
        category = str(bundle.get("failure_category") or bundle.get("status") or "market_bundle_not_ready")
        raise DailySnapshotUnavailableError(f"market_bundle_not_ready:{trade_date}:{category}")
    build_stock_daily_snapshot(trade_date)
    snapshot = _load_snapshot_from_postgres(trade_date)
    if not snapshot:
        master_rows = int(master.get("rows_written") or 0)
        raise DailySnapshotUnavailableError(
            f"normalized_snapshot_missing:{trade_date}:stock_master_rows={master_rows}"
        )
    return snapshot


def get_daily_snapshot(
    trade_date: Optional[str] = None,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> Dict[str, DailySnapshotRow]:
    """
    获取某个交易日的快照字典（symbol -> DailySnapshotRow），结果按 trade_date 缓存。
    trade_date 默认为最近可用交易日；显式日期只接受该精确日期，绝不借用前日切片。
    force_refresh=True 时通过数据平台发起多源同步。
    cache_only=True 时只读规范 PostgreSQL；无数据返回空，由调用方的硬门阻断业务。
    """
    if trade_date is None:
        trade_date = _resolve_recent_trade_date(datetime.now(_BUSINESS_TZ).date())
    elif len(str(trade_date)) == 8:
        # 显式日期是 PIT 合同的一部分：只规范格式，不把周末/节假日
        # 静默改写成前一交易日。调用方若需要最近交易日必须显式计算。
        try:
            d = date(int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:8]))
            trade_date = d.strftime("%Y%m%d")
        except (ValueError, IndexError):
            pass
    if cache_only:
        # 仅缓存模式：只读内存和规范 PostgreSQL，无缓存则返回 {}，不触发同步。
        if trade_date in _snapshot_cache:
            return _snapshot_cache[trade_date]
        postgres_cached = _load_snapshot_from_postgres(trade_date)
        if postgres_cached is not None:
            _snapshot_cache[trade_date] = postgres_cached
            return postgres_cached
        logger.info(
            "规范快照: cache_only=True 且无精确日期数据（trade_date=%s），返回空；请先执行数据初始化",
            trade_date,
        )
        return {}
    if not force_refresh:
        now = time.time()
        fail_until = _snapshot_fail_until.get(trade_date, 0.0)
        if fail_until > now:
            # 退避只抑制重复同步，不能把一次失败伪装成空快照或成功结果。
            raise DailySnapshotUnavailableError(f"snapshot_refresh_backoff:{trade_date}")
        if trade_date in _snapshot_cache:
            return _snapshot_cache[trade_date]
        postgres_cached = _load_snapshot_from_postgres(trade_date)
        if postgres_cached is not None:
            _snapshot_cache[trade_date] = postgres_cached
            return postgres_cached
    # 单飞：同一 trade_date 只允许一个线程真正发起数据平台同步。
    with _snapshot_lock:
        if not force_refresh:
            if trade_date in _snapshot_cache:
                return _snapshot_cache[trade_date]
            postgres_cached = _load_snapshot_from_postgres(trade_date)
            if postgres_cached is not None:
                _snapshot_cache[trade_date] = postgres_cached
                return postgres_cached
        redis_lock_token = _acquire_redis_lock(trade_date)
        if redis_lock_token is None and _get_redis_client() is not None:
            wait_start = time.time()
            while time.time() - wait_start <= _LOCK_WAIT_SEC:
                postgres_cached = _load_snapshot_from_postgres(trade_date)
                if postgres_cached is not None:
                    _snapshot_cache[trade_date] = postgres_cached
                    return postgres_cached
                time.sleep(_LOCK_POLL_SEC)
            logger.warning(
                "规范快照: 等待 Redis 分布式锁超时（trade_date=%s），将检查本地锁",
                trade_date,
            )
        # 单机多进程单飞：一个进程同步，其它进程等待后只复用 PostgreSQL 精确切片。
        lock_fd: Optional[int] = None
        lock_path: Optional[Path] = None
        if redis_lock_token is None:
            wait_start = time.time()
            while True:
                lock_fd, lock_path = _try_acquire_file_lock(trade_date)
                if lock_fd is not None:
                    break
                cached = _load_snapshot_from_postgres(trade_date)
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
                        "规范快照: 等待本地锁超时（trade_date=%s），当前进程将继续同步",
                        trade_date,
                    )
                    break
                time.sleep(_LOCK_POLL_SEC)
        logger.info("规范快照: 同步 trade_date=%s（市值/估值过滤依赖）", trade_date)
        try:
            data = _build_snapshot_for_date(trade_date)
            if not data:
                raise DailySnapshotUnavailableError(f"normalized_snapshot_empty:{trade_date}")
            _snapshot_cache[trade_date] = data
            _snapshot_fail_until.pop(trade_date, None)
            logger.info("规范快照: 完成，trade_date=%s rows=%d", trade_date, len(data))
            return data
        except Exception as e:
            existing = _load_existing_non_empty_snapshot(trade_date)
            if existing:
                logger.warning(
                    "规范快照刷新异常：%s；保留已有精确日期快照（trade_date=%s, rows=%d）",
                    str(e),
                    trade_date,
                    len(existing),
                )
                _snapshot_cache[trade_date] = existing
                _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
                return existing
            logger.warning(
                "规范快照不可用：%s；将在 %.0fs 内拒绝重复同步",
                str(e),
                _SNAPSHOT_FAIL_BACKOFF_SEC,
            )
            _snapshot_fail_until[trade_date] = time.time() + _SNAPSHOT_FAIL_BACKOFF_SEC
            if isinstance(e, DailySnapshotUnavailableError):
                raise
            raise DailySnapshotUnavailableError(f"snapshot_refresh_failed:{trade_date}:{str(e)[:160]}") from e
        finally:
            _release_redis_lock(trade_date, redis_lock_token)
            _release_file_lock(lock_fd, lock_path)

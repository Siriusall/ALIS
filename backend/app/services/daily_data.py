"""
日线数据聚合服务：为因子层提供多股票、日期范围内的统一 OHLCV 日线数据。
内部复用 market_data_client.fetch_stock_zh_a_hist，带简单内存缓存以降低重复请求。
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Tuple

from app.core.trade_calendar import get_last_trade_date_str
from app.core.runtime_paths import backend_cache_root
from app.data_platform.db import _conn
from app.integrations.market_data_client import fetch_daily_by_trade_date, fetch_stock_zh_a_hist, hist_concurrency_scope

logger = logging.getLogger("quant.daily_data")


class DailyCacheNotReadyError(Exception):
    """策略所需日线无法从缓存满足，需先执行初始化（启动时或 POST /api/init）。"""
    def __init__(self, missing_count: int, end_date: str, lookback_days: int, message: str | None = None):
        self.missing_count = missing_count
        self.end_date = end_date
        self.lookback_days = lookback_days
        msg = message or (
            f"日线缓存未覆盖策略所需标的（缺 {missing_count} 只，end_date={end_date}, lookback={lookback_days} 天）。"
            "请先执行初始化：POST /api/init 或重启后端。"
        )
        super().__init__(msg)

# 内存 + 文件双层缓存：一天拉一次日线后复用，避免频繁触发远端限流。
_DAILY_CACHE: Dict[Tuple[str, str, str], List["DailyBar"]] = {}

# 每次请求远端日线前的固定延迟（秒）；若遇限流可调大。
_DAILY_REQUEST_DELAY_SEC = 0

def _parse_int_env(name: str, default: int, min_v: int, max_v: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    return max(min_v, min(max_v, v))


# 日线远端线程数；默认恢复为 16（可通过环境变量覆盖）。
_DAILY_FETCH_WORKERS = _parse_int_env("TUSHARE_DAILY_FETCH_WORKERS", 16, 1, 32)
# 滚动更新心跳日志间隔（秒）：用于避免长时间无输出造成“假卡死”观感。
_DAILY_ROLL_HEARTBEAT_SEC = 10.0
# 日线预拉心跳日志间隔（秒）：初始化大批量拉取时输出等待提示。
_DAILY_PREFETCH_HEARTBEAT_SEC = 10.0
# 日线请求并发自动阈值：批量任务时放开到限制值，小规模保持 1。
_DAILY_HIST_BULK_THRESHOLD = 20
_DAILY_HIST_BULK_CONCURRENCY = _parse_int_env("TUSHARE_HIST_BULK_CONCURRENCY", 8, 1, 16)
# 单日批量拉取阈值：当「仅补今天1天」的标的数量达到该值时，走一次 TuShare 全市场日线接口。
_DAILY_SINGLE_DAY_BATCH_THRESHOLD = _parse_int_env("TUSHARE_DAILY_SINGLE_DAY_BATCH_THRESHOLD", 200, 50, 5000)

# 日线远端拉取全局锁：同一时间只允许一批拉取，避免多路 /api/candidates 并发导致请求翻倍。
_DAILY_FETCH_LOCK = threading.Lock()


@dataclass
class DailyBar:
    """单日 K 线，至少包含 date, close, volume，可扩展 open, high, low, turnover。"""
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def _raw_to_daily_bar(raw: dict) -> DailyBar:
    return DailyBar(
        date=str(raw.get("date", ""))[:10],
        open=float(raw.get("open", 0) or 0),
        high=float(raw.get("high", 0) or 0),
        low=float(raw.get("low", 0) or 0),
        close=float(raw.get("close", 0) or 0),
        volume=float(raw.get("volume", 0) or 0),
        turnover=float(raw.get("turnover", 0) or 0),
    )


def _load_daily_from_postgres(symbols: List[str], start_date: str, end_date: str) -> Dict[str, List[DailyBar]]:
    """Load OHLCV history from durable TuShare raw records in PostgreSQL."""
    normalized = [_normalize_symbol(symbol) for symbol in symbols]
    ts_codes = []
    for symbol in normalized:
        suffix = ".SH" if symbol.startswith(("6", "9")) else ".SZ" if symbol.startswith(("0", "3")) else ".BJ"
        ts_codes.append(f"{symbol}{suffix}")
    if not ts_codes:
        return {}
    start_ymd, end_ymd = start_date.replace("-", ""), end_date.replace("-", "")
    placeholders = ",".join("?" for _ in ts_codes)
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"""SELECT ts_code,trade_date,payload_json FROM dc_tushare_raw_records
                    WHERE api_name='daily' AND trade_date BETWEEN ? AND ? AND ts_code IN ({placeholders})
                    ORDER BY ts_code,trade_date""",
                (start_ymd, end_ymd, *ts_codes),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL日线读取失败: %s", str(exc)[:160])
        return {}
    out: Dict[str, List[DailyBar]] = {}
    for row in rows:
        item = dict(row)
        payload = json.loads(str(item.get("payload_json") or "{}"))
        symbol = _normalize_symbol(str(item.get("ts_code") or payload.get("ts_code") or ""))
        if not symbol:
            continue
        out.setdefault(symbol, []).append(DailyBar(
            date=str(payload.get("trade_date") or item.get("trade_date") or ""),
            open=float(payload.get("open") or 0), high=float(payload.get("high") or 0),
            low=float(payload.get("low") or 0), close=float(payload.get("close") or 0),
            volume=float(payload.get("vol") or 0), turnover=float(payload.get("amount") or 0),
        ))
    return out


def _normalize_symbol(s: str) -> str:
    s = str(s).strip()
    if "." in s:
        s = s.split(".", 1)[0]
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if s.lower().startswith(prefix):
            return s[len(prefix) :]
    if len(s) < 6:
        return s.zfill(6)
    return s


def _cache_dir() -> Path:
    d = backend_cache_root() / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(symbol: str, start_date: str, end_date: str) -> Path:
    sym = _normalize_symbol(symbol)
    return _cache_dir() / f"{sym}_{start_date}_{end_date}.json"


def clear_previous_day_daily_cache() -> None:
    """
    已弃用：不再整体删除前一日日线，请使用 roll_forward_daily_cache 做滚动更新。
    保留本函数仅为兼容，内部为空操作。
    """
    pass


def roll_forward_daily_cache() -> None:
    """
    对已有日线缓存做滚动更新：删除最早一天、加入最新一天。
    遍历 .cache/daily 下 end_date < 今日 的文件，读入后去掉第一条（最早日），
    拉取该标的从 end_date 下一日到今日的日线并追加，写回新文件并删除旧文件。
    """
    cache_dir = backend_cache_root() / "daily"
    if not cache_dir.exists():
        return
    today_str = get_last_trade_date_str(for_data_availability=True)
    # 收集需要滚动的文件: (path, symbol, file_start_date, file_end_date)
    to_roll: List[tuple] = []
    for path in cache_dir.glob("*_*_*.json"):
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        sym, start_date, end_date = parts[0], parts[1], parts[2]
        if len(end_date) != 8 or len(start_date) != 8:
            continue
        to_roll.append((path, sym, start_date, end_date))

    if not to_roll:
        logger.info("daily_data: 无需滚动的日线缓存（均已为今日）")
        return

    # 先读文件，收集 (path, sym, file_start_date, file_end_date, kept_bars, next_start)
    roll_tasks: List[Tuple[Any, str, str, str, List[DailyBar], str]] = []
    for path, sym, file_start_date, file_end_date in to_roll:
        try:
            with path.open("r", encoding="utf-8") as f:
                rows = json.load(f)
        except Exception as e:
            logger.warning("daily_data: 读取日线缓存失败 %s | %s", path, str(e)[:80])
            continue
        if not rows or len(rows) < 2:
            continue
        bars = [
            DailyBar(
                date=str(r.get("date", "")),
                open=float(r.get("open", 0) or 0),
                high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0),
                close=float(r.get("close", 0) or 0),
                volume=float(r.get("volume", 0) or 0),
                turnover=float(r.get("turnover", 0) or 0),
            )
            for r in rows
        ]
        actual_end = bars[-1].date.replace("-", "")
        # 以文件内容真实最后日期为准，修复“文件名是今天、内容仍是昨天”的假更新场景。
        if len(actual_end) != 8 or not actual_end.isdigit() or actual_end >= today_str:
            continue
        new_bars = bars[1:]
        try:
            end_d = date(int(actual_end[:4]), int(actual_end[4:6]), int(actual_end[6:8]))
        except (ValueError, IndexError):
            continue
        next_d = end_d + timedelta(days=1)
        next_start = next_d.strftime("%Y%m%d")
        if next_start > today_str:
            continue
        roll_tasks.append((path, sym, file_start_date, file_end_date, new_bars, next_start))

    def _fetch_roll(task_id: int, sym: str, next_start: str, end_today: str) -> Tuple[int, List[DailyBar]]:
        try:
            raw_list = fetch_stock_zh_a_hist(sym, next_start, end_today)
            return (task_id, [_raw_to_daily_bar(r) for r in raw_list])
        except Exception as e:
            logger.warning("daily_data: 滚动拉取 %s %s-%s 失败 | %s", sym, next_start, end_today, str(e)[:80])
            return (task_id, [])

    total = len(roll_tasks)
    hist_limit = _DAILY_HIST_BULK_CONCURRENCY if total >= _DAILY_HIST_BULK_THRESHOLD else 1
    with _DAILY_FETCH_LOCK:
        logger.info(
            "daily_data: 日线滚动更新 %d 个文件（%d 线程，TuShare 日线并发=%d）",
            total,
            _DAILY_FETCH_WORKERS,
            hist_limit,
        )
        appended_by_id: Dict[int, List[DailyBar]] = {}
        done = 0
        t0 = time.perf_counter()
        last_heartbeat = t0
        # 对「仅补今天1天」的大批量任务，优先走单次批量接口，显著降低请求数与限频概率。
        single_day_indices = [idx for idx, (_, _, _, _, _, next_start) in enumerate(roll_tasks) if next_start == today_str]
        if len(single_day_indices) >= _DAILY_SINGLE_DAY_BATCH_THRESHOLD:
            batch_map = fetch_daily_by_trade_date(today_str)
            if batch_map:
                for idx in single_day_indices:
                    sym = roll_tasks[idx][1]
                    row = batch_map.get(sym)
                    appended_by_id[idx] = [_raw_to_daily_bar(row)] if row else []
                    done += 1
                logger.info(
                    "daily_data: 单日批量拉取生效 trade_date=%s 覆盖=%d/%d",
                    today_str,
                    len(single_day_indices),
                    total,
                )
            else:
                logger.warning("daily_data: 单日批量拉取失败，将回退逐只拉取 trade_date=%s", today_str)

        remaining_indices = [idx for idx in range(total) if idx not in appended_by_id]
        if remaining_indices:
            with hist_concurrency_scope(hist_limit), ThreadPoolExecutor(max_workers=_DAILY_FETCH_WORKERS) as executor:
                futures = {
                    executor.submit(_fetch_roll, idx, roll_tasks[idx][1], roll_tasks[idx][5], today_str): idx
                    for idx in remaining_indices
                }
                for future in as_completed(futures):
                    task_id, appended = future.result()
                    appended_by_id[task_id] = appended
                    done += 1
                    now = time.perf_counter()
                    elapsed = now - t0
                    # 两类进度日志：
                    # 1) 里程碑（每 500 条） 2) 心跳（固定时间间隔）
                    if done % 500 == 0 or done == total or (now - last_heartbeat) >= _DAILY_ROLL_HEARTBEAT_SEC:
                        logger.info("daily_data: 日线滚动更新进度 %d/%d（已用 %.1f 秒）", done, total, elapsed)
                        last_heartbeat = now
        updated = 0
        for idx, (path, sym, file_start_date, file_end_date, new_bars, _) in enumerate(roll_tasks):
            appended = appended_by_id.get(idx, [])
            # 远端没拿到新K线时，保持原文件不动，避免“仅改文件名/覆盖旧内容”的假更新。
            if not appended:
                logger.warning(
                    "daily_data: 滚动更新跳过（无新增K线，保留旧文件） symbol=%s file=%s",
                    sym,
                    path.name,
                )
                continue
            new_bars = list(new_bars) + list(appended)
            if not new_bars:  # 防御性分支
                continue
            new_start = new_bars[0].date.replace("-", "")
            new_end = new_bars[-1].date.replace("-", "")
            _save_daily_to_file(sym, new_start, new_end, new_bars)
            key_old = (sym, file_start_date, file_end_date)
            key_new = (sym, new_start, new_end)
            if key_old in _DAILY_CACHE:
                del _DAILY_CACHE[key_old]
            _DAILY_CACHE[key_new] = new_bars
            try:
                path.unlink()
            except OSError as e:
                logger.warning("daily_data: 删除旧缓存失败 %s | %s", path, str(e)[:80])
            updated += 1
    logger.info("daily_data: 日线滚动更新完成，共 %d 个文件", updated)


def _load_daily_from_file(symbol: str, start_date: str, end_date: str) -> List[DailyBar] | None:
    path = _cache_path(symbol, start_date, end_date)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            rows = json.load(f)
        return [
            DailyBar(
                date=str(r.get("date", "")),
                open=float(r.get("open", 0) or 0),
                high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0),
                close=float(r.get("close", 0) or 0),
                volume=float(r.get("volume", 0) or 0),
                turnover=float(r.get("turnover", 0) or 0),
            )
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("daily_data: 读取日线缓存失败 %s | %s", path, str(e)[:80])
        return None


def _get_cache_latest_end_date() -> str | None:
    """返回 .cache/daily 中所有文件名里 end_ymd 的最大值；无缓存时返回 None。用于 require_cache 时与缓存对齐，避免请求日历「最新交易日」而缓存只到昨日。"""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS trade_date FROM dc_tushare_raw_records WHERE api_name='daily'"
            ).fetchone()
        postgres_latest = str(dict(row).get("trade_date") or "") if row else ""
        if len(postgres_latest) == 8 and postgres_latest.isdigit():
            return postgres_latest
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL daily cache date read failed: %s", str(exc)[:120])
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return None
    latest: str | None = None
    for path in cache_dir.glob("*_*_*.json"):
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        fe = parts[2]
        if len(fe) == 8 and fe.isdigit():
            if latest is None or fe > latest:
                latest = fe
    return latest


def _build_daily_cache_index() -> Dict[str, List[Tuple[Path, str, str]]]:
    """扫描 .cache/daily 一次，返回 symbol -> [(path, start_ymd, end_ymd)]，避免全市场时对每只股票重复 glob。"""
    cache_dir = _cache_dir()
    if not cache_dir.exists():
        return {}
    index: Dict[str, List[Tuple[Path, str, str]]] = {}
    for path in cache_dir.glob("*.json"):
        parts = path.stem.split("_")
        if len(parts) < 3:
            continue
        sym, fs, fe = parts[0], parts[1], parts[2]
        if len(fs) != 8 or len(fe) != 8:
            continue
        index.setdefault(sym, []).append((path, fs, fe))
    return index


def _load_daily_from_file_containing(
    symbol: str, start_date: str, end_date: str, cache_index: Dict[str, List[Tuple[Path, str, str]]] | None = None
) -> List[DailyBar] | None:
    """
    若不存在精确 (start_date, end_date) 的缓存，则查找该标的下「日期范围包含」请求区间的文件，
    读入后截取 [start_date, end_date] 返回。传入 cache_index 时不再逐只 glob，全市场时显著提速。
    """
    sym = _normalize_symbol(symbol)
    start_ymd = start_date.replace("-", "") if len(start_date) == 10 else start_date
    end_ymd = end_date.replace("-", "") if len(end_date) == 10 else end_date
    if len(start_ymd) != 8 or len(end_ymd) != 8:
        return None
    candidates: List[Tuple[Path, str, str]] = []
    if cache_index is not None:
        for path, fs, fe in cache_index.get(sym, []):
            if fs <= start_ymd and fe >= end_ymd:
                candidates.append((path, fs, fe))
            elif fe >= end_ymd and fs <= end_ymd:
                candidates.append((path, fs, fe))
    else:
        cache_dir = _cache_dir()
        for path in cache_dir.glob(f"{sym}_*.json"):
            parts = path.stem.split("_")
            if len(parts) < 3:
                continue
            fs, fe = parts[1], parts[2]
            if len(fs) != 8 or len(fe) != 8:
                continue
            if fs <= start_ymd and fe >= end_ymd:
                candidates.append((path, fs, fe))
            elif fe >= end_ymd and fs <= end_ymd:
                candidates.append((path, fs, fe))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[2], x[1]))
    path, _, _ = candidates[0]
    try:
        with path.open("r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return None
    bars = [
        DailyBar(
            date=str(r.get("date", ""))[:10],
            open=float(r.get("open", 0) or 0),
            high=float(r.get("high", 0) or 0),
            low=float(r.get("low", 0) or 0),
            close=float(r.get("close", 0) or 0),
            volume=float(r.get("volume", 0) or 0),
            turnover=float(r.get("turnover", 0) or 0),
        )
        for r in rows
    ]
    out = [b for b in bars if start_ymd <= b.date.replace("-", "") <= end_ymd]
    return out if out else None


def _save_daily_to_file(symbol: str, start_date: str, end_date: str, bars: List[DailyBar]) -> None:
    path = _cache_path(symbol, start_date, end_date)
    try:
        payload = [
            {
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "turnover": b.turnover,
            }
            for b in bars
        ]
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("daily_data: 写入日线缓存失败 %s | %s", path, str(e)[:80])


def _load_latest_daily_for_symbol(symbol: str) -> List[DailyBar] | None:
    """网络失败时，按文件名日期倒序选择最近一次该 symbol 的日线缓存做兜底。"""
    sym = _normalize_symbol(symbol)
    cache_dir = _cache_dir()
    files = sorted(cache_dir.glob(f"{sym}_*.json"), reverse=True)
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as f:
                rows = json.load(f)
            return [
                DailyBar(
                    date=str(r.get("date", "")),
                    open=float(r.get("open", 0) or 0),
                    high=float(r.get("high", 0) or 0),
                    low=float(r.get("low", 0) or 0),
                    close=float(r.get("close", 0) or 0),
                    volume=float(r.get("volume", 0) or 0),
                    turnover=float(r.get("turnover", 0) or 0),
                )
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            continue
    return None


def _fetch_one_daily(sym: str, start_date: str, end_date: str) -> Tuple[str, List[DailyBar]]:
    """单标的拉取日线并落盘，失败时尝试历史文件兜底。供线程池调用。"""
    try:
        raw_list = fetch_stock_zh_a_hist(sym, start_date, end_date)
        bars = [_raw_to_daily_bar(r) for r in raw_list]
        _save_daily_to_file(sym, start_date, end_date, bars)
        return (sym, bars)
    except Exception as e:  # noqa: BLE001
        logger.warning("TuShare: 日线 %s %s-%s 失败 | %s", sym, start_date, end_date, str(e)[:80])
        fallback = _load_latest_daily_for_symbol(sym)
        return (sym, fallback if fallback else [])


def get_daily_bars(
    symbols: List[str],
    start_date: str,
    end_date: str,
    require_cache: bool = False,
    lookback_days_for_error: int = 0,
) -> Dict[str, List[DailyBar]]:
    """
    获取多只股票在给定日期范围内的日线数据（OHLCV）。
    - symbols: 股票代码列表（可含 sh/sz 前缀，会归一化为 6 位）。
    - start_date / end_date: 格式 YYYYMMDD。
    - require_cache: 为 True 时（策略场景），若有标的无法从缓存满足则抛 DailyCacheNotReadyError，不发起远端拉取。
    - lookback_days_for_error: 仅用于异常文案（与策略 lookback 一致）。
    - 返回: { symbol: [DailyBar, ...] }，按日期升序；缺失或失败的股票不出现或为空列表。
    """
    if not symbols:
        return {}
    with _DAILY_FETCH_LOCK:
        seen: set[str] = set()
        uniq: List[str] = []
        for s in symbols:
            if not s or not str(s).strip():
                continue
            n = _normalize_symbol(s)
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        pg_bars = _load_daily_from_postgres(uniq, start_date, end_date)
        # 全市场时一次扫描缓存目录建索引，避免对每只股票重复 glob 导致卡顿
        cache_index: Dict[str, List[Tuple[Path, str, str]]] | None = None
        if len(uniq) >= 300:
            cache_index = _build_daily_cache_index()
        out: Dict[str, List[DailyBar]] = {}
        need_fetch: List[str] = []
        for sym in uniq:
            key = (sym, start_date, end_date)
            if key in _DAILY_CACHE:
                out[sym] = _DAILY_CACHE[key]
                continue
            if pg_bars.get(sym):
                _DAILY_CACHE[key] = pg_bars[sym]
                out[sym] = pg_bars[sym]
                continue
            bars: List[DailyBar] | None = None
            start_ymd = start_date.replace("-", "") if len(start_date) == 10 else start_date
            end_ymd = end_date.replace("-", "") if len(end_date) == 10 else end_date
            if cache_index is not None:
                containing_candidate: Tuple[Path, str, str] | None = None
                overlap_candidate: Tuple[Path, str, str] | None = None
                for path, s, e in cache_index.get(sym, []):
                    if s == start_date and e == end_date:
                        try:
                            with path.open("r", encoding="utf-8") as f:
                                rows = json.load(f)
                            bars = [
                                DailyBar(
                                    date=str(r.get("date", "")),
                                    open=float(r.get("open", 0) or 0),
                                    high=float(r.get("high", 0) or 0),
                                    low=float(r.get("low", 0) or 0),
                                    close=float(r.get("close", 0) or 0),
                                    volume=float(r.get("volume", 0) or 0),
                                    turnover=float(r.get("turnover", 0) or 0),
                                )
                                for r in rows
                            ]
                        except Exception:
                            pass
                        break
                    if len(s) == 8 and len(e) == 8 and s <= start_ymd and e >= end_ymd:
                        if containing_candidate is None or (e > containing_candidate[2] or (e == containing_candidate[2] and s < containing_candidate[1])):
                            containing_candidate = (path, s, e)
                    # 放宽：文件与请求有重叠即可（文件 end>=请求 end 且 start<=请求 end），如 20251231_20260227 可满足 20251229_20260227
                    if len(s) == 8 and len(e) == 8 and e >= end_ymd and s <= end_ymd:
                        if overlap_candidate is None or (e > overlap_candidate[2] or (e == overlap_candidate[2] and s < overlap_candidate[1])):
                            overlap_candidate = (path, s, e)
                if bars is None and containing_candidate is not None:
                    path, _fs, _fe = containing_candidate
                    try:
                        with path.open("r", encoding="utf-8") as f:
                            rows = json.load(f)
                        all_bars = [
                            DailyBar(
                                date=str(r.get("date", ""))[:10],
                                open=float(r.get("open", 0) or 0),
                                high=float(r.get("high", 0) or 0),
                                low=float(r.get("low", 0) or 0),
                                close=float(r.get("close", 0) or 0),
                                volume=float(r.get("volume", 0) or 0),
                                turnover=float(r.get("turnover", 0) or 0),
                            )
                            for r in rows
                        ]
                        bars = [b for b in all_bars if start_ymd <= b.date.replace("-", "") <= end_ymd]
                        if bars:
                            _save_daily_to_file(sym, start_date, end_date, bars)
                    except Exception:
                        pass
                if bars is None and overlap_candidate is not None:
                    path, _fs, _fe = overlap_candidate
                    try:
                        with path.open("r", encoding="utf-8") as f:
                            rows = json.load(f)
                        all_bars = [
                            DailyBar(
                                date=str(r.get("date", ""))[:10],
                                open=float(r.get("open", 0) or 0),
                                high=float(r.get("high", 0) or 0),
                                low=float(r.get("low", 0) or 0),
                                close=float(r.get("close", 0) or 0),
                                volume=float(r.get("volume", 0) or 0),
                                turnover=float(r.get("turnover", 0) or 0),
                            )
                            for r in rows
                        ]
                        bars = [b for b in all_bars if start_ymd <= b.date.replace("-", "") <= end_ymd]
                        if bars:
                            _save_daily_to_file(sym, start_date, end_date, bars)
                    except Exception:
                        pass
            if bars is None:
                bars = _load_daily_from_file(sym, start_date, end_date)
            if bars is None:
                bars = _load_daily_from_file_containing(sym, start_date, end_date, cache_index)
                if bars is not None:
                    _save_daily_to_file(sym, start_date, end_date, bars)
            if bars is not None:
                _DAILY_CACHE[key] = bars
                out[sym] = bars
                continue
            need_fetch.append(sym)
        total = len(uniq)
        # 策略/选股路径（require_cache=True）：仅用缓存，不发起远端请求。若部分缺缓存则返回已有部分，避免整批 503/无候选。
        if need_fetch and require_cache:
            logger.warning(
                "日线仅缓存: 缺 %d 只（共 %d），返回已有 %d 只；缺的标的将无日线因子（如 momentum=0）",
                len(need_fetch),
                total,
                len(out),
            )
            return out
        if need_fetch:
            hist_limit = _DAILY_HIST_BULK_CONCURRENCY if len(need_fetch) >= _DAILY_HIST_BULK_THRESHOLD else 1
            if _DAILY_REQUEST_DELAY_SEC > 0:
                logger.info(
                    "日线拉取: 共 %d 只，其中 %d 只远端请求（延迟 %.1fs，TuShare 日线并发=%d）",
                    total,
                    len(need_fetch),
                    _DAILY_REQUEST_DELAY_SEC,
                    hist_limit,
                )
            else:
                logger.info(
                    "日线拉取: 共 %d 只，其中 %d 只远端并发请求（%d 线程，TuShare 日线并发=%d）",
                    total,
                    len(need_fetch),
                    _DAILY_FETCH_WORKERS,
                    hist_limit,
                )
            logger.info("日线拉取: 开始执行，请耐心等待；将每 %.0f 秒输出一次进度", _DAILY_PREFETCH_HEARTBEAT_SEC)
            with hist_concurrency_scope(hist_limit), ThreadPoolExecutor(max_workers=_DAILY_FETCH_WORKERS) as executor:
                futures = {executor.submit(_fetch_one_daily, sym, start_date, end_date): sym for sym in need_fetch}
                done = 0
                t0 = time.perf_counter()
                last_heartbeat = t0
                for future in as_completed(futures):
                    sym, bars = future.result()
                    key = (sym, start_date, end_date)
                    _DAILY_CACHE[key] = bars
                    out[sym] = bars
                    done += 1
                    now = time.perf_counter()
                    elapsed = now - t0
                    if done == len(need_fetch) or done % 500 == 0 or (now - last_heartbeat) >= _DAILY_PREFETCH_HEARTBEAT_SEC:
                        logger.info(
                            "日线拉取: 等待中，已完成 %d/%d（%.1f%%，已用 %.1f 秒）",
                            done,
                            len(need_fetch),
                            (done * 100.0 / len(need_fetch)) if need_fetch else 100.0,
                            elapsed,
                        )
                        last_heartbeat = now
        logger.info("日线拉取: 完成，共 %d 只（其中远端 %d 只）", total, len(need_fetch))
        return out


def get_daily_bars_lookback(
    symbols: List[str],
    end_date: str,
    lookback_days: int = 60,
    require_cache: bool = False,
) -> Dict[str, List[DailyBar]]:
    """
    按「截止日期 + 向前回溯天数」拉取日线，便于因子计算固定窗口。
    end_date 格式 YYYYMMDD，应与 get_last_trade_date_str(for_data_availability=True) 一致（与初始化预拉一致）。
    require_cache=True 时，策略所需日线必须全部来自缓存；且 end_date 会对齐到缓存中实际存在的最大 end，
    避免日历「最新交易日」已推进（如新一天 01:29）而缓存仍是昨日，导致整批判为缺缓存。
    """
    if lookback_days <= 0:
        return get_daily_bars(symbols, end_date, end_date, require_cache=require_cache, lookback_days_for_error=lookback_days)
    # 仅用缓存时：对齐到缓存里已有的最大 end_date，避免请求超出缓存范围
    if require_cache:
        cache_end = _get_cache_latest_end_date()
        if cache_end and end_date > cache_end:
            logger.debug("日线仅缓存: 请求 end_date=%s 对齐到缓存最新 end_date=%s", end_date, cache_end)
            end_date = cache_end
    try:
        end_d = date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:8]))
    except (ValueError, IndexError):
        return get_daily_bars(symbols, end_date, end_date, require_cache=require_cache, lookback_days_for_error=lookback_days)
    start_d = end_d - timedelta(days=lookback_days)
    start_date = start_d.strftime("%Y%m%d")
    return get_daily_bars(
        symbols, start_date, end_date,
        require_cache=require_cache,
        lookback_days_for_error=lookback_days,
    )


# 初始化时预拉日线：全市场（不设上限），lookback 从默认策略读取（与选股逻辑一致）
_INIT_DAILY_SYMBOL_CAP = 10000


def _symbols_with_daily_cache_for_today() -> set:
    """
    扫描 .cache/daily 与内存缓存，返回「已有覆盖最近交易日」的标的集合（归一化代码）。
    用于预拉时一次性判断，避免与滚动更新重复拉取。交易日 15:00 前按上一交易日算。
    """
    today_str = get_last_trade_date_str(for_data_availability=True)
    out: set = set()
    # 内存缓存
    for (s, _, end) in _DAILY_CACHE:
        if len(end) == 8 and end >= today_str:
            out.add(s)
    # 文件缓存：一次遍历所有文件
    cache_dir = backend_cache_root() / "daily"
    if cache_dir.exists():
        for path in cache_dir.glob("*_*_*.json"):
            parts = path.stem.split("_")
            if len(parts) >= 3 and len(parts[-1]) == 8 and parts[-1] >= today_str:
                out.add(parts[0])
    return out


def _symbol_has_daily_cache_for_today(symbol: str, symbols_with_cache: set | None = None) -> bool:
    """判断该标的是否已有覆盖「最近交易日」的日线缓存。可传入已算好的 symbols_with_cache 避免重复扫描。"""
    sym = _normalize_symbol(symbol)
    if symbols_with_cache is not None:
        return sym in symbols_with_cache
    return sym in _symbols_with_daily_cache_for_today()


def prefetch_daily_bars_for_init(quotes: List[Any], symbol_cap: int = _INIT_DAILY_SYMBOL_CAP) -> None:
    """
    初始化时预拉日线，供选股策略使用。按成交量取前 symbol_cap 只，lookback 取所有策略中的最大值（含自定义策略），避免某策略 lookback 大于预拉范围导致 503。
    若某标的已有当日的日线缓存则跳过，不重复拉取。
    quotes 需有 .symbol 与 .volume 属性（如 StockQuote）。
    """
    if not quotes:
        return
    lookback_days = 60
    try:
        from app.db.store import get_default_strategy_id, get_strategy, list_strategies
        from app.services.strategy_config import parse_strategy_content

        # 取所有策略（含自定义 strategy_id=2 等）的 lookback_days 最大值，保证任意策略选股时日线缓存都够
        for s in list_strategies():
            content = s.get("content")
            if isinstance(content, dict):
                config = parse_strategy_content(content, s.get("type", "formula"))
                lookback_days = max(lookback_days, config.lookback_days)
        # 若没有任何策略或解析失败，上面循环不执行，保持 60
    except Exception:  # noqa: BLE001
        pass
    by_vol = sorted(quotes, key=lambda q: float(getattr(q, "volume", 0) or 0), reverse=True)
    symbols = [str(getattr(q, "symbol", "")).strip() for q in by_vol[:symbol_cap] if getattr(q, "symbol", None)]
    if not symbols:
        return
    # 一次性扫描缓存目录+内存，得到「已有最近交易日」的标的，避免与滚动更新重复拉取
    symbols_with_cache = _symbols_with_daily_cache_for_today()
    symbols_to_fetch = [s for s in symbols if _normalize_symbol(s) not in symbols_with_cache]
    skipped = len(symbols) - len(symbols_to_fetch)
    if skipped:
        logger.info("日线预拉: %d 只已有当日缓存，跳过", skipped)
    if not symbols_to_fetch:
        logger.info("日线预拉: 全部已有当日缓存，无需拉取")
        return
    end_date_str = get_last_trade_date_str(for_data_availability=True)
    logger.info("日线预拉: %d 只，lookback %d 天", len(symbols_to_fetch), lookback_days)
    get_daily_bars_lookback(symbols_to_fetch, end_date_str, lookback_days=lookback_days)
    logger.info("日线预拉完成")

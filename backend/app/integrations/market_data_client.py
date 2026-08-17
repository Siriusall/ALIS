from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from app.core.trade_calendar import get_last_trade_date, get_last_trade_date_str
from app.data_platform.db import _conn
from app.services.tushare_universe import get_basic_info_map

logger = logging.getLogger("quant.market_data")

_quotes_cache: tuple[str, List["StockQuote"]] = ("", [])
_fetch_lock = threading.Lock()

_hist_cache: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
_HIST_CACHE_MAX = 256
_daily_by_trade_date_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
_DAILY_BY_DATE_CACHE_MAX = 16


def _parse_int_env(name: str, default: int, min_v: int, max_v: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    return max(min_v, min(max_v, v))


def _parse_float_env(name: str, default: float, min_v: float, max_v: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = default
    return max(min_v, min(max_v, v))


# TuShare 日线默认并发：提升到 8（可通过环境变量覆盖）。
_HIST_CONCURRENCY = _parse_int_env("TUSHARE_HIST_CONCURRENCY", 8, 1, 16)
_hist_condition = threading.Condition()
_hist_inflight = 0

# 全局最小请求间隔（秒），用于跨线程节流，避免瞬时洪峰触发“请求过于频繁”。
_HIST_MIN_INTERVAL_SEC = _parse_float_env("TUSHARE_HIST_MIN_INTERVAL_SEC", 0.08, 0.0, 5.0)
_hist_rate_lock = threading.Lock()
_hist_last_req_at = 0.0

_HIST_RETRY_BACKOFF_SEC = (1.0, 2.0, 4.0, 8.0)
_HIST_MAX_ATTEMPTS = len(_HIST_RETRY_BACKOFF_SEC) + 1


@dataclass
class StockQuote:
    symbol: str
    name: str
    price: float
    change_pct: float
    volume: float
    turnover: float


class MarketDataUnavailableError(RuntimeError):
    """Exact-date normalized market quotes are not available."""


def _normalize_symbol(s: str) -> str:
    raw = str(s or "").strip()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    low = raw.lower()
    for prefix in ("sh", "sz", "bj"):
        if low.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    if raw.isdigit() and len(raw) < 6:
        raw = raw.zfill(6)
    return raw


def _today_trade_date() -> str:
    return get_last_trade_date_str(for_data_availability=True)


def _as_of_iso() -> str:
    ymd = _today_trade_date()
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}T15:00:00"


def _get_quotes_cache() -> Optional[List["StockQuote"]]:
    cache_date, quotes = _quotes_cache
    if quotes and cache_date == _today_trade_date():
        return quotes
    return None


def _set_quotes_cache(quotes: List["StockQuote"]) -> None:
    global _quotes_cache
    _quotes_cache = (_today_trade_date(), quotes)


def _load_quotes_from_postgres(trade_date: Optional[str] = None) -> Optional[List["StockQuote"]]:
    """Read one exact normalized PostgreSQL session; never choose MAX(date)."""
    try:
        with _conn() as conn:
            target_date = str(trade_date or _today_trade_date()).replace("-", "")[:8]
            if not target_date:
                return None
            rows = conn.execute(
                """SELECT symbol,name,close,change_pct,volume,turnover
                   FROM dc_stock_daily_snapshot
                   WHERE trade_date=? AND close IS NOT NULL ORDER BY symbol""",
                (target_date,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL market snapshot read failed: %s", str(exc)[:160])
        return None
    quotes = []
    for row in rows:
        item = dict(row)
        symbol = _normalize_symbol(str(item.get("symbol") or ""))
        if symbol:
            quotes.append(StockQuote(
                symbol=symbol, name=str(item.get("name") or ""),
                price=float(item.get("close") or 0), change_pct=float(item.get("change_pct") or 0),
                volume=float(item.get("volume") or 0), turnover=float(item.get("turnover") or 0),
            ))
    return quotes or None


def _refresh_market_in_postgres(trade_date: str) -> None:
    """Route remote refresh through the durable data-platform ingestion flow."""
    from app.data_platform.services.data_center import build_stock_daily_snapshot, sync_market_bundle, sync_stock_basic

    sync_stock_basic()
    result = sync_market_bundle(trade_date)
    if not result.get("ok"):
        raise RuntimeError(
            f"market_bundle_not_ready:{result.get('failure_category') or result.get('status') or 'unknown'}"
        )
    build_stock_daily_snapshot(trade_date)


def _to_quote_dict(rows: List["StockQuote"]) -> Dict[str, "StockQuote"]:
    return {q.symbol: q for q in rows if q.symbol}


def _to_ts_code(symbol: str) -> str:
    sym = _normalize_symbol(symbol)
    # 北交所新代码段（如 920xxx）需优先映射到 .BJ，避免误判为上交所 .SH。
    if sym.startswith("92"):
        return f"{sym}.BJ"
    if sym.startswith(("6", "9")):
        return f"{sym}.SH"
    if sym.startswith(("0", "3")):
        return f"{sym}.SZ"
    return f"{sym}.BJ"


def _is_retriable_hist_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "请求过于频繁" in msg
        or "too frequent" in msg
        or "rate limit" in msg
        or "timeout" in msg
        or "temporarily unavailable" in msg
        or "网络" in msg
    )


def _set_hist_concurrency(limit: int) -> None:
    global _HIST_CONCURRENCY
    clamped = max(1, min(16, int(limit)))
    with _hist_condition:
        _HIST_CONCURRENCY = clamped
        _hist_condition.notify_all()


def _acquire_hist_slot() -> None:
    global _hist_inflight
    with _hist_condition:
        while _hist_inflight >= _HIST_CONCURRENCY:
            _hist_condition.wait()
        _hist_inflight += 1


def _release_hist_slot() -> None:
    global _hist_inflight
    with _hist_condition:
        if _hist_inflight > 0:
            _hist_inflight -= 1
        _hist_condition.notify_all()


def _wait_hist_rate_limit() -> None:
    global _hist_last_req_at
    if _HIST_MIN_INTERVAL_SEC <= 0:
        return
    with _hist_rate_lock:
        now = time.monotonic()
        wait_sec = (_hist_last_req_at + _HIST_MIN_INTERVAL_SEC) - now
        if wait_sec > 0:
            time.sleep(wait_sec)
            now = time.monotonic()
        _hist_last_req_at = now


def _fetch_spot_from_tushare() -> List["StockQuote"]:
    """Compatibility name retained; remote access must go through the data platform."""
    return _load_quotes_from_postgres(_today_trade_date()) or []


def fetch_a_share_spot(limit: Optional[int] = None, allow_remote: bool = False) -> List[StockQuote]:
    cached = _get_quotes_cache()
    if cached:
        return cached[:limit] if limit is not None else cached

    postgres_quotes = _load_quotes_from_postgres()
    if postgres_quotes:
        _set_quotes_cache(postgres_quotes)
        return postgres_quotes[:limit] if limit is not None else postgres_quotes

    with _fetch_lock:
        cached = _get_quotes_cache()
        if cached:
            return cached[:limit] if limit is not None else cached
        postgres_quotes = _load_quotes_from_postgres()
        if postgres_quotes:
            _set_quotes_cache(postgres_quotes)
            return postgres_quotes[:limit] if limit is not None else postgres_quotes
        if not allow_remote:
            return []
        try:
            _refresh_market_in_postgres(_today_trade_date())
            quotes = _load_quotes_from_postgres(_today_trade_date()) or []
            if quotes:
                _set_quotes_cache(quotes)
                return quotes[:limit] if limit is not None else quotes
        except Exception as e:  # noqa: BLE001
            logger.error("规范行情刷新失败（trade_date=%s）: %s", _today_trade_date(), str(e)[:160])
            raise MarketDataUnavailableError(f"market_data_refresh_failed:{str(e)[:160]}") from e
        raise MarketDataUnavailableError(
            f"market_snapshot_empty:{_today_trade_date()}"
        )


def fetch_symbol_detail(symbol: str) -> Dict[str, Any]:
    sym = _normalize_symbol(symbol)
    rows = fetch_a_share_spot(limit=None, allow_remote=False)
    data = _to_quote_dict(rows).get(sym)
    if data is None:
        return {"symbol": symbol, "note": "数据不可用（请先初始化）"}
    return {
        "代码": data.symbol,
        "名称": data.name,
        "最新价": data.price,
        "涨跌幅": data.change_pct,
        "成交量": data.volume,
        "成交额": data.turnover,
    }


def fetch_symbol_names(symbols: List[str]) -> Dict[str, str]:
    if not symbols:
        return {}
    spot_map = _to_quote_dict(fetch_a_share_spot(limit=None, allow_remote=False))
    basic_map = get_basic_info_map(cache_only=True)
    out: Dict[str, str] = {}
    for raw in symbols:
        norm = _normalize_symbol(raw)
        name = ""
        q = spot_map.get(norm)
        if q and q.name:
            name = q.name
        elif norm in basic_map:
            name = basic_map[norm].name
        out[str(raw)] = name
    return out


def fetch_symbol_prices(symbols: List[str]) -> Dict[str, float]:
    prices, _ = fetch_symbol_prices_with_time(symbols)
    return prices


def fetch_symbol_prices_with_time(symbols: List[str]) -> tuple[Dict[str, float], str]:
    if not symbols:
        return {}, _as_of_iso()
    rows = fetch_a_share_spot(limit=None, allow_remote=False)
    by_symbol = _to_quote_dict(rows)
    out: Dict[str, float] = {}
    for raw in symbols:
        norm = _normalize_symbol(raw)
        out[str(raw)] = float(by_symbol.get(norm).price if norm in by_symbol else 0.0)
    return out, _as_of_iso()


@contextmanager
def hist_concurrency_scope(limit: int):
    prev = _HIST_CONCURRENCY
    _set_hist_concurrency(limit)
    try:
        yield
    finally:
        _set_hist_concurrency(prev)


def fetch_stock_zh_a_hist(symbol: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    sym = _normalize_symbol(symbol)
    key = (sym, start_date, end_date)
    if key in _hist_cache:
        return _hist_cache[key]

    out: List[Dict[str, Any]] = []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT trade_date,open,high,low,close,volume,turnover
                   FROM dc_stock_daily_snapshot
                   WHERE symbol=? AND trade_date BETWEEN ? AND ?
                   ORDER BY trade_date""",
                (sym, str(start_date).replace("-", ""), str(end_date).replace("-", "")),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - caller receives a visible data gap
        logger.warning("PostgreSQL 日线: %s %s-%s 读取失败 | %s", sym, start_date, end_date, str(exc)[:120])
        rows = []
    for raw in rows:
        row = dict(raw)
        td = str(row.get("trade_date") or "")
        date_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td[:10]
        out.append({
            "date": date_str,
            "open": float(row.get("open") or 0),
            "high": float(row.get("high") or 0),
            "low": float(row.get("low") or 0),
            "close": float(row.get("close") or 0),
            "volume": float(row.get("volume") or 0),
            "turnover": float(row.get("turnover") or 0),
        })

    # An empty read is an unresolved data gap, not a successful cache entry;
    # keeping it would turn a transient DB/provider failure into a terminal
    # false-negative for the rest of the process.
    if out:
        if len(_hist_cache) >= _HIST_CACHE_MAX:
            _hist_cache.pop(next(iter(_hist_cache)))
        _hist_cache[key] = out
    return out


def fetch_daily_by_trade_date(trade_date: str) -> Dict[str, Dict[str, Any]]:
    """
    按交易日一次性拉取全市场日线，返回：
      {
        "000001": {"date","open","high","low","close","volume","turnover"},
        ...
      }
    用于批量滚动更新，避免逐股票 5000+ 次请求触发频率限制。
    """
    ymd = str(trade_date or "").strip().replace("-", "")
    if len(ymd) != 8 or not ymd.isdigit():
        return {}
    if ymd in _daily_by_trade_date_cache:
        return _daily_by_trade_date_cache[ymd]

    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT symbol,trade_date,open,high,low,close,volume,turnover
                   FROM dc_stock_daily_snapshot WHERE trade_date=?""",
                (ymd,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - preserve the missing-slice signal
        logger.warning("PostgreSQL 日线批量: %s 读取失败 | %s", ymd, str(exc)[:120])
        rows = []
    for raw in rows:
        row = dict(raw)
        symbol = _normalize_symbol(str(row.get("symbol") or ""))
        if not symbol:
            continue
        td = str(row.get("trade_date") or ymd)
        date_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td[:10]
        out[symbol] = {
            "date": date_str,
            "open": float(row.get("open") or 0),
            "high": float(row.get("high") or 0),
            "low": float(row.get("low") or 0),
            "close": float(row.get("close") or 0),
            "volume": float(row.get("volume") or 0),
            "turnover": float(row.get("turnover") or 0),
        }

    if out:
        if len(_daily_by_trade_date_cache) >= _DAILY_BY_DATE_CACHE_MAX:
            _daily_by_trade_date_cache.pop(next(iter(_daily_by_trade_date_cache)))
        _daily_by_trade_date_cache[ymd] = out
    return out

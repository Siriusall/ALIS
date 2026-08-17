"""Normalized PostgreSQL daily-bar reads for factors, valuation, and reports."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List

from app.data_platform.db import _conn

logger = logging.getLogger("quant.daily_data")


class DailyCacheNotReadyError(Exception):
    """A required exact-session normalized daily bar is not available."""

    def __init__(self, missing_count: int, end_date: str, lookback_days: int, message: str | None = None):
        self.missing_count = missing_count
        self.end_date = end_date
        self.lookback_days = lookback_days
        super().__init__(message or (
            f"规范 PostgreSQL 日线未覆盖策略所需标的（缺 {missing_count} 只，"
            f"end_date={end_date}, lookback={lookback_days} 天）。"
        ))


@dataclass
class DailyBar:
    """One normalized daily OHLCV observation."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def _normalize_symbol(value: str) -> str:
    symbol = str(value).strip()
    if "." in symbol:
        symbol = symbol.split(".", 1)[0]
    for prefix in ("sh", "sz", "bj"):
        if symbol.lower().startswith(prefix):
            symbol = symbol[len(prefix):]
            break
    return symbol.zfill(6) if len(symbol) < 6 else symbol


def _normalize_ymd(value: str) -> str:
    return str(value or "").replace("-", "")[:8]


def _load_daily_from_postgres(
    symbols: List[str], start_date: str, end_date: str,
) -> Dict[str, List[DailyBar]]:
    """Read only durable data-platform records, never local files or a live feed."""
    normalized = [_normalize_symbol(symbol) for symbol in symbols]
    ts_codes = [
        f"{symbol}{'.SH' if symbol.startswith(('6', '9')) else '.SZ' if symbol.startswith(('0', '3')) else '.BJ'}"
        for symbol in normalized
    ]
    if not ts_codes:
        return {}
    start_ymd, end_ymd = _normalize_ymd(start_date), _normalize_ymd(end_date)
    placeholders = ",".join("?" for _ in ts_codes)
    try:
        with _conn() as conn:
            rows = conn.execute(
                f"""SELECT ts_code, trade_date, payload_json
                    FROM dc_tushare_raw_records
                    WHERE api_name='daily' AND trade_date BETWEEN ? AND ? AND ts_code IN ({placeholders})
                    ORDER BY ts_code, trade_date""",
                (start_ymd, end_ymd, *ts_codes),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL daily-bar read failed: %s", str(exc)[:160])
        return {}

    result: Dict[str, List[DailyBar]] = {}
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(str(item.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("PostgreSQL daily-bar payload is invalid: ts_code=%s", item.get("ts_code"))
            continue
        symbol = _normalize_symbol(str(item.get("ts_code") or payload.get("ts_code") or ""))
        if not symbol:
            continue
        result.setdefault(symbol, []).append(DailyBar(
            date=str(payload.get("trade_date") or item.get("trade_date") or ""),
            open=float(payload.get("open") or 0),
            high=float(payload.get("high") or 0),
            low=float(payload.get("low") or 0),
            close=float(payload.get("close") or 0),
            volume=float(payload.get("vol") or 0),
            turnover=float(payload.get("amount") or 0),
        ))
    return result


def get_daily_bars(
    symbols: List[str],
    start_date: str,
    end_date: str,
    require_cache: bool = False,
    lookback_days_for_error: int = 0,
) -> Dict[str, List[DailyBar]]:
    """Read an exact date range from PostgreSQL.

    ``require_cache=True`` is the formal path: every requested symbol must
    contain an observation on the requested end date. An earlier bar inside
    the range is evidence of a gap, not an acceptable substitute.
    """
    seen: set[str] = set()
    normalized: List[str] = []
    for value in symbols:
        if not str(value or "").strip():
            continue
        symbol = _normalize_symbol(value)
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    if not normalized:
        return {}

    end_ymd = _normalize_ymd(end_date)
    bars_by_symbol = _load_daily_from_postgres(normalized, start_date, end_date)
    result: Dict[str, List[DailyBar]] = {}
    missing: List[str] = []
    for symbol in normalized:
        bars = bars_by_symbol.get(symbol, [])
        if bars:
            result[symbol] = bars
        if not any(_normalize_ymd(bar.date) == end_ymd for bar in bars):
            missing.append(symbol)

    if missing and require_cache:
        message = (
            f"规范 PostgreSQL 日线未覆盖目标交易日 {end_ymd}："
            f"缺 {len(missing)}/{len(normalized)} 只标的；不会使用本地文件、旧日期或临时网络回退。"
        )
        logger.warning(message)
        raise DailyCacheNotReadyError(len(missing), end_ymd, lookback_days_for_error, message)
    return result


def get_daily_bars_lookback(
    symbols: List[str],
    end_date: str,
    lookback_days: int = 60,
    require_cache: bool = False,
) -> Dict[str, List[DailyBar]]:
    """Read a lookback ending on the requested exact target session."""
    end_ymd = _normalize_ymd(end_date)
    try:
        end_day = date(int(end_ymd[:4]), int(end_ymd[4:6]), int(end_ymd[6:8]))
    except (ValueError, IndexError):
        return get_daily_bars(
            symbols, end_ymd, end_ymd,
            require_cache=require_cache,
            lookback_days_for_error=lookback_days,
        )
    start_ymd = (end_day - timedelta(days=max(0, lookback_days))).strftime("%Y%m%d")
    return get_daily_bars(
        symbols, start_ymd, end_ymd,
        require_cache=require_cache,
        lookback_days_for_error=lookback_days,
    )

"""Bounded point-in-time reads used by the read-only positions console."""

from __future__ import annotations

from typing import Any

from app.core.a_share_fees import is_exchange_etf
from app.data_platform.db import _conn as data_conn
from app.data_platform.services.observation_clock import _table_columns
from app.services.defensive_assets import defensive_asset_series_batch
from app.services.point_in_time import signal_available_time


def _etf_ts_code(symbol: str) -> str:
    """Resolve the exchange for a bare exchange-traded fund code."""
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ")):
        return value
    code = value.split(".", 1)[0].zfill(6)
    # 50/51/52/56/58 are SSE ETF namespaces; 15/16 are SZSE.
    return f"{code}.SH" if code.startswith(("50", "51", "52", "56", "58")) else f"{code}.SZ"


def resolve_stock_position_prices(
    symbols: list[str], trade_date: str,
) -> tuple[dict[str, float], str]:
    """Read the exact requested-session stock closes in one bounded query.

    A position console is a valuation read, not a permission to relabel the
    prior close as today's price.  Missing exact-session rows stay missing.
    """
    compact_date = str(trade_date or "").replace("-", "")[:8]
    if not compact_date:
        return {}, ""
    symbol_by_code: dict[str, str] = {}
    for symbol in symbols:
        raw = str(symbol or "").strip().upper()
        if not raw:
            continue
        code = raw.split(".", 1)[0].zfill(6)
        suffix = (
            ".SH" if code.startswith(("6", "9"))
            else ".BJ" if code.startswith(("4", "8"))
            else ".SZ"
        )
        symbol_by_code.setdefault(raw if "." in raw else f"{code}{suffix}", raw)
    if not symbol_by_code:
        return {}, ""
    codes = tuple(sorted(symbol_by_code))
    placeholders = ",".join("?" for _ in codes)
    cutoff = signal_available_time(compact_date)
    with data_conn() as c:
        columns = _table_columns(c, "dc_stock_daily_snapshot")
        if "observed_at" in columns:
            clock_column = "observed_at"
        elif bool(getattr(c, "_is_postgres", False)):
            return {}, ""
        else:
            # Isolated legacy SQLite fixtures only. Production PostgreSQL
            # always takes the immutable observation-clock branch above.
            clock_column = "available_time"
        rows = c.execute(
            f"""WITH ranked AS (
                    SELECT ts_code,trade_date,{clock_column} AS available_time,close,
                           ROW_NUMBER() OVER (
                               PARTITION BY ts_code
                               ORDER BY trade_date DESC,{clock_column} DESC
                           ) AS row_rank
                      FROM dc_stock_daily_snapshot
                     WHERE ts_code IN ({placeholders})
                       AND trade_date=?
                       AND {clock_column}<=?
                       AND close IS NOT NULL
                )
                SELECT ts_code,trade_date,available_time,close
                  FROM ranked
                 WHERE row_rank=1
                 ORDER BY ts_code""",
            (*codes, compact_date, cutoff),
        ).fetchall()
    prices: dict[str, float] = {}
    source_times: list[str] = []
    for row in rows:
        item = dict(row)
        close = float(item.get("close") or 0)
        symbol = symbol_by_code.get(str(item.get("ts_code") or ""))
        if symbol and close > 0:
            prices[symbol] = close
            if item.get("available_time"):
                source_times.append(str(item["available_time"]))
    return prices, min(source_times) if source_times else ""


def resolve_position_prices(symbols: list[str], trade_date: str) -> tuple[dict[str, float], str]:
    """Resolve stock and ETF closes with bounded local batch reads."""
    normalized = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    stock_symbols = [symbol for symbol in normalized if not is_exchange_etf(symbol)]
    prices, source_time = resolve_stock_position_prices(stock_symbols, trade_date)
    etf_by_code = {
        _etf_ts_code(symbol): symbol
        for symbol in normalized if is_exchange_etf(symbol)
    }
    if not etf_by_code:
        return prices, source_time
    series_by_code = defensive_asset_series_batch(etf_by_code, trade_date, limit=20, exact_date=True)
    source_times = [source_time] if source_time else []
    for code, symbol in etf_by_code.items():
        bar, _returns = series_by_code.get(code, (None, []))
        close = float((bar or {}).get("close") or 0)
        if close > 0:
            prices[symbol] = close
        if (bar or {}).get("available_time"):
            source_times.append(str(bar["available_time"]))
    return prices, min(source_times) if source_times else ""

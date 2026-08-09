from __future__ import annotations

from typing import Any, Iterable

from app.data_platform.db import _conn as data_conn
from app.core.a_share_fees import is_exchange_etf
from app.services.backtest_data_source import BacktestDataSource
from app.services.defensive_assets import defensive_asset_series, defensive_asset_series_batch


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _etf_ts_code(symbol: str) -> str:
    code = str(symbol or "").strip().upper()
    if code.endswith((".SH", ".SZ")):
        return code
    return f"{code}.SH" if code.startswith("5") else f"{code}.SZ"


def point_in_time_position_market_series(
    *,
    symbol: str,
    as_of: str,
    fallback_price: float = 0.0,
    lookback: int = 120,
    data_source: BacktestDataSource | None = None,
) -> dict[str, Any]:
    """Resolve one position's point-in-time close and return history."""
    compact_date = str(as_of or "").replace("-", "")[:8]
    normalized_symbol = str(symbol or "").strip()
    history_limit = max(20, min(252, int(lookback or 120)))

    if is_exchange_etf(normalized_symbol) and compact_date:
        ts_code = _etf_ts_code(normalized_symbol)
        try:
            bar, returns = defensive_asset_series(ts_code, compact_date, limit=history_limit)
        except Exception:  # noqa: BLE001 - a read-only valuation must degrade safely
            bar, returns = None, []
        close = _number((bar or {}).get("close"))
        if close > 0:
            return {
                "price": close,
                "returns": returns,
                "price_source": "fund_daily_point_in_time_close",
                "trade_date": str((bar or {}).get("trade_date") or compact_date),
            }

    # Current account valuation must use the quoted, unadjusted close.  An
    # adj_factor belongs to historical return reconstruction; multiplying a
    # live holding's market price by it turns (for example) a CNY 5.43 quote
    # into a fictitious CNY 136.65 position value.  Keep adjusted prices only
    # for the return series used by risk diagnostics.
    if compact_date:
        try:
            with data_conn() as conn:
                code = normalized_symbol.split(".", 1)[0].zfill(6)
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                rows = conn.execute(
                    """SELECT trade_date, close, adj_factor
                         FROM dc_stock_daily_snapshot
                        WHERE ts_code=? AND trade_date<=? AND close IS NOT NULL
                        ORDER BY trade_date DESC LIMIT ?""",
                    (f"{code}{suffix}", compact_date, history_limit),
                ).fetchall()
            if rows:
                ordered = [dict(row) for row in reversed(rows)]
                raw_closes = [_number(row.get("close")) for row in ordered]
                adjusted_closes = [
                    _number(row.get("close")) * _number(row.get("adj_factor"), 1.0)
                    for row in ordered
                ]
                raw_closes = [value for value in raw_closes if value > 0]
                adjusted_closes = [value for value in adjusted_closes if value > 0]
                if raw_closes:
                    return {
                        "price": raw_closes[-1],
                        "returns": [
                            adjusted_closes[index] / adjusted_closes[index - 1] - 1.0
                            for index in range(1, len(adjusted_closes))
                            if adjusted_closes[index - 1] > 0
                        ],
                        "price_source": "stock_daily_point_in_time_close",
                        "trade_date": compact_date,
                    }
        except Exception:  # noqa: BLE001 - valuation must retain safe fallback
            pass

    ds = data_source or BacktestDataSource()
    try:
        bars = ds.get_hist_window(normalized_symbol, compact_date, history_limit) if compact_date else []
    except Exception:  # noqa: BLE001 - cost fallback keeps read-only pages available
        bars = []
    closes = [_number(row.close) for row in bars if _number(row.close) > 0]
    returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    if closes:
        return {
            "price": closes[-1],
            "returns": returns,
            "price_source": "stock_daily_point_in_time_close",
            "trade_date": compact_date,
        }
    return {
        "price": _number(fallback_price),
        "returns": [],
        "price_source": "position_cost_fallback",
        "trade_date": compact_date,
    }


def build_position_market_series_cache(
    positions: Iterable[dict[str, Any]],
    *,
    as_of: str,
    lookback: int = 120,
    data_source: BacktestDataSource | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve each live holding once for a read-only aggregate request."""
    if data_source is not None:
        source = data_source
        result: dict[str, dict[str, Any]] = {}
        for position in positions:
            if int(position.get("quantity") or 0) <= 0:
                continue
            symbol = str(position.get("symbol") or "").strip()
            if not symbol or symbol in result:
                continue
            result[symbol] = point_in_time_position_market_series(
                symbol=symbol,
                as_of=as_of,
                fallback_price=_number(position.get("price")),
                lookback=lookback,
                data_source=source,
            )
        return result

    compact_date = str(as_of or "").replace("-", "")[:8]
    history_limit = max(20, min(252, int(lookback or 120)))
    live_positions = [
        position for position in positions if int(position.get("quantity") or 0) > 0
    ]
    result = {}
    etf_positions = [
        position for position in live_positions
        if is_exchange_etf(str(position.get("symbol") or "").strip())
    ]
    etf_codes = {
        _etf_ts_code(str(position.get("symbol") or "").strip()): str(position.get("symbol") or "").strip()
        for position in etf_positions
    }
    stock_positions = [
        position for position in live_positions
        if str(position.get("symbol") or "").strip() not in result
        and not is_exchange_etf(str(position.get("symbol") or "").strip())
    ]
    stock_codes = []
    symbol_by_code: dict[str, str] = {}
    for position in stock_positions:
        symbol = str(position.get("symbol") or "").strip()
        code = symbol.split(".", 1)[0].zfill(6)
        suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
        ts_code = f"{code}{suffix}"
        if ts_code not in symbol_by_code:
            symbol_by_code[ts_code] = symbol
            stock_codes.append(ts_code)
    if compact_date and (etf_codes or stock_codes):
        placeholders = ",".join("?" for _ in stock_codes)
        try:
            with data_conn() as conn:
                if etf_codes:
                    series_by_code = defensive_asset_series_batch(
                        etf_codes.keys(), compact_date, limit=history_limit, connection=conn
                    )
                    for code, symbol in etf_codes.items():
                        bar, returns = series_by_code.get(code, (None, []))
                        close = _number((bar or {}).get("close"))
                        if close > 0:
                            result[symbol] = {
                                "price": close,
                                "returns": returns,
                                "price_source": "fund_daily_point_in_time_close",
                                "trade_date": str((bar or {}).get("trade_date") or compact_date),
                            }
                if stock_codes:
                    rows = conn.execute(
                        f"""WITH ranked AS (
                               SELECT ts_code,trade_date,close,adj_factor,
                                      ROW_NUMBER() OVER (
                                          PARTITION BY ts_code ORDER BY trade_date DESC
                                      ) AS series_rank
                                 FROM dc_stock_daily_snapshot
                                WHERE ts_code IN ({placeholders})
                                  AND trade_date<=?
                                  AND adj_factor IS NOT NULL
                           )
                           SELECT ts_code,trade_date,close,adj_factor
                             FROM ranked
                            WHERE series_rank<=?
                            ORDER BY ts_code,trade_date""",
                        (*stock_codes, compact_date, history_limit),
                    ).fetchall()
                    grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in stock_codes}
                    for row in rows:
                        grouped.setdefault(str(dict(row).get("ts_code") or ""), []).append(dict(row))
                    for ts_code, symbol in symbol_by_code.items():
                        raw_rows = grouped.get(ts_code) or []
                        raw_closes = [
                            _number(row.get("close"))
                            for row in raw_rows
                            if _number(row.get("close")) > 0
                        ]
                        adjusted_closes = [
                            _number(row.get("close")) * _number(row.get("adj_factor"), 1.0)
                            for row in raw_rows
                            if _number(row.get("close")) > 0
                        ]
                        if raw_closes:
                            result[symbol] = {
                                "price": raw_closes[-1],
                                "returns": [
                                    adjusted_closes[index] / adjusted_closes[index - 1] - 1.0
                                    for index in range(1, len(adjusted_closes))
                                    if adjusted_closes[index - 1] > 0
                                ],
                                "price_source": "stock_daily_point_in_time_close",
                                "trade_date": compact_date,
                            }
        except Exception:  # noqa: BLE001 - preserve per-position safe fallback
            pass

    for position in live_positions:
        symbol = str(position.get("symbol") or "").strip()
        if not symbol or symbol in result:
            continue
        result[symbol] = point_in_time_position_market_series(
            symbol=symbol,
            as_of=as_of,
            fallback_price=_number(position.get("price")),
            lookback=history_limit,
        )
    return result

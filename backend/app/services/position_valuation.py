from __future__ import annotations

from typing import Any

from app.core.a_share_fees import is_exchange_etf
from app.services.backtest_data_source import BacktestDataSource
from app.services.defensive_assets import defensive_asset_bar, defensive_asset_returns


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
        bar = defensive_asset_bar(ts_code, compact_date)
        close = _number((bar or {}).get("close"))
        returns = defensive_asset_returns(ts_code, compact_date, limit=history_limit)
        if close > 0:
            return {
                "price": close,
                "returns": returns,
                "price_source": "fund_daily_point_in_time_close",
                "trade_date": str((bar or {}).get("trade_date") or compact_date),
            }

    ds = data_source or BacktestDataSource()
    bars = ds.get_hist_window(normalized_symbol, compact_date, history_limit) if compact_date else []
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

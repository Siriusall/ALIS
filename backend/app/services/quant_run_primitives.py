"""Pure date, price, and risk-budget primitives for quant execution."""

from __future__ import annotations

from typing import Any, Callable

from app.services.cache_consolidation import list_available_trade_dates
from app.services.quant_run_contracts import HistoricalDataMissingError


def list_consolidated_trade_dates() -> list[str]:
    dates = []
    for value in list_available_trade_dates():
        if len(value) == 8 and value.isdigit():
            dates.append(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    return sorted(set(dates))


def require_consolidated_trade_dates(
    min_count: int = 2,
    *,
    date_loader: Callable[[], list[str]] = list_consolidated_trade_dates,
) -> list[str]:
    dates = date_loader()
    if len(dates) < max(1, int(min_count or 2)):
        raise HistoricalDataMissingError("可用交易日不足，无法执行任务/补跑")
    return dates


def resolve_execute_date(
    signal_date: str,
    *,
    date_loader: Callable[[], list[str]] = list_consolidated_trade_dates,
) -> tuple[str, bool]:
    dates = date_loader()
    signal = str(signal_date or "").strip()
    if signal not in dates:
        raise ValueError(f"signal_date 不在可用交易日列表: {signal}")
    index = dates.index(signal)
    return (dates[index + 1], False) if index + 1 < len(dates) else (signal, True)


def row_trade_price(row: Any, *, prefer_open: bool = True) -> float:
    if row is None:
        return 0.0
    open_px = float(getattr(row, "open", 0) or 0)
    close_px = float(getattr(row, "close", 0) or 0)
    if prefer_open and open_px > 0:
        return open_px
    return close_px if close_px > 0 else open_px


def parse_risk_budget(risk_budget: dict[str, Any] | None) -> tuple[float, float]:
    values = risk_budget if isinstance(risk_budget, dict) else {}
    try:
        target_pct = float(values.get("target_position_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        target_pct = 1.0
    try:
        max_single = float(values.get("max_single_position_pct", 0.15) or 0.15)
    except (TypeError, ValueError):
        max_single = 0.15
    return max(0.0, min(1.0, target_pct)), max(0.0, min(1.0, max_single))

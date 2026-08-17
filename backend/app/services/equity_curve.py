"""Compute an equity curve from exact-session normalized daily bars."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from app.core.trade_calendar import get_last_trade_date
from app.services.daily_data import DailyCacheNotReadyError, get_daily_bars_lookback


def _parse_date(s: str) -> str:
    """Return an explicit execution date, never substitute the current session."""
    if not s:
        raise DailyCacheNotReadyError(0, "", 0, "权益曲线缺少执行日期，禁止用当前交易日代替。")
    raw = str(s).strip()
    date_part = raw[:10]
    try:
        if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
            return date.fromisoformat(date_part).strftime("%Y%m%d")
        if len(raw) == 8 and raw.isdigit():
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])).strftime("%Y%m%d")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y%m%d")
    except Exception as exc:
        raise DailyCacheNotReadyError(
            0, "", 0, "权益曲线执行日期无效，禁止用当前交易日代替。"
        ) from exc


def compute_equity_curve(
    orders: List[Dict[str, Any]],
    executed_at: str,
) -> List[Dict[str, Any]]:
    """
    orders: [{"symbol": str, "quantity": int, "price": float}, ...]
    executed_at: ISO date string.
    Returns [{ "date": "YYYY-MM-DD", "total_value": float, "by_symbol": { symbol: value } }, ...].
    只使用规范 PostgreSQL 日线；历史缺口直接抛出数据未就绪错误，不以建仓价或旧 spot 伪造曲线。
    """
    if not orders:
        return []
    symbols = []
    for o in orders:
        sym = (o.get("symbol") or "").strip()
        if sym and int(o.get("quantity") or 0) > 0:
            symbols.append(sym)
    if not symbols:
        return []
    start_str = _parse_date(executed_at)
    end_str = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    try:
        end_d = date(int(end_str[:4]), int(end_str[4:6]), int(end_str[6:8]))
        start_d = date(int(start_str[:4]), int(start_str[4:6]), int(start_str[6:8]))
        if start_d > end_d:
            raise DailyCacheNotReadyError(
                len(symbols), end_str, 0,
                "权益曲线执行日期晚于当前目标交易日，禁止用当前行情伪造历史曲线。",
            )
        lookback_days = max(1, (end_d - start_d).days)
    except (ValueError, IndexError):
        lookback_days = 60
    series: Dict[str, List[tuple[str, float]]] = {}
    bars_map = get_daily_bars_lookback(symbols, end_str, lookback_days=lookback_days, require_cache=True)
    for sym, bars in bars_map.items():
        if bars:
            series[sym] = [(b.date, b.close) for b in bars]

    if not series:
        raise DailyCacheNotReadyError(
            len(symbols), end_str, lookback_days,
            "规范 PostgreSQL 日线为空，无法生成权益曲线；禁止用建仓价或旧 spot 代替。",
        )

    missing_symbols = [symbol for symbol in symbols if symbol not in series]
    if missing_symbols:
        raise DailyCacheNotReadyError(
            len(missing_symbols), end_str, lookback_days,
            "权益曲线缺少持仓日线：" + ", ".join(sorted(set(missing_symbols))),
        )
    symbol_close_by_date: Dict[str, Dict[str, float]] = {}
    common_dates: set[str] | None = None
    for sym, pts in series.items():
        symbol_close_by_date[sym] = {}
        for d, c in pts:
            if c > 0:
                symbol_close_by_date[sym][d] = c
        dates = set(symbol_close_by_date[sym])
        common_dates = dates if common_dates is None else common_dates & dates
    sorted_dates = sorted(common_dates or set())
    if not sorted_dates or end_str not in {date.replace("-", "")[:8] for date in sorted_dates}:
        raise DailyCacheNotReadyError(
            len(symbols), end_str, lookback_days,
            "权益曲线缺少全部持仓共同覆盖的目标交易日收盘价，禁止沿用前收。",
        )

    result: List[Dict[str, Any]] = []
    for d in sorted_dates:
        total = 0.0
        by_symbol: Dict[str, float] = {}
        for o in orders:
            sym = (o.get("symbol") or "").strip()
            qty = int(o.get("quantity") or 0)
            if not sym or qty <= 0:
                continue
            closes = symbol_close_by_date.get(sym, {})
            close = closes[d]
            value = close * qty
            total += value
            by_symbol[sym] = round(value, 2)
        result.append({
            "date": d,
            "total_value": round(total, 2),
            "by_symbol": by_symbol,
        })
    return result

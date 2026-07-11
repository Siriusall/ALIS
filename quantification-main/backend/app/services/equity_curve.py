"""Compute equity curve from execution + historical daily bars or spot fallback."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from app.core.trade_calendar import get_last_trade_date
from app.integrations.market_data_client import fetch_a_share_spot
from app.services.daily_data import DailyCacheNotReadyError, get_daily_bars_lookback

logger = logging.getLogger("quant.equity_curve")


def _parse_date(s: str) -> str:
    """Return YYYYMMDD from ISO or similar."""
    if not s:
        return get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    s = s[:10].replace("-", "")
    if len(s) == 8:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except Exception:
        return get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")


def _to_iso_date(s: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD."""
    s = (s or "").replace("-", "")[:8]
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return get_last_trade_date(for_data_availability=True).isoformat()


def _compute_equity_curve_spot_fallback(
    orders: List[Dict[str, Any]],
    executed_at: str,
) -> List[Dict[str, Any]]:
    """
    当日收益回退：无日 K 时用「建仓成本 + 当前市值」两点绘制折线图。
    使用 A 股实时行情（新浪）获取当前价，日 K 亦为新浪。
    """
    if not orders:
        return []
    executed_ymd = _to_iso_date(_parse_date(executed_at))
    today_ymd = get_last_trade_date(for_data_availability=True).isoformat()

    cost = 0.0
    by_symbol_open: Dict[str, float] = {}
    for o in orders:
        sym = (o.get("symbol") or "").strip()
        qty = int(o.get("quantity") or 0)
        price = float(o.get("price") or 0)
        if not sym or qty <= 0:
            continue
        v = price * qty
        cost += v
        by_symbol_open[sym] = round(v, 2)

    point_open = {
        "date": executed_ymd,
        "total_value": round(cost, 2),
        "by_symbol": dict(by_symbol_open),
    }

    try:
        quotes = fetch_a_share_spot(limit=None)
        symbol_to_price = {q.symbol: q.price for q in (quotes or [])}
    except Exception as e:
        logger.warning("Spot fallback: 获取实时价失败，使用建仓价 | %s", str(e)[:80])
        symbol_to_price = {}

    current_value = 0.0
    by_symbol_now: Dict[str, float] = {}
    for o in orders:
        sym = (o.get("symbol") or "").strip()
        qty = int(o.get("quantity") or 0)
        exec_price = float(o.get("price") or 0)
        price = symbol_to_price.get(sym, exec_price)
        v = price * qty
        current_value += v
        by_symbol_now[sym] = round(v, 2)

    point_now = {
        "date": today_ymd,
        "total_value": round(current_value, 2),
        "by_symbol": by_symbol_now,
    }

    if executed_ymd == today_ymd:
        return [point_open, point_now]
    return [point_open, point_now]


def compute_equity_curve(
    orders: List[Dict[str, Any]],
    executed_at: str,
) -> List[Dict[str, Any]]:
    """
    orders: [{"symbol": str, "quantity": int, "price": float}, ...]
    executed_at: ISO date string.
    Returns [{ "date": "YYYY-MM-DD", "total_value": float, "by_symbol": { symbol: value } }, ...].
    优先使用日线缓存（require_cache=True）；若缓存未就绪则退化为「建仓日 + 当日」两点（用 spot 缓存）。
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
        lookback_days = max(1, (end_d - start_d).days)
    except (ValueError, IndexError):
        lookback_days = 60
    series: Dict[str, List[tuple[str, float]]] = {}
    try:
        bars_map = get_daily_bars_lookback(symbols, end_str, lookback_days=lookback_days, require_cache=True)
        for sym, bars in bars_map.items():
            if bars:
                series[sym] = [(b.date, b.close) for b in bars]
    except DailyCacheNotReadyError as e:
        logger.info("日线缓存未就绪（%s），使用当日收益回退（建仓成本 + 当前市值两点）", str(e)[:80])
        return _compute_equity_curve_spot_fallback(orders, executed_at)

    if not series:
        logger.info("日 K 无数据，使用当日收益回退（建仓成本 + 当前市值两点）")
        return _compute_equity_curve_spot_fallback(orders, executed_at)

    symbol_close_by_date: Dict[str, Dict[str, float]] = {}
    all_dates: set[str] = set()
    for sym, pts in series.items():
        symbol_close_by_date[sym] = {}
        for d, c in pts:
            symbol_close_by_date[sym][d] = c
            all_dates.add(d)
    sorted_dates = sorted(all_dates)

    last_close: Dict[str, float] = {}
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
            if d in closes:
                last_close[sym] = closes[d]
            close = last_close.get(sym, 0.0)
            value = close * qty
            total += value
            by_symbol[sym] = round(value, 2)
        result.append({
            "date": d,
            "total_value": round(total, 2),
            "by_symbol": by_symbol,
        })
    return result

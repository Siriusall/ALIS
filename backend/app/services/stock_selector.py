from __future__ import annotations

from datetime import date
from typing import List, Optional

from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.core.trade_calendar import get_last_trade_date
from app.integrations.market_data_client import StockQuote, fetch_a_share_spot
from app.services.cache_consolidation import read_consolidated_cache

from app.core.a_share_fees import LOT_SIZE


def select_affordable_stocks(
    limit: Optional[int] = None,
    max_one_lot_cost: Optional[float] = None,
    exchange: Optional[str] = None,
    *,
    allow_realtime_fallback: bool = False,
) -> List[StockQuote]:
    """
    获取可参与筛选的 A 股（真实行情）。
    - 价格 > 1 元。
    - 若传 max_one_lot_cost（总金额）：只保留 1 手价格 <= 该金额的股票（1手=100股）。
    """
    # 优先使用 all_in_one 聚合缓存，避免每次请求都触发分散缓存读取链路。
    quotes: List[StockQuote] = []
    try:
        trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
        payload = read_consolidated_cache(date=trade_date)
        data = payload.get("data") if isinstance(payload, dict) else None
        day = data.get(trade_date) if isinstance(data, dict) else None
        if isinstance(day, dict):
            for symbol, row in day.items():
                if not isinstance(row, dict):
                    continue
                quotes.append(
                    StockQuote(
                        symbol=str(symbol),
                        name=str(row.get("name", "")),
                        price=float(row.get("price", row.get("close", 0)) or 0),
                        change_pct=float(row.get("change_pct", 0) or 0),
                        volume=float(row.get("volume", 0) or 0),
                        turnover=float(row.get("turnover", 0) or 0),
                    )
                )
    except Exception:
        quotes = []
    if not quotes and allow_realtime_fallback:
        quotes = fetch_a_share_spot(limit=limit)

    out = [q for q in quotes if q.price > 1.0]
    exchange_norm = normalize_exchange_group(exchange)
    if exchange_norm != "ALL":
        out = [q for q in out if matches_exchange_group(q.symbol, exchange_norm)]
    if max_one_lot_cost is not None and max_one_lot_cost > 0:
        one_lot = LOT_SIZE
        out = [q for q in out if (q.price * one_lot) <= max_one_lot_cost]
    if limit is not None:
        return out[:limit]
    return out

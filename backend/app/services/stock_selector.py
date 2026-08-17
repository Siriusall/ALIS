from __future__ import annotations

from datetime import date
from typing import List, Optional

from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.integrations.market_data_client import StockQuote
from app.data_platform.db import _conn as data_conn
from app.data_platform.services.decision_data_contract import decision_data_contract
from app.data_platform.services.market_calendar import target_trade_date

from app.core.a_share_fees import LOT_SIZE


class StockSelectionDataNotReadyError(RuntimeError):
    """The current exchange-calendar session cannot safely feed a score."""

    def __init__(self, trade_date: str, contract: dict):
        self.trade_date = str(trade_date or "")
        self.contract = dict(contract or {})
        super().__init__(
            str(self.contract.get("reason") or f"{self.trade_date} 决策数据未就绪")
        )


def _current_selection_trade_date() -> str:
    """Require the full current data contract before exposing a stock score."""
    with data_conn() as conn:
        trade_date = target_trade_date(conn)
        contract = decision_data_contract(trade_date, conn=conn)
    if not bool(contract.get("ok")):
        raise StockSelectionDataNotReadyError(trade_date, contract)
    return trade_date


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
    # The selector is a research/read path, but it still must not turn a
    # stale aggregate cache or realtime quote into a T-day close.  Read only
    # the exact target-session normalized snapshot and retain its evidence.
    quotes: List[StockQuote] = []
    trade_date = _current_selection_trade_date()
    with data_conn() as c:
        rows = c.execute(
            """SELECT ts_code,symbol,name,close,change_pct,volume,turnover
                 FROM dc_stock_daily_snapshot
                WHERE trade_date=? AND close IS NOT NULL AND close>0
                  AND COALESCE(observed_at,'')<>''
                  AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'""",
            (trade_date,),
        ).fetchall()
    for row in rows:
        item = dict(row)
        symbol = str(item.get("symbol") or item.get("ts_code") or "").split(".", 1)[0]
        if not symbol:
            continue
        quotes.append(
            StockQuote(
                symbol=symbol,
                name=str(item.get("name") or ""),
                price=float(item.get("close") or 0),
                change_pct=float(item.get("change_pct") or 0),
                volume=float(item.get("volume") or 0),
                turnover=float(item.get("turnover") or 0),
            )
        )

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

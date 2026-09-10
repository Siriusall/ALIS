from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.a_share_fees import LOT_SIZE

TradeSide = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class TradeConstraintResult:
    ok: bool
    code: str
    message: str


def normalize_lot_quantity(raw_quantity: int) -> int:
    """Round quantity down to a valid A-share board lot."""

    qty = max(int(raw_quantity or 0), 0)
    return (qty // LOT_SIZE) * LOT_SIZE


def is_st_name(name: str | None) -> bool:
    n = str(name or "").strip().upper()
    return n.startswith("ST") or n.startswith("*ST")


def is_suspended(*, volume: float, open_price: float) -> bool:
    volume_val = float(volume if volume is not None else -1)
    return float(open_price or 0) <= 0 or volume_val == 0


def is_limit_up(*, change_pct: float, open_price: float, close_price: float = 0.0) -> bool:
    if float(open_price or 0) <= 0:
        return False
    return float(change_pct or 0) >= 9.7 or (
        float(close_price or 0) > 0 and abs(float(open_price) - float(close_price)) <= 0.01 and float(change_pct or 0) >= 9.5
    )


def is_limit_down(
    *,
    change_pct: float,
    open_price: float,
    close_price: float = 0.0,
    consecutive_limit_down_days: int = 0,
    is_st: bool = False,
) -> bool:
    if float(open_price or 0) <= 0:
        return False
    base_limit = float(change_pct or 0) <= -9.7 or (
        float(close_price or 0) > 0 and abs(float(open_price) - float(close_price)) <= 0.01 and float(change_pct or 0) <= -9.5
    )
    st_limit = bool(is_st) and float(change_pct or 0) <= -4.8
    consecutive_lock = int(consecutive_limit_down_days or 0) >= 2 and float(change_pct or 0) <= -9.0
    return base_limit or st_limit or consecutive_lock


def check_open_trade_constraints(
    *,
    side: TradeSide,
    symbol: str,
    name: str = "",
    open_price: float,
    close_price: float = 0.0,
    change_pct: float = 0.0,
    volume: float = 0.0,
    allow_st_buy: bool = False,
    consecutive_limit_down_days: int = 0,
) -> TradeConstraintResult:
    """Conservative daily-bar tradability check for T+1 open execution."""

    side = "SELL" if str(side).upper() == "SELL" else "BUY"
    if is_suspended(volume=volume, open_price=open_price):
        return TradeConstraintResult(False, "suspended_or_no_open_price", f"{symbol} 停牌或无有效开盘价，禁止{side}")
    if side == "BUY" and (not allow_st_buy) and is_st_name(name):
        return TradeConstraintResult(False, "st_buy_blocked", f"{symbol} 为 ST/*ST，默认禁止新开仓")
    if side == "BUY" and is_limit_up(change_pct=change_pct, open_price=open_price, close_price=close_price):
        return TradeConstraintResult(False, "limit_up_buy_blocked", f"{symbol} 近似涨停，按买不进处理")
    if side == "SELL" and is_limit_down(
        change_pct=change_pct,
        open_price=open_price,
        close_price=close_price,
        consecutive_limit_down_days=consecutive_limit_down_days,
        is_st=is_st_name(name),
    ):
        return TradeConstraintResult(False, "limit_down_sell_blocked", f"{symbol} 近似跌停，按卖不出处理")
    return TradeConstraintResult(True, "tradable", f"{symbol} 可按开盘价模拟{side}")

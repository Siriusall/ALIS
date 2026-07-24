"""
真实 A 股交易成本（滑点、佣金、印花税、过户费）。
参考：交易所与券商常见标准，买入仅佣金+过户费，卖出加印花税。
"""
from decimal import Decimal, ROUND_HALF_UP
# A 股 1 手 = 100 股
LOT_SIZE = 100

# 买入滑点（真实撮合偏离），按比例估算
SLIPPAGE_BUY_RATE = 0.001   # 0.1%
SLIPPAGE_SELL_RATE = 0.001  # 0.1%
ETF_SLIPPAGE_RATE = 0.0005  # 0.05%

# 佣金：万3，不足 5 元按 5 元
COMMISSION_RATE = 0.0003
COMMISSION_MIN_CNY = 5.0

# 印花税：仅卖出，万5（当前政策）
STAMP_TAX_RATE = 0.0005

# 过户费：十万分之一，双向
TRANSFER_FEE_RATE = 0.00001
FEE_SCHEDULE_VERSION = "cn_a_share_fee_v1"
ETF_FEE_SCHEDULE_VERSION = "cn_exchange_etf_fee_v1"


def is_exchange_etf(symbol: str | None) -> bool:
    """Identify exchange-traded funds from the security code namespace."""
    code = str(symbol or "").strip().upper().split(".", 1)[0].zfill(6)
    return code.startswith(("15", "16", "50", "51", "52", "56", "58"))


def _round_tick(value: float, decimals: int) -> float:
    quantum = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def apply_slippage_buy(price: float, symbol: str | None = None) -> float:
    """买入成交价（含滑点）。"""
    etf = is_exchange_etf(symbol)
    return _round_tick(price * (1.0 + (ETF_SLIPPAGE_RATE if etf else SLIPPAGE_BUY_RATE)), 3 if etf else 2)


def apply_slippage_sell(price: float, symbol: str | None = None) -> float:
    """卖出成交价（含滑点）。"""
    etf = is_exchange_etf(symbol)
    return _round_tick(price * (1.0 - (ETF_SLIPPAGE_RATE if etf else SLIPPAGE_SELL_RATE)), 3 if etf else 2)


def commission(amount: float) -> float:
    """佣金（双向），金额 = 成交价 * 数量。"""
    return max(amount * COMMISSION_RATE, COMMISSION_MIN_CNY)


def stamp_tax(amount: float) -> float:
    """印花税（仅卖出）。"""
    return round(amount * STAMP_TAX_RATE, 2)


def transfer_fee(amount: float) -> float:
    """过户费（双向）。"""
    return round(amount * TRANSFER_FEE_RATE, 2)


def cost_buy(executed_price: float, quantity: int, symbol: str | None = None) -> tuple[float, float, float]:
    """买入单笔：成交金额、佣金、过户费。返回 (金额, 佣金, 过户费)。"""
    amount = executed_price * quantity
    c = commission(amount)
    t = 0.0 if is_exchange_etf(symbol) else transfer_fee(amount)
    return (round(amount, 2), round(c, 2), t)


def sell_cost_breakdown(raw_price: float, quantity: int, symbol: str | None = None) -> dict[str, float | str]:
    """按委托价计算卖出成交价、逐项费用与净回款。"""
    requested_price = max(0.0, float(raw_price or 0))
    qty = max(0, int(quantity or 0))
    etf = is_exchange_etf(symbol)
    executed_price = apply_slippage_sell(requested_price, symbol)
    gross = round(executed_price * qty, 2)
    fee_commission = round(commission(gross), 2) if gross > 0 else 0.0
    fee_transfer = 0.0 if etf else transfer_fee(gross) if gross > 0 else 0.0
    fee_stamp_tax = 0.0 if etf else stamp_tax(gross) if gross > 0 else 0.0
    slippage_bps = (
        round((executed_price / requested_price - 1.0) * 10000.0, 4)
        if requested_price > 0
        else 0.0
    )
    return {
        "requested_price": round(requested_price, 4),
        "executed_price": executed_price,
        "gross_amount": gross,
        "commission": fee_commission,
        "transfer_fee": fee_transfer,
        "stamp_tax": fee_stamp_tax,
        "net_amount": max(0.0, round(gross - fee_commission - fee_transfer - fee_stamp_tax, 2)),
        "slippage_bps": slippage_bps,
        "fee_schedule_version": ETF_FEE_SCHEDULE_VERSION if etf else FEE_SCHEDULE_VERSION,
    }

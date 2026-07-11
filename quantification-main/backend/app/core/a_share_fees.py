"""
真实 A 股交易成本（滑点、佣金、印花税、过户费）。
参考：交易所与券商常见标准，买入仅佣金+过户费，卖出加印花税。
"""
# A 股 1 手 = 100 股
LOT_SIZE = 100

# 买入滑点（真实撮合偏离），按比例估算
SLIPPAGE_BUY_RATE = 0.001   # 0.1%
SLIPPAGE_SELL_RATE = 0.001  # 0.1%

# 佣金：万3，不足 5 元按 5 元
COMMISSION_RATE = 0.0003
COMMISSION_MIN_CNY = 5.0

# 印花税：仅卖出，万5（当前政策）
STAMP_TAX_RATE = 0.0005

# 过户费：十万分之一，双向
TRANSFER_FEE_RATE = 0.00001


def apply_slippage_buy(price: float) -> float:
    """买入成交价（含滑点）。"""
    return round(price * (1.0 + SLIPPAGE_BUY_RATE), 2)


def apply_slippage_sell(price: float) -> float:
    """卖出成交价（含滑点）。"""
    return round(price * (1.0 - SLIPPAGE_SELL_RATE), 2)


def commission(amount: float) -> float:
    """佣金（双向），金额 = 成交价 * 数量。"""
    return max(amount * COMMISSION_RATE, COMMISSION_MIN_CNY)


def stamp_tax(amount: float) -> float:
    """印花税（仅卖出）。"""
    return round(amount * STAMP_TAX_RATE, 2)


def transfer_fee(amount: float) -> float:
    """过户费（双向）。"""
    return round(amount * TRANSFER_FEE_RATE, 2)


def cost_buy(executed_price: float, quantity: int) -> tuple[float, float, float]:
    """买入单笔：成交金额、佣金、过户费。返回 (金额, 佣金, 过户费)。"""
    amount = executed_price * quantity
    c = commission(amount)
    t = transfer_fee(amount)
    return (round(amount, 2), round(c, 2), t)

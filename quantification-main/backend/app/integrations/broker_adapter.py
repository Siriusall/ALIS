from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class OrderRequest:
    symbol: str
    side: str
    price: float
    quantity: int
    name: str = ""
    change_pct: float = 0.0
    volume: float = -1.0
    close: float = 0.0


@dataclass
class OrderResult:
    symbol: str
    side: str
    price: float  # 成交价（含滑点）
    quantity: int
    status: str
    message: str
    commission: float = 0.0
    transfer_fee: float = 0.0
    block_code: str = ""


class BrokerAdapter:
    def place_orders(self, orders: List[OrderRequest]) -> List[OrderResult]:
        raise NotImplementedError("Broker adapter is not implemented yet.")

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from app.core.a_share_fees import LOT_SIZE, apply_slippage_buy, cost_buy
from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity
from app.integrations.broker_adapter import OrderRequest, OrderResult


@dataclass
class PlannedBuyOrder:
    symbol: str
    name: str
    request_price: float
    exec_price: float
    quantity: int
    amount: float
    commission: float
    transfer_fee: float
    total_cost: float
    position_pct: float
    score: float
    status: str
    message: str
    block_code: str


def _total_buy_cost(exec_price: float, quantity: int) -> float:
    amount, comm, trans = cost_buy(exec_price, quantity)
    return round(amount + comm + trans, 2)


def _min_lot_cost(exec_price: float) -> float:
    return _total_buy_cost(exec_price, LOT_SIZE)


def _build_tradable_orders(
    candidates: List[OrderRequest],
) -> tuple[List[OrderRequest], List[OrderResult]]:
    tradable: List[OrderRequest] = []
    blocked: List[OrderResult] = []
    for order in candidates:
        raw_price = max(float(order.price or 0), 0.01)
        tradable_check = check_open_trade_constraints(
            side="BUY",
            symbol=order.symbol,
            name=order.name,
            open_price=raw_price,
            close_price=float(order.close or 0),
            change_pct=float(order.change_pct or 0),
            volume=float(order.volume or 0),
        )
        if not tradable_check.ok:
            blocked.append(
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    price=round(raw_price, 2),
                    quantity=0,
                    status="blocked",
                    message=tradable_check.message,
                    block_code=tradable_check.code,
                )
            )
            continue
        tradable.append(order)
    return tradable, blocked


def _prune_candidates_for_budget(
    candidates: List[OrderRequest],
    *,
    budget: float,
    total_equity: float,
    max_single_position_pct: float,
    existing_position_value: Dict[str, float],
    symbol_scores: Dict[str, float],
) -> tuple[List[OrderRequest], List[str]]:
    """小资金账户：按得分从高到低保留能等权买入 1 手的标的，买不起的单独裁剪。"""
    if not candidates:
        return [], []

    ranked = sorted(
        candidates,
        key=lambda o: float(symbol_scores.get(str(o.symbol), 0) or 0),
        reverse=True,
    )
    kept: List[OrderRequest] = []
    pruned: List[str] = []
    max_single = max(0.0, min(1.0, float(max_single_position_pct or 1.0)))

    def _trial_affordable(trial: List[OrderRequest]) -> bool:
        if not trial:
            return False
        per_symbol_budget = budget / len(trial)
        for order in trial:
            raw_price = max(float(order.price or 0), 0.01)
            exec_price = apply_slippage_buy(raw_price)
            cap_room = max(
                0.0,
                total_equity * max_single - float(existing_position_value.get(order.symbol, 0) or 0),
            )
            alloc = min(per_symbol_budget, cap_room)
            if alloc < _min_lot_cost(exec_price):
                return False
        return True

    for order in ranked:
        trial = kept + [order]
        if _trial_affordable(trial):
            kept = trial
        else:
            pruned.append(str(order.symbol))

    return kept, pruned


def allocate_buy_orders(
    candidates: List[OrderRequest],
    *,
    budget: float,
    total_equity: float,
    max_single_position_pct: float = 1.0,
    existing_position_value: Dict[str, float] | None = None,
    symbol_scores: Dict[str, float] | None = None,
) -> tuple[List[PlannedBuyOrder], List[OrderResult], List[str]]:
    """
    等权 + 单票上限 + 二次分配剩余资金。
    返回：计划买单、被拦截订单、被裁剪标的。
    """
    existing_position_value = dict(existing_position_value or {})
    symbol_scores = dict(symbol_scores or {})
    if budget <= 0 or not candidates:
        return [], [], []

    tradable, blocked = _build_tradable_orders(candidates)
    pruned: List[str] = []
    if tradable:
        tradable, pruned = _prune_candidates_for_budget(
            tradable,
            budget=budget,
            total_equity=total_equity,
            max_single_position_pct=max_single_position_pct,
            existing_position_value=existing_position_value,
            symbol_scores=symbol_scores,
        )

    if not tradable:
        return [], blocked, pruned

    max_single = max(0.0, min(1.0, float(max_single_position_pct or 1.0)))
    n = len(tradable)
    per_equal = budget / n

    qty_map: Dict[str, int] = {}
    exec_price_map: Dict[str, float] = {}
    request_price_map: Dict[str, float] = {}
    name_map: Dict[str, str] = {}
    score_map: Dict[str, float] = {}

    for order in tradable:
        sym = str(order.symbol)
        raw_price = max(float(order.price or 0), 0.01)
        exec_price = apply_slippage_buy(raw_price)
        cap_room = max(0.0, total_equity * max_single - float(existing_position_value.get(sym, 0) or 0))
        alloc = min(per_equal, cap_room)
        qty = normalize_lot_quantity(int(alloc / exec_price))
        qty_map[sym] = qty
        exec_price_map[sym] = exec_price
        request_price_map[sym] = raw_price
        name_map[sym] = str(order.name or "")
        score_map[sym] = float(symbol_scores.get(sym, 0) or 0)

    def _spent() -> float:
        total = 0.0
        for sym, qty in qty_map.items():
            if qty > 0:
                total += _total_buy_cost(exec_price_map[sym], qty)
        return round(total, 2)

    # 二次分配：按得分从高到低，每次尝试加 1 手
    leftover = round(budget - _spent(), 2)
    ranked_symbols = sorted(qty_map.keys(), key=lambda s: score_map.get(s, 0), reverse=True)
    improved = True
    while leftover > 0 and improved:
        improved = False
        for sym in ranked_symbols:
            exec_price = exec_price_map[sym]
            incremental = _total_buy_cost(exec_price, LOT_SIZE)
            if incremental > leftover:
                continue
            current_value = float(existing_position_value.get(sym, 0) or 0) + exec_price_map[sym] * qty_map[sym]
            if current_value + exec_price * LOT_SIZE > total_equity * max_single + 1e-6:
                continue
            qty_map[sym] += LOT_SIZE
            leftover = round(leftover - incremental, 2)
            improved = True
            if leftover <= 0:
                break

    planned: List[PlannedBuyOrder] = []
    for order in tradable:
        sym = str(order.symbol)
        qty = int(qty_map.get(sym, 0) or 0)
        exec_price = exec_price_map[sym]
        if qty <= 0:
            blocked.append(
                OrderResult(
                    symbol=sym,
                    side="BUY",
                    price=round(exec_price, 2),
                    quantity=0,
                    status="blocked",
                    message="资金不足以买入 1 手或超过单票上限",
                    block_code="insufficient_budget_for_lot",
                )
            )
            continue
        amount, comm, trans = cost_buy(exec_price, qty)
        total_cost = round(amount + comm + trans, 2)
        position_pct = round((amount / total_equity) * 100.0, 2) if total_equity > 0 else 0.0
        planned.append(
            PlannedBuyOrder(
                symbol=sym,
                name=name_map.get(sym, ""),
                request_price=round(request_price_map[sym], 4),
                exec_price=round(exec_price, 4),
                quantity=qty,
                amount=round(amount, 2),
                commission=round(comm, 2),
                transfer_fee=round(trans, 2),
                total_cost=total_cost,
                position_pct=position_pct,
                score=score_map.get(sym, 0.0),
                status="simulated",
                message="模拟成交（次日开盘价口径）",
                block_code="",
            )
        )
    return planned, blocked, pruned


def planned_to_order_results(planned: List[PlannedBuyOrder]) -> List[OrderResult]:
    return [
        OrderResult(
            symbol=x.symbol,
            side="BUY",
            price=x.exec_price,
            quantity=x.quantity,
            status=x.status,
            message=x.message,
            commission=x.commission,
            transfer_fee=x.transfer_fee,
            block_code=x.block_code,
        )
        for x in planned
    ]

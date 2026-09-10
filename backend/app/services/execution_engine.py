from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List

from app.core.a_share_fees import apply_slippage_buy, cost_buy
from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity
from app.integrations.broker_adapter import OrderRequest, OrderResult
from app.services.rebalance_allocator import allocate_buy_orders, planned_to_order_results
from app.services.risk_limit_contract import (
    DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    DEFAULT_INDUSTRY_LIMIT,
)


@dataclass
class ExecutionState:
    executed_at: str
    orders: List[OrderResult]


def simulate_open_execution(
    candidates: List[OrderRequest],
    budget: float = 200_000.0,
    executed_at: str | None = None,
    *,
    total_equity: float | None = None,
    max_single_position_pct: float = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    existing_position_value: dict[str, float] | None = None,
    symbol_scores: dict[str, float] | None = None,
    industry_by_symbol: dict[str, str] | None = None,
    existing_industry_value: dict[str, float] | None = None,
    max_industry_position_pct: float = DEFAULT_INDUSTRY_LIMIT,
    target_weights: dict[str, float] | None = None,
    symbol_position_limits: dict[str, float] | None = None,
    max_correlated_industry_pct: float = DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    high_risk_symbols: set[str] | None = None,
    max_order_value_by_symbol: dict[str, float] | None = None,
    initial_entry_fraction: float = 0.5,
    residual_sweep_symbol: str | None = None,
    fixed_quantities: dict[str, int] | None = None,
) -> ExecutionState:
    """
    按真实 A 股规则模拟买入：1手=100股、滑点、佣金、过户费。
    支持单票上限、总权益口径、小资金裁剪与二次分配提升资金利用率。
    """
    equity = float(total_equity if total_equity is not None else budget)
    planned, blocked, _pruned = allocate_buy_orders(
        candidates,
        budget=budget,
        total_equity=equity,
        max_single_position_pct=max_single_position_pct,
        existing_position_value=existing_position_value,
        symbol_scores=symbol_scores,
        industry_by_symbol=industry_by_symbol,
        existing_industry_value=existing_industry_value,
        max_industry_position_pct=max_industry_position_pct,
        target_weights=target_weights,
        symbol_position_limits=symbol_position_limits,
        max_correlated_industry_pct=max_correlated_industry_pct,
        high_risk_symbols=high_risk_symbols,
        max_order_value_by_symbol=max_order_value_by_symbol,
        initial_entry_fraction=initial_entry_fraction,
        residual_sweep_symbol=residual_sweep_symbol,
        fixed_quantities=fixed_quantities,
    )
    orders = planned_to_order_results(planned) + blocked
    return ExecutionState(executed_at=executed_at or datetime.now().isoformat(), orders=orders)


# 保留旧版简单实现供测试对照（内部不再默认使用）
def simulate_open_execution_legacy(
    candidates: List[OrderRequest],
    budget: float = 200_000.0,
    executed_at: str | None = None,
) -> ExecutionState:
    orders: List[OrderResult] = []
    if not candidates:
        return ExecutionState(executed_at=executed_at or datetime.now().isoformat(), orders=orders)

    per_order_budget = budget / len(candidates)
    for order in candidates:
        raw_price = max(order.price, 0.01)
        tradable = check_open_trade_constraints(
            side="BUY",
            symbol=order.symbol,
            name=order.name,
            open_price=raw_price,
            close_price=order.close,
            change_pct=order.change_pct,
            volume=order.volume,
            consecutive_limit_down_days=order.consecutive_limit_down_days,
        )
        if not tradable.ok:
            orders.append(
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    price=round(raw_price, 2),
                    quantity=0,
                    status="blocked",
                    message=tradable.message,
                    block_code=tradable.code,
                )
            )
            continue
        exec_price = apply_slippage_buy(raw_price, order.symbol)
        qty = normalize_lot_quantity(int(per_order_budget / exec_price))
        if qty <= 0:
            orders.append(
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    price=round(exec_price, 2),
                    quantity=0,
                    status="blocked",
                    message="资金不足以买入 1 手",
                    block_code="insufficient_budget_for_lot",
                )
            )
            continue
        amount, comm, trans = cost_buy(exec_price, qty, order.symbol)
        orders.append(
            OrderResult(
                symbol=order.symbol,
                side=order.side,
                price=round(exec_price, 2),
                quantity=qty,
                status="simulated",
                message="模拟成交（真实A股滑点与手续费）",
                commission=comm,
                transfer_fee=trans,
            )
        )

    return ExecutionState(executed_at=executed_at or datetime.now().isoformat(), orders=orders)

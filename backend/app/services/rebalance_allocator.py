from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, List

from app.core.a_share_fees import LOT_SIZE, apply_slippage_buy, cost_buy
from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity
from app.integrations.broker_adapter import OrderRequest, OrderResult
from app.services.five_bucket_contract import build_target_weight_contract
from app.services.risk_limit_contract import (
    DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    DEFAULT_INDUSTRY_LIMIT,
)


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
    target_weight_contract: dict


def _total_buy_cost(exec_price: float, quantity: int, symbol: str | None = None) -> float:
    amount, comm, trans = cost_buy(exec_price, quantity, symbol)
    return round(amount + comm + trans, 2)


def _min_lot_cost(exec_price: float, symbol: str | None = None) -> float:
    return _total_buy_cost(exec_price, LOT_SIZE, symbol)


def correlated_industry_group(industry: str) -> str:
    value = str(industry or "unclassified")
    groups = {
        "financial_property": ("银行", "保险", "证券", "多元金融", "房地产"),
        "technology": ("半导体", "电子", "计算机", "通信", "软件", "互联网"),
        "resources": ("煤炭", "石油", "有色", "钢铁", "化工", "采掘"),
        "consumer": ("食品", "饮料", "家电", "商贸", "零售", "纺织", "服装"),
    }
    return next((group for group, keywords in groups.items() if any(key in value for key in keywords)), value)


def _build_tradable_orders(
    candidates: List[OrderRequest],
) -> tuple[List[OrderRequest], List[OrderResult]]:
    tradable: List[OrderRequest] = []
    blocked: List[OrderResult] = []
    for order in candidates:
        try:
            candidate_price = float(order.price or 0)
        except (TypeError, ValueError):
            candidate_price = 0.0
        if not math.isfinite(candidate_price) or candidate_price <= 0:
            blocked.append(
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    price=0.0,
                    quantity=0,
                    status="blocked",
                    message="无有效开盘价，禁止生成买入订单",
                    block_code="invalid_open_price",
                )
            )
            continue
        raw_price = candidate_price
        tradable_check = check_open_trade_constraints(
            side="BUY",
            symbol=order.symbol,
            name=order.name,
            open_price=raw_price,
            close_price=float(order.close or 0),
            change_pct=float(order.change_pct or 0),
            volume=float(order.volume or 0),
            consecutive_limit_down_days=int(order.consecutive_limit_down_days or 0),
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
    max_single = max(0.0, min(1.0, float(max_single_position_pct or DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)))

    def _trial_affordable(trial: List[OrderRequest]) -> bool:
        if not trial:
            return False
        per_symbol_budget = budget / len(trial)
        for order in trial:
            raw_price = max(float(order.price or 0), 0.01)
            exec_price = apply_slippage_buy(raw_price, order.symbol)
            cap_room = max(
                0.0,
                total_equity * max_single - float(existing_position_value.get(order.symbol, 0) or 0),
            )
            alloc = min(per_symbol_budget, cap_room)
            if alloc < _min_lot_cost(exec_price, order.symbol):
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
    max_single_position_pct: float = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    existing_position_value: Dict[str, float] | None = None,
    symbol_scores: Dict[str, float] | None = None,
    industry_by_symbol: Dict[str, str] | None = None,
    existing_industry_value: Dict[str, float] | None = None,
    max_industry_position_pct: float = DEFAULT_INDUSTRY_LIMIT,
    target_weights: Dict[str, float] | None = None,
    symbol_position_limits: Dict[str, float] | None = None,
    initial_entry_fraction: float = 0.5,
    max_correlated_industry_pct: float = DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    high_risk_symbols: set[str] | None = None,
    max_high_risk_total_pct: float = 0.05,
    max_order_value_by_symbol: Dict[str, float] | None = None,
    residual_sweep_symbol: str | None = None,
    fixed_quantities: Dict[str, int] | None = None,
) -> tuple[List[PlannedBuyOrder], List[OrderResult], List[str]]:
    """
    等权 + 单票上限 + 二次分配剩余资金。
    返回：计划买单、被拦截订单、被裁剪标的。
    """
    existing_position_value = dict(existing_position_value or {})
    symbol_scores = dict(symbol_scores or {})
    industry_by_symbol = dict(industry_by_symbol or {})
    existing_industry_value = dict(existing_industry_value or {})
    target_weights = {str(key): max(0.0, float(value or 0)) for key, value in (target_weights or {}).items()}
    symbol_position_limits = {
        str(key): max(0.0, min(float(value or 0), 1.0))
        for key, value in (symbol_position_limits or {}).items()
    }
    high_risk_symbols = {str(symbol) for symbol in (high_risk_symbols or set())}
    max_order_value_by_symbol = {str(symbol): max(0.0, float(value or 0)) for symbol, value in (max_order_value_by_symbol or {}).items()}
    fixed_quantities = {
        str(symbol): max(0, int(quantity or 0))
        for symbol, quantity in (fixed_quantities or {}).items()
    }
    if budget <= 0 or not candidates:
        return [], [], []

    tradable, blocked = _build_tradable_orders(candidates)
    pruned: List[str] = []
    if tradable and not target_weights and not fixed_quantities:
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

    if fixed_quantities:
        planned: List[PlannedBuyOrder] = []
        remaining = round(max(0.0, float(budget or 0.0)), 2)
        for order in tradable:
            sym = str(order.symbol)
            quantity = int(fixed_quantities.get(sym) or 0)
            raw_price = max(float(order.price or 0), 0.01)
            exec_price = apply_slippage_buy(raw_price, sym)
            block_code = ""
            message = ""
            if quantity <= 0 or quantity % LOT_SIZE != 0:
                block_code, message = "lot_size", "冻结买入数量不是正整数手"
            elif sym in max_order_value_by_symbol and exec_price * quantity > max_order_value_by_symbol[sym] + 1e-6:
                block_code, message = "liquidity_capacity", "冻结买入数量超过执行日流动性容量"
            else:
                amount, comm, trans = cost_buy(exec_price, quantity, sym)
                total_cost = round(amount + comm + trans, 2)
                if total_cost > remaining + 1e-6:
                    block_code, message = "cash_no_overdraft", "执行日现金不足以完成冻结数量"
            if block_code:
                blocked.append(OrderResult(
                    symbol=sym,
                    side="BUY",
                    price=round(exec_price, 4),
                    quantity=0,
                    status="blocked",
                    message=message,
                    block_code=block_code,
                ))
                continue
            remaining = round(remaining - total_cost, 2)
            planned.append(PlannedBuyOrder(
                symbol=sym,
                name=str(order.name or ""),
                request_price=round(raw_price, 4),
                exec_price=round(exec_price, 4),
                quantity=quantity,
                amount=round(amount, 2),
                commission=round(comm, 2),
                transfer_fee=round(trans, 2),
                total_cost=total_cost,
                position_pct=round(amount / total_equity * 100.0, 2) if total_equity > 0 else 0.0,
                score=float(symbol_scores.get(sym, 0) or 0),
                status="simulated",
                message="模拟成交（T日冻结数量，T+1开盘价与费用）",
                block_code="",
                target_weight_contract=build_target_weight_contract(
                    symbol=sym,
                    current_weight=round(float(existing_position_value.get(sym, 0) or 0) / total_equity, 6) if total_equity > 0 else 0.0,
                    target_weight=round((float(existing_position_value.get(sym, 0) or 0) + amount) / total_equity, 6) if total_equity > 0 else 0.0,
                    score=float(symbol_scores.get(sym, 0) or 0),
                    evidence={
                        "return_source": "T日冻结整手订单",
                        "holding_period": "按冻结Controller合同执行",
                        "exit_conditions": ["后续冻结计划改变目标", "T+1安全否决"],
                    },
                    data_status="ready",
                ),
            ))
        return planned, blocked, pruned

    max_single = max(0.0, min(1.0, float(max_single_position_pct or DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)))
    max_industry = max(0.0, min(1.0, float(max_industry_position_pct or DEFAULT_INDUSTRY_LIMIT)))
    max_correlated = max(0.0, min(1.0, float(max_correlated_industry_pct or DEFAULT_CORRELATED_INDUSTRY_LIMIT)))
    high_risk_limit = max(0.0, min(1.0, float(max_high_risk_total_pct)))
    existing_high_risk_value = sum(float(existing_position_value.get(symbol, 0) or 0) for symbol in high_risk_symbols)
    n = len(tradable)
    per_equal = budget / n
    weight_total = sum(target_weights.get(str(order.symbol), 0.0) for order in tradable)
    initial_fraction = max(0.0, min(1.0, float(initial_entry_fraction)))
    target_gap_by_symbol: Dict[str, float] = {}
    if weight_total > 0:
        target_weight_scale = 1.0 if weight_total <= 1.000001 else 1.0 / weight_total
        for order in tradable:
            sym = str(order.symbol)
            normalized_target = target_weights.get(sym, 0.0) * target_weight_scale
            target_gap_by_symbol[sym] = max(
                0.0,
                total_equity * normalized_target - float(existing_position_value.get(sym, 0) or 0),
            )
    target_gap_total = sum(target_gap_by_symbol.values())
    target_gap_scale = min(1.0, budget / target_gap_total) if target_gap_total > 0 else 0.0

    qty_map: Dict[str, int] = {}
    exec_price_map: Dict[str, float] = {}
    request_price_map: Dict[str, float] = {}
    name_map: Dict[str, str] = {}
    score_map: Dict[str, float] = {}
    industry_map: Dict[str, str] = {}
    industry_candidate_count: Dict[str, int] = {}
    group_candidate_count: Dict[str, int] = {}
    planned_symbol_caps: Dict[str, float] = {}
    existing_group_value: Dict[str, float] = {}
    for industry, value in existing_industry_value.items():
        group = correlated_industry_group(industry)
        existing_group_value[group] = existing_group_value.get(group, 0.0) + float(value or 0)
    for order in tradable:
        industry = str(industry_by_symbol.get(str(order.symbol)) or "unclassified")
        industry_candidate_count[industry] = industry_candidate_count.get(industry, 0) + 1
        group = correlated_industry_group(industry)
        group_candidate_count[group] = group_candidate_count.get(group, 0) + 1

    for order in tradable:
        sym = str(order.symbol)
        raw_price = max(float(order.price or 0), 0.01)
        exec_price = apply_slippage_buy(raw_price, sym)
        symbol_limit = symbol_position_limits.get(sym, max_single)
        if sym in high_risk_symbols:
            symbol_limit = min(symbol_limit, .01)
        cap_room = max(0.0, total_equity * symbol_limit - float(existing_position_value.get(sym, 0) or 0))
        industry = str(industry_by_symbol.get(sym) or "unclassified")
        industry_room = max(0.0, total_equity * max_industry - float(existing_industry_value.get(industry, 0) or 0))
        group = correlated_industry_group(industry)
        group_room = max(0.0, total_equity * max_correlated - float(existing_group_value.get(group, 0) or 0))
        high_risk_room = max(0.0, total_equity * high_risk_limit - existing_high_risk_value) if sym in high_risk_symbols else float("inf")
        existing_value = float(existing_position_value.get(sym, 0) or 0)
        if weight_total > 0:
            desired = target_gap_by_symbol.get(sym, 0.0) * target_gap_scale
        else:
            desired = per_equal
        if existing_value <= 0:
            desired *= initial_fraction
        if sym in max_order_value_by_symbol:
            desired = min(desired, max_order_value_by_symbol[sym])
        planned_symbol_caps[sym] = min(total_equity * symbol_limit, existing_value + desired)
        if sym == str(residual_sweep_symbol or ""):
            planned_symbol_caps[sym] = total_equity * symbol_limit
        high_risk_count = max(1, sum(1 for order in tradable if str(order.symbol) in high_risk_symbols))
        alloc = min(desired, cap_room, industry_room / max(1, industry_candidate_count.get(industry, 1)), group_room / max(1, group_candidate_count.get(group, 1)), high_risk_room / high_risk_count)
        qty = normalize_lot_quantity(int(alloc / exec_price))
        qty_map[sym] = qty
        exec_price_map[sym] = exec_price
        request_price_map[sym] = raw_price
        name_map[sym] = str(order.name or "")
        score_map[sym] = float(symbol_scores.get(sym, 0) or 0)
        industry_map[sym] = industry

    def _spent() -> float:
        total = 0.0
        for sym, qty in qty_map.items():
            if qty > 0:
                total += _total_buy_cost(exec_price_map[sym], qty, sym)
        return round(total, 2)

    # 二次分配：按得分从高到低，每次尝试加 1 手
    leftover = round(budget - _spent(), 2)
    ranked_symbols = sorted(qty_map.keys(), key=lambda s: score_map.get(s, 0), reverse=True)
    planned_industry_value: Dict[str, float] = dict(existing_industry_value)
    planned_group_value: Dict[str, float] = dict(existing_group_value)
    planned_high_risk_value = existing_high_risk_value
    for sym, qty in qty_map.items():
        industry = industry_map.get(sym, "unclassified")
        planned_industry_value[industry] = planned_industry_value.get(industry, 0.0) + exec_price_map[sym] * qty
        group = correlated_industry_group(industry)
        planned_group_value[group] = planned_group_value.get(group, 0.0) + exec_price_map[sym] * qty
        if sym in high_risk_symbols:
            planned_high_risk_value += exec_price_map[sym] * qty
    improved = True
    while leftover > 0 and improved:
        improved = False
        for sym in ranked_symbols:
            exec_price = exec_price_map[sym]
            incremental = _total_buy_cost(exec_price, LOT_SIZE, sym)
            if incremental > leftover:
                continue
            current_value = float(existing_position_value.get(sym, 0) or 0) + exec_price_map[sym] * qty_map[sym]
            symbol_limit = symbol_position_limits.get(sym, max_single)
            if sym in high_risk_symbols:
                symbol_limit = min(symbol_limit, .01)
            if current_value + exec_price * LOT_SIZE > total_equity * symbol_limit + 1e-6:
                continue
            if current_value + exec_price * LOT_SIZE > planned_symbol_caps.get(sym, total_equity * symbol_limit) + 1e-6:
                continue
            industry = industry_map.get(sym, "unclassified")
            if planned_industry_value.get(industry, 0.0) + exec_price * LOT_SIZE > total_equity * max_industry + 1e-6:
                continue
            group = correlated_industry_group(industry)
            if planned_group_value.get(group, 0.0) + exec_price * LOT_SIZE > total_equity * max_correlated + 1e-6:
                continue
            if sym in high_risk_symbols and planned_high_risk_value + exec_price * LOT_SIZE > total_equity * high_risk_limit + 1e-6:
                continue
            qty_map[sym] += LOT_SIZE
            planned_industry_value[industry] = planned_industry_value.get(industry, 0.0) + exec_price * LOT_SIZE
            planned_group_value[group] = planned_group_value.get(group, 0.0) + exec_price * LOT_SIZE
            if sym in high_risk_symbols:
                planned_high_risk_value += exec_price * LOT_SIZE
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
            industry = industry_map.get(sym, "unclassified")
            industry_room = total_equity * max_industry - float(existing_industry_value.get(industry, 0) or 0)
            industry_blocked = industry_room < _min_lot_cost(exec_price, sym)
            blocked.append(
                OrderResult(
                    symbol=sym,
                    side="BUY",
                    price=round(exec_price, 2),
                    quantity=0,
                    status="blocked",
                    message="行业仓位已达上限" if industry_blocked else "资金不足以买入 1 手或超过单票上限",
                    block_code="industry_position_limit" if industry_blocked else "insufficient_budget_for_lot",
                )
            )
            continue
        amount, comm, trans = cost_buy(exec_price, qty, sym)
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
                target_weight_contract=build_target_weight_contract(
                    symbol=sym,
                    current_weight=round(float(existing_position_value.get(sym, 0) or 0) / total_equity, 6) if total_equity > 0 else 0.0,
                    target_weight=round((float(existing_position_value.get(sym, 0) or 0) + amount) / total_equity, 6) if total_equity > 0 else 0.0,
                    score=score_map.get(sym, 0.0),
                    evidence={
                        "return_source": "调仓目标权重分配",
                        "holding_period": "按功能仓合同执行",
                        "exit_conditions": ["目标权重被新一轮组合优化降至0", "风险硬门触发", "交易成本后预期收益转负"],
                    },
                    data_status="ready",
                ),
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
            target_weight_contract=x.target_weight_contract,
        )
        for x in planned
    ]

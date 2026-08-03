from __future__ import annotations

import hashlib
import json
import math
from statistics import mean
from typing import Any

from app.core.a_share_fees import (
    LOT_SIZE,
    apply_slippage_buy,
    cost_buy,
    sell_cost_breakdown,
)
from app.services.quant_risk import hrp_fallback_weights
from app.services.risk_limit_contract import DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalise_to_budget(weights: dict[str, float], budget: float) -> dict[str, float]:
    positive = {str(key): max(0.0, _safe(value)) for key, value in weights.items() if _safe(value) > 0}
    total = sum(positive.values())
    if total <= 0 or budget <= 0:
        return {}
    return {key: value / total * budget for key, value in positive.items()}


def _apply_weight_bounds(
    weights: dict[str, float],
    *,
    symbols: list[str],
    bounds: dict[str, dict[str, Any]],
    budget: float,
) -> dict[str, float]:
    result = {
        symbol: max(
            _safe((bounds.get(symbol) or {}).get("min_weight")),
            min(
                _safe((bounds.get(symbol) or {}).get("max_weight")),
                max(0.0, _safe(weights.get(symbol))),
            ),
        )
        for symbol in symbols
    }
    for _ in range(max(4, len(symbols) * 2)):
        gap = budget - sum(result.values())
        if abs(gap) <= 1e-10:
            break
        if gap > 0:
            room = {
                symbol: max(0.0, _safe((bounds.get(symbol) or {}).get("max_weight")) - result[symbol])
                for symbol in symbols
            }
        else:
            room = {
                symbol: max(0.0, result[symbol] - _safe((bounds.get(symbol) or {}).get("min_weight")))
                for symbol in symbols
            }
        room_total = sum(room.values())
        if room_total <= 1e-12:
            break
        amount = min(abs(gap), room_total)
        direction = 1.0 if gap > 0 else -1.0
        for symbol in symbols:
            result[symbol] += direction * amount * room[symbol] / room_total
    return {symbol: value for symbol, value in result.items() if value > 1e-12}


def _planned_cost(
    *,
    symbol: str,
    side: str,
    quantity: int,
    reference_price: float,
    execution_cost_contract: dict[str, Any],
) -> dict[str, float]:
    qty = max(0, int(quantity or 0))
    price = max(0.0, _safe(reference_price))
    if qty <= 0 or price <= 0:
        return {
            "planned_execution_price": price,
            "gross_amount": 0.0,
            "commission": 0.0,
            "transfer_fee": 0.0,
            "stamp_tax": 0.0,
            "slippage_cost": 0.0,
            "calibration_buffer": 0.0,
            "total_cost": 0.0,
        }
    side = str(side or "").upper()
    if side == "SELL":
        breakdown = sell_cost_breakdown(price, qty, symbol)
        execution_price = _safe(breakdown.get("executed_price"))
        gross = _safe(breakdown.get("gross_amount"))
        commission = _safe(breakdown.get("commission"))
        transfer = _safe(breakdown.get("transfer_fee"))
        stamp = _safe(breakdown.get("stamp_tax"))
        slippage = max(0.0, (price - execution_price) * qty)
        rate = max(0.0, _safe(execution_cost_contract.get("sell_rate")))
    else:
        execution_price = apply_slippage_buy(price, symbol)
        gross, commission, transfer = cost_buy(execution_price, qty, symbol)
        stamp = 0.0
        slippage = max(0.0, (execution_price - price) * qty)
        rate = max(0.0, _safe(execution_cost_contract.get("buy_rate")))
    explicit = commission + transfer + stamp + slippage
    conservative = max(explicit, price * qty * rate)
    return {
        "planned_execution_price": round(execution_price, 4),
        "gross_amount": round(gross, 2),
        "commission": round(commission, 2),
        "transfer_fee": round(transfer, 2),
        "stamp_tax": round(stamp, 2),
        "slippage_cost": round(slippage, 2),
        "calibration_buffer": round(max(0.0, conservative - explicit), 2),
        "total_cost": round(conservative, 2),
    }


def _project_to_executable_lots(
    desired_weights: dict[str, float],
    *,
    current_weights: dict[str, float],
    current_quantities: dict[str, int],
    reference_prices: dict[str, float],
    account_equity: float,
    bounds: dict[str, dict[str, Any]],
    execution_cost_contract: dict[str, Any],
) -> dict[str, Any]:
    equity = max(0.0, _safe(account_equity))
    symbols = sorted(set(desired_weights) | set(current_weights) | set(bounds))
    projected = {
        symbol: max(0.0, _safe(current_weights.get(symbol)))
        for symbol in symbols
        if _safe(current_weights.get(symbol)) > 1e-12
    }
    orders: list[dict[str, Any]] = []
    residuals: dict[str, float] = {}
    blocked: list[str] = []
    cash_available = max(0.0, equity * (1.0 - sum(projected.values())))

    def _append_order(symbol: str, side: str, quantity: int, desired: float) -> None:
        current = max(0.0, _safe(current_weights.get(symbol)))
        price = max(0.0, _safe(reference_prices.get(symbol)))
        bound = bounds.get(symbol) if isinstance(bounds.get(symbol), dict) else {}
        obligation = bound.get("obligation") if isinstance(bound.get("obligation"), dict) else {}
        held_quantity = max(0, int(current_quantities.get(symbol) or 0))
        signed_weight = quantity * price / equity
        actual = current + signed_weight if side == "BUY" else max(0.0, current - signed_weight)
        if str(obligation.get("type") or "") == "mandatory_exit" and quantity >= held_quantity:
                actual = 0.0
        cost = _planned_cost(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=price,
            execution_cost_contract=execution_cost_contract,
        )
        if actual > 1e-12:
            projected[symbol] = round(actual, 8)
        else:
            projected.pop(symbol, None)
        orders.append({
            "symbol": symbol,
            "side": side,
            "planned_quantity": quantity,
            "planned_lots": round(quantity / LOT_SIZE, 4),
            "whole_lot_quantity": quantity % LOT_SIZE == 0,
            "full_position_exit": side == "SELL" and quantity == held_quantity,
            "reference_close": round(price, 4),
            "estimated_execution_amount": round(_safe(cost.get("gross_amount")), 2),
            "post_lot_target_weight": round(actual, 8),
            "lot_rounding_residual": round(desired - actual, 8),
            "intent": str(bound.get("intent") or ""),
            "permission": bound.get("permission") or {},
            "obligation": obligation,
            "residual_breach": {
                "active": False,
                "remaining_excess_weight": 0.0,
                "remaining_excess_amount": 0.0,
                "reason_code": "",
            },
            "cost": cost,
        })

    # Sells are frozen first so their conservative net proceeds can fund buys.
    for symbol in symbols:
        current = max(0.0, _safe(current_weights.get(symbol)))
        desired = max(0.0, _safe(desired_weights.get(symbol)))
        if desired >= current - 1e-12:
            continue
        price = max(0.0, _safe(reference_prices.get(symbol)))
        held_quantity = max(0, int(current_quantities.get(symbol) or 0))
        obligation = ((bounds.get(symbol) or {}).get("obligation") or {})
        obligation_type = str(obligation.get("type") or "none")
        if equity <= 0 or price <= 0 or held_quantity <= 0:
            blocked.append(f"reference_price_equity_or_position:{symbol}")
            continue
        excess_value = (current - desired) * equity
        if obligation_type == "mandatory_exit":
            quantity = held_quantity
        elif obligation_type == "mandatory_reduce":
            quantity = int(math.ceil(excess_value / (price * LOT_SIZE) - 1e-12)) * LOT_SIZE
        else:
            quantity = int(excess_value // (price * LOT_SIZE)) * LOT_SIZE
        quantity = min(held_quantity, quantity)
        if quantity <= 0:
            if obligation.get("required"):
                blocked.append(f"mandatory_obligation_not_executable:{symbol}")
            continue
        _append_order(symbol, "SELL", quantity, desired)
        sell_cost = orders[-1]["cost"]
        cash_available += max(
            0.0,
            _safe(sell_cost.get("gross_amount"))
            - _safe(sell_cost.get("commission"))
            - _safe(sell_cost.get("transfer_fee"))
            - _safe(sell_cost.get("stamp_tax"))
            - _safe(sell_cost.get("calibration_buffer")),
        )

    # Buys use the remaining cash exactly once; rejected quantities are never
    # redistributed to another method or symbol after this projection.
    for symbol in symbols:
        current = max(0.0, _safe(current_weights.get(symbol)))
        desired = max(0.0, _safe(desired_weights.get(symbol)))
        if desired <= current + 1e-12:
            continue
        price = max(0.0, _safe(reference_prices.get(symbol)))
        if equity <= 0 or price <= 0:
            blocked.append(f"reference_price_or_equity:{symbol}")
            continue
        execution_price = apply_slippage_buy(price, symbol)
        quantity = int(((desired - current) * equity) // (execution_price * LOT_SIZE)) * LOT_SIZE
        while quantity > 0:
            cost = _planned_cost(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                reference_price=price,
                execution_cost_contract=execution_cost_contract,
            )
            cash_required = (
                _safe(cost.get("gross_amount"))
                + _safe(cost.get("commission"))
                + _safe(cost.get("transfer_fee"))
                + _safe(cost.get("calibration_buffer"))
            )
            if cash_required <= cash_available + 1e-6:
                break
            quantity -= LOT_SIZE
        if quantity <= 0:
            continue
        _append_order(symbol, "BUY", quantity, desired)
        buy_cost = orders[-1]["cost"]
        cash_available -= (
            _safe(buy_cost.get("gross_amount"))
            + _safe(buy_cost.get("commission"))
            + _safe(buy_cost.get("transfer_fee"))
            + _safe(buy_cost.get("calibration_buffer"))
        )

    for symbol in symbols:
        residuals[symbol] = round(
            max(0.0, _safe(desired_weights.get(symbol))) - max(0.0, _safe(projected.get(symbol))),
            8,
        )
    residual_breaches: dict[str, dict[str, Any]] = {}
    for symbol, bound in bounds.items():
        obligation = bound.get("obligation") if isinstance(bound.get("obligation"), dict) else {}
        if str(obligation.get("type") or "") != "mandatory_reduce" or not obligation.get("required"):
            continue
        target_max = max(0.0, _safe(obligation.get("target_max_weight")))
        projected_weight = max(0.0, _safe(projected.get(symbol)))
        if projected_weight <= target_max + 1e-8:
            continue
        order = next((item for item in orders if str(item.get("symbol") or "") == symbol and item.get("side") == "SELL"), None)
        residual = {
            "active": True,
            "breach_type": "single_position_limit",
            "required_max_weight": round(target_max, 8),
            "projected_post_weight": round(projected_weight, 8),
            "remaining_excess_weight": round(projected_weight - target_max, 8),
            "remaining_excess_amount": round((projected_weight - target_max) * equity, 2),
            "reason_code": "mandatory_obligation_not_executable" if not order or int(order.get("planned_quantity") or 0) < LOT_SIZE else "lot_or_liquidity_residual",
            "planned_repair_quantity": int((order or {}).get("planned_quantity") or 0),
            "requires_next_day_review": True,
        }
        residual_breaches[symbol] = residual
        if order:
            order["residual_breach"] = residual
    return {
        "weights": projected,
        "orders": orders,
        "residuals": residuals,
        "residual_breaches": residual_breaches,
        "blocked": blocked,
        "projected_cash_cny": round(max(0.0, cash_available), 2),
    }


def _portfolio_returns(weights: dict[str, float], returns: dict[str, list[float]]) -> list[float]:
    available = [symbol for symbol in weights if returns.get(symbol)]
    common = min((len(returns[symbol]) for symbol in available), default=0)
    if common < 2:
        return []
    return [
        sum(_safe(weights.get(symbol)) * _safe(returns[symbol][-common + index]) for symbol in available)
        for index in range(common)
    ]


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return drawdown


def _industry_budget_penalty(
    weights: dict[str, float],
    *,
    symbol_industries: dict[str, str],
    industry_targets: dict[str, float],
    penalty_rate: float,
) -> tuple[float, dict[str, float]]:
    actual: dict[str, float] = {}
    for symbol, weight in weights.items():
        industry = str(symbol_industries.get(symbol) or "未分类")
        actual[industry] = actual.get(industry, 0.0) + max(0.0, _safe(weight))
    deviation = sum(
        abs(actual.get(industry, 0.0) - max(0.0, _safe(industry_targets.get(industry))))
        for industry in set(actual) | set(industry_targets)
    )
    return max(0.0, float(penalty_rate)) * deviation, {
        key: round(value, 8) for key, value in actual.items()
    }


def _evaluate(
    *,
    method: str,
    weights: dict[str, float],
    returns: dict[str, list[float]],
    expected_returns: dict[str, float],
    current_weights: dict[str, float],
    execution_projection: dict[str, Any],
    account_equity: float,
    equity_budget: float,
    weight_bounds: dict[str, dict[str, Any]],
    symbol_industries: dict[str, str],
    industry_targets: dict[str, float],
    industry_penalty_rate: float,
) -> dict[str, Any]:
    weights = dict(execution_projection.get("weights") or {})
    series = _portfolio_returns(weights, returns)
    ordered = sorted(series)
    tail_n = max(1, math.ceil(len(ordered) * 0.05)) if ordered else 1
    cvar_daily = max(0.0, -mean(ordered[:tail_n])) if ordered else 1.0
    expected_20d = sum(_safe(weights.get(symbol)) * _safe(expected_returns.get(symbol)) for symbol in weights)
    turnover = sum(
        abs(_safe(weights.get(symbol)) - _safe(current_weights.get(symbol)))
        for symbol in set(weights) | set(current_weights)
    )
    orders = [item for item in (execution_projection.get("orders") or []) if isinstance(item, dict)]
    cost_amount = sum(_safe((item.get("cost") or {}).get("total_cost")) for item in orders)
    fee_amount = sum(
        _safe((item.get("cost") or {}).get("commission"))
        + _safe((item.get("cost") or {}).get("transfer_fee"))
        + _safe((item.get("cost") or {}).get("stamp_tax"))
        for item in orders
    )
    slippage_amount = sum(_safe((item.get("cost") or {}).get("slippage_cost")) for item in orders)
    cost = cost_amount / max(1.0, _safe(account_equity))
    volatility_daily = (
        math.sqrt(sum((value - mean(series)) ** 2 for value in series) / len(series))
        if series else 1.0
    )
    cvar_20d = cvar_daily * math.sqrt(20.0)
    industry_penalty, industry_weights = _industry_budget_penalty(
        weights,
        symbol_industries=symbol_industries,
        industry_targets=industry_targets,
        penalty_rate=industry_penalty_rate,
    )
    objective = (
        expected_20d
        - 1.25 * cvar_20d
        - cost
        - 0.10 * volatility_daily * math.sqrt(20.0)
        - industry_penalty
    )
    breaches: list[str] = []
    if any(value < -1e-12 for value in weights.values()):
        breaches.append("negative_weight")
    if sum(weights.values()) > equity_budget + 1e-6:
        breaches.append("equity_budget")
    for symbol in weight_bounds:
        value = _safe(weights.get(symbol))
        lower = _safe((weight_bounds.get(symbol) or {}).get("min_weight"))
        upper = _safe((weight_bounds.get(symbol) or {}).get("max_weight"))
        residual = (execution_projection.get("residual_breaches") or {}).get(symbol) or {}
        residual_allowed = bool(residual.get("active")) and any(
            str(item.get("symbol") or "") == symbol
            and str(item.get("side") or "").upper() == "SELL"
            and int(item.get("planned_quantity") or 0) >= LOT_SIZE
            for item in orders
        )
        if (value < lower - 1e-8 or value > upper + 1e-8) and not (value > upper + 1e-8 and residual_allowed):
            breaches.append(f"controller_bound:{symbol}")
        obligation = (
            (weight_bounds.get(symbol) or {}).get("obligation")
            if isinstance((weight_bounds.get(symbol) or {}).get("obligation"), dict) else {}
        )
        if obligation.get("required"):
            obligation_type = str(obligation.get("type") or "")
            current = _safe(current_weights.get(symbol))
            has_sell = any(
                str(item.get("symbol") or "") == symbol
                and str(item.get("side") or "") == "SELL"
                and int(item.get("planned_quantity") or 0) > 0
                for item in orders
            )
            if obligation_type == "mandatory_exit" and value > 1e-8:
                breaches.append(f"mandatory_exit:{symbol}")
            if obligation_type == "mandatory_reduce" and (
                value >= current - 1e-8 or (value > upper + 1e-8 and not residual_allowed) or not has_sell
            ):
                breaches.append(f"mandatory_reduce:{symbol}")
    breaches.extend(str(item) for item in execution_projection.get("blocked") or [])
    if len(series) < 20:
        breaches.append("return_history_lt_20")
    return {
        "method": method,
        "eligible": not breaches,
        "breaches": breaches,
        "weights": {key: round(value, 8) for key, value in weights.items()},
        "expected_20d_return": round(expected_20d, 8),
        "cvar_20d": round(cvar_20d, 8),
        "volatility_20d": round(volatility_daily * math.sqrt(20.0), 8),
        "max_drawdown_history": round(_max_drawdown(series), 8),
        "turnover": round(turnover, 8),
        "estimated_one_way_cost": round(cost, 8),
        "estimated_total_cost_cny": round(cost_amount, 2),
        "estimated_fees_cny": round(fee_amount, 2),
        "estimated_slippage_cny": round(slippage_amount, 2),
        "execution_orders": orders,
        "executable_order_count": len(orders),
        "lot_rounding_residuals": execution_projection.get("residuals") or {},
        "residual_breaches": execution_projection.get("residual_breaches") or {},
        "industry_weights": industry_weights,
        "industry_budget_penalty": round(industry_penalty, 8),
        "objective": round(objective, 8),
        "sample_count": len(series),
    }


def compete_portfolio_methods(
    *,
    returns_by_symbol: dict[str, list[float]],
    expected_returns: dict[str, float],
    current_weights: dict[str, float],
    trading_costs: dict[str, float],
    current_quantities: dict[str, int] | None = None,
    reference_prices: dict[str, float] | None = None,
    account_equity: float = 0.0,
    execution_cost_contract: dict[str, Any] | None = None,
    risk_limit_contract: dict[str, Any] | None = None,
    equity_budget: float,
    cvar_weights: dict[str, float],
    weight_bounds: dict[str, dict[str, Any]] | None = None,
    symbol_industries: dict[str, str] | None = None,
    industry_targets: dict[str, float] | None = None,
    industry_penalty_rate: float = 0.01,
) -> dict[str, Any]:
    """Choose among four lot-executable portfolios under one frozen contract."""
    symbols = sorted(
        set(symbol for symbol, values in returns_by_symbol.items() if values)
        | set(current_weights)
        | set((weight_bounds or {}).keys())
    )
    bounds_enabled = weight_bounds is not None
    raw_bounds = dict(weight_bounds or {})
    frozen_symbol_industries = {
        str(symbol): str(industry or "未分类")
        for symbol, industry in (symbol_industries or {}).items()
    }
    frozen_industry_targets = {
        str(industry): max(0.0, _safe(weight))
        for industry, weight in (industry_targets or {}).items()
    }
    bounds = {
        symbol: {
            "min_weight": max(0.0, _safe((raw_bounds.get(symbol) or {}).get("min_weight"))),
            "max_weight": max(0.0, _safe((raw_bounds.get(symbol) or {}).get("max_weight", 1.0))),
            "action": str((raw_bounds.get(symbol) or {}).get("action") or ""),
            "permission": (raw_bounds.get(symbol) or {}).get("permission") or {},
            "intent": str((raw_bounds.get(symbol) or {}).get("intent") or ""),
            "obligation": (raw_bounds.get(symbol) or {}).get("obligation") or {
                "type": "none", "required": False, "target_max_weight": None, "reason_codes": [],
            },
        }
        for symbol in symbols
    }
    minimum_budget = sum(item["min_weight"] for item in bounds.values())
    maximum_budget = sum(item["max_weight"] for item in bounds.values())
    requested_budget = max(0.0, min(1.0, _safe(equity_budget)))
    budget = max(minimum_budget, min(maximum_budget, requested_budget)) if bounds_enabled else requested_budget
    equal = {symbol: 1.0 / len(symbols) for symbol in symbols} if symbols else {}
    hrp = hrp_fallback_weights({symbol: returns_by_symbol[symbol] for symbol in symbols}, max_single=DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
    raw_candidates = {
        "empirical_cvar": _normalise_to_budget(cvar_weights, budget),
        "equal_weight": _normalise_to_budget(equal, budget),
        "hrp": _normalise_to_budget(hrp, budget),
        "minimum_turnover_feasible": {
            str(symbol): max(0.0, _safe(value))
            for symbol, value in current_weights.items()
            if _safe(value) > 0
        },
    }
    if bounds_enabled:
        candidates = {
            method: (
                _apply_weight_bounds(weights, symbols=symbols, bounds=bounds, budget=budget)
            )
            for method, weights in raw_candidates.items()
        }
    else:
        candidates = raw_candidates
    cost_contract = dict(execution_cost_contract or {})
    if not cost_contract:
        fallback = max(0.0003, mean([_safe(value) for value in trading_costs.values()]) if trading_costs else 0.0015)
        cost_contract = {
            "buy_rate": fallback,
            "sell_rate": fallback,
            "round_trip_rate": fallback * 2.0,
            "source": "legacy_symbol_cost_fallback",
            "includes_fees_and_slippage": True,
        }
    constraint_payload = {
        "budget": round(budget, 8),
        "bounds": bounds,
        "industry_targets": frozen_industry_targets,
        "industry_penalty_rate": round(max(0.0, float(industry_penalty_rate)), 8),
        "execution_cost_contract": cost_contract,
        "risk_limit_contract": risk_limit_contract or {},
        "lot_size": LOT_SIZE,
    }
    constraint_hash = hashlib.sha256(
        json.dumps(constraint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    projections = {
        method: _project_to_executable_lots(
            weights,
            current_weights=current_weights,
            current_quantities={str(k): int(v or 0) for k, v in (current_quantities or {}).items()},
            reference_prices={str(k): _safe(v) for k, v in (reference_prices or {}).items()},
            account_equity=account_equity,
            bounds=bounds,
            execution_cost_contract=cost_contract,
        )
        for method, weights in candidates.items()
    }
    evaluations = [
        _evaluate(
            method=method,
            weights=weights,
            returns=returns_by_symbol,
            expected_returns=expected_returns,
            current_weights=current_weights,
            execution_projection=projections[method],
            account_equity=account_equity,
            equity_budget=budget,
            weight_bounds=bounds if bounds_enabled else {},
            symbol_industries=frozen_symbol_industries,
            industry_targets=frozen_industry_targets,
            industry_penalty_rate=industry_penalty_rate,
        ) | {"constraint_hash": constraint_hash}
        for method, weights in candidates.items()
    ]
    eligible = [item for item in evaluations if item["eligible"]]
    hold = next((item for item in evaluations if item["method"] == "minimum_turnover_feasible"), None)
    winner = max(eligible, key=lambda item: (item["objective"], -item["turnover"], item["method"])) if eligible else hold
    if winner and hold and hold["eligible"] and winner["method"] != "minimum_turnover_feasible":
        required_edge = max(0.001, float(winner["estimated_one_way_cost"]))
        if float(winner["objective"]) - float(hold["objective"]) <= required_edge:
            winner = hold
    return {
        "ok": bool(winner),
        "schema_version": "portfolio_method_competition_v3",
        "winner": winner,
        "selected_method": str((winner or {}).get("method") or ""),
        "weights": dict((winner or {}).get("weights") or {}),
        "evaluations": evaluations,
        "weight_bounds": bounds,
        "constraint_hash": constraint_hash,
        "execution_orders": list((winner or {}).get("execution_orders") or []),
        "execution_cost_contract": cost_contract,
        "risk_limit_contract": risk_limit_contract or {},
        "residual_breaches": dict((winner or {}).get("residual_breaches") or {}),
        "industry_budget": {
            "schema_version": "alis_portfolio_industry_budget_v1",
            "symbol_industries": frozen_symbol_industries,
            "industry_targets": {
                key: round(value, 8) for key, value in frozen_industry_targets.items()
            },
            "penalty_rate": round(max(0.0, float(industry_penalty_rate)), 8),
            "application": "soft_deviation_penalty_applied_to_every_competing_method",
        },
        "target_budget": round(budget, 8),
        "policy": {
            "methods": ["empirical_cvar", "equal_weight", "hrp", "minimum_turnover_feasible"],
            "risk_penalty": "1.25 * CVaR20",
            "cost_gate": "winner objective edge must exceed max(0.10%, estimated cost)",
            "constraint_policy": "all methods share the same budget, Controller bounds, obligations, lot size and cost contract",
            "cost_policy": "commission, transfer fee, stamp tax, slippage and conservative calibration buffer are charged after lot projection",
            "industry_budget": "every method pays the same frozen industry-budget deviation penalty",
            "simulation_only": True,
        },
    }

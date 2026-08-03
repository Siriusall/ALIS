"""Pure portfolio-preparation contracts shared by quant execution paths."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from app.services.risk_limit_contract import DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT


def compute_buy_budget(
    *,
    total_equity: float,
    position_market_value: float,
    cash_balance: float,
    target_position_pct: float,
    budget_ratio: float = 1.0,
) -> float:
    target_invested = float(total_equity) * max(0.0, min(1.0, float(target_position_pct)))
    need_to_buy = max(0.0, target_invested - float(position_market_value))
    ratio = max(0.0, min(1.0, float(budget_ratio)))
    return max(0.0, round(min(float(cash_balance), need_to_buy) * ratio, 2))


def monthly_turnover_budget(
    execute_date: str,
    total_equity: float,
    operations: List[Dict[str, Any]],
) -> Dict[str, float]:
    month = str(execute_date)[:7]
    used = sum(
        float(item.get("price") or 0) * int(item.get("quantity") or 0)
        for item in operations
        if str(item.get("at") or "")[:7] == month
    )
    limit = max(0.0, float(total_equity)) * 0.30
    return {
        "limit": round(limit, 2),
        "used_before": round(used, 2),
        "remaining": round(max(0.0, limit - used), 2),
    }


def pretrade_symbol_limits(
    symbols: List[str],
    *,
    max_single: float,
    theses: List[Dict[str, Any]],
    is_etf: Callable[[str], bool],
) -> Dict[str, float]:
    thesis_by_symbol = {
        str(item.get("ts_code") or "").split(".")[0]: item
        for item in theses
    }
    limits: Dict[str, float] = {}
    for symbol in symbols:
        if is_etf(symbol):
            limits[symbol] = min(max_single, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
            continue
        thesis = thesis_by_symbol.get(str(symbol).split(".")[0]) or {}
        core = (
            float(thesis.get("derived_confidence") or 0) >= 75
            and str(thesis.get("derived_status") or "") in {"active", "confirmed", "valid"}
        )
        limits[symbol] = min(max_single, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
    return limits


def selection_score_map(detail: Dict[str, Any]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for row in detail.get("selections") or []:
        symbol = str((row or {}).get("symbol") or "").strip()
        if symbol:
            result[symbol] = float((row or {}).get("score") or 0)
    return result


def strategy_portfolio_method(strategy: Dict[str, Any] | None) -> str:
    content = strategy.get("content") if strategy and isinstance(strategy.get("content"), dict) else {}
    method = str(content.get("portfolio_method") or "cvar").strip().lower()
    return method if method in {"cvar", "equal_weight", "hrp"} else "cvar"


def base_portfolio_weights(
    returns_by_symbol: Dict[str, List[float]],
    *,
    max_single: float,
    method: str,
    constrained_cvar: Callable[..., Dict[str, float]],
    hrp_fallback: Callable[..., Dict[str, float]],
) -> tuple[Dict[str, float], str]:
    eligible = {
        str(symbol): list(values)
        for symbol, values in returns_by_symbol.items()
        if len(values) >= 2
    }
    if method == "equal_weight":
        weight = min(float(max_single), 1.0 / len(eligible)) if eligible else 0.0
        return (
            {symbol: weight for symbol in sorted(eligible)} if weight > 0 else {},
            "equal_weight_baseline_v1",
        )
    if method == "hrp":
        return (
            hrp_fallback(eligible, max_single=max_single),
            "hrp_inverse_volatility_v1",
        )
    weights = constrained_cvar(eligible, max_single=max_single)
    if weights:
        return weights, "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    return (
        hrp_fallback(eligible, max_single=max_single),
        "hrp_inverse_volatility_fallback_v1",
    )


def calc_total_buy_cost(orders: List[Any]) -> float:
    total = 0.0
    for order in orders:
        amount = float(getattr(order, "price", 0) or 0) * int(getattr(order, "quantity", 0) or 0)
        total += (
            amount
            + float(getattr(order, "commission", 0) or 0)
            + float(getattr(order, "transfer_fee", 0) or 0)
        )
    return round(total, 2)


def filled_orders(orders: List[Any]) -> List[Any]:
    return [order for order in orders if int(getattr(order, "quantity", 0) or 0) > 0]


def selection_explanation(
    ranked: List[tuple[Any, float]],
    *,
    weights: Dict[str, float],
    factor_values: Dict[str, Dict[str, float]],
    top_n: int,
) -> List[Dict[str, Any]]:
    selections: List[Dict[str, Any]] = []
    for row, score in ranked[: max(0, int(top_n))]:
        symbol = str(getattr(row, "symbol", "") or "").strip()
        values = factor_values.get(symbol) or {}
        contributions: List[Dict[str, Any]] = []
        for factor, weight in weights.items():
            frozen_weight = round(float(weight or 0), 8)
            frozen_raw = round(float(values.get(factor, 0) or 0), 8)
            contributions.append({
                "factor": factor,
                "weight": frozen_weight,
                "raw_value": frozen_raw,
                "contribution": round(frozen_weight * frozen_raw, 8),
            })
        contributions.sort(key=lambda item: abs(float(item.get("contribution") or 0)), reverse=True)
        selections.append({
            "symbol": symbol,
            "name": str(getattr(row, "name", "") or ""),
            "score": round(float(score or 0), 6),
            "factor_contributions": contributions,
        })
    return selections

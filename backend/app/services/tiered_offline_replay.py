"""Offline evidence for the tiered research/portfolio decision path.

The helper deliberately stops before persisting a plan or touching the account
ledger.  It still runs the production full-market selection, tier contract,
StrategyController, portfolio competition, and immutable T-day decision
contract for each requested historical signal date.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Callable, Iterable

from app.services.profit_loop import (
    build_daily_decision_contract,
    build_portfolio_reoptimization,
)
from app.services.quant_run_plan_input import validate_unified_decision_contract
from app.services.quant_run_engine import resolve_execute_date


OFFLINE_REPLAY_SCHEMA_VERSION = "alis_tiered_offline_replay_v1"


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary(*, signal_date: str, portfolio: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    selection = portfolio.get("selection_snapshot") if isinstance(portfolio.get("selection_snapshot"), dict) else {}
    tier_contract = portfolio.get("research_tier_contract") if isinstance(portfolio.get("research_tier_contract"), dict) else {}
    budget = portfolio.get("tiered_equity_budget") if isinstance(portfolio.get("tiered_equity_budget"), dict) else {}
    controller = portfolio.get("pretrade_controller") if isinstance(portfolio.get("pretrade_controller"), dict) else {}
    optimizer = portfolio.get("optimizer") if isinstance(portfolio.get("optimizer"), dict) else {}
    actions = controller.get("actions") if isinstance(controller.get("actions"), list) else []
    proposal = portfolio.get("rebalance_proposal") if isinstance(portfolio.get("rebalance_proposal"), list) else []
    return {
        "signal_date": signal_date,
        "portfolio_status": portfolio.get("status"),
        "selection_source": portfolio.get("selection_source"),
        "selection_schema_version": selection.get("schema_version"),
        "tier_schema_version": tier_contract.get("schema_version"),
        "tier_contract_hash": tier_contract.get("contract_hash"),
        "tier_counts": {
            "ranked": len(selection.get("ranked_symbols") or []),
            "core": len(selection.get("core_symbols") or []),
            "restricted": len(selection.get("restricted_symbols") or []),
            "hard_blocked": len(selection.get("hard_blocked_symbols") or []),
            "tradable": len(selection.get("tradable_symbols") or []),
        },
        "hard_blocked_symbols": list(selection.get("hard_blocked_symbols") or []),
        "opportunity_status": budget.get("opportunity_status"),
        "base_equity_budget": budget.get("base_equity_budget"),
        "effective_equity_budget": budget.get("effective_equity_budget"),
        "restricted_pool_budget": budget.get("restricted_pool_budget"),
        "restricted_symbol_cap": budget.get("restricted_symbol_cap"),
        "budget_contract_hash": budget.get("contract_hash"),
        "controller_schema_version": controller.get("schema_version"),
        "controller_action_counts": dict(Counter(str(item.get("action") or "") for item in actions if isinstance(item, dict))),
        "proposal_count": len(proposal),
        "execution_order_count": len(portfolio.get("execution_orders") or []),
        "optimizer_schema_version": optimizer.get("schema_version"),
        "optimizer_status": optimizer.get("status"),
        "decision_schema_version": contract.get("schema_version"),
        "t1_frozen": bool((contract.get("execution_safety_contract") or {}).get("t1_rerank_forbidden")),
        "target_symbols": list(contract.get("target_symbols") or []),
    }


def run_tiered_offline_replay_comparison(
    signal_dates: Iterable[str] = ("20260914", "20260916"),
    *,
    portfolio_builder: Callable[..., dict[str, Any]] = build_portfolio_reoptimization,
) -> dict[str, Any]:
    """Run the real production path for historical dates without persistence.

    ``portfolio_builder`` is injectable only for unit tests.  The default path
    is the production builder and therefore exercises the same full-market
    selection and optimizer used by the daily loop.
    """
    dates = [str(value) for value in signal_dates if str(value)]
    if len(dates) < 2:
        raise ValueError("at least two signal dates are required for comparison")
    observations: list[dict[str, Any]] = []
    for signal_date in dates:
        portfolio = portfolio_builder(signal_date=signal_date)
        if not isinstance(portfolio, dict):
            raise TypeError(f"portfolio builder returned non-object for {signal_date}")
        decision_gate = portfolio.get("decision_data_gate") if isinstance(portfolio.get("decision_data_gate"), dict) else {}
        planned_execute_date = str(decision_gate.get("next_trade_date") or decision_gate.get("planned_execute_date") or "")
        if not planned_execute_date:
            planned_execute_date, _ = resolve_execute_date(signal_date)
        contract = build_daily_decision_contract(
            portfolio=portfolio,
            trade_date=signal_date,
            planned_execute_date=planned_execute_date,
        )
        validation_error = ""
        try:
            validate_unified_decision_contract(contract)
        except Exception as exc:  # evidence must retain a real contract failure
            validation_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        observation = _summary(signal_date=signal_date, portfolio=portfolio, contract=contract)
        observation["contract_valid"] = not validation_error
        observation["validation_error"] = validation_error
        observations.append(observation)

    comparison = {
        "same_selection_schema": len({item["selection_schema_version"] for item in observations}) == 1,
        "same_tier_schema": len({item["tier_schema_version"] for item in observations}) == 1,
        "same_decision_schema": len({item["decision_schema_version"] for item in observations}) == 1,
        "all_t1_frozen": all(item["t1_frozen"] for item in observations),
        "all_portfolios_ready": all(item["portfolio_status"] == "ready" for item in observations),
        "all_contracts_valid": all(item["contract_valid"] for item in observations),
        "no_hard_blocked_target": all(
            not set(item["target_symbols"]) & set(item["hard_blocked_symbols"])
            for item in observations
        ),
    }
    result = {
        "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
        "signal_dates": dates,
        "observations": observations,
        "comparison": comparison,
        "replay_hash": _hash({"signal_dates": dates, "observations": observations, "comparison": comparison}),
        "persisted": False,
        "ledger_touched": False,
        "execution_performed": False,
    }
    return result

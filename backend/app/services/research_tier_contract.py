from __future__ import annotations

import hashlib
import json
from typing import Any


RESEARCH_TIER_SCHEMA_VERSION = "alis_equity_research_tier_v1"
EQUITY_BUDGET_SCHEMA_VERSION = "alis_tiered_equity_budget_v1"

CORE = "core"
RESTRICTED = "restricted"
HARD_BLOCKED = "hard_blocked"

# Missing authoritative facts are execution blockers. Research quality and a
# missing/weak thesis do not certify a name for core research, but they may
# leave it eligible for a tightly bounded trading sleeve.
# Valuation quality controls research rank and position sizing.  It is not a
# data-integrity or tradability prerequisite, so a valuation shortfall can be
# represented by the restricted opportunity tier.  Missing authoritative
# financial fields remain a hard block because they make the evidence
# unavailable rather than merely weak.
HARD_ELIGIBILITY_GATES = {"stock_master", "risk", "financial_fields"}

RESTRICTED_SINGLE_ASSET_LIMIT = 0.03
RESTRICTED_POOL_LIMIT_BY_STATE = {
    "S0": 0.20,
    "S1": 0.20,
    "S2": 0.10,
    "S3": 0.15,
}


def _hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_core_eligibility(eligibility: dict[str, Any]) -> dict[str, Any]:
    gates = [item for item in (eligibility.get("gates") or []) if isinstance(item, dict)]
    failed = [str(item.get("key") or "") for item in gates if not bool(item.get("passed"))]
    hard = [key for key in failed if key in HARD_ELIGIBILITY_GATES]
    tier = CORE if not failed else HARD_BLOCKED if hard else RESTRICTED
    return {
        "tier": tier,
        "core_research_eligible": tier == CORE,
        "trading_eligible": tier != HARD_BLOCKED,
        "failed_gates": failed,
        "hard_failed_gates": hard,
        "research_quality_failed_gates": [key for key in failed if key not in hard],
        "reason_code": (
            "core_research_qualified"
            if tier == CORE
            else "hard_research_data_or_risk_block"
            if tier == HARD_BLOCKED
            else "research_quality_limited"
        ),
    }


def classify_dynamic_gate(gate: dict[str, Any], *, prior_tier: str) -> dict[str, Any]:
    hard = [str(value) for value in (gate.get("hard_blockers") or []) if str(value)]
    research = [str(value) for value in (gate.get("research_blockers") or []) if str(value)]
    if hard:
        tier = HARD_BLOCKED
    elif prior_tier == RESTRICTED or research or not bool(gate.get("admitted")):
        tier = RESTRICTED
    else:
        tier = CORE
    return {
        "tier": tier,
        "core_research_eligible": tier == CORE,
        "trading_eligible": tier != HARD_BLOCKED,
        "hard_blockers": hard,
        "research_quality_blockers": research,
        "reason_code": (
            "core_research_qualified"
            if tier == CORE
            else "dynamic_hard_risk_block"
            if tier == HARD_BLOCKED
            else "dynamic_research_quality_limited"
        ),
    }


def build_tiered_equity_budget(
    *,
    market_state: str,
    base_equity_budget: float,
    core_symbols: list[str],
    restricted_symbols: list[str],
) -> dict[str, Any]:
    state = str(market_state or "")
    base = max(0.0, min(1.0, float(base_equity_budget or 0.0)))
    core = list(dict.fromkeys(str(value) for value in core_symbols if str(value)))
    restricted = list(dict.fromkeys(str(value) for value in restricted_symbols if str(value)))
    restricted_limit = min(
        base,
        RESTRICTED_POOL_LIMIT_BY_STATE.get(state, 0.0),
    ) if restricted else 0.0
    if core:
        equity_budget = base
        opportunity_status = "core_and_restricted" if restricted else "core_only"
    elif restricted:
        # S1 retains a bounded equity sleeve when tradable factor-ranked names
        # exist but their research quality is not sufficient for the core tier.
        equity_budget = restricted_limit
        opportunity_status = "restricted_only"
    else:
        equity_budget = 0.0
        opportunity_status = "no_qualified_equity_candidate"
    per_symbol_cap = min(
        RESTRICTED_SINGLE_ASSET_LIMIT,
        restricted_limit,
    ) if restricted else 0.0
    base_contract = {
        "schema_version": EQUITY_BUDGET_SCHEMA_VERSION,
        "research_tier_schema_version": RESEARCH_TIER_SCHEMA_VERSION,
        "market_state": state,
        "base_equity_budget": round(base, 8),
        "effective_equity_budget": round(equity_budget, 8),
        "opportunity_status": opportunity_status,
        "core_symbols": core,
        "restricted_symbols": restricted,
        "restricted_pool_budget": round(restricted_limit, 8),
        "restricted_single_asset_limit": RESTRICTED_SINGLE_ASSET_LIMIT,
        "restricted_symbol_cap": round(per_symbol_cap, 8),
        "restricted_may_support_model_promotion": False,
        "hard_blocked_may_receive_weight": False,
        "s1_minimum_equity_sleeve_when_restricted_only": RESTRICTED_POOL_LIMIT_BY_STATE["S1"],
    }
    return {**base_contract, "contract_hash": _hash(base_contract)}


def build_research_tier_contract(items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized = {
        str(symbol): dict(value)
        for symbol, value in sorted(items.items())
        if str(symbol) and isinstance(value, dict)
    }
    base = {
        "schema_version": RESEARCH_TIER_SCHEMA_VERSION,
        "items": normalized,
        "core_symbols": [symbol for symbol, item in normalized.items() if item.get("tier") == CORE],
        "restricted_symbols": [symbol for symbol, item in normalized.items() if item.get("tier") == RESTRICTED],
        "hard_blocked_symbols": [symbol for symbol, item in normalized.items() if item.get("tier") == HARD_BLOCKED],
        "model_promotion_evidence_tiers": [CORE],
        "trading_candidate_tiers": [CORE, RESTRICTED],
    }
    return {**base, "contract_hash": _hash(base)}

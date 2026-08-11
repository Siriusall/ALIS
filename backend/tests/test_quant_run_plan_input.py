import hashlib
import json

import pytest

from app.services.quant_run_contracts import ReplayRiskGateError
from app.services.quant_run_plan_input import (
    build_authoritative_selection_detail,
    normalize_authoritative_plan,
    validate_unified_decision_contract,
)


def _plan() -> dict:
    return {
        "plan_id": 9, "plan_version": "20260724-v1", "input_hash": "hash",
        "execution_claim_token": "task-1", "target_symbols": ["000001", "600000"],
        "target_weights": {"000001": .4, "600000": .5},
        "proposal": [{"symbol": "000001", "name": "平安银行", "expected_20d_return": .03}],
        "risk_budget": {"target_position_pct": .9}, "decision_version_manifest": {"hash": "v1"},
    }


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _unified_plan() -> dict:
    factor_bindings = {
        "momentum_20": {
            "definition_id": 1,
            "factor_key": "momentum_20",
            "version": "v1",
        },
    }
    environment = {
        "schema_version": "alis_selection_environment_snapshot_v1",
        "selection_market_state": "S1",
        "production_market_state": "S1",
        "market_profile": {},
    }
    factor_snapshot = {
        "schema_version": "alis_factor_expert_selection_v1",
        "selected_factors": ["momentum_20"],
        "factor_weights": {"momentum_20": 1.0},
        "factor_bindings": factor_bindings,
        "expert_route": {"route": "balanced"},
    }
    model_rows = [{
        "model_key": "medium_horizon_lambdarank_v1",
        "version_id": 7,
        "version": "model-v7",
        "consumed": True,
        "point_in_time_available": True,
    }]
    thesis_snapshot = {
        "id": 9,
        "title": "盈利修复",
        "thesis_type": "fundamental",
        "core_thesis": "盈利修复尚未被完全定价",
        "derived_status": "active",
        "derived_confidence": 0.8,
        "hypotheses": [{
            "statement": "核心利润不能进入显著衰退",
            "metric": "core_profit_growth",
            "necessary": True,
            "expected": {"min": -15},
            "status": "verified",
            "latest_evidence": {"value": 8.0},
        }],
    }
    scored_selection = {
        "symbol": "000001",
        "score": 90,
        "factor_contributions": [{
            "factor": "momentum_20",
            "weight": 1.0,
            "raw_value": 0.1,
            "contribution": 0.1,
        }],
        "model_signals": {
            "schema_version": "model_signal_fusion_v2",
            "models": {
                "medium_horizon_lambdarank_v1": {
                    "raw_score": 0.4,
                    "percentile": 0.8,
                    "version_id": 7,
                    "version": "model-v7",
                    "point_in_time_available": True,
                },
            },
            "trigger_models": ["medium_horizon_lambdarank_v1"],
        },
    }
    selection = {
        "schema_version": "alis_unified_stock_selection_v1",
        "signal_date": "20260724",
        "data_as_of_time": "2026-07-24T15:00:00+08:00",
        "strategy_id": 3,
        "champion_id": 50,
        "champion_version": "champion-v50",
        "ranked_symbols": ["000001"],
        "research_admitted_symbols": ["000001"],
        "admitted_symbols": ["000001"],
        "holding_symbols": [],
        "holding_admitted_symbols": [],
        "research_evidence": {
            "000001": {
                "eligible_for_core": True,
                "gates": [
                    {"key": key, "passed": True}
                    for key in (
                        "stock_master", "risk", "financial_fields",
                        "research", "valuation", "thesis",
                    )
                ],
                "thesis_snapshot": thesis_snapshot,
            },
        },
        "dynamic_gate": {
            "as_of_time": "2026-07-24T15:00:00+08:00",
            "items": [{
                "symbol": "000001",
                "gate": {"admitted": True},
            }],
        },
        "environment_snapshot": environment,
        "factor_expert_snapshot": factor_snapshot,
        "model_signal_snapshot": {
            "schema_version": "alis_model_signal_snapshot_v1",
            "fusion": {
                "schema_version": "model_signal_fusion_v2",
                "production_models": model_rows,
                "policy": {
                    "effective_model_weights": {
                        "medium_horizon_lambdarank_v1": 0.25,
                    },
                },
            },
            "symbols": {
                "000001": {
                    "status": "scored",
                    "selection": scored_selection,
                    "blocks": [],
                },
            },
        },
        "research_errors": [],
        "coverage": {
            "universe_count": 800,
            "ranked_count": 1,
            "holding_review_count": 0,
            "research_error_count": 0,
            "evaluated_count": 1,
            "research_error_ratio": 0.0,
            "gates": {
                "full_market_snapshot": True,
                "research_recall_nonempty": True,
                "research_evaluation_coverage": True,
                "at_least_one_research_evaluation": True,
            },
        },
        "authority": {
            "formal_candidate_authority": "unified_selection_contract",
            "legacy_pool_membership_required": False,
            "legacy_pool_mutation_may_change_contract": False,
        },
    }
    selection["selection_hash"] = _hash(selection)
    manifest = {
        "schema_version": "alis_decision_version_manifest_v2",
        "trade_date": "20260724",
        "release": {
            "id": 9,
            "release_key": "alis-test-release",
            "manifest_hash": "a" * 64,
            "status": "active",
        },
        "factor_contract": {
            "schema_version": "alis_factor_registry_v1",
            "bindings": factor_bindings,
        },
        "model_versions": {
            "medium_horizon_lambdarank_v1": {
                "id": 7,
                "version": "model-v7",
                "status": "healthy",
            },
        },
        "selection": {
            "schema_version": "alis_unified_stock_selection_v1",
            "selection_hash": selection["selection_hash"],
            "environment_snapshot": environment,
            "factor_expert_snapshot": factor_snapshot,
        },
        "strategy": {
            "model_version_id": 50,
            "version": "champion-v50",
            "strategy_id": 3,
            "strategy_controller_version_id": 1,
            "strategy_controller_version": "controller-v1",
            "pretrade_controller_schema": "alis_strategy_controller_pretrade_v1",
        },
        "portfolio": {
            "selected_method": "empirical_cvar",
            "competition_schema": "portfolio_method_competition_v2",
        },
    }
    manifest["manifest_hash"] = _hash(manifest)
    action = {
        "symbol": "000001",
        "action": "buy_candidate",
        "permission_allowed": True,
        "current_weight": 0.0,
        "min_weight": 0.0,
        "max_weight": 0.1,
        "expected_20d_return_before_trade_cost": 0.03,
    }
    contract = {
        "schema_version": "alis_unified_daily_decision_v1",
        "trade_date": "20260724",
        "signal_date": "20260724",
        "data_as_of_time": "2026-07-24T15:00:00+08:00",
        "planned_execute_date": "20260727",
        "proposal": [{
            "symbol": "000001",
            "current_weight": 0.0,
            "target_weight": 0.1,
            "expected_20d_return": 0.03,
        }],
        "target_symbols": ["000001"],
        "target_weights": {"000001": 0.1},
        "final_target_weights": {"000001": 0.1},
        "selection_snapshot": selection,
        "pretrade_controller": {
            "schema_version": "alis_strategy_controller_pretrade_v1",
            "generated_at": "2026-07-24T15:00:00+08:00",
            "strategy_policy_version_id": 1,
            "strategy_policy_version": "controller-v1",
            "actions": [action],
            "weight_bounds": {"000001": {
                "min_weight": 0.0, "max_weight": 0.1,
                "action": "buy_candidate", "permission_allowed": True,
            }},
        },
        "industry_allocation": {
            "schema_version": "alis_industry_allocation_v1",
            "as_of_time": "2026-07-24T15:00:00+08:00",
            "industry_targets": {"银行": 0.1},
            "symbol_industries": {"000001": "银行"},
            "items": [],
            "method": "market_then_industry_then_stock_v1",
        },
        "expected_return_contract": {
            "schema_version": "alis_a_share_expected_return_20d_v1",
            "horizon_sessions": 20,
            "market_state": "S1",
            "optimizer_expected_returns": {"000001": 0.03},
            "items": [{
                "symbol": "000001",
                "expected_20d_return_before_trade_cost": 0.03,
                "components": {"factor_composite": 0.03},
                "penalties": {"model_degradation": 0.0},
            }],
        },
        "portfolio_method_competition": {
            "schema_version": "portfolio_method_competition_v2",
            "ok": True,
            "selected_method": "empirical_cvar",
            "weights": {"000001": 0.1},
            "winner": {
                "method": "empirical_cvar",
                "weights": {"000001": 0.1},
                "eligible": True,
            },
            "evaluations": [{
                "method": "empirical_cvar",
                "weights": {"000001": 0.1},
                "eligible": True,
            }],
            "weight_bounds": {
                "000001": {
                    "min_weight": 0.0,
                    "max_weight": 0.1,
                    "action": "buy_candidate",
                },
            },
            "industry_budget": {
                "schema_version": "alis_portfolio_industry_budget_v1",
                "symbol_industries": {"000001": "银行"},
                "industry_targets": {"银行": 0.1},
                "application": "soft_deviation_penalty_applied_to_every_competing_method",
            },
        },
        "execution_checks": [{"key": "market_data", "status": "pass"}],
        "execution_authorized": True,
        "decision_data_gate": {
            "target_trade_date": "20260724",
            "decision_ready": True,
        },
        "execution_safety_contract": {
            "schema_version": "alis_t1_execution_safety_v1",
            "investment_opinion_frozen_on_signal_date": True,
            "t1_rerank_forbidden": True,
            "t1_research_refresh_forbidden": True,
            "t1_industry_reallocation_forbidden": True,
            "t1_controller_regeneration_forbidden": True,
            "t1_portfolio_reoptimization_forbidden": True,
            "allowed_vetoes": [
                "missing_open_price",
                "suspension",
                "st_status",
                "limit_up_down",
                "lot_size",
                "liquidity_capacity",
                "cash_no_overdraft",
                "no_oversell",
                "t1_sell_restriction",
                "ledger_consistency",
                "overnight_champion_restriction_only",
            ],
        },
        "decision_version_manifest": manifest,
    }
    return {
        "plan_id": 10,
        "plan_version": "20260724-v2",
        "input_hash": _hash(contract),
        "execution_claim_token": "task-2",
        "target_symbols": ["000001"],
        "target_weights": {"000001": 0.1},
        "proposal": contract["proposal"],
        "decision_version_manifest": manifest,
        "decision_contract": contract,
    }


def _rehash_unified_plan(
    plan: dict,
    *,
    selection_changed: bool = False,
    manifest_changed: bool = False,
) -> None:
    contract = plan["decision_contract"]
    manifest = contract["decision_version_manifest"]
    if selection_changed:
        selection = contract["selection_snapshot"]
        selection["selection_hash"] = _hash({
            key: value for key, value in selection.items()
            if key != "selection_hash"
        })
        manifest["selection"]["selection_hash"] = selection["selection_hash"]
        manifest_changed = True
    if manifest_changed:
        manifest["manifest_hash"] = _hash({
            key: value for key, value in manifest.items()
            if key != "manifest_hash"
        })
        plan["decision_version_manifest"] = manifest
    plan["input_hash"] = _hash(contract)


def test_authoritative_plan_normalizes_and_freezes_provenance():
    plan = normalize_authoritative_plan(_plan())
    detail = build_authoritative_selection_detail(plan, signal_date="2026-07-24", exchange_group="HS")
    assert plan["target_weights"] == {"000001": .4, "600000": .5}
    assert detail["plan_provenance"]["plan_id"] == 9
    assert detail["selections"][0]["score"] == 3.0


@pytest.mark.parametrize("mutation", [
    {"execution_claim_token": ""},
    {"target_symbols": ["000001", "000001"]},
    {"target_weights": {"000001": -.1}},
    {"target_weights": {"000001": .6, "600000": .5}},
    {"target_weights": {"000001": .4, "300001": .1}},
])
def test_authoritative_plan_rejects_unsafe_contracts(mutation):
    plan = {**_plan(), **mutation}
    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)
    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_validates_all_frozen_contracts_and_hashes():
    normalized = normalize_authoritative_plan(_unified_plan())

    assert normalized["is_unified_frozen_plan"] is True
    assert normalized["pretrade_controller"]["actions"][0]["action"] == "buy_candidate"
    assert normalized["execution_safety_contract"]["t1_research_refresh_forbidden"] is True


def test_unified_plan_allows_fully_quarantined_model_family_with_factor_fallback():
    plan = _unified_plan()
    contract = plan["decision_contract"]
    selection = contract["selection_snapshot"]
    fusion = selection["model_signal_snapshot"]["fusion"]
    model = fusion["production_models"][0]
    model.update({
        "version_id": None,
        "version": None,
        "consumed": False,
        "point_in_time_available": False,
        "availability_reason": "no_production_eligible_version",
    })
    fusion["policy"]["effective_model_weights"] = {
        "medium_horizon_lambdarank_v1": 0.0,
    }
    per_stock = selection["model_signal_snapshot"]["symbols"]["000001"]["selection"]["model_signals"]
    per_stock["models"]["medium_horizon_lambdarank_v1"] = {
        "raw_score": None,
        "percentile": None,
        "version_id": None,
        "version": None,
        "point_in_time_available": False,
    }
    per_stock["trigger_models"] = []
    contract["decision_version_manifest"]["model_versions"] = {}
    _rehash_unified_plan(plan, selection_changed=True, manifest_changed=True)

    normalized = normalize_authoritative_plan(plan)

    assert normalized["is_unified_frozen_plan"] is True
    assert normalized["decision_version_manifest"]["model_versions"] == {}


def test_unified_plan_factor_fallback_rejects_phantom_manifest_model():
    plan = _unified_plan()
    contract = plan["decision_contract"]
    selection = contract["selection_snapshot"]
    fusion = selection["model_signal_snapshot"]["fusion"]
    model = fusion["production_models"][0]
    model.update({
        "version_id": None,
        "version": None,
        "consumed": False,
        "point_in_time_available": False,
    })
    fusion["policy"]["effective_model_weights"] = {
        "medium_horizon_lambdarank_v1": 0.0,
    }
    per_stock = selection["model_signal_snapshot"]["symbols"]["000001"]["selection"]["model_signals"]
    per_stock["models"]["medium_horizon_lambdarank_v1"].update({
        "raw_score": None,
        "percentile": None,
        "version_id": None,
        "version": None,
        "point_in_time_available": False,
    })
    per_stock["trigger_models"] = []
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_tampered_selection_hash():
    plan = _unified_plan()
    plan["decision_contract"]["selection_snapshot"]["ranked_symbols"].append("600000")
    plan["input_hash"] = _hash(plan["decision_contract"])

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan_hash"


def test_unified_plan_rejects_optimizer_weight_outside_controller_permission():
    plan = _unified_plan()
    plan["decision_contract"]["final_target_weights"]["000001"] = 0.2
    plan["decision_contract"]["portfolio_method_competition"]["winner"]["weights"]["000001"] = 0.2
    plan["input_hash"] = _hash(plan["decision_contract"])

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_outer_target_weights_that_differ_from_frozen_contract():
    plan = _unified_plan()
    plan["target_weights"] = {"000001": 0.05}

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_dynamic_gate_result_that_does_not_match_admission():
    plan = _unified_plan()
    plan["decision_contract"]["selection_snapshot"]["dynamic_gate"]["items"][0]["gate"]["admitted"] = False
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_non_falsifiable_thesis():
    plan = _unified_plan()
    evidence = plan["decision_contract"]["selection_snapshot"]["research_evidence"]["000001"]
    evidence["thesis_snapshot"]["hypotheses"] = []
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_empty_expected_return_explanation():
    plan = _unified_plan()
    plan["decision_contract"]["expected_return_contract"]["items"][0]["components"] = {}
    _rehash_unified_plan(plan)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_controller_action_direction_change():
    plan = _unified_plan()
    controller = plan["decision_contract"]["pretrade_controller"]
    controller["actions"][0]["current_weight"] = 0.2
    plan["decision_contract"]["proposal"][0]["current_weight"] = 0.2
    plan["proposal"][0]["current_weight"] = 0.2
    _rehash_unified_plan(plan)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_manifest_controller_version_mismatch():
    plan = _unified_plan()
    manifest = plan["decision_contract"]["decision_version_manifest"]
    manifest["strategy"]["strategy_controller_version"] = "controller-v2"
    _rehash_unified_plan(plan, manifest_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_incomplete_factor_formula_contributions():
    plan = _unified_plan()
    selection = plan["decision_contract"]["selection_snapshot"]
    selection["factor_expert_snapshot"]["selected_factors"].append("volatility_20")
    selection["factor_expert_snapshot"]["factor_bindings"]["volatility_20"] = {
        "definition_id": 2,
        "factor_key": "volatility_20",
        "version": "v1",
    }
    selection["factor_expert_snapshot"]["factor_weights"]["volatility_20"] = 0.5
    manifest = plan["decision_contract"]["decision_version_manifest"]
    manifest["selection"]["factor_expert_snapshot"] = selection["factor_expert_snapshot"]
    manifest["factor_contract"]["bindings"] = selection["factor_expert_snapshot"]["factor_bindings"]
    _rehash_unified_plan(plan, selection_changed=True, manifest_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_per_stock_factor_weight_mismatch():
    plan = _unified_plan()
    selection = plan["decision_contract"]["selection_snapshot"]
    contribution = selection["model_signal_snapshot"]["symbols"]["000001"]["selection"]["factor_contributions"][0]
    contribution["weight"] = 0.5
    contribution["contribution"] = 0.05
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_per_stock_model_version_mismatch():
    plan = _unified_plan()
    selection = plan["decision_contract"]["selection_snapshot"]
    model = selection["model_signal_snapshot"]["symbols"]["000001"]["selection"]["model_signals"]["models"][
        "medium_horizon_lambdarank_v1"
    ]
    model["version_id"] = 8
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_plan_rejects_unconsumed_model_with_nonzero_fusion_weight():
    plan = _unified_plan()
    selection = plan["decision_contract"]["selection_snapshot"]
    fusion = selection["model_signal_snapshot"]["fusion"]
    fusion["production_models"][0]["consumed"] = False
    fusion["production_models"][0]["point_in_time_available"] = False
    per_stock = selection["model_signal_snapshot"]["symbols"]["000001"]["selection"]["model_signals"]
    per_stock["models"]["medium_horizon_lambdarank_v1"]["point_in_time_available"] = False
    per_stock["trigger_models"] = []
    _rehash_unified_plan(plan, selection_changed=True)

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("environment_snapshot", "selection_market_state", "S2"),
        ("factor_expert_snapshot", "expert_route", {"route": "aggressive"}),
        ("model_signal_snapshot", "schema_version", "tampered"),
    ],
)
def test_selection_hash_freezes_environment_factor_route_and_model_outputs(section, key, value):
    plan = _unified_plan()
    plan["decision_contract"]["selection_snapshot"][section][key] = value
    plan["input_hash"] = _hash(plan["decision_contract"])

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan_hash"


def test_selection_hash_freezes_thesis_content():
    plan = _unified_plan()
    thesis = plan["decision_contract"]["selection_snapshot"]["research_evidence"]["000001"]["thesis_snapshot"]
    thesis["core_thesis"] = "被篡改"
    plan["input_hash"] = _hash(plan["decision_contract"])

    with pytest.raises(ReplayRiskGateError) as exc:
        normalize_authoritative_plan(plan)

    assert exc.value.code == "invalid_authoritative_plan_hash"


def test_public_unified_validator_rejects_legacy_contract():
    with pytest.raises(ReplayRiskGateError):
        validate_unified_decision_contract({"schema_version": "alis_legacy_daily_plan_v1"})

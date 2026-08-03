from __future__ import annotations

from datetime import datetime
from typing import Any


MODEL_OUTPUT_CONTRACTS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "output_schema": {"score": "number", "rank_percentile": "number"},
        "consumers": ["function_bucket", "return_source", "optimizer_expected_return"],
        "fallback": "industry_momentum_baseline_v1",
    },
    "capital_persistence_multitask_v1": {
        "output_schema": {
            "direction_probability": "number",
            "strength_p10": "number",
            "strength_p50": "number",
            "strength_p90": "number",
            "failure_hazard": "number",
        },
        "consumers": ["funding_confirmation", "add_reduce_gate", "optimizer_expected_return"],
        "fallback": "capital_persistence_proxy_v1",
    },
    "leader_lambdarank_v1": {
        "output_schema": {"score": "number", "rank_percentile": "number"},
        "consumers": ["function_bucket", "return_source", "position_cap"],
        "fallback": "leader_strength_rule_v1",
    },
    "event_impact_assessor_v2": {
        "output_schema": {
            "impact_contract": "string",
            "event_relevance": "number",
            "positive_impact_probability": "number",
            "priced_in_probability": "number",
            "continuation_probability": "number",
            "reversal_probability": "number",
            "direction_realization_probability": "number",
            "abnormal_return_q10": "number",
            "abnormal_return_q50": "number",
            "abnormal_return_q90": "number",
            "crowding_risk": "number",
            "recommended_holding_horizon_sessions": "number",
            "event_hard_veto": "boolean",
        },
        "consumers": [
            "return_source", "holding_period", "exit_conditions",
            "add_reduce_gate", "event_risk_veto",
        ],
        "fallback": "event_impact_deterministic_rule_v1",
    },
    "narrative_transition_multitask_v1": {
        "output_schema": {
            "transition_probability": "number",
            "conditional_destination_probabilities": "object",
            "next_state_probabilities": "object",
            "exit_risk": "number",
            "confidence": "number",
        },
        "consumers": ["narrative_lifecycle", "exit_conditions", "add_reduce_gate"],
        "fallback": "narrative_lifecycle_proxy_v1",
    },
    "narrative_bertopic_v1": {
        "pipeline_schema": {
            "embedding_model": "BAAI/bge-m3",
            "umap": {"n_neighbors": 15, "n_components": 5, "min_dist": 0.0, "metric": "cosine", "random_state": 20260719},
            "hdbscan": {"min_cluster_size": 10, "min_samples": 5, "metric": "euclidean", "cluster_selection_method": "eom"},
            "topic_terms": "c_tf_idf",
        },
        "output_schema": {
            "topic_id": "string",
            "freshness_score": "number",
            "entity_mappings": "array",
            "lifecycle": "enum",
            "online_strength": "number",
        },
        "consumers": ["narrative_lifecycle", "function_bucket", "return_source"],
        "fallback": "deterministic_theme_score_v1",
    },
}


EVIDENCE_BINDINGS: dict[str, dict[str, str]] = {
    "function_bucket": {
        "primary": "medium_horizon_lambdarank_v1|leader_lambdarank_v1|event_impact_assessor_v2|narrative_bertopic_v1",
        "fallback": "five_bucket_rule_v1",
    },
    "return_source": {
        "primary": "medium_horizon_lambdarank_v1|leader_lambdarank_v1|event_impact_assessor_v2",
        "fallback": "thesis_proxy_v1",
    },
    "holding_period": {
        "primary": "event_impact_assessor_v2|narrative_transition_multitask_v1",
        "fallback": "bucket_policy_v1",
    },
    "narrative_lifecycle": {
        "primary": "narrative_bertopic_v1|narrative_transition_multitask_v1",
        "fallback": "narrative_lifecycle_proxy_v1",
    },
    "funding_confirmation": {
        "primary": "capital_persistence_multitask_v1",
        "fallback": "capital_persistence_proxy_v1",
    },
    "exit_conditions": {
        "primary": "narrative_transition_multitask_v1|event_impact_assessor_v2|risk_policy_v1",
        "fallback": "risk_exit_policy_v1",
    },
    "data_status": {
        "primary": "data_readiness_validator_v1",
        "fallback": "data_health_gate_v1",
    },
}


def model_output_contracts() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": "plan_book_model_output_contracts_v1",
        "contracts": MODEL_OUTPUT_CONTRACTS,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def evidence_binding_registry() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": "target_weight_evidence_binding_registry_v1",
        "bindings": EVIDENCE_BINDINGS,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def evidence_binding_for(field: str) -> dict[str, Any]:
    binding = EVIDENCE_BINDINGS.get(field)
    return {"ok": bool(binding), "field": field, "binding": binding or {}, "schema_version": "target_weight_evidence_binding_v1"}


def normalize_model_output(model_key: str, raw: dict[str, Any]) -> dict[str, Any]:
    contract = MODEL_OUTPUT_CONTRACTS.get(model_key) or {}
    schema = dict(contract.get("output_schema") or {})
    normalized: dict[str, Any] = {}
    missing: list[str] = []
    for key, expected_type in schema.items():
        value = raw.get(key)
        if value is None:
            missing.append(key)
            continue
        if expected_type == "number":
            normalized[key] = float(value)
        elif expected_type == "integer":
            normalized[key] = int(value)
        else:
            normalized[key] = value
    return {
        "ok": not missing,
        "model_key": model_key,
        "schema_version": "normalized_plan_model_output_v1",
        "output": normalized,
        "missing_fields": missing,
        "fallback": contract.get("fallback"),
    }

from __future__ import annotations

from datetime import datetime
from typing import Any


MODEL_OUTPUT_CONTRACTS: dict[str, dict[str, Any]] = {
    "medium_horizon_lambdarank_v1": {
        "output_schema": {"score": "number", "rank_percentile": "number", "positive_excess_return_probability_20d": "number"},
        "consumers": ["function_bucket", "return_source", "optimizer_expected_return"],
        "fallback": "industry_momentum_baseline_v1",
    },
    "capital_persistence_multitask_v1": {
        "output_schema": {
            "direction_probability_3d": "number",
            "direction_probability_5d": "number",
            "flow_ratio_p10": "number",
            "flow_ratio_p50": "number",
            "flow_ratio_p90": "number",
            "hazard_end_1d": "number",
            "hazard_end_3d": "number",
            "hazard_end_5d": "number",
            "hazard_end_10d": "number",
            "confirmation_score": "number",
        },
        "consumers": ["funding_confirmation", "add_reduce_gate", "optimizer_expected_return"],
        "fallback": "capital_persistence_proxy_v1",
    },
    "leader_lambdarank_v1": {
        "output_schema": {"score": "number", "theme_group_rank": "integer", "funding_confirmation_probability": "number"},
        "consumers": ["function_bucket", "return_source", "position_cap"],
        "fallback": "leader_strength_rule_v1",
    },
    "event_surprise_lgbm_v1": {
        "output_schema": {
            "abnormal_return_1d": "number",
            "abnormal_return_3d": "number",
            "abnormal_return_5d": "number",
            "abnormal_return_10d": "number",
            "positive_probability_5d": "number",
            "event_half_life_days": "number",
        },
        "consumers": ["return_source", "holding_period", "exit_conditions"],
        "fallback": "event_surprise_rule_v1",
    },
    "narrative_transition_multitask_v1": {
        "output_schema": {
            "continue_diffusion_probability": "number",
            "acceleration_probability": "number",
            "divergence_probability": "number",
            "cooldown_probability": "number",
            "fast_fade_probability": "number",
            "excess_return_p10": "number",
            "breadth_change_p50": "number",
            "leader_drawdown_p90": "number",
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
        "primary": "medium_horizon_lambdarank_v1|leader_lambdarank_v1|event_surprise_lgbm_v1|narrative_bertopic_v1",
        "fallback": "five_bucket_rule_v1",
    },
    "return_source": {
        "primary": "medium_horizon_lambdarank_v1|leader_lambdarank_v1|event_surprise_lgbm_v1",
        "fallback": "thesis_proxy_v1",
    },
    "holding_period": {
        "primary": "event_surprise_lgbm_v1|narrative_transition_multitask_v1",
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
        "primary": "narrative_transition_multitask_v1|event_surprise_lgbm_v1|risk_policy_v1",
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
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def evidence_binding_registry() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": "target_weight_evidence_binding_registry_v1",
        "bindings": EVIDENCE_BINDINGS,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
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

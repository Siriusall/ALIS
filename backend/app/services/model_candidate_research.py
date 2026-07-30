"""Compatibility boundary for the retired generic model-candidate protocol.

Candidate mutation previously ranked every model family by the same top-K
return lift.  That is invalid for classifiers, transition models, event
regressors and HSMMs, so the writer is intentionally closed.  Future candidate
generation must consume task-native failure evidence from
``model_evaluation_contracts`` and use the independent frozen strategy A/B
gate.
"""

from __future__ import annotations

from typing import Any, Callable

from app.services.model_evaluation_contracts import SCHEMA_VERSION


RETIRED_STATUS = "retired_legacy_model_candidate_protocol"


def _retired_result(**evidence: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "status": RETIRED_STATUS,
        "replacement_contract": SCHEMA_VERSION,
        "production_change_forbidden": True,
        **evidence,
    }


def attribute_walk_forward_failure(
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return _retired_result(
        model_key=str(metrics.get("model_key") or ""),
        reason="generic_return_lift_attribution_removed",
    )


def generate_constrained_model_candidates(
    model_key: str,
    attribution: dict[str, Any],
) -> list[dict[str, Any]]:
    del model_key, attribution
    return []


def run_model_candidate_search(
    *,
    model_version_id: int,
    evaluator: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    formal_dispatcher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    maximum_formal_winners: int = 1,
) -> dict[str, Any]:
    del evaluator, formal_dispatcher, maximum_formal_winners
    return _retired_result(
        model_version_id=int(model_version_id),
        reason="task_native_candidate_research_not_implemented_yet",
    )

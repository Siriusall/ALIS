"""Read-only compatibility boundary for the retired generic ``main`` model.

ALIS now governs the five business model families with task-native evaluation
and evaluates market regime separately as HSMM.  The former generic model
trainer used one return-lift protocol for unrelated tasks and is therefore not
allowed to schedule training, promote a version, or change an active binding.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.model_evaluation_contracts import SCHEMA_VERSION


RETIRED_STATUS = "retired_legacy_generic_model_protocol"


def _retired_result(**evidence: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "started": False,
        "status": RETIRED_STATUS,
        "reason": "replaced_by_task_native_five_model_governance",
        "replacement_contract": SCHEMA_VERSION,
        "production_change_forbidden": True,
        **evidence,
    }


def automatic_retraining_decision(
    *,
    latest_run: dict[str, Any] | None,
    latest_version: dict[str, Any] | None,
    current_trade_date: str,
    now: datetime | None = None,
    retrain_interval_hours: int = 24 * 7,
    failed_retry_hours: int = 24,
    stale_running_hours: int = 12,
) -> dict[str, Any]:
    del (
        latest_run,
        latest_version,
        current_trade_date,
        now,
        retrain_interval_hours,
        failed_retry_hours,
        stale_running_hours,
    )
    return _retired_result(due=False)


def bootstrap_candidate_eligible(version: dict[str, Any]) -> bool:
    del version
    return False


def mark_bootstrap_promotion(version: dict[str, Any]) -> dict[str, Any]:
    return _retired_result(model_version_id=version.get("id"))


def request_model_training(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    exchange_group: str = "HS",
    force: bool = False,
) -> dict[str, Any]:
    del start_date, end_date, exchange_group, force
    return _retired_result()


def auto_tune_models_once(*, allow_retrain: bool = True) -> dict[str, Any]:
    del allow_retrain
    return _retired_result(decision="no_action")


def start_model_governance_scheduler() -> bool:
    """Do not start the retired writer; kept for startup compatibility."""

    return False

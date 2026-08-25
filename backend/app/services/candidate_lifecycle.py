"""Unified candidate observation runtime and production-eligibility contract."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any


OBSERVATION_CONTRACT = "alis_immediate_isolated_candidate_observation_v1"
RUNTIME_STATUSES = {"created", "observing", "stopped", "archived"}
ELIGIBILITY_STATUSES = {"ineligible", "evaluating", "eligible", "promoted", "rolled_back"}
CHANGE_TYPES = {"bugfix", "performance_tuning", "structural_change"}


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat()


def _candidate_type(model_key: str, config: dict[str, Any]) -> str:
    explicit = str(config.get("candidate_type") or "")
    if explicit in {"model", "strategy", "controller", "factor", "portfolio"}:
        return explicit
    if model_key == "alis_strategy_challenger":
        return "strategy"
    if model_key == "strategy_controller":
        return "controller"
    if model_key.startswith("factor_") or config.get("factor_combination"):
        return "factor"
    if model_key.startswith("portfolio_") or config.get("portfolio_optimizer_candidate"):
        return "portfolio"
    return "model"


def _change_type(config: dict[str, Any]) -> str:
    explicit = str(config.get("change_type") or "")
    if explicit in CHANGE_TYPES:
        return explicit
    generation = str(config.get("generation") or "").lower()
    if any(token in generation for token in ("bugfix", "repair", "contract_fix")):
        return "bugfix"
    if any(token in generation for token in ("parameter", "tuning", "optimization")):
        return "performance_tuning"
    return "structural_change"


def _lineage(config: dict[str, Any], model_version_id: int) -> dict[str, Any]:
    value = config.get("lineage") if isinstance(config.get("lineage"), dict) else {}
    parent = int(value.get("parent_model_version_id") or config.get("parent_model_version_id") or 0)
    root = str(
        value.get("root_experiment_id")
        or value.get("root_model_version_id")
        or config.get("root_experiment_id")
        or model_version_id
    )
    return {
        "parent_model_version_id": parent or None,
        "supersedes_model_version_id": int(config.get("supersedes_model_version_id") or 0) or None,
        "root_experiment_id": root,
        "problem_family": str(value.get("hypothesis_family") or config.get("problem_family") or "unclassified"),
        "changed_fields": sorted({str(item) for item in (
            config.get("changed_fields")
            or ((config.get("evidence_contract") or {}).get("changed_fields") if isinstance(config.get("evidence_contract"), dict) else [])
            or []
        ) if str(item)}),
    }


def register_candidate_lifecycle(
    conn: Any,
    *,
    model_version_id: int,
    model_key: str,
    config: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Register a runnable candidate immediately, without granting production."""
    now = _now()
    lineage = _lineage(config, model_version_id)
    runtime_status = "observing"
    eligibility_status = "eligible" if bool(validation.get("production_eligible")) else "ineligible"
    candidate_type = _candidate_type(model_key, config)
    change_type = _change_type(config)
    conn.execute(
        """INSERT INTO candidate_lifecycle_records(
               model_version_id,candidate_type,runtime_status,eligibility_status,
               change_type,root_experiment_id,parent_model_version_id,
               supersedes_model_version_id,problem_family,changed_fields_json,
               observation_contract,production_change_forbidden,stop_reason,
               created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(model_version_id) DO NOTHING""",
        (
            int(model_version_id), candidate_type, runtime_status, eligibility_status,
            change_type, lineage["root_experiment_id"], lineage["parent_model_version_id"],
            lineage["supersedes_model_version_id"], lineage["problem_family"],
            json.dumps(lineage["changed_fields"], ensure_ascii=False), OBSERVATION_CONTRACT,
            1, "", now, now,
        ),
    )
    existing = conn.execute(
        "SELECT 1 AS present FROM candidate_lifecycle_events WHERE model_version_id=? LIMIT 1",
        (int(model_version_id),),
    ).fetchone()
    if not existing:
        conn.execute(
            """INSERT INTO candidate_lifecycle_events(
                   model_version_id,event_type,runtime_status,eligibility_status,
                   reason,evidence_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                int(model_version_id), "candidate_observation_started", runtime_status,
                eligibility_status, "candidate_created",
                json.dumps({
                    "candidate_type": candidate_type,
                    "change_type": change_type,
                    "observation_contract": OBSERVATION_CONTRACT,
                    "production_change_forbidden": True,
                }, ensure_ascii=False),
                now,
            ),
        )
    from app.services.candidate_simulation_ledger import initialize_candidate_account

    account = initialize_candidate_account(
        conn,
        model_version_id=int(model_version_id),
        model_key=str(model_key),
        candidate_version=str(config.get("version") or "") or str(model_version_id),
    )
    return {
        "model_version_id": int(model_version_id),
        "candidate_type": candidate_type,
        "runtime_status": runtime_status,
        "eligibility_status": eligibility_status,
        "change_type": change_type,
        "production_change_forbidden": True,
        "isolated_account_id": int(account["account_id"]),
    }


def get_candidate_lifecycle(model_version_id: int) -> dict[str, Any] | None:
    from app.db.store import _conn

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM candidate_lifecycle_records WHERE model_version_id=?",
            (int(model_version_id),),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        events = conn.execute(
            "SELECT * FROM candidate_lifecycle_events WHERE model_version_id=? ORDER BY id",
            (int(model_version_id),),
        ).fetchall()
    try:
        result["changed_fields"] = json.loads(result.pop("changed_fields_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["changed_fields"] = []
    result["events"] = [dict(item) for item in events]
    return result


def transition_candidate_lifecycle(
    model_version_id: int,
    *,
    runtime_status: str | None = None,
    eligibility_status: str | None = None,
    event_type: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one audited lifecycle transition without mutating version evidence."""
    if runtime_status is not None and runtime_status not in RUNTIME_STATUSES:
        raise ValueError(f"invalid candidate runtime status: {runtime_status}")
    if eligibility_status is not None and eligibility_status not in ELIGIBILITY_STATUSES:
        raise ValueError(f"invalid candidate eligibility status: {eligibility_status}")
    now = _now()
    from app.db.store import _conn

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM candidate_lifecycle_records WHERE model_version_id=?",
            (int(model_version_id),),
        ).fetchone()
        if not row:
            raise ValueError("candidate lifecycle record not found")
        current = dict(row)
        next_runtime = runtime_status or str(current["runtime_status"])
        next_eligibility = eligibility_status or str(current["eligibility_status"])
        production_forbidden = next_eligibility not in {"promoted"}
        stop_reason = reason if next_runtime in {"stopped", "archived"} else ""
        conn.execute(
            """UPDATE candidate_lifecycle_records
                  SET runtime_status=?,eligibility_status=?,
                      production_change_forbidden=?,stop_reason=?,updated_at=?
                WHERE model_version_id=?""",
            (
                next_runtime, next_eligibility, int(production_forbidden),
                stop_reason, now, int(model_version_id),
            ),
        )
        conn.execute(
            """INSERT INTO candidate_lifecycle_events(
                   model_version_id,event_type,runtime_status,eligibility_status,
                   reason,evidence_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                int(model_version_id), str(event_type), next_runtime,
                next_eligibility, str(reason),
                json.dumps(evidence or {}, ensure_ascii=False), now,
            ),
        )
    return get_candidate_lifecycle(int(model_version_id)) or {}


def reconcile_existing_candidates(*, limit: int = 1000) -> dict[str, Any]:
    """Enroll valid candidates and stop records whose version evidence rejects them."""
    from app.db.store import _conn

    with _conn() as conn:
        rows = conn.execute(
            """SELECT v.id,v.model_key,v.config_json,v.validation_json
                 FROM model_versions v
                 LEFT JOIN candidate_lifecycle_records c ON c.model_version_id=v.id
                WHERE v.is_active=0 AND c.model_version_id IS NULL
                  AND v.status IN ('candidate','shadow_observation','promoted_cooling')
                ORDER BY v.id LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        registered: list[int] = []
        for raw in rows:
            row = dict(raw)
            try:
                config = json.loads(row.get("config_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            try:
                validation = json.loads(row.get("validation_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                validation = {}
            register_candidate_lifecycle(
                conn,
                model_version_id=int(row["id"]),
                model_key=str(row.get("model_key") or ""),
                config=config if isinstance(config, dict) else {},
                validation=validation if isinstance(validation, dict) else {},
            )
            registered.append(int(row["id"]))
        current = conn.execute(
            """SELECT c.model_version_id,c.runtime_status,v.status,v.is_active,
                      v.validation_json
                 FROM candidate_lifecycle_records c
                 JOIN model_versions v ON v.id=c.model_version_id
                WHERE c.runtime_status='observing'
                ORDER BY c.model_version_id LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        missing_accounts = conn.execute(
            """SELECT c.model_version_id,v.model_key,v.version
                 FROM candidate_lifecycle_records c
                 JOIN model_versions v ON v.id=c.model_version_id
                 LEFT JOIN candidate_simulation_accounts a
                   ON a.model_version_id=c.model_version_id
                WHERE c.runtime_status='observing' AND a.id IS NULL
                ORDER BY c.model_version_id LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        from app.services.candidate_simulation_ledger import initialize_candidate_account

        initialized_accounts = []
        for raw in missing_accounts:
            item = dict(raw)
            account = initialize_candidate_account(
                conn,
                model_version_id=int(item["model_version_id"]),
                model_key=str(item.get("model_key") or ""),
                candidate_version=str(item.get("version") or item["model_version_id"]),
            )
            initialized_accounts.append(int(account["account_id"]))
    stopped: list[int] = []
    for raw in current:
        row = dict(raw)
        try:
            validation = json.loads(row.get("validation_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            validation = {}
        review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
        blockers = {
            str(item) for item in (
                validation.get("blocking_reasons")
                or validation.get("blocked_reasons")
                or []
            )
        }
        validation_status = str(validation.get("status") or "")
        reason = ""
        if bool(row.get("is_active")):
            reason = "promoted_to_active"
        elif str(row.get("status") or "") not in {"candidate", "shadow_observation", "promoted_cooling"}:
            reason = str(row.get("status") or "version_not_observable")
        elif validation_status in {"rejected", "quick_diagnostic_failed", "shadow_rejected", "rolled_back"}:
            reason = validation_status
        elif str(review.get("status") or "") == "rejected":
            reason = "human_review_rejected"
        elif blockers.intersection({"operator_rejected", "human_review_rejected", "superseded"}):
            reason = sorted(blockers.intersection({"operator_rejected", "human_review_rejected", "superseded"}))[0]
        if reason:
            transition_candidate_lifecycle(
                int(row["model_version_id"]),
                runtime_status="stopped",
                eligibility_status="promoted" if reason == "promoted_to_active" else "ineligible",
                event_type="candidate_observation_stopped",
                reason=reason,
                evidence={"version_status": row.get("status"), "validation_status": validation_status},
            )
            stopped.append(int(row["model_version_id"]))
    return {
        "ok": True,
        "schema_version": "alis_candidate_lifecycle_reconciliation_v1",
        "registered_count": len(registered),
        "model_version_ids": registered,
        "stopped_count": len(stopped),
        "stopped_model_version_ids": stopped,
        "initialized_account_ids": initialized_accounts,
        "policy": "inactive_candidates_observe_immediately_without_production_permission",
    }

"""Archive and isolate model evidence produced by retired protocols."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.db.store import _conn, _read_conn
from app.services.model_evaluation_contracts import SCHEMA_VERSION
from app.services.model_registry import models_root


MODEL_KEYS = (
    "main",
    "medium_horizon_lambdarank_v1",
    "capital_persistence_multitask_v1",
    "leader_lambdarank_v1",
    "event_impact_assessor_v2",
    "event_surprise_lgbm_v1",
    "narrative_transition_multitask_v1",
    "market_regime",
)
RETIRED_MODEL_KEYS = {"event_surprise_lgbm_v1"}
RETIRED_CONTRACTS = {
    "daily_cross_sectional_task_metric_and_strategy_ab_v3",
    "non_promotional_top_k_return_diagnostic_v1",
    "fixed_state_multiplier_research_diagnostic_only",
}
REMOVED_KEYS = {
    "economic_momentum_diagnostic",
    "shadow_strategy_diagnostic",
    "simple_momentum_net_lift",
    "model_minus_simple_momentum",
    "complex_model_improves_baseline",
}
LEGACY_TOP_LEVEL_KEYS = {
    "lift",
    "net_lift",
    "walk_forward_lift",
    "worst_window_net_lift",
    "validation_net_lift",
    "hit_rate",
    "rank_return_corr",
    "market_state_metrics",
    "market_state_evaluation_contract",
    "market_profile_metrics",
    "environment_metrics",
    "cost_after_backplay",
    "cost_evaluation_contract",
    "baseline_comparison_executed",
    "baseline_outperformed",
    "baseline_metrics",
    "walk_forward_contract",
    "transaction_cost_bps",
}
EMBEDDED_RETIRED_KEYS = REMOVED_KEYS | {
    "market_state_evaluation_contract",
    "cost_evaluation_contract",
    "walk_forward_contract",
}


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _contains_retired_contract(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return any(contract in text for contract in RETIRED_CONTRACTS)


def _uses_current_contract(
    metrics: dict[str, Any],
    validation: dict[str, Any],
) -> bool:
    return (
        str(metrics.get("model_evaluation_contract") or "") == SCHEMA_VERSION
        or str(metrics.get("evaluation_contract") or "") == SCHEMA_VERSION
        or str(validation.get("evaluation_contract") or "") == SCHEMA_VERSION
    )


def _remove_retired_fragments(value: Any) -> Any:
    if isinstance(value, list):
        return [_remove_retired_fragments(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key in REMOVED_KEYS:
            continue
        if isinstance(item, str) and item in RETIRED_CONTRACTS:
            continue
        cleaned[key] = _remove_retired_fragments(item)
    return cleaned


def _contains_legacy_evaluation_fragment(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_legacy_evaluation_fragment(item) for item in value)
    if not isinstance(value, dict):
        return isinstance(value, str) and value in RETIRED_CONTRACTS
    return any(
        key in EMBEDDED_RETIRED_KEYS
        or _contains_legacy_evaluation_fragment(item)
        for key, item in value.items()
    )


def _sanitize_embedded_evaluation(value: Any) -> Any:
    """Remove retired evaluation fragments from nested task evidence."""

    if isinstance(value, list):
        return [_sanitize_embedded_evaluation(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key in EMBEDDED_RETIRED_KEYS:
            continue
        if isinstance(item, str) and item in RETIRED_CONTRACTS:
            continue
        cleaned[key] = _sanitize_embedded_evaluation(item)
    return cleaned


def _sanitize_current_evaluation(value: dict[str, Any]) -> dict[str, Any]:
    cleaned = _remove_retired_fragments(value)
    for key in LEGACY_TOP_LEVEL_KEYS:
        cleaned.pop(key, None)
    windows = cleaned.get("walk_forward_evidence")
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            for key in (
                "net_lift",
                "market_state_metrics",
                "feature_permutation_effect",
            ):
                window.pop(key, None)
    task_oos = cleaned.get("task_native_oos")
    if (
        not isinstance(cleaned.get("task_baseline"), dict)
        and isinstance(task_oos, dict)
    ):
        task_metrics = cleaned.get("task_metrics")
        task_metrics = task_metrics if isinstance(task_metrics, dict) else {}
        cleaned["task_baseline"] = {
            "contract": str(task_metrics.get("contract") or ""),
            "comparison_executed": int(task_oos.get("window_count") or 0) > 0,
            "outperformed": bool(task_oos.get("baseline_outperformed")),
            "walk_forward_windows_outperformed": int(
                task_oos.get("baseline_outperformed_windows") or 0
            ),
        }
    coverage = cleaned.get("environment_coverage")
    if isinstance(coverage, dict):
        coverage["contract"] = (
            "point_in_time_four_parent_states_and_at_least_eight_"
            "of_twelve_profiles_coverage_v1"
        )
    return cleaned


def repair_current_model_evaluation_payloads() -> dict[str, Any]:
    """Idempotently sanitize current versions and copied runtime evidence."""

    raw = _raw_audit()
    updated: list[int] = []
    now_text = datetime.now().astimezone().isoformat()
    with _conn() as c:
        for row in raw["current_versions"]:
            metrics = _sanitize_current_evaluation(
                _loads(row.get("metrics_json"))
            )
            validation = _remove_retired_fragments(
                _loads(row.get("validation_json"))
            )
            if (
                str(row.get("model_key") or "") != "market_regime"
                and bool((metrics.get("task_native_oos") or {}).get("passed"))
                and not isinstance(metrics.get("strategy_impact_ab"), dict)
            ):
                prior_protocol = validation.get("training_protocol")
                task_baseline = metrics.get("task_baseline") or {}
                environment = metrics.get("environment_coverage") or {}
                gates = {
                    "data_ready": True,
                    "task_metric_passed": bool(
                        metrics.get("task_metric_passed")
                    ),
                    "current_task_native_evaluation_contract": True,
                    "task_native_walk_forward_oos": True,
                    "task_native_baseline_outperformed": bool(
                        task_baseline.get("outperformed")
                    ),
                    "task_baseline_comparison_executed": bool(
                        task_baseline.get("comparison_executed")
                    ),
                    "environment_coverage_complete": bool(
                        environment.get("complete")
                    ),
                    "training_protocol": bool(
                        (prior_protocol or {}).get("ok")
                    ),
                    "controlled_strategy_ab": False,
                    "human_review": False,
                    "live_observation_20_sessions": False,
                }
                validation = {
                    "status": "pending_strategy_ab",
                    "production_eligible": False,
                    "production_eligibility": {
                        "eligible": False,
                        "blocked_reasons": ["controlled_strategy_ab"],
                        "gates": gates,
                    },
                    "training_protocol": prior_protocol,
                }
            c.execute(
                """UPDATE model_versions
                   SET metrics_json=?,validation_json=?,updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                    json.dumps(validation, ensure_ascii=False, allow_nan=False),
                    now_text,
                    int(row["id"]),
                ),
            )
            updated.append(int(row["id"]))
        updated_runs: list[int] = []
        for row in raw["current_runs"]:
            metrics = _sanitize_current_evaluation(
                _loads(row.get("metrics_json"))
            )
            if metrics == _loads(row.get("metrics_json")):
                continue
            c.execute(
                """UPDATE model_train_runs
                   SET metrics_json=?,updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(
                        metrics,
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    now_text,
                    int(row["id"]),
                ),
            )
            updated_runs.append(int(row["id"]))
        updated_tasks: list[int] = []
        task_rows = c.execute(
            """SELECT id,payload_json,checkpoint_json,result_json
               FROM evolution_tasks ORDER BY id"""
        ).fetchall()
        for task_row in task_rows:
            task = dict(task_row)
            parsed = {
                key: _loads(task.get(key))
                for key in (
                    "payload_json",
                    "checkpoint_json",
                    "result_json",
                )
            }
            if not any(
                _contains_legacy_evaluation_fragment(value)
                for value in parsed.values()
            ):
                continue
            cleaned = {
                key: _sanitize_embedded_evaluation(value)
                for key, value in parsed.items()
            }
            c.execute(
                """UPDATE evolution_tasks
                   SET payload_json=?,checkpoint_json=?,result_json=?,
                       updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(
                        cleaned["payload_json"],
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    json.dumps(
                        cleaned["checkpoint_json"],
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    json.dumps(
                        cleaned["result_json"],
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    now_text,
                    int(task["id"]),
                ),
            )
            updated_tasks.append(int(task["id"]))
    artifact_updates: list[str] = []
    for path in raw["current_artifact_paths"]:
        artifact_path = Path(path)
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        cleaned = _sanitize_current_evaluation(payload)
        if isinstance(cleaned.get("metrics"), dict):
            cleaned["metrics"] = _sanitize_current_evaluation(
                cleaned["metrics"]
            )
        if isinstance(cleaned.get("validation"), dict):
            cleaned["validation"] = _remove_retired_fragments(
                cleaned["validation"]
            )
        if cleaned != payload:
            artifact_path.write_text(
                json.dumps(
                    cleaned,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            artifact_updates.append(str(artifact_path))
    return {
        "ok": True,
        "schema_version": "alis_current_model_evaluation_repair_v1",
        "updated_model_version_ids": updated,
        "updated_model_train_run_ids": updated_runs,
        "updated_evolution_task_ids": updated_tasks,
        "updated_artifact_paths": artifact_updates,
        "production_change_forbidden": True,
    }


def _archive_marker(
    *,
    archive_id: str,
    active: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "alis_model_evaluation_archive_marker_v1",
        "evaluation_status": "not_evaluated_under_current_contract",
        "current_evaluation_contract": SCHEMA_VERSION,
        "legacy_evaluation_removed": True,
        "runtime_compatibility_only": bool(active),
        "production_eligible": False,
        "archive_id": archive_id,
    }


def _raw_audit() -> dict[str, Any]:
    placeholders = ",".join("?" for _ in MODEL_KEYS)
    # This audit only reads and then decodes JSON outside the database call.
    # Keep the session autocommit/read-only so a large historical scan cannot
    # leave the business database idle in transaction.
    with _read_conn() as c:
        versions = [
            dict(row)
            for row in c.execute(
                f"""SELECT * FROM model_versions
                    WHERE model_key IN ({placeholders}) ORDER BY id""",
                MODEL_KEYS,
            ).fetchall()
        ]
        runs = [
            dict(row)
            for row in c.execute(
                "SELECT * FROM model_train_runs ORDER BY id"
            ).fetchall()
        ]
        tasks = [
            dict(row)
            for row in c.execute(
                """SELECT id,task_type,status,payload_json,result_json,
                          error,created_at,updated_at
                   FROM evolution_tasks
                   WHERE task_type IN (
                     'model_candidate_research',
                     'plan_model_autonomy',
                     'model_strategy_impact_validation'
                   )
                   OR result_json LIKE ? ORDER BY id""",
                ("%daily_cross_sectional_task_metric_and_strategy_ab_v3%",),
            ).fetchall()
        ]
    old_versions = []
    current_versions = []
    for row in versions:
        metrics = _loads(row.get("metrics_json"))
        validation = _loads(row.get("validation_json"))
        target = (
            current_versions
            if str(row.get("model_key") or "") not in RETIRED_MODEL_KEYS
            and _uses_current_contract(metrics, validation)
            else old_versions
        )
        target.append(row)
    old_runs = []
    current_runs = []
    for row in runs:
        metrics = _loads(row.get("metrics_json"))
        target = (
            current_runs
            if str(metrics.get("model_key") or "") not in RETIRED_MODEL_KEYS
            and str(metrics.get("model_evaluation_contract") or "")
            == SCHEMA_VERSION
            else old_runs
        )
        target.append(row)
    artifact_paths = sorted({
        str(row.get("artifact_path") or "")
        for row in old_versions
        if str(row.get("artifact_path") or "").endswith(".json")
        and Path(str(row.get("artifact_path"))).is_file()
    })
    current_artifact_paths = sorted({
        str(row.get("artifact_path") or "")
        for row in current_versions
        if str(row.get("artifact_path") or "").endswith(".json")
        and Path(str(row.get("artifact_path"))).is_file()
    })
    return {
        "versions": versions,
        "old_versions": old_versions,
        "current_versions": current_versions,
        "runs": runs,
        "old_runs": old_runs,
        "current_runs": current_runs,
        "tasks": tasks,
        "artifact_paths": artifact_paths,
        "current_artifact_paths": current_artifact_paths,
    }


def audit_legacy_model_evaluations() -> dict[str, Any]:
    raw = _raw_audit()
    return {
        "ok": True,
        "schema_version": "alis_legacy_model_evaluation_audit_v1",
        "execute": False,
        "current_contract": SCHEMA_VERSION,
        "old_model_version_count": len(raw["old_versions"]),
        "current_model_version_count": len(raw["current_versions"]),
        "old_train_run_count": len(raw["old_runs"]),
        "current_train_run_count": len(raw["current_runs"]),
        "affected_evolution_task_count": len(raw["tasks"]),
        "old_artifact_json_count": len(raw["artifact_paths"]),
        "current_artifact_json_count": len(raw["current_artifact_paths"]),
        "active_runtime_bindings_preserved": [
            {
                "id": int(row["id"]),
                "model_key": str(row.get("model_key") or ""),
                "version": str(row.get("version") or ""),
            }
            for row in raw["old_versions"]
            if int(row.get("is_active") or 0)
        ],
    }


def _archive_file(path: Path, root: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    target = root / "artifacts" / digest[:2] / f"{digest}-{path.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return {
        "source": str(path),
        "archive": str(target),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def _scrub_artifact(path: Path, marker: dict[str, Any]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    cleaned = _remove_retired_fragments(payload)
    if "metrics" in cleaned:
        cleaned["metrics"] = dict(marker)
    if "validation" in cleaned:
        cleaned["validation"] = dict(marker)
    cleaned["evaluation_archive"] = dict(marker)
    path.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return True


def execute_legacy_model_evaluation_archive() -> dict[str, Any]:
    raw = _raw_audit()
    now = datetime.now().astimezone()
    archive_id = now.strftime("model-evaluation-%Y%m%dT%H%M%S%z")
    archive_root = (
        models_root().parent
        / "model_evaluation_archives"
        / archive_id
    )
    archive_root.mkdir(parents=True, exist_ok=False)
    artifact_records = [
        _archive_file(Path(path), archive_root)
        for path in [
            *raw["artifact_paths"],
            *raw["current_artifact_paths"],
        ]
    ]
    archive_payload = {
        "schema_version": "alis_model_evaluation_archive_v1",
        "archive_id": archive_id,
        "created_at": now.isoformat(),
        "current_contract": SCHEMA_VERSION,
        "model_versions": raw["versions"],
        "model_train_runs": raw["runs"],
        "evolution_tasks": raw["tasks"],
        "artifacts": artifact_records,
    }
    archive_json = json.dumps(
        archive_payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        allow_nan=False,
    ).encode("utf-8")
    archive_path = archive_root / "database_evidence.json.gz"
    with gzip.open(archive_path, "wb") as handle:
        handle.write(archive_json)
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    old_ids = [int(row["id"]) for row in raw["old_versions"]]
    old_run_ids = [int(row["id"]) for row in raw["old_runs"]]
    now_text = now.isoformat()
    with _conn() as c:
        for row in raw["old_versions"]:
            active = bool(int(row.get("is_active") or 0))
            marker = _archive_marker(archive_id=archive_id, active=active)
            c.execute(
                """UPDATE model_versions
                   SET status=?,metrics_json=?,validation_json=?,updated_at=?
                   WHERE id=?""",
                (
                    str(row.get("status") or "active")
                    if active
                    else "evaluation_archived",
                    json.dumps(marker, ensure_ascii=False),
                    json.dumps(marker, ensure_ascii=False),
                    now_text,
                    int(row["id"]),
                ),
            )
        for row in raw["current_versions"]:
            metrics = _sanitize_current_evaluation(
                _loads(row.get("metrics_json"))
            )
            validation = _remove_retired_fragments(
                _loads(row.get("validation_json"))
            )
            c.execute(
                """UPDATE model_versions
                   SET metrics_json=?,validation_json=?,updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                    json.dumps(validation, ensure_ascii=False, allow_nan=False),
                    now_text,
                    int(row["id"]),
                ),
            )
        if old_run_ids:
            placeholders = ",".join("?" for _ in old_run_ids)
            c.execute(
                f"DELETE FROM model_train_runs WHERE id IN ({placeholders})",
                tuple(old_run_ids),
            )
        for row in raw["tasks"]:
            c.execute(
                """UPDATE evolution_tasks
                   SET result_json=?,updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(
                        {
                            "ok": False,
                            "status": (
                                "retired_legacy_model_candidate_protocol"
                                if str(row.get("task_type") or "")
                                == "model_candidate_research"
                                else "legacy_model_evaluation_evidence_archived"
                            ),
                            "archive_id": archive_id,
                            "current_contract": SCHEMA_VERSION,
                            "production_change_forbidden": True,
                        },
                        ensure_ascii=False,
                    ),
                    now_text,
                    int(row["id"]),
                ),
            )

    scrubbed_old = sum(
        _scrub_artifact(
            Path(path),
            _archive_marker(archive_id=archive_id, active=False),
        )
        for path in raw["artifact_paths"]
    )
    scrubbed_current = 0
    for path in raw["current_artifact_paths"]:
        artifact_path = Path(path)
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        cleaned = _sanitize_current_evaluation(payload)
        if isinstance(cleaned.get("metrics"), dict):
            cleaned["metrics"] = _sanitize_current_evaluation(
                cleaned["metrics"]
            )
        if isinstance(cleaned.get("validation"), dict):
            cleaned["validation"] = _remove_retired_fragments(
                cleaned["validation"]
            )
        if cleaned != payload:
            artifact_path.write_text(
                json.dumps(
                    cleaned,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            scrubbed_current += 1
    return {
        "ok": True,
        "schema_version": "alis_model_evaluation_archive_execution_v1",
        "archive_id": archive_id,
        "archive_path": str(archive_path),
        "archive_sha256": archive_sha256,
        "archived_model_version_ids": old_ids,
        "deleted_old_train_run_ids": old_run_ids,
        "scrubbed_old_artifact_count": scrubbed_old,
        "scrubbed_current_artifact_count": scrubbed_current,
        "affected_evolution_task_count": len(raw["tasks"]),
        "active_runtime_bindings_preserved": [
            int(row["id"])
            for row in raw["old_versions"]
            if int(row.get("is_active") or 0)
        ],
        "production_change_forbidden": True,
    }

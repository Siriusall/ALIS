"""Immutable ALIS system releases built from existing version authorities."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn


RELEASE_SCHEMA_VERSION = "alis_software_release_manifest_v2"
DECISION_MANIFEST_SCHEMA_VERSION = "alis_decision_version_manifest_v3"
SIM_SCHEMA_HEAD = "sim_0017_pub_probe_unique"
DATA_SCHEMA_HEAD = "data_0016_perf_indices"
REPO_ROOT = Path(__file__).resolve().parents[3]

CONTRACT_CATALOG: dict[str, dict[str, Any]] = {
    "contract.decision_data": {"version": "alis_decision_data_contract_v3", "critical": True},
    "contract.market_ingestion": {"version": "alis_market_progress_aware_hedged_checkpoint_v3", "critical": True},
    "contract.market_field_lineage": {"version": "alis_market_field_lineage_v1", "critical": True},
    "contract.decision_manifest": {"version": DECISION_MANIFEST_SCHEMA_VERSION, "critical": True},
    "contract.point_in_time": {"version": "alis_market_observation_clock_v1", "critical": True},
    "contract.execution": {"version": "alis_t1_execution_safety_v2", "critical": True},
    "contract.unified_decision": {"version": "alis_unified_daily_decision_v2", "critical": True},
    "contract.oos_hard_data": {"version": "alis_progressive_oos_v5", "critical": False},
    "contract.sealed_holdout": {"version": "alis_sealed_strategy_holdout_v4_immutable_observation", "critical": False},
}
COMPATIBILITY_RULES = (
    ("contract.market_ingestion", "contract.decision_data", "exact_version", "alis_decision_data_contract_v3"),
    ("contract.market_field_lineage", "contract.decision_data", "exact_version", "alis_decision_data_contract_v3"),
    ("contract.decision_manifest", "contract.execution", "exact_version", "alis_t1_execution_safety_v2"),
)

SAFE_CONFIG_KEYS = (
    "APP_ENV", "ALIS_DAILY_UPDATE_HOUR", "ALIS_DAILY_UPDATE_MINUTE",
    "ALIS_DAILY_UPDATE_INTERVAL_SEC", "MARKET_BULK_HEDGE_MIN_SECONDS",
    "MARKET_BULK_HEDGE_MAX_SECONDS", "MARKET_BULK_HEDGE_ADVANTAGE_MS",
    "MARKET_BULK_HARD_TIMEOUT_SECONDS", "MARKET_PROVIDER_CIRCUIT_FAILURES",
    "EVOLUTION_RESEARCH_CUTOFF_HOUR", "EVOLUTION_RESEARCH_CUTOFF_MINUTE",
)
AUTO_ACTIVATE_ENABLED = str(os.getenv("ALIS_SYSTEM_RELEASE_AUTO_ACTIVATE", "1")).lower() in {
    "1", "true", "yes", "on",
}
_PROCESS_GIT_STATE: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def git_state() -> dict[str, Any]:
    override = str(os.getenv("ALIS_BUILD_GIT_SHA") or "").strip()
    try:
        commit = override or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()) if not override else False
        return {"commit": commit, "branch": branch, "dirty": dirty, "available": True}
    except (OSError, subprocess.SubprocessError):
        return {"commit": override, "branch": "", "dirty": False, "available": bool(override)}


def process_git_state() -> dict[str, Any]:
    """Freeze the code identity actually imported by this backend process."""
    global _PROCESS_GIT_STATE
    if _PROCESS_GIT_STATE is None:
        _PROCESS_GIT_STATE = dict(git_state())
    return dict(_PROCESS_GIT_STATE)


def _database_revision(conn: Any, expected: str) -> str:
    try:
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str((dict(row) if row else {}).get("version_num") or "")
    except Exception:
        return ""


def database_revisions() -> dict[str, str]:
    with business_conn() as conn:
        sim = _database_revision(conn, SIM_SCHEMA_HEAD)
    with data_conn() as conn:
        data = _database_revision(conn, DATA_SCHEMA_HEAD)
    return {"sim": sim, "data": data}


def _safe_config() -> dict[str, str]:
    return {key: str(os.getenv(key) or "") for key in SAFE_CONFIG_KEYS}


def _software_component_payloads() -> dict[str, dict[str, Any]]:
    """Collect release inputs without touching mutable autonomy authority."""
    git = process_git_state()
    revisions = database_revisions()
    payloads: dict[str, dict[str, Any]] = {
        "code.backend": {"version": git.get("commit") or "unknown", **git},
        "database.sim": {"version": revisions["sim"] or "unversioned", "expected": SIM_SCHEMA_HEAD},
        "database.data": {"version": revisions["data"] or "unversioned", "expected": DATA_SCHEMA_HEAD},
        "config.runtime": {"version": _hash(_safe_config())[:16], "values": _safe_config()},
    }
    for key, value in CONTRACT_CATALOG.items():
        payloads[key] = dict(value)
    return payloads


def build_release_manifest() -> dict[str, Any]:
    # A software release is deliberately independent from mutable decision
    # authority.  Champion/controller/model/factor bindings are frozen by the
    # per-decision manifest; including them here made every autonomous research
    # change look like a code deployment and could block an otherwise safe
    # restart indefinitely.
    components = {
        key: value
        for key, value in _software_component_payloads().items()
        if key.startswith(("code.", "database.", "config.", "contract."))
    }
    manifest = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "components": {key: components[key] for key in sorted(components)},
        "simulation_only": True,
        "authority_scope": "software_only",
    }
    manifest["manifest_hash"] = _hash(manifest)
    return manifest


def validate_release_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    components = dict(manifest.get("components") or {})
    compatibility = {}
    for component, dependency, _constraint, required_version in COMPATIBILITY_RULES:
        compatibility[f"{component}->{dependency}"] = (
            str((components.get(dependency) or {}).get("version") or "") == required_version
        )
    gates = {
        "manifest_hash": str(manifest.get("manifest_hash") or "") == _hash({
            key: value for key, value in manifest.items() if key != "manifest_hash"
        }),
        "git_available": bool((components.get("code.backend") or {}).get("available")),
        "git_clean": not bool((components.get("code.backend") or {}).get("dirty")),
        "sim_db_at_head": str((components.get("database.sim") or {}).get("version") or "") == SIM_SCHEMA_HEAD,
        "data_db_at_head": str((components.get("database.data") or {}).get("version") or "") == DATA_SCHEMA_HEAD,
        "required_contracts_present": all(key in components for key in CONTRACT_CATALOG),
        "component_compatibility": all(compatibility.values()),
        "simulation_only": manifest.get("simulation_only") is True,
    }
    failed = [key for key, passed in gates.items() if not passed]
    return {
        "ok": not failed, "schema_version": "alis_system_release_validation_v1",
        "gates": gates, "compatibility": compatibility, "failed_gates": failed,
    }


def _component_type(key: str) -> str:
    return key.split(".", 1)[0]


def create_release(*, actor: str = "system", reason: str = "observed_system_state") -> dict[str, Any]:
    manifest = build_release_manifest()
    validation = validate_release_manifest(manifest)
    manifest_hash = str(manifest["manifest_hash"])
    now = _now()
    with business_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM system_releases WHERE manifest_hash=?", (manifest_hash,),
        ).fetchone()
        if existing:
            return _release_row(dict(existing), conn=conn)
        release_key = f"alis-{datetime.now().astimezone():%Y%m%d-%H%M%S}-{manifest_hash[:8]}"
        components = dict(manifest["components"])
        component_ids: dict[str, int] = {}
        for key, payload in components.items():
            component_type = _component_type(key)
            conn.execute(
                """INSERT INTO system_components
                   (component_key,component_type,name,decision_critical,native_registry,description,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(component_key) DO UPDATE SET
                   component_type=excluded.component_type,name=excluded.name,
                   decision_critical=excluded.decision_critical,native_registry=excluded.native_registry,
                   description=excluded.description,updated_at=excluded.updated_at""",
                (key, component_type, key, 1 if (CONTRACT_CATALOG.get(key) or {}).get("critical") else 0,
                 str(payload.get("native_table") or ""), "ALIS system release component", now, now),
            )
            version = str(payload.get("version") or payload.get("bindings_hash") or "unknown")
            content_hash = _hash(payload)
            row = conn.execute(
                """INSERT INTO system_component_versions
                   (component_key,version,content_hash,native_table,native_id,git_commit,artifact_uri,status,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(component_key,version,content_hash) DO UPDATE SET
                   evidence_json=excluded.evidence_json RETURNING id""",
                (key, version, content_hash, str(payload.get("native_table") or ""),
                 int(payload.get("native_id") or 0) or None,
                 str((components.get("code.backend") or {}).get("commit") or ""), "", "registered",
                 _canonical(payload), now),
            ).fetchone()
            component_ids[key] = int(dict(row)["id"])
        for component, dependency, constraint_type, required_version in COMPATIBILITY_RULES:
            conn.execute(
                """INSERT INTO system_component_dependencies
                   (component_version_id,dependency_component_key,constraint_type,constraint_json,required,reason,created_at)
                   SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (
                     SELECT 1 FROM system_component_dependencies WHERE component_version_id=?
                     AND dependency_component_key=? AND constraint_type=? AND constraint_json=?)""",
                (component_ids[component], dependency, constraint_type,
                 _canonical({"version": required_version}), 1, "release compatibility contract", now,
                 component_ids[component], dependency, constraint_type, _canonical({"version": required_version})),
            )
        git = components["code.backend"]
        revisions = {"sim": components["database.sim"], "data": components["database.data"]}
        config_hash = _hash(components["config.runtime"])
        release = conn.execute(
            """INSERT INTO system_releases
               (release_key,status,manifest_hash,manifest_json,git_commit,git_dirty,sim_db_revision,
                data_db_revision,config_hash,validation_status,validation_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (release_key, "validated" if validation["ok"] else "blocked", manifest_hash, _canonical(manifest),
             str(git.get("commit") or ""), 1 if git.get("dirty") else 0,
             str(revisions["sim"].get("version") or ""), str(revisions["data"].get("version") or ""),
             config_hash, "passed" if validation["ok"] else "failed", _canonical(validation), now),
        ).fetchone()
        release_id = int(dict(release)["id"])
        for key, version_id in component_ids.items():
            conn.execute(
                """INSERT INTO system_release_components
                   (release_id,component_key,component_version_id,role,required,frozen_payload_json)
                   VALUES(?,?,?,?,?,?)""",
                (release_id, key, version_id, "required", 1, _canonical(components[key])),
            )
        conn.execute(
            """INSERT INTO system_release_events(release_id,event_type,actor,reason,evidence_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (release_id, "created", actor, reason, _canonical(validation), now),
        )
        row = conn.execute("SELECT * FROM system_releases WHERE id=?", (release_id,)).fetchone()
        return _release_row(dict(row), conn=conn)


def activate_release(release_id: int, *, actor: str = "system", reason: str = "validated_activation") -> dict[str, Any]:
    now = _now()
    with business_conn() as conn:
        row = conn.execute("SELECT * FROM system_releases WHERE id=? FOR UPDATE", (int(release_id),)).fetchone()
        if not row:
            raise ValueError("release_not_found")
        item = dict(row)
        validation = json.loads(item.get("validation_json") or "{}")
        if item.get("validation_status") != "passed" or not validation.get("ok"):
            raise ValueError("release_validation_not_passed")
        current_manifest = build_release_manifest()
        if str(current_manifest.get("manifest_hash") or "") != str(item.get("manifest_hash") or ""):
            raise ValueError("release_binding_drift_before_activation")
        conn.execute(
            """UPDATE system_releases SET status='superseded',retired_at=?
               WHERE status='active' AND id<>?""", (now, int(release_id)),
        )
        conn.execute(
            """UPDATE system_releases SET status='active',activated_at=?,retired_at=NULL WHERE id=?""",
            (now, int(release_id)),
        )
        conn.execute(
            """INSERT INTO system_release_events(release_id,event_type,actor,reason,evidence_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (int(release_id), "activated", actor, reason, "{}", now),
        )
        active = conn.execute("SELECT * FROM system_releases WHERE id=?", (int(release_id),)).fetchone()
        return _release_row(dict(active), conn=conn)


def _release_row(item: dict[str, Any], *, conn: Any | None = None) -> dict[str, Any]:
    for field in ("manifest_json", "validation_json"):
        try:
            item[field.removesuffix("_json")] = json.loads(item.get(field) or "{}")
        except (TypeError, json.JSONDecodeError):
            item[field.removesuffix("_json")] = {}
    if conn is not None:
        rows = conn.execute(
            """SELECT component_key,component_version_id,role,required,frozen_payload_json
               FROM system_release_components WHERE release_id=? ORDER BY component_key""",
            (int(item["id"]),),
        ).fetchall()
        item["components"] = [dict(row) for row in rows]
    return item


def active_release() -> dict[str, Any] | None:
    with business_conn() as conn:
        row = conn.execute("SELECT * FROM system_releases WHERE status='active' LIMIT 1").fetchone()
        return _release_row(dict(row), conn=conn) if row else None


def list_releases(limit: int = 50) -> list[dict[str, Any]]:
    with business_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM system_releases ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [_release_row(dict(row), conn=conn) for row in rows]


def release_events(release_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with business_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM system_release_events WHERE release_id=?
               ORDER BY id DESC LIMIT ?""", (int(release_id), max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(row) for row in rows]


def release_diff(left_id: int, right_id: int) -> dict[str, Any]:
    with business_conn() as conn:
        rows = conn.execute(
            "SELECT id,release_key,manifest_json FROM system_releases WHERE id IN (?,?)",
            (int(left_id), int(right_id)),
        ).fetchall()
    by_id = {int(dict(row)["id"]): dict(row) for row in rows}
    if int(left_id) not in by_id or int(right_id) not in by_id:
        raise ValueError("release_not_found")
    manifests = {}
    for release_id, item in by_id.items():
        manifests[release_id] = json.loads(item.get("manifest_json") or "{}")
    left = dict(manifests[int(left_id)].get("components") or {})
    right = dict(manifests[int(right_id)].get("components") or {})
    changes = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) == right.get(key):
            continue
        changes.append({
            "component_key": key, "before": left.get(key), "after": right.get(key),
            "decision_critical": bool((CONTRACT_CATALOG.get(key) or {}).get("critical")) or
            key.startswith(("strategy.", "model.", "factor.", "database.", "code.")),
        })
    return {
        "schema_version": "alis_system_release_diff_v1", "left_id": int(left_id),
        "right_id": int(right_id), "changes": changes,
        "decision_critical_changes": sum(1 for item in changes if item["decision_critical"]),
    }


def rollback_release(release_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    # This never changes Git, schema, Champion or model bindings.  Operators must
    # first restore those authorities; only an exact observed match can activate.
    return activate_release(
        int(release_id), actor=actor, reason=f"rollback_after_external_authorities_restored:{reason}",
    )


def validate_frozen_release_for_execution(release_id: int, manifest_hash: str) -> dict[str, Any]:
    with business_conn() as conn:
        frozen_row = conn.execute("SELECT * FROM system_releases WHERE id=?", (int(release_id),)).fetchone()
        current_row = conn.execute("SELECT * FROM system_releases WHERE status='active' LIMIT 1").fetchone()
    if not frozen_row or not current_row:
        return {"ok": False, "reason": "release_not_found_or_no_active_release"}
    frozen = dict(frozen_row)
    current = dict(current_row)
    if str(frozen.get("manifest_hash") or "") != str(manifest_hash or ""):
        return {"ok": False, "reason": "frozen_release_hash_mismatch"}
    frozen_manifest = json.loads(frozen.get("manifest_json") or "{}")
    current_manifest = json.loads(current.get("manifest_json") or "{}")
    frozen_execution = ((frozen_manifest.get("components") or {}).get("contract.execution") or {}).get("version")
    current_execution = ((current_manifest.get("components") or {}).get("contract.execution") or {}).get("version")
    return {
        "ok": bool(frozen_execution) and frozen_execution == current_execution,
        "reason": "" if frozen_execution == current_execution else "execution_contract_incompatible",
        "frozen_release_id": int(release_id), "active_release_id": int(current["id"]),
        "execution_contract": frozen_execution,
    }


def release_status() -> dict[str, Any]:
    active = active_release()
    observed = build_release_manifest()
    drift = bool(active and str(active.get("manifest_hash") or "") != str(observed.get("manifest_hash") or ""))
    drift_components: list[str] = []
    if active and drift:
        frozen_components = dict((active.get("manifest") or {}).get("components") or {})
        observed_components = dict(observed.get("components") or {})
        drift_components = [
            key for key in sorted(set(frozen_components) | set(observed_components))
            if frozen_components.get(key) != observed_components.get(key)
        ]
    legacy_unlinked = {"daily_rebalance_plans": 0, "decision_snapshots": 0, "executions": 0}
    with business_conn() as conn:
        for table in legacy_unlinked:
            try:
                row = conn.execute(f"SELECT COUNT(*) count FROM {table} WHERE release_id IS NULL").fetchone()
                legacy_unlinked[table] = int(dict(row).get("count") or 0)
            except Exception:
                legacy_unlinked[table] = -1
    blocked_reason = ""
    if not active:
        blocked_reason = "no_active_system_release"
    elif drift:
        blocked_reason = "active_release_drift"
    repository = git_state()
    runtime_git = process_git_state()
    restart_required = bool(
        repository.get("available") and runtime_git.get("available")
        and str(repository.get("commit") or "") != str(runtime_git.get("commit") or "")
    )
    with business_conn() as conn:
        candidate_row = conn.execute(
            "SELECT * FROM system_releases WHERE status IN ('validated','blocked') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        event_row = conn.execute(
            "SELECT * FROM system_release_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "ok": bool(active) and not drift,
        "schema_version": "alis_system_release_status_v1",
        "active_release": active,
        "observed_manifest_hash": observed["manifest_hash"],
        "drift": drift,
        "drift_components": drift_components,
        "blocked_reason": blocked_reason,
        "blocking_new_formal_plans": not active or drift,
        "database_revisions": database_revisions(),
        "expected_database_revisions": {"sim": SIM_SCHEMA_HEAD, "data": DATA_SCHEMA_HEAD},
        "legacy_unlinked_records": legacy_unlinked,
        "governance": {
            "auto_activate_enabled": AUTO_ACTIVATE_ENABLED,
            "runtime_git": runtime_git,
            "repository_git": repository,
            "restart_required": restart_required,
            "latest_candidate": _release_row(dict(candidate_row)) if candidate_row else None,
            "latest_event": dict(event_row) if event_row else None,
        },
    }


def discover_release_change() -> dict[str, Any]:
    """Read-only detector for runtime bindings, repository changes and release drift."""
    active = active_release()
    observed = build_release_manifest()
    repository = git_state()
    runtime_git = process_git_state()
    restart_required = bool(
        repository.get("available") and runtime_git.get("available")
        and str(repository.get("commit") or "") != str(runtime_git.get("commit") or "")
    )
    changed = not active or str(active.get("manifest_hash") or "") != str(observed["manifest_hash"])
    return {
        "ok": True,
        "schema_version": "alis_system_release_change_discovery_v1",
        "changed": changed,
        "restart_required": restart_required,
        "active_release_id": int((active or {}).get("id") or 0) or None,
        "active_manifest_hash": str((active or {}).get("manifest_hash") or ""),
        "observed_manifest_hash": str(observed["manifest_hash"]),
        "runtime_commit": str(runtime_git.get("commit") or ""),
        "repository_commit": str(repository.get("commit") or ""),
        "repository_dirty": bool(repository.get("dirty")),
    }


def validate_release_preflight(release: dict[str, Any]) -> dict[str, Any]:
    """Run bounded live safety gates before deterministic activation."""
    from app.services.simulation_ledger_audit import audit_simulation_ledger

    stored_validation = release.get("validation") if isinstance(release.get("validation"), dict) else {}
    base = (
        stored_validation.get("manifest")
        if isinstance(stored_validation.get("manifest"), dict)
        else stored_validation
    )
    ledger = audit_simulation_ledger(persist=False)
    with business_conn() as conn:
        inflight = conn.execute(
            "SELECT COUNT(*) count FROM daily_rebalance_plans WHERE status='executing'"
        ).fetchone()
        frozen = conn.execute(
            "SELECT COUNT(*) count FROM daily_rebalance_plans WHERE status='planned'"
        ).fetchone()
    inflight_count = int(dict(inflight).get("count") or 0)
    frozen_count = int(dict(frozen).get("count") or 0)
    active = active_release()
    target_manifest = release.get("manifest") if isinstance(release.get("manifest"), dict) else {}
    current_manifest = (active or {}).get("manifest") if isinstance((active or {}).get("manifest"), dict) else {}
    target_execution = ((target_manifest.get("components") or {}).get("contract.execution") or {}).get("version")
    current_execution = ((current_manifest.get("components") or {}).get("contract.execution") or {}).get("version")
    execution_compatible = not active or target_execution == current_execution or frozen_count == 0
    gates = {
        "release_manifest_validation": bool(base.get("ok")),
        "simulation_ledger_consistent": bool(ledger.get("ok")) and str(ledger.get("status") or "") == "passed",
        "no_execution_in_progress": inflight_count == 0,
        "frozen_plan_execution_contract_compatible": execution_compatible,
        "runtime_matches_repository_commit": not discover_release_change().get("restart_required"),
        "repository_worktree_clean": not bool(git_state().get("dirty")),
    }
    return {
        "ok": all(gates.values()),
        "schema_version": "alis_system_release_preflight_v1",
        "gates": gates,
        "failed_gates": [key for key, passed in gates.items() if not passed],
        "evidence": {
            "ledger_status": ledger.get("status"),
            "cash_drift": ledger.get("cash_drift"),
            "position_drift_count": len(ledger.get("position_deltas") or []),
            "executing_plan_count": inflight_count,
            "frozen_plan_count": frozen_count,
            "current_execution_contract": current_execution,
            "target_execution_contract": target_execution,
            "autonomy_tasks_blocking": False,
            "blocking_scope": "executing_trade_and_frozen_execution_contract_only",
        },
    }


def run_release_governance_cycle(*, actor: str = "system_release_governance") -> dict[str, Any]:
    """Discover, validate and, when safe, activate one immutable system release."""
    discovery = discover_release_change()
    if discovery["restart_required"]:
        return {
            "ok": False, "status": "restart_required", "discovery": discovery,
            "production_change": False,
        }
    # A dirty worktree cannot produce a valid immutable release.  Stop before
    # creating a blocked database candidate; the discovery result itself is the
    # auditable evidence and avoids filling the release list with duplicates of
    # the same operator action.
    if discovery.get("repository_dirty"):
        return {
            "ok": False, "status": "worktree_dirty", "discovery": discovery,
            "production_change": False,
            "reason": "repository_worktree_must_be_clean_before_release",
        }
    if not discovery["changed"]:
        return {
            "ok": True, "status": "unchanged", "discovery": discovery,
            "active_release": active_release(), "production_change": False,
        }
    candidate = create_release(actor=actor, reason="automatic_change_discovery")
    preflight = validate_release_preflight(candidate)
    now = _now()
    with business_conn() as conn:
        conn.execute(
            """UPDATE system_releases SET status=?,validation_status=?,validation_json=? WHERE id=?""",
            (
                "validated" if preflight["ok"] else "blocked",
                "passed" if preflight["ok"] else "failed",
                _canonical({"ok": preflight["ok"], "manifest": candidate.get("validation") or {}, "preflight": preflight}),
                int(candidate["id"]),
            ),
        )
        conn.execute(
            """INSERT INTO system_release_events(release_id,event_type,actor,reason,evidence_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (int(candidate["id"]), "preflight_passed" if preflight["ok"] else "preflight_blocked",
             actor, "automatic_release_preflight", _canonical(preflight), now),
        )
    if not preflight["ok"]:
        return {
            "ok": False, "status": "candidate_blocked", "candidate": candidate,
            "preflight": preflight, "discovery": discovery, "production_change": False,
        }
    if not AUTO_ACTIVATE_ENABLED:
        return {
            "ok": True, "status": "candidate_validated", "candidate": candidate,
            "preflight": preflight, "discovery": discovery, "production_change": False,
        }
    activated = activate_release(
        int(candidate["id"]), actor=actor, reason="automatic_preflight_all_gates_passed",
    )
    return {
        "ok": True, "status": "activated", "active_release": activated,
        "preflight": preflight, "discovery": discovery, "production_change": True,
    }

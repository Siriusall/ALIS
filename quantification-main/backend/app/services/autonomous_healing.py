"""Bounded remediation for the daily autonomous cycle."""

from __future__ import annotations

from typing import Any


MAX_OOS_REPAIR_DATES = 5
_FAILED = {"failed", "blocked", "error", "rejected", "repair_partial"}
_DEGRADED = {"partial", "degraded", "warning", "attention"}
_OBSERVING = {"insufficient_observations", "observing", "not_triggered", "skipped_not_configured", "unchanged"}


def classify_step_health(step: dict[str, Any]) -> dict[str, str]:
    """Normalise heterogeneous service results into one health contract."""
    if step.get("error") or not bool(step.get("ok", True)):
        return {"status": "failed", "reason": str(step.get("error") or "module_returned_not_ok")}
    result = step.get("result") if isinstance(step.get("result"), dict) else {}
    status = str(result.get("status") or "").lower()
    if status in _FAILED:
        return {"status": "failed", "reason": status}
    if status in _DEGRADED:
        return {"status": "degraded", "reason": status}
    if status in _OBSERVING:
        return {"status": "observing", "reason": status}
    return {"status": "healthy", "reason": status or "completed"}


def _repair_hard_data() -> dict[str, Any]:
    from app.data_platform.services.oos_hard_data import audit_oos_hard_data, repair_oos_hard_data_batch

    detailed = audit_oos_hard_data(include_dates=True)
    gaps = {
        str(item.get("trade_date") or ""): item
        for item in detailed.get("dates") or []
        if isinstance(item, dict) and not bool(item.get("eligible")) and str(item.get("trade_date") or "")
    }
    dates = sorted(gaps)[:MAX_OOS_REPAIR_DATES]
    if not dates:
        return {"kind": "oos_hard_data", "status": "blocked", "reason": "no_explicit_repairable_dates"}
    repair = repair_oos_hard_data_batch(dates, date_gaps=gaps)
    after = audit_oos_hard_data(include_dates=False)
    return {
        "kind": "oos_hard_data",
        "status": "repaired" if repair.get("ok") and after.get("status") == "ready" else "degraded",
        "dates": dates,
        "repair": repair,
        "verification": after,
    }


def _factor_repair_needed(step: dict[str, Any]) -> bool:
    if classify_step_health(step)["status"] == "failed":
        return True
    result = step.get("result") if isinstance(step.get("result"), dict) else {}
    materialization = result.get("materialization") if isinstance(result.get("materialization"), dict) else {}
    point_in_time = materialization.get("point_in_time") if isinstance(materialization.get("point_in_time"), dict) else {}
    return point_in_time.get("passed") is False


def _refresh_latest_decision_data() -> dict[str, Any]:
    """Schedule the existing idempotent close-data refresh, never mutate data directly."""
    from app.data_platform.services.daily_update import daily_update_status, start_daily_update

    before = daily_update_status()
    started = start_daily_update()
    after = daily_update_status()
    return {
        "kind": "latest_decision_data",
        "status": "refresh_scheduled" if bool(started.get("ok")) else "refresh_already_running" if started.get("reason") == "already_running" else "refresh_failed",
        "before": before,
        "start": started,
        "after": after,
        "verification": {
            "refresh_running": bool(after.get("running")),
            "latest_decision_ready_trade_date": str(after.get("latest_decision_ready_trade_date") or ""),
        },
    }


def run_autonomous_self_healing(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply only deterministic, idempotent repairs; quarantine all else."""
    by_name = {str(step.get("name") or ""): step for step in steps}
    components = {name: classify_step_health(step) for name, step in by_name.items()}
    actions: list[dict[str, Any]] = []
    hard_data = by_name.get("hard_data_gate")
    if hard_data and components["hard_data_gate"]["status"] == "failed":
        action = _repair_hard_data()
        actions.append(action)
        if str((action.get("verification") or {}).get("status") or "") == "ready":
            components["hard_data_gate"] = {"status": "recovered", "reason": "bounded_repair_verified"}

    decision_data = by_name.get("decision_data_gate")
    if decision_data and components["decision_data_gate"]["status"] in {"failed", "degraded"}:
        actions.append(_refresh_latest_decision_data())

    factor_step = by_name.get("factor_daily_cycle")
    hard_data_ready = components.get("hard_data_gate", {}).get("status") in {"healthy", "recovered"}
    if factor_step and hard_data_ready and _factor_repair_needed(factor_step):
        from app.services.factor_platform import run_factor_daily_cycle

        try:
            retry = run_factor_daily_cycle()
            passed = bool(((retry.get("materialization") or {}).get("point_in_time") or {}).get("passed"))
            actions.append({
                "kind": "factor_daily_cycle",
                "status": "recovered" if passed else "degraded",
                "retry": retry,
                "verification": {"point_in_time_passed": passed},
            })
            if passed:
                components["factor_daily_cycle"] = {"status": "recovered", "reason": "idempotent_retry_verified"}
        except Exception as exc:  # noqa: BLE001
            actions.append({"kind": "factor_daily_cycle", "status": "failed", "reason": str(exc)[:500]})

    ledger = by_name.get("simulation_ledger_audit")
    if ledger and components["simulation_ledger_audit"]["status"] == "failed":
        actions.append({
            "kind": "simulation_ledger",
            "status": "quarantined",
            "reason": "ledger_drift_is_not_auto_repairable; execution_gate_blocks_new_operations",
        })

    unresolved = [
        {"module": name, **health}
        for name, health in components.items()
        if health["status"] in {"failed", "degraded"}
    ]
    quarantined = any(action.get("status") == "quarantined" for action in actions)
    return {
        "ok": not any(item["status"] == "failed" for item in unresolved),
        "schema_version": "alis_autonomous_healing_v1",
        "status": "blocked" if quarantined or any(item["status"] == "failed" for item in unresolved) else "degraded" if unresolved else "healthy",
        "components": components,
        "actions": actions,
        "unresolved": unresolved,
        "policy": "bounded_oos_repair_idempotent_factor_retry_and_latest_decision_refresh_schedule_only; ledger_and_unknown_failures_are_quarantined",
    }

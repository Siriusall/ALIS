from __future__ import annotations

from datetime import datetime
import json
import os
import threading
from typing import Any, Callable

from app.core.error_evidence import error_evidence
from app.db.store import _conn


_STEP_TIMEOUT_SECONDS = max(15, min(int(os.getenv("ALIS_AUTONOMY_STEP_TIMEOUT_SECONDS", "120")), 900))
_LONG_STEP_TIMEOUT_SECONDS = max(
    _STEP_TIMEOUT_SECONDS,
    min(int(os.getenv("ALIS_AUTONOMY_LONG_STEP_TIMEOUT_SECONDS", "3600")), 7200),
)
_LONG_RUNNING_STEPS = frozenset({"factor_daily_cycle", "plan_model_autonomy"})
_running_steps: set[str] = set()
_running_steps_lock = threading.Lock()


def _run_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = datetime.now().astimezone()
    with _running_steps_lock:
        if name in _running_steps:
            return {
                "name": name, "ok": False, "duration_seconds": 0.0,
                "started_at": started.isoformat(), "finished_at": started.isoformat(),
                "error": "previous_autonomy_step_still_running",
                "health": {"status": "degraded", "reason": "previous_autonomy_step_still_running"},
            }
        _running_steps.add(name)
    box: dict[str, Any] = {}

    def invoke() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # returned on the owning bus thread
            box["error"] = exc
        finally:
            with _running_steps_lock:
                _running_steps.discard(name)

    worker = threading.Thread(target=invoke, daemon=True, name=f"autonomy-step:{name}")
    worker.start()
    timeout_seconds = (
        _LONG_STEP_TIMEOUT_SECONDS
        if name in _LONG_RUNNING_STEPS
        else _STEP_TIMEOUT_SECONDS
    )
    worker.join(timeout_seconds)
    if worker.is_alive():
        step = {
            "name": name, "ok": False,
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "started_at": started.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
            "error": f"step_timeout_after_{timeout_seconds}s",
            "result": {"status": "timed_out", "timeout_seconds": timeout_seconds},
        }
        from app.services.autonomous_healing import classify_step_health

        step["health"] = classify_step_health(step)
        return step
    try:
        if "error" in box:
            raise box["error"]
        result = box.get("result")
        if not isinstance(result, dict):
            raise TypeError("autonomy_step_must_return_dict")
        step = {
            "name": name,
            "ok": bool(result.get("ok", True)),
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "started_at": started.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
            "result": result,
        }
        from app.services.autonomous_healing import classify_step_health

        step["health"] = classify_step_health(step)
        return step
    except Exception as exc:
        step = {
            "name": name,
            "ok": False,
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "started_at": started.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
            "error": str(exc)[:2000],
        }
        from app.services.autonomous_healing import classify_step_health

        step["health"] = classify_step_health(step)
        return step


def _skip_experimental_step(name: str, gate: dict[str, Any]) -> dict[str, Any]:
    """Record a safe no-op when current data cannot support new research evidence."""
    now = datetime.now().astimezone().isoformat()
    step = {
        "name": name,
        "ok": True,
        "duration_seconds": 0.0,
        "started_at": now,
        "finished_at": now,
        "result": {
            "ok": True,
            "status": "observing",
            "reason": "skipped_latest_decision_data_gate",
            "data_gate": {
                "status": gate.get("status"),
                "experiment_ready": bool(gate.get("experiment_ready")),
                "issues": gate.get("issues") if isinstance(gate.get("issues"), list) else [],
            },
        },
    }
    from app.services.autonomous_healing import classify_step_health

    step["health"] = classify_step_health(step)
    return step


def _skip_decision_step(name: str, gate: dict[str, Any]) -> dict[str, Any]:
    """Record a safe no-op when production decision data is not ready."""
    step = _skip_experimental_step(name, gate)
    step["result"]["reason"] = "skipped_decision_data_gate"
    step["result"]["data_gate"]["decision_ready"] = bool(gate.get("decision_ready"))
    return step


def _owner_summary(steps: list[dict[str, Any]]) -> dict[str, Any]:
    from app.services.production_strategy import champion_execution_gate

    by_name = {str(step["name"]): step for step in steps}
    profit = ((by_name.get("profit_and_portfolio") or {}).get("result") or {})
    attribution = profit.get("attribution") if isinstance(profit.get("attribution"), dict) else {}
    portfolio_candles = profit.get("portfolio_candles") if isinstance(profit.get("portfolio_candles"), dict) else {}
    benchmark_comparison = (
        portfolio_candles.get("latest_comparison")
        if isinstance(portfolio_candles.get("latest_comparison"), dict) else {}
    )
    portfolio = profit.get("portfolio_optimization") if isinstance(profit.get("portfolio_optimization"), dict) else {}
    rebalance = profit.get("rebalance_plan") if isinstance(profit.get("rebalance_plan"), dict) else {}
    model_monitor = profit.get("model_monitor") if isinstance(profit.get("model_monitor"), dict) else {}
    degraded_models = [
        {
            "model_key": str(item.get("model_key") or ""),
            "weight_multiplier": item.get("weight_multiplier"),
            "reason": str(item.get("reason") or ""),
        }
        for item in (model_monitor.get("items") or [])
        if str(item.get("status") or "") == "degraded"
    ]
    model = ((by_name.get("plan_model_autonomy") or {}).get("result") or {})
    strategy = ((by_name.get("strategy_policy_candidates") or {}).get("result") or {})
    strategy_maturity = ((by_name.get("strategy_policy_maturity") or {}).get("result") or {})
    cost = ((by_name.get("execution_cost_calibration") or {}).get("result") or {})
    corporate_actions = ((by_name.get("simulation_corporate_actions") or {}).get("result") or {})
    corporate_materialization = (
        corporate_actions.get("materialization")
        if isinstance(corporate_actions.get("materialization"), dict) else {}
    )
    ledger = ((by_name.get("simulation_ledger_audit") or {}).get("result") or {})
    performance_review = ((by_name.get("periodic_performance_review") or {}).get("result") or {})
    data_quality_governance = ((by_name.get("review_data_quality_governance") or {}).get("result") or {})
    llm_research = ((by_name.get("llm_research_agent") or {}).get("result") or {})
    llm_governance = ((by_name.get("llm_research_governance") or {}).get("result") or {})
    llm_router = ((by_name.get("llm_proposal_router") or {}).get("result") or {})
    llm_data_operations = ((by_name.get("llm_data_operations") or {}).get("result") or {})
    incident_supervisor = ((by_name.get("llm_incident_supervisor") or {}).get("result") or {})
    healing = ((by_name.get("autonomous_self_healing") or {}).get("result") or {})
    cost_active = cost.get("active") if isinstance(cost.get("active"), dict) else {}
    hard_data = ((by_name.get("hard_data_gate") or {}).get("result") or {})
    decision_data = ((by_name.get("decision_data_gate") or {}).get("result") or {})
    optimizer = portfolio.get("optimizer") if isinstance(portfolio.get("optimizer"), dict) else {}
    rebalance_items = list(portfolio.get("rebalance_proposal") or portfolio.get("proposal") or [])
    rebalance_actions = [action for action in rebalance_items if int(action.get("planned_quantity") or 0) > 0]
    champion_gate = champion_execution_gate()
    anomalies = [
        {
            "module": str(step["name"]),
            "message": str(step.get("error") or ((step.get("health") or {}).get("reason")) or "模块返回未通过"),
        }
        for step in steps
        if str(step.get("name") or "") not in {"autonomous_self_healing", "llm_incident_supervisor"}
        if str((step.get("health") or {}).get("status") or ("healthy" if step.get("ok") else "failed")) in {"failed", "degraded"}
    ]
    return {
        "schema_version": "alis_owner_daily_summary_v3",
        "generated_at": datetime.now().astimezone().isoformat(),
        "questions": {
            "today_rebalance": {
                "status": str(rebalance.get("status") or "generated"),
                "plan_date": rebalance.get("plan_date") or rebalance.get("trade_date"),
                "summary": rebalance.get("summary") or {},
                "proposal_count": len(rebalance_actions),
                "execution_authorized": bool(rebalance.get("execution_authorized")),
                "planned_execute_at": rebalance.get("planned_execute_at"),
                "planned_execute_date": rebalance.get("planned_execute_date"),
                "blocked_reason": rebalance.get("blocked_reason"),
                "actions": [_owner_rebalance_action(action) for action in rebalance_items],
            },
            "profit": {
                "daily_return_pct": attribution.get("latest_daily_return_pct"),
                "monthly_return_pct": attribution.get("latest_monthly_return_pct"),
                "unit": attribution.get("unit") or "percentage_points",
                "status": attribution.get("status"),
                "hit_rate": attribution.get("hit_rate"),
                "period_return": attribution.get("period_return"),
                "benchmark_return": (
                    benchmark_comparison.get("benchmark_return_pct")
                    if benchmark_comparison else attribution.get("benchmark_return")
                ),
                "excess_return": (
                    benchmark_comparison.get("excess_return_pct")
                    if benchmark_comparison else attribution.get("excess_return")
                ),
                "benchmark_period_start": (
                    portfolio_candles.get("series", [{}])[0].get("date")
                    if portfolio_candles.get("series") else None
                ),
                "benchmark_period_end": benchmark_comparison.get("date"),
                "total_fees": attribution.get("total_fees"),
                "trading_cost_contribution": (
                    (attribution.get("components") or {}).get("trading_cost")
                    if isinstance(attribution.get("components"), dict) else None
                ),
            },
            "system_changes": {
                "model_auto_promoted": int(model.get("auto_promoted_count") or 0),
                "model_auto_rejected": int(model.get("auto_rejected_count") or 0),
                "model_training_started": int(model.get("started_count") or 0),
                "model_degraded_count": len(degraded_models),
                "degraded_models": degraded_models,
                "strategy_candidates_created": int(strategy.get("created_count") or 0),
                "strategy_learning_candidate_count": int(strategy.get("learning_candidate_count") or 0),
                "strategy_candidates_re_evaluated": int((strategy_maturity.get("counts") or {}).get("re_evaluated") or 0),
                "strategy_candidates_ready_for_review": int((strategy_maturity.get("counts") or {}).get("ready_for_review") or 0),
                "champion_execution_gate": champion_gate,
                "portfolio_method": optimizer.get("selected_method") or optimizer.get("optimizer"),
                "execution_cost_version": cost_active.get("version"),
                "execution_cost_status": cost.get("status"),
                "execution_cost_observations": int(
                    ((cost.get("metrics") or {}).get("operation_count"))
                    if isinstance(cost.get("metrics"), dict) else 0
                ),
                "hard_data_status": hard_data.get("status"),
                "decision_data_status": decision_data.get("status"),
                "decision_data_experiment_ready": decision_data.get("experiment_ready"),
                "decision_data_issue_codes": [
                    str(item.get("code") or "")
                    for item in (decision_data.get("issues") or [])
                    if isinstance(item, dict) and str(item.get("code") or "")
                ],
                "corporate_actions_applied": len(corporate_materialization.get("applied") or []),
                "dividend_cash_applied": round(sum(
                    float(item.get("cash_amount") or 0)
                    for item in (corporate_materialization.get("applied") or [])
                ), 2),
                "ledger_status": ledger.get("status"),
                "ledger_cash_drift": ledger.get("cash_drift"),
                "ledger_position_drift_count": len(ledger.get("position_deltas") or []),
                "performance_review_status": performance_review.get("status"),
                "performance_learning_signal_count": int(performance_review.get("learning_signal_count") or 0),
                "performance_learning_signals": performance_review.get("learning_signals") or [],
                "data_quality_governance_status": data_quality_governance.get("status"),
                "data_quality_repair_count": len(((data_quality_governance.get("repair") or {}).get("dates") or [])),
                "llm_research_status": llm_research.get("status"),
                "llm_research_proposal_count": int(llm_research.get("proposal_count") or 0),
                "llm_research_assessed_count": int(llm_governance.get("assessed_count") or 0),
                "llm_research_routed_count": int(llm_router.get("routed_count") or 0),
                "llm_data_operations_status": llm_data_operations.get("status"),
                "llm_data_operations_action_count": len(((llm_data_operations.get("execution") or {}).get("actions") or [])),
                "llm_data_operations_escalation_count": len(((llm_data_operations.get("execution") or {}).get("escalations") or [])),
                "self_healing_status": healing.get("status"),
                "self_healing_action_count": len(healing.get("actions") or []),
                "self_healing_unresolved_count": len(healing.get("unresolved") or []),
            },
            "anomalies": anomalies,
            "incident_supervision": incident_supervisor,
        },
        "healthy": int(incident_supervisor.get("owner_action_required_count") or 0) == 0 if incident_supervisor else not anomalies,
    }


def run_system_autonomy_cycle() -> dict[str, Any]:
    """Run the existing deterministic modules as one auditable daily bus cycle."""
    from app.data_platform.services.oos_hard_data import audit_oos_hard_data
    from app.services.auto_pipeline import run_daily_maintenance
    from app.services.corporate_actions import run_corporate_action_cycle
    from app.services.execution_cost_calibration import calibrate_execution_cost_model
    from app.services.factor_platform import run_factor_daily_cycle
    from app.services.plan_model_auto_optimizer import run_auto_optimization_once
    from app.services.performance_reports import run_periodic_performance_review
    from app.services.review_data_quality_governance import run_review_data_quality_governance
    from app.services.research_data_assets import audit_research_data_assets
    from app.services.theme_revenue_exposure import materialize_theme_revenue_exposure
    from app.services.free_research_data import materialize_cn_ppi_price_proxies, materialize_financial_industry_series, materialize_futures_price_proxies
    from app.data_platform.services.data_center import sync_tushare_api_raw
    from app.services.official_policy_data import materialize_official_capacity_utilization, materialize_official_policy_supply, sync_official_policy_documents
    from app.services.caam_ocr_data import audit_caam_ocr_candidates, reconcile_caam_ocr_with_miit, sync_caam_ocr_candidates
    from app.services.professional_metric_documents import audit_professional_metric_candidates, sync_professional_metric_candidates
    from app.data_platform.services.data_center import sync_pmi_industry_proxies
    from app.services.llm_research_agent import run_llm_research_agent
    from app.services.llm_research_governance import assess_llm_research_proposals
    from app.services.llm_proposal_router import route_llm_research_proposals
    from app.services.llm_data_operations import run_llm_data_operations_cycle
    from app.services.profit_loop import _latest_trade_date, build_profit_loop
    from app.services.simulation_ledger_audit import audit_simulation_ledger
    from app.services.strategy_controller import advance_waiting_strategy_policy_candidates, run_strategy_policy_auto_optimization
    from app.services.autonomous_healing import run_autonomous_self_healing
    from app.services.sealed_strategy_holdout import audit_sealed_strategy_holdout
    from app.services.data_health import assess_latest_decision_data_health

    hard_data_step = _run_step("hard_data_gate", lambda: audit_oos_hard_data(include_dates=False))
    sealed_holdout_step = _run_step("sealed_holdout_data_gate", lambda: audit_sealed_strategy_holdout(include_dates=False))
    decision_data_step = _run_step("decision_data_gate", assess_latest_decision_data_health)
    decision_data = decision_data_step.get("result") if isinstance(decision_data_step.get("result"), dict) else {}
    experiments_allowed = bool(decision_data.get("experiment_ready")) and str((hard_data_step.get("result") or {}).get("status") or "") == "ready"
    decisions_allowed = bool(decision_data.get("decision_ready"))
    def experimental_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return _run_step(name, fn) if experiments_allowed else _skip_experimental_step(name, decision_data)
    def decision_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return _run_step(name, fn) if decisions_allowed else _skip_decision_step(name, decision_data)

    steps = [
        hard_data_step,
        sealed_holdout_step,
        decision_data_step,
        _run_step("execution_cost_calibration", calibrate_execution_cost_model),
        _run_step(
            "simulation_corporate_actions",
            lambda: run_corporate_action_cycle(as_of_date=_latest_trade_date()),
        ),
        _run_step("simulation_ledger_audit", audit_simulation_ledger),
        decision_step("profit_and_portfolio", build_profit_loop),
        experimental_step("factor_daily_cycle", run_factor_daily_cycle),
        experimental_step("plan_model_autonomy", lambda: run_auto_optimization_once(force=False)),
        _run_step("periodic_performance_review", run_periodic_performance_review),
        _run_step("review_data_quality_governance", run_review_data_quality_governance),
        experimental_step("strategy_policy_maturity", advance_waiting_strategy_policy_candidates),
        experimental_step("strategy_policy_candidates", lambda: run_strategy_policy_auto_optimization(force=False)),
        _run_step("research_maintenance", lambda: run_daily_maintenance(max_candidates=50)),
        _run_step("free_industry_financials", lambda: materialize_financial_industry_series(max_periods=1)),
        _run_step("pmi_demand_proxies", sync_pmi_industry_proxies),
        _run_step("cn_ppi_raw", lambda: sync_tushare_api_raw("cn_ppi")),
        _run_step("cn_ppi_price_proxies", materialize_cn_ppi_price_proxies),
        _run_step("official_policy_documents", sync_official_policy_documents),
        _run_step("official_policy_supply", materialize_official_policy_supply),
        _run_step("official_capacity_utilization", materialize_official_capacity_utilization),
        _run_step("caam_ocr_candidates", sync_caam_ocr_candidates),
        _run_step("caam_ocr_candidate_audit", audit_caam_ocr_candidates),
        _run_step("caam_ocr_miit_reconciliation", reconcile_caam_ocr_with_miit),
        _run_step("professional_metric_pdf_candidates", sync_professional_metric_candidates),
        _run_step("professional_metric_candidate_audit", audit_professional_metric_candidates),
        _run_step(
            "futures_price_proxies",
            lambda: materialize_futures_price_proxies(start_date=_latest_trade_date(), end_date=_latest_trade_date()),
        ),
        _run_step("theme_revenue_exposure", materialize_theme_revenue_exposure),
        _run_step("research_data_assets", audit_research_data_assets),
        _run_step("llm_research_agent", run_llm_research_agent),
        _run_step("llm_research_governance", assess_llm_research_proposals),
        _run_step("llm_proposal_router", route_llm_research_proposals),
    ]
    steps.append(_run_step("llm_data_operations", lambda: run_llm_data_operations_cycle(cycle_steps=steps)))
    healing_step = _run_step("autonomous_self_healing", lambda: run_autonomous_self_healing(steps))
    healing_components = ((healing_step.get("result") or {}).get("components") or {})
    for step in steps:
        component = healing_components.get(str(step.get("name") or ""))
        if isinstance(component, dict) and component.get("status") == "recovered":
            step["ok"] = True
            step["health"] = component
    steps.append(healing_step)
    from app.services.incident_supervisor import run_incident_supervisor
    steps.append(_run_step("llm_incident_supervisor", lambda: run_incident_supervisor(steps)))
    summary = _owner_summary(steps)
    return {
        "ok": all(bool(step.get("ok")) for step in steps),
        "schema_version": "alis_system_autonomy_cycle_v1",
        "status": "completed_owner_action_required" if not summary["healthy"] else "completed",
        "steps": steps,
        "owner_summary": summary,
    }


def _owner_rebalance_action(action: dict[str, Any]) -> dict[str, Any]:
    """Keep the compact owner view without dropping portfolio values."""
    return {
        "symbol": str(action.get("symbol") or ""),
        "name": str(action.get("name") or ""),
        "action": str(action.get("action") or ""),
        "reason": str(action.get("reason") or ""),
        "current_weight": action.get("current_weight"),
        "target_weight": action.get("target_weight"),
        "weight_change": action.get("weight_change"),
        "current_amount": action.get("current_amount"),
        "target_amount": action.get("target_amount"),
        "estimated_trade_amount": action.get("estimated_trade_amount"),
        "planned_side": action.get("planned_side"),
        "planned_quantity": action.get("planned_quantity"),
        "planned_lots": action.get("planned_lots"),
        "reference_close": action.get("reference_close"),
        "estimated_execution_amount": action.get("estimated_execution_amount"),
        "planned_cost": action.get("planned_cost") or {},
        "controller_permission": action.get("controller_permission") or {},
        "controller_intent": action.get("controller_intent"),
        "controller_obligation": action.get("controller_obligation") or {},
    }


def latest_owner_summary() -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT id,status,result_json,finished_at FROM evolution_tasks
               WHERE task_type='system_autonomy_cycle' AND status='completed'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {
            "ok": True,
            "available": False,
            "message": "等待首个每日系统自治周期",
        }
    item = dict(row)
    try:
        result = json.loads(item.get("result_json") or "{}")
    except Exception:
        result = {}
    summary = result.get("owner_summary") or {}

    def append_read_error(exc: BaseException, operation: str) -> None:
        questions = summary.setdefault("questions", {})
        questions.setdefault("evidence_errors", []).append(
            error_evidence(
                exc,
                operation=operation,
                source="owner_summary",
                deferred=True,
            )
        )
    if (
        str(summary.get("schema_version") or "") not in {"alis_owner_daily_summary_v2", "alis_owner_daily_summary_v3"}
        and isinstance(result.get("steps"), list)
    ):
        summary = _owner_summary(result["steps"])
    try:
        from app.services.acceptance_progress import build_acceptance_progress

        summary["acceptance_progress"] = build_acceptance_progress()
    except Exception as exc:
        evidence = error_evidence(
            exc,
            operation="owner_summary:acceptance_progress",
            source="owner_summary",
            deferred=True,
        )
        summary["acceptance_progress"] = {
            "schema_version": "alis_acceptance_progress_v1",
            "status": "unavailable",
            "stages": [],
            "error": evidence["message"],
            "error_evidence": evidence,
        }
    try:
        from app.services.sealed_strategy_holdout import audit_sealed_strategy_holdout
        holdout = audit_sealed_strategy_holdout(include_dates=False)
        if holdout.get("status") == "ready":
            questions = summary.setdefault("questions", {})
            anomalies = questions.setdefault("anomalies", [])
            anomalies[:] = [item for item in anomalies if str(item.get("module") or "") != "sealed_holdout_data_gate"]
            supervision = questions.get("incident_supervision") if isinstance(questions.get("incident_supervision"), dict) else {}
            cases = supervision.get("cases") if isinstance(supervision.get("cases"), list) else []
            supervision["cases"] = [item for item in cases if str(item.get("module") or "") != "sealed_holdout_data_gate"]
            supervision["owner_action_required_count"] = sum(1 for case in supervision["cases"] if case.get("owner_action_required"))
            supervision["internal_fault_count"] = sum(1 for case in supervision["cases"] if case.get("resolution_class") == "internal_fault")
            supervision["waiting_condition_count"] = sum(1 for case in supervision["cases"] if case.get("resolution_class") == "waiting_condition")
            supervision["status"] = "owner_action_required" if supervision["owner_action_required_count"] else "self_managed" if supervision["cases"] else "healthy"
            questions["incident_supervision"] = supervision
    except Exception as exc:
        append_read_error(exc, "owner_summary:sealed_holdout")
    try:
        from app.services.profit_loop import get_latest_daily_rebalance_plan
        latest_plan = get_latest_daily_rebalance_plan()
        proposal = latest_plan.get("proposal") if isinstance(latest_plan.get("proposal"), list) else []
        actionable = [action for action in proposal if int(action.get("planned_quantity") or 0) > 0]
        questions = summary.setdefault("questions", {})
        questions["today_rebalance"] = {
            "status": str(latest_plan.get("status") or "not_generated"),
            "plan_date": latest_plan.get("trade_date"),
            "proposal_count": len(actionable),
            "execution_authorized": bool(latest_plan.get("execution_authorized")),
            "planned_execute_at": latest_plan.get("planned_execute_at"),
            "planned_execute_date": latest_plan.get("planned_execute_date"),
            "blocked_reason": latest_plan.get("blocked_reason"),
            "actions": [_owner_rebalance_action(action) for action in proposal],
        }
    except Exception as exc:
        append_read_error(exc, "owner_summary:latest_rebalance_plan")
    try:
        from app.services.profit_loop import build_portfolio_candles
        comparison_payload = build_portfolio_candles(limit=90)
        comparison = comparison_payload.get("latest_comparison") or {}
        profit = ((summary.get("questions") or {}).get("profit") or {})
        if comparison:
            profit.update({
                "benchmark_return": comparison.get("benchmark_return_pct"),
                "excess_return": comparison.get("excess_return_pct"),
                "benchmark_period_start": (
                    (comparison_payload.get("series") or [{}])[0].get("date")
                ),
                "benchmark_period_end": comparison.get("date"),
            })
    except Exception as exc:
        append_read_error(exc, "owner_summary:portfolio_comparison")
    try:
        from app.services.simulation_ledger_audit import audit_simulation_ledger
        ledger = audit_simulation_ledger(persist=False)
        questions = summary.setdefault("questions", {})
        changes = questions.setdefault("system_changes", {})
        changes.update({
            "ledger_status": ledger.get("status"),
            "ledger_cash_drift": ledger.get("cash_drift"),
            "ledger_position_drift_count": len(ledger.get("position_deltas") or []),
        })
        anomalies = questions.setdefault("anomalies", [])
        anomalies[:] = [item for item in anomalies if str(item.get("module") or "") != "simulation_ledger_audit"]
        if not ledger.get("ok"):
            anomalies.append({
                "module": "simulation_ledger_audit",
                "message": "；".join(str(item.get("message") or "") for item in ledger.get("issues") or []),
            })
        supervision = questions.get("incident_supervision") if isinstance(questions.get("incident_supervision"), dict) else {}
        summary["healthy"] = int(supervision.get("owner_action_required_count") or 0) == 0 if supervision else not anomalies
    except Exception as exc:
        questions = summary.setdefault("questions", {})
        anomalies = questions.setdefault("anomalies", [])
        evidence = error_evidence(
            exc,
            operation="owner_summary:simulation_ledger_audit",
            source="owner_summary",
        )
        anomalies.append({
            "module": "simulation_ledger_audit",
            "message": f"独立账本审计失败：{evidence['message']}",
            "error_evidence": evidence,
        })
        supervision = questions.get("incident_supervision") if isinstance(questions.get("incident_supervision"), dict) else {}
        summary["healthy"] = int(supervision.get("owner_action_required_count") or 0) == 0 if supervision else False
    try:
        # The stored daily summary is historical evidence, but execution
        # permission is live state. Never leave the UI on a stale Champion gate
        # after quarantine, promotion, rollback, or recovery-policy changes.
        from app.services.production_strategy import champion_execution_gate

        questions = summary.setdefault("questions", {})
        changes = questions.setdefault("system_changes", {})
        changes["champion_execution_gate"] = champion_execution_gate()
    except Exception as exc:
        questions = summary.setdefault("questions", {})
        changes = questions.setdefault("system_changes", {})
        evidence = error_evidence(
            exc,
            operation="owner_summary:champion_execution_gate",
            source="owner_summary",
        )
        changes["champion_execution_gate"] = {
            "status": "unavailable",
            "mode": "fail_closed",
            "reason": f"live_champion_gate_unavailable:{evidence['message']}",
            "error_evidence": evidence,
        }
    return {
        "ok": True,
        "available": True,
        "task_id": int(item.get("id") or 0),
        "task_status": str(item.get("status") or ""),
        "finished_at": str(item.get("finished_at") or ""),
        "summary": summary,
    }


AUTONOMY_SLO_WINDOW_SESSIONS = 20
AUTONOMY_SLO_TARGETS = {
    "cycle_completion_rate": 0.95,
    "critical_data_gate_rate": 0.95,
    "ledger_zero_drift_rate": 1.0,
    "recovery_rate": 0.90,
}


def _autonomy_trade_date(task: dict[str, Any]) -> str:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    steps = result.get("steps") if isinstance(result.get("steps"), list) else []
    decision = next((item for item in steps if item.get("name") == "decision_data_gate"), {})
    decision_result = decision.get("result") if isinstance(decision.get("result"), dict) else {}
    value = (
        decision_result.get("target_trade_date")
        or decision_result.get("latest_decision_ready_trade_date")
        or (task.get("payload") or {}).get("trade_date")
        or str(task.get("created_at") or "")[:10]
    )
    return str(value or "").replace("-", "")[:8]


def build_autonomy_slo(tasks: list[dict[str, Any]] | None = None, *, window_sessions: int = AUTONOMY_SLO_WINDOW_SESSIONS) -> dict[str, Any]:
    """Measure rolling autonomy reliability from persisted cycle evidence."""
    if tasks is None:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT * FROM evolution_tasks WHERE task_type='system_autonomy_cycle'
                   ORDER BY id DESC LIMIT 200"""
            ).fetchall()
        from app.services.evolution_orchestrator import _row
        tasks = [_row(row) for row in rows]
    sessions: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        if str(task.get("task_type") or "") != "system_autonomy_cycle":
            continue
        trade_date = _autonomy_trade_date(task)
        if trade_date:
            sessions.setdefault(trade_date, []).append(task)
    selected_dates = sorted(sessions, reverse=True)[:max(1, int(window_sessions))]
    observations: list[dict[str, Any]] = []
    for trade_date in sorted(selected_dates):
        attempts = sorted(sessions[trade_date], key=lambda item: int(item.get("id") or 0))
        successful = [item for item in attempts if str(item.get("status") or "") == "completed"]
        latest = successful[-1] if successful else attempts[-1]
        result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
        steps = result.get("steps") if isinstance(result.get("steps"), list) else []
        by_name = {str(item.get("name") or ""): item for item in steps if isinstance(item, dict)}
        hard_result = (by_name.get("hard_data_gate") or {}).get("result") or {}
        decision_result = (by_name.get("decision_data_gate") or {}).get("result") or {}
        hard_data_ok = bool((by_name.get("hard_data_gate") or {}).get("ok")) and str(hard_result.get("status") or "") == "ready"
        decision_data_ok = (
            bool((by_name.get("decision_data_gate") or {}).get("ok"))
            and bool(decision_result.get("decision_ready", decision_result.get("ok", False)))
        )
        ledger_result = (by_name.get("simulation_ledger_audit") or {}).get("result") or {}
        ledger_ok = bool((by_name.get("simulation_ledger_audit") or {}).get("ok")) and abs(float(ledger_result.get("cash_drift") or 0.0)) <= .01 and not (ledger_result.get("position_deltas") or [])
        observations.append({
            "trade_date": trade_date,
            "attempt_count": len(attempts),
            "completed_count": len(successful),
            "cycle_completed": bool(successful),
            "critical_data_gates_passed": hard_data_ok and decision_data_ok,
            "ledger_zero_drift": ledger_ok,
            "recovery_required": len(attempts) > 1 or any(str(item.get("status") or "") in {"failed", "retry_wait"} for item in attempts[:-1]),
            "recovered": bool(successful),
        })
    count = len(observations)
    recovery_rows = [item for item in observations if item["recovery_required"]]

    def rate(key: str, rows: list[dict[str, Any]] = observations) -> float | None:
        return round(sum(bool(item.get(key)) for item in rows) / len(rows), 6) if rows else None

    metrics = {
        "cycle_completion_rate": rate("cycle_completed"),
        "critical_data_gate_rate": rate("critical_data_gates_passed"),
        "ledger_zero_drift_rate": rate("ledger_zero_drift"),
        "recovery_rate": rate("recovered", recovery_rows),
        "duplicate_successful_cycle_count": sum(max(0, int(item["completed_count"]) - 1) for item in observations),
    }
    enough_samples = count >= max(1, int(window_sessions))
    gates = {
        key: bool(enough_samples and metrics[key] is not None and float(metrics[key]) >= target)
        for key, target in AUTONOMY_SLO_TARGETS.items()
        if key != "recovery_rate" or recovery_rows
    }
    gates["no_duplicate_successful_cycles"] = bool(enough_samples and metrics["duplicate_successful_cycle_count"] == 0)
    status = "observing" if not enough_samples else "passed" if all(gates.values()) else "failed"
    return {
        "schema_version": "alis_autonomy_rolling_slo_v1",
        "status": status,
        "window_sessions": int(window_sessions),
        "observed_sessions": count,
        "window_start": observations[0]["trade_date"] if observations else None,
        "window_end": observations[-1]["trade_date"] if observations else None,
        "targets": AUTONOMY_SLO_TARGETS,
        "metrics": metrics,
        "gates": gates,
        "observations": observations,
        "limitations": ["T日计划到T+1成交的跨表及时率由后续执行链SLO单独验收。"],
    }

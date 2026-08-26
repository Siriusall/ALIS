from __future__ import annotations

import copy
from datetime import datetime
import json
import os
import socket
import threading
import time
from datetime import timedelta
from typing import Any, Callable
import uuid

from app.core.error_evidence import error_evidence
from app.db.store import _conn, _read_conn


_STEP_TIMEOUT_SECONDS = max(15, min(int(os.getenv("ALIS_AUTONOMY_STEP_TIMEOUT_SECONDS", "120")), 900))
_LONG_STEP_TIMEOUT_SECONDS = max(
    _STEP_TIMEOUT_SECONDS,
    min(int(os.getenv("ALIS_AUTONOMY_LONG_STEP_TIMEOUT_SECONDS", "3600")), 7200),
)
_LONG_RUNNING_STEPS = frozenset({
    "factor_daily_cycle",
    "plan_model_autonomy",
    "strategy_shadow_recovery",
    "profit_and_portfolio",
    "periodic_performance_review",
    "research_maintenance",
    "free_industry_financials",
    "pmi_demand_proxies",
    "cn_ppi_price_proxies",
    "theme_revenue_exposure",
})
_running_steps: set[str] = set()
_running_steps_lock = threading.Lock()
_owner_summary_cache_lock = threading.Lock()
_owner_summary_build_lock = threading.Lock()
_owner_summary_cache: tuple[float, dict[str, Any]] | None = None
_OWNER_SUMMARY_CACHE_TTL_SECONDS = 30.0


def _claim_distributed_step_lease(name: str, timeout_seconds: float) -> str | None:
    """Claim one cross-process lease for an autonomy step name."""
    token = uuid.uuid4().hex
    now = datetime.now().astimezone()
    expires_at = now + timedelta(seconds=max(900.0, float(timeout_seconds) * 2.0 + 300.0))
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS autonomy_step_leases (
            step_name VARCHAR(160) PRIMARY KEY,
            lease_token VARCHAR(64) NOT NULL,
            lease_owner VARCHAR(255) NOT NULL,
            acquired_at VARCHAR(64) NOT NULL,
            expires_at VARCHAR(64) NOT NULL
        )""")
        if getattr(conn, "_is_postgres", False):
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"autonomy-step:{name}",))
        row = conn.execute(
            "SELECT lease_token,lease_owner,expires_at FROM autonomy_step_leases WHERE step_name=?",
            (name,),
        ).fetchone()
        if row:
            item = dict(row)
            try:
                active_until = datetime.fromisoformat(str(item.get("expires_at") or ""))
            except ValueError:
                active_until = now
            owner = str(item.get("lease_owner") or "")
            owner_alive = True
            try:
                if owner.startswith("pid:"):
                    owner_host, owner_pid_text = socket.gethostname(), owner[4:]
                else:
                    owner_host, owner_pid_text = owner.rsplit(":pid:", 1)
                if owner_host == socket.gethostname():
                    owner_pid = int(owner_pid_text)
                    os.kill(owner_pid, 0)
            except ProcessLookupError:
                owner_alive = False
            except (ValueError, PermissionError, OSError):
                owner_alive = True
            if active_until > now and owner_alive:
                return None
        conn.execute(
            """INSERT INTO autonomy_step_leases(step_name,lease_token,lease_owner,acquired_at,expires_at)
               VALUES(?,?,?,?,?) ON CONFLICT(step_name) DO UPDATE SET
               lease_token=excluded.lease_token,lease_owner=excluded.lease_owner,
               acquired_at=excluded.acquired_at,expires_at=excluded.expires_at""",
            (name, token, f"{socket.gethostname()}:pid:{os.getpid()}", now.isoformat(), expires_at.isoformat()),
        )
    return token


def _release_distributed_step_lease(name: str, token: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM autonomy_step_leases WHERE step_name=? AND lease_token=?",
            (name, token),
        )


def _run_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a bounded step without blocking the autonomous bus.

    Python cannot safely kill a running thread.  On timeout the worker is
    therefore left to finish cooperatively, while the bus receives a terminal
    ``timed_out`` observation immediately.  The name remains in
    ``_running_steps`` until the original worker exits, preventing re-entry;
    late results are never written back into the already-returned step.
    """
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
    timeout_seconds = (
        _LONG_STEP_TIMEOUT_SECONDS
        if name in _LONG_RUNNING_STEPS
        else _STEP_TIMEOUT_SECONDS
    )
    finished = threading.Event()
    outcome: dict[str, Any] = {}
    scope_holder: dict[str, str] = {}

    def worker() -> None:
        lease_token = ""
        try:
            from app.core.autonomy_step_control import autonomy_step_scope, raise_if_cancelled

            with autonomy_step_scope() as scope_id:
                scope_holder["id"] = scope_id
                lease_token = _claim_distributed_step_lease(name, timeout_seconds) or ""
                if not lease_token:
                    outcome["error"] = "previous_autonomy_step_still_running_across_workers"
                    return
                raise_if_cancelled()
                result = fn()
                raise_if_cancelled()
                if not isinstance(result, dict):
                    raise TypeError("autonomy_step_must_return_dict")
                outcome["result"] = result
        except Exception as exc:  # noqa: BLE001 - step failure is evidence, not bus failure
            outcome["error"] = str(exc)[:2000]
        finally:
            if lease_token:
                try:
                    _release_distributed_step_lease(name, lease_token)
                except Exception as exc:  # noqa: BLE001 - preserve the step outcome
                    outcome.setdefault("lease_release_error", str(exc)[:500])
            with _running_steps_lock:
                _running_steps.discard(name)
            finished.set()

    thread = threading.Thread(target=worker, name=f"alis-autonomy-{name}", daemon=True)
    thread.start()
    if not finished.wait(timeout_seconds):
        from app.core.autonomy_step_control import cancel_autonomy_scope

        cancelled_connections = cancel_autonomy_scope(scope_holder.get("id", ""))
        step = {
            "name": name,
            "ok": False,
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "started_at": started.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "result": {
                "status": "timed_out",
                "timeout_seconds": timeout_seconds,
                "worker_alive": thread.is_alive(),
                "late_result_policy": "discarded_from_bus_terminal_state",
                "cancelled_postgres_connections": cancelled_connections,
            },
            "error": f"autonomy_step_timeout:{timeout_seconds}s",
            "timing": {
                "budget_seconds": timeout_seconds,
                "over_budget": True,
                "policy": "bus_continues; postgres_queries_cancelled; lease_blocks_cross_worker_reentry",
            },
            "health": {"status": "degraded", "reason": "timed_out"},
        }
        return step

    duration_seconds = round((datetime.now().astimezone() - started).total_seconds(), 3)
    if "error" in outcome:
        step = {
            "name": name,
            "ok": False,
            "duration_seconds": duration_seconds,
            "started_at": started.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
            "error": outcome["error"],
        }
    else:
        result = outcome["result"]
        step = {
            "name": name,
            "ok": bool(result.get("ok", True)),
            "duration_seconds": duration_seconds,
            "started_at": started.isoformat(), "finished_at": datetime.now().astimezone().isoformat(),
            "result": result,
            "timing": {
                "budget_seconds": timeout_seconds,
                "over_budget": False,
                "policy": "bounded_worker",
            },
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
    llm_data_operations = (((by_name.get("data_operations") or by_name.get("llm_data_operations") or {}).get("result")) or {})
    incident_supervisor = ((by_name.get("llm_incident_supervisor") or {}).get("result") or {})
    healing = ((by_name.get("autonomous_self_healing") or {}).get("result") or {})
    historical_gate_waits = []
    for gate_name in ("hard_data_gate", "sealed_holdout_data_gate"):
        gate_step = by_name.get(gate_name) or {}
        gate_result = gate_step.get("result") if isinstance(gate_step.get("result"), dict) else {}
        gate_status = str(gate_result.get("status") or "")
        if gate_status and gate_status != "ready":
            historical_gate_waits.append({
                "module": gate_name,
                "status": gate_status,
                "scope": "formal_oos_or_sealed_holdout_only",
                "eligible_sessions": gate_result.get("eligible_sessions"),
                "required_sessions": gate_result.get("required_sessions"),
                "repair_dates": list(gate_result.get("repair_dates") or []),
                "non_repairable_dates": list(gate_result.get("non_repairable_dates") or []),
                "non_repairable_sessions": gate_result.get("non_repairable_sessions"),
                "non_repairable_dates_truncated": bool(gate_result.get("non_repairable_dates_truncated")),
            })
    cost_active = cost.get("active") if isinstance(cost.get("active"), dict) else {}
    hard_data = ((by_name.get("hard_data_gate") or {}).get("result") or {})
    decision_data = ((by_name.get("decision_data_gate") or {}).get("result") or {})
    optimizer = portfolio.get("optimizer") if isinstance(portfolio.get("optimizer"), dict) else {}
    rebalance_items = list(portfolio.get("rebalance_proposal") or portfolio.get("proposal") or [])
    rebalance_actions = [action for action in rebalance_items if int(action.get("planned_quantity") or 0) > 0]
    champion_gate_error = ""
    try:
        champion_gate = champion_execution_gate()
    except Exception as exc:  # noqa: BLE001 - preserve the completed step evidence
        # Summary construction is not allowed to turn a completed, auditable
        # autonomy cycle into an opaque task failure.  The live execution gate
        # remains fail-closed and the unavailable read is surfaced below.
        champion_gate_error = type(exc).__name__
        champion_gate = {
            "status": "unavailable",
            "mode": "fail_closed",
            "reason": f"live_champion_gate_unavailable:{champion_gate_error}",
        }
    anomalies = [
        {
            "module": str(step["name"]),
            "message": str(step.get("error") or ((step.get("health") or {}).get("reason")) or "模块返回未通过"),
        }
        for step in steps
        if str(step.get("name") or "") not in {"autonomous_self_healing", "llm_incident_supervisor"}
        if str(step.get("name") or "") not in {"hard_data_gate", "sealed_holdout_data_gate"}
        if str((step.get("health") or {}).get("status") or ("healthy" if step.get("ok") else "failed")) in {"failed", "degraded"}
    ]
    # The supervisor and self-healing layers are not allowed to erase their
    # own timeout/failure.  Their *cases* are handled separately, but a
    # failed wrapper remains a deterministic cycle anomaly.
    for meta_name in ("autonomous_self_healing", "llm_incident_supervisor"):
        if meta_name not in by_name:
            # Older persisted cycles predate these wrappers.  Missing evidence
            # must stay distinguishable from a wrapper that actually timed
            # out; otherwise opening an old summary manufactures an outage.
            continue
        meta_step = by_name.get(meta_name) or {}
        meta_health = meta_step.get("health") if isinstance(meta_step.get("health"), dict) else {}
        meta_status = str(meta_health.get("status") or ("healthy" if meta_step.get("ok") else "failed"))
        if meta_status in {"failed", "degraded"}:
            anomalies.append({
                "module": meta_name,
                "message": str(meta_step.get("error") or meta_health.get("reason") or "模块返回未通过"),
            })
    if champion_gate_error:
        anomalies.append({
            "module": "champion_execution_gate",
            "message": f"live_champion_gate_unavailable:{champion_gate_error}",
        })
    operational_healthy = (
        not anomalies
        and (
            int(incident_supervisor.get("owner_action_required_count") or 0) == 0
            if incident_supervisor
            else True
        )
    )
    research_readiness = "accumulating" if historical_gate_waits else "ready"
    return {
        "schema_version": "alis_owner_daily_summary_v4_split_health",
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
                "model_training_delegated": bool(
                    str(model.get("status") or "") == "delegated"
                    or str(model.get("execution_mode") or "") == "persistent_research_task"
                ),
                "model_autonomy_task_id": int(model.get("task_id") or 0) or None,
                "model_autonomy_task_status": str(
                    model.get("task_status") or model.get("status") or ""
                ),
                "model_autonomy_worker_pool": str(model.get("worker_pool") or ""),
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
            "historical_research_waits": historical_gate_waits,
            "incident_supervision": incident_supervisor,
        },
        # The LLM incident supervisor is an additional escalation layer, not
        # an authority that may erase deterministic step failures.  A timeout
        # or degraded module must keep the daily cycle non-green even when the
        # supervisor decides that no owner action is necessary.
        # Historical OOS/holdout evidence is a promotion concern, not an
        # operational outage.  Keep it visible above while preventing an
        # unrecoverable research wait from painting the whole system red.
        "healthy": operational_healthy,
        "operational_health": "healthy" if operational_healthy else "degraded",
        "trading_readiness": "ready" if operational_healthy else "blocked",
        "research_readiness": research_readiness,
        "promotion_readiness": "blocked" if historical_gate_waits else "ready",
    }


def run_system_autonomy_cycle(*, trade_date: str = "") -> dict[str, Any]:
    """Run the existing deterministic modules as one auditable daily bus cycle."""
    from app.data_platform.services.oos_hard_data import audit_progressive_oos
    from app.services.corporate_actions import run_corporate_action_cycle
    from app.services.execution_cost_calibration import calibrate_execution_cost_model
    from app.services.performance_reports import run_periodic_performance_review
    from app.services.review_data_quality_governance import run_review_data_quality_governance
    from app.services.evolution_orchestrator import (
        enqueue_plan_model_autonomy_task,
        enqueue_factor_daily_task,
        enqueue_research_maintenance_task,
        enqueue_strategy_shadow_daily_task,
    )
    from app.services.research_data_assets import audit_research_data_assets
    from app.services.theme_revenue_exposure import materialize_theme_revenue_exposure
    from app.services.free_research_data import materialize_cn_ppi_price_proxies, materialize_financial_industry_series, materialize_futures_price_proxies
    from app.data_platform.services.data_center import refresh_cn_ppi_from_first_source
    from app.services.official_policy_data import materialize_official_capacity_utilization, materialize_official_policy_supply, sync_official_policy_documents
    from app.services.caam_ocr_data import audit_caam_ocr_candidates, reconcile_caam_ocr_with_miit, sync_caam_ocr_candidates
    from app.services.professional_metric_documents import audit_professional_metric_candidates, sync_professional_metric_candidates
    from app.data_platform.services.data_center import sync_pmi_industry_proxies
    from app.services.llm_research_agent import run_llm_research_agent
    from app.services.llm_research_governance import assess_llm_research_proposals
    from app.services.llm_proposal_router import route_llm_research_proposals
    from app.services.llm_data_operations import run_deterministic_data_operations_cycle
    from app.services.profit_loop import build_profit_loop
    from app.services.simulation_ledger_audit import audit_simulation_ledger
    from app.services.strategy_controller import advance_waiting_strategy_policy_candidates, run_strategy_policy_auto_optimization
    from app.services.controller_candidate_observation import ensure_controller_candidate_observation_tasks
    from app.services.autonomous_healing import run_autonomous_self_healing
    from app.services.sealed_strategy_holdout import audit_sealed_strategy_holdout
    from app.services.data_health import assess_latest_decision_data_health

    hard_data_step = _run_step("hard_data_gate", lambda: audit_progressive_oos(include_dates=False))
    sealed_holdout_step = _run_step("sealed_holdout_data_gate", lambda: audit_sealed_strategy_holdout(include_dates=False))
    frozen_trade_date = str(trade_date or "").replace("-", "")[:8]
    decision_data_step = _run_step(
        "decision_data_gate",
        lambda: (
            assess_latest_decision_data_health(frozen_trade_date)
            if frozen_trade_date
            else assess_latest_decision_data_health()
        ),
    )
    decision_data = decision_data_step.get("result") if isinstance(decision_data_step.get("result"), dict) else {}
    current_market_date = frozen_trade_date or str(
        ((decision_data.get("readiness") or {}).get("target_trade_date") or "")
    ).replace("-", "")[:8]

    def require_current_market_date() -> str:
        if current_market_date:
            return current_market_date
        raise RuntimeError(
            "current_market_data_target_missing: 决策数据门未提供权威目标交易日，"
            "禁止从历史快照或工作日推断研究日期"
        )
    # Keep historical research-gate evidence explicit in the cycle payload.
    # It is intentionally separate from the current decision-data gate below;
    # a missing old PIT record must not disable today's isolated account.
    for historical_step in (hard_data_step, sealed_holdout_step):
        historical_result = historical_step.get("result")
        if isinstance(historical_result, dict) and str(historical_result.get("status") or "") != "ready":
            historical_result.setdefault("scope", "formal_oos_or_sealed_holdout_only")
            historical_result.setdefault("current_decision_impact", False)
    # Current experiments consume only the current decision-data contract and
    # their own frozen evidence.  Formal OOS / sealed-holdout PIT history
    # remains a strict production-eligibility gate in its dedicated consumer,
    # but an irrecoverable historical evidence gap must not deadlock isolated
    # future observation or today’s decision/execution path.
    # A readiness flag is only authoritative when the step itself completed
    # successfully.  Do not trust a stale/partial result object after a
    # wrapper error or an optimistic producer projection.
    decision_gate_ok = bool(decision_data_step.get("ok"))
    experiments_allowed = decision_gate_ok and bool(decision_data.get("experiment_ready"))
    decisions_allowed = decision_gate_ok and bool(decision_data.get("decision_ready"))
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
            lambda: run_corporate_action_cycle(as_of_date=require_current_market_date()),
        ),
        _run_step("simulation_ledger_audit", audit_simulation_ledger),
        decision_step(
            "profit_and_portfolio",
            lambda: (
                build_profit_loop(signal_date=frozen_trade_date)
                if frozen_trade_date
                else build_profit_loop()
            ),
        ),
        experimental_step(
            "factor_daily_cycle",
            lambda: enqueue_factor_daily_task(
                frozen_trade_date or current_market_date,
                trigger="daily_system_autonomy",
            ),
        ),
        # Model training is a large historical read and belongs to the
        # research worker.  The control-plane daily bus only persists an
        # idempotent task and continues immediately, preserving the production
        # decision/ledger path even when the research database is busy.
        experimental_step(
            "plan_model_autonomy",
            lambda: enqueue_plan_model_autonomy_task(
                frozen_trade_date or current_market_date,
                trigger="daily_system_autonomy",
            ),
        ),
        # Future shadow consumes the current experiment gate and the
        # candidate's already-frozen formal evidence. It must not inherit the
        # rolling historical OOS audit, whose historical PIT failures cannot
        # become repairable through time.
        (
            _run_step(
                "strategy_shadow_recovery",
                lambda: enqueue_strategy_shadow_daily_task(
                    frozen_trade_date or str((decision_data.get("readiness") or {}).get("target_trade_date") or ""),
                ),
            )
            if experiments_allowed
            else _skip_experimental_step("strategy_shadow_recovery", decision_data)
        ),
        _run_step("periodic_performance_review", run_periodic_performance_review),
        _run_step("review_data_quality_governance", run_review_data_quality_governance),
        experimental_step(
            "controller_candidate_observation",
            ensure_controller_candidate_observation_tasks,
        ),
        experimental_step("strategy_policy_maturity", advance_waiting_strategy_policy_candidates),
        experimental_step("strategy_policy_candidates", lambda: run_strategy_policy_auto_optimization(force=False)),
        _run_step(
            "research_maintenance",
            lambda: enqueue_research_maintenance_task(
                frozen_trade_date or current_market_date,
                max_candidates=50,
                trigger="daily_system_autonomy",
            ),
        ),
        _run_step("free_industry_financials", lambda: materialize_financial_industry_series(max_periods=1)),
        _run_step("pmi_demand_proxies", sync_pmi_industry_proxies),
        _run_step("cn_ppi_raw", refresh_cn_ppi_from_first_source),
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
            lambda: materialize_futures_price_proxies(
                start_date=require_current_market_date(),
                end_date=require_current_market_date(),
            ),
        ),
        _run_step("theme_revenue_exposure", materialize_theme_revenue_exposure),
        _run_step("research_data_assets", audit_research_data_assets),
        _run_step("llm_research_agent", run_llm_research_agent),
        _run_step("llm_research_governance", assess_llm_research_proposals),
        _run_step("llm_proposal_router", route_llm_research_proposals),
    ]
    steps.append(_run_step(
        "data_operations",
        lambda: run_deterministic_data_operations_cycle(cycle_steps=steps),
    ))
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
    owner_action_required = int(
        ((summary.get("questions") or {}).get("incident_supervision") or {}).get(
            "owner_action_required_count"
        )
        or 0
    ) > 0
    return {
        "ok": all(bool(step.get("ok")) for step in steps),
        "schema_version": "alis_system_autonomy_cycle_v1",
        "status": (
            "completed"
            if summary["healthy"]
            else "completed_owner_action_required"
            if owner_action_required
            else "completed_with_anomalies"
        ),
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
        "residual_breach": action.get("residual_breach") or {},
    }


def _build_latest_owner_summary() -> dict[str, Any]:
    # This projection is read-only and is followed by JSON decoding outside
    # the database call.  Use an autocommit connection so that decoding a
    # large completed cycle cannot leave ``idle in transaction`` on PostgreSQL.
    with _read_conn() as conn:
        if bool(getattr(conn, "_is_postgres", False)):
            row = conn.execute(
                """SELECT id,status,finished_at,
                          (result_json::jsonb->'owner_summary')::text owner_summary_json
                     FROM evolution_tasks
                    WHERE task_type='system_autonomy_cycle' AND status='completed'
                    ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        else:
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
    result: dict[str, Any] = {}
    try:
        summary = json.loads(item.get("owner_summary_json") or "null")
    except Exception:
        summary = None
    if not isinstance(summary, dict):
        # Compatibility fallback for SQLite and old rows without the compact
        # owner summary. PostgreSQL only transfers the full evidence when the
        # latest row genuinely predates that projection.
        if "result_json" not in item:
            with _read_conn() as conn:
                fallback = conn.execute(
                    "SELECT result_json FROM evolution_tasks WHERE id=?",
                    (int(item.get("id") or 0),),
                ).fetchone()
            item["result_json"] = dict(fallback).get("result_json") if fallback else "{}"
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
    # The stored summary is evidence from a completed cycle, not a live
    # health probe. Keep that provenance explicit so a historical timeout is
    # not presented as an unqualified current outage.
    summary["evidence_source"] = {
        "kind": "completed_autonomy_cycle",
        "task_id": int(item.get("id") or 0),
        "finished_at": str(item.get("finished_at") or ""),
        "live_rechecked": False,
    }
    questions = summary.get("questions") if isinstance(summary.get("questions"), dict) else {}
    anomalies = questions.get("anomalies") if isinstance(questions.get("anomalies"), list) else []
    for anomaly in anomalies:
        if isinstance(anomaly, dict):
            anomaly.setdefault("evidence_scope", "completed_cycle")
            anomaly.setdefault("observed_at", str(item.get("finished_at") or ""))
    supervision = questions.get("incident_supervision") if isinstance(questions.get("incident_supervision"), dict) else {}
    cases = supervision.get("cases") if isinstance(supervision.get("cases"), list) else []
    for case in cases:
        if isinstance(case, dict):
            case.setdefault("evidence_scope", "completed_cycle")
            case.setdefault("observed_at", str(item.get("finished_at") or ""))
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
    # The latest completed cycle already contains the sealed-data gate result.
    # Re-running the 126-session audit on every console cold start made a
    # historical evidence endpoint compete with the live readiness endpoint.
    # Use that persisted result; the admin readiness route remains the live
    # place for a fresh audit.
    try:
        sealed_step = next(
            (step for step in (result.get("steps") or [])
             if isinstance(step, dict) and str(step.get("name") or "") == "sealed_holdout_data_gate"),
            None,
        )
        sealed_result = sealed_step.get("result") if isinstance(sealed_step, dict) else None
        if isinstance(sealed_result, dict) and str(sealed_result.get("status") or "") == "ready":
            questions = summary.setdefault("questions", {})
            anomalies = questions.setdefault("anomalies", [])
            anomalies[:] = [item for item in anomalies if str(item.get("module") or "") != "sealed_holdout_data_gate"]
            historical_waits = questions.get("historical_research_waits")
            if isinstance(historical_waits, list):
                questions["historical_research_waits"] = [
                    item for item in historical_waits
                    if not isinstance(item, dict)
                    or str(item.get("module") or "") != "sealed_holdout_data_gate"
                ]
            supervision = questions.get("incident_supervision") if isinstance(questions.get("incident_supervision"), dict) else {}
            cases = supervision.get("cases") if isinstance(supervision.get("cases"), list) else []
            supervision["cases"] = [item for item in cases if str(item.get("module") or "") != "sealed_holdout_data_gate"]
            supervision["owner_action_required_count"] = sum(1 for case in supervision["cases"] if case.get("owner_action_required"))
            supervision["internal_fault_count"] = sum(1 for case in supervision["cases"] if case.get("resolution_class") == "internal_fault")
            supervision["waiting_condition_count"] = sum(1 for case in supervision["cases"] if case.get("resolution_class") == "waiting_condition")
            supervision["status"] = "owner_action_required" if supervision["owner_action_required_count"] else "self_managed" if supervision["cases"] else "healthy"
            questions["incident_supervision"] = supervision
            summary["healthy"] = bool(
                not questions.get("anomalies")
                and not questions.get("historical_research_waits")
                and int(supervision.get("owner_action_required_count") or 0) == 0
            )
    except Exception as exc:
        append_read_error(exc, "owner_summary:sealed_holdout")
    # Rebalance, performance and ledger values are also frozen in the latest
    # cycle summary. Their dedicated APIs provide the live detail when the
    # owner drills down, so do not rebuild historical curves or replay the
    # entire ledger during this compact overview request.
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


def latest_owner_summary() -> dict[str, Any]:
    """Return the owner snapshot without repeating expensive read-only audits."""
    global _owner_summary_cache
    now = time.monotonic()
    with _owner_summary_cache_lock:
        cached = _owner_summary_cache
    if cached and now - cached[0] < _OWNER_SUMMARY_CACHE_TTL_SECONDS:
        return copy.deepcopy(cached[1])

    # The owner page and the evolution page can ask for the same snapshot at
    # once. Only one request performs the bounded audits; the other reuses it.
    with _owner_summary_build_lock:
        now = time.monotonic()
        with _owner_summary_cache_lock:
            cached = _owner_summary_cache
        if cached and now - cached[0] < _OWNER_SUMMARY_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
        value = _build_latest_owner_summary()
        with _owner_summary_cache_lock:
            _owner_summary_cache = (time.monotonic(), copy.deepcopy(value))
        return value


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
        or (task.get("payload") or {}).get("trade_date")
    )
    return str(value or "").replace("-", "")[:8]


def build_autonomy_slo(tasks: list[dict[str, Any]] | None = None, *, window_sessions: int = AUTONOMY_SLO_WINDOW_SESSIONS) -> dict[str, Any]:
    """Measure rolling autonomy reliability from persisted cycle evidence."""
    if tasks is None:
        # SLO computation is pure Python after this bounded SELECT.  Keep the
        # read outside a writable transaction while the task evidence is
        # normalized and grouped below.
        with _read_conn() as conn:
            if getattr(conn, "_is_postgres", False):
                # SLO checks only need three gate results. Avoid transferring
                # every historical step and its nested evidence to the API.
                query = """
                    SELECT id,task_type,status,payload_json,attempt,created_at,finished_at,
                           jsonb_build_object(
                               'steps', COALESCE((
                                   SELECT jsonb_agg(
                                       jsonb_build_object(
                                           'name', step->>'name',
                                           'ok', step->'ok',
                                           'result', CASE step->>'name'
                                               WHEN 'hard_data_gate' THEN jsonb_build_object(
                                                   'status', step->'result'->'status'
                                               )
                                               WHEN 'decision_data_gate' THEN jsonb_build_object(
                                                   'decision_ready', step->'result'->'decision_ready',
                                                   'ok', step->'result'->'ok'
                                               )
                                               WHEN 'simulation_ledger_audit' THEN jsonb_build_object(
                                                   'cash_drift', step->'result'->'cash_drift',
                                                   'position_deltas', CASE
                                                       WHEN jsonb_typeof(step->'result'->'position_deltas')='array'
                                                            AND jsonb_array_length(step->'result'->'position_deltas') > 0
                                                       THEN '[1]'::jsonb
                                                       ELSE '[]'::jsonb
                                                   END
                                               )
                                               ELSE '{}'::jsonb
                                           END
                                       )
                                   )
                                   FROM jsonb_array_elements(
                                       COALESCE(result_json::jsonb->'steps','[]'::jsonb)
                                   ) AS step
                                   WHERE step->>'name' IN (
                                       'hard_data_gate','decision_data_gate','simulation_ledger_audit'
                                   )
                               ), '[]'::jsonb)
                           )::text AS result_json
                    FROM evolution_tasks
                    WHERE task_type='system_autonomy_cycle'
                    ORDER BY id DESC LIMIT 200
                """
            else:
                query = """
                    SELECT id,task_type,status,payload_json,result_json,attempt,created_at,finished_at
                    FROM evolution_tasks
                    WHERE task_type='system_autonomy_cycle'
                    ORDER BY id DESC LIMIT 200
                """
            rows = conn.execute(query).fetchall()
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
        decision_result = (by_name.get("decision_data_gate") or {}).get("result") or {}
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
            # The rolling autonomy SLO measures today's decision/execution
            # contract.  Permanent OOS PIT history is a separate research
            # gate and must not make an otherwise valid isolated cycle fail.
            "critical_data_gates_passed": decision_data_ok,
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

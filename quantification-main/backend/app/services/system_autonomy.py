from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Callable

from app.db.store import _conn


def _run_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = datetime.now().astimezone()
    try:
        result = fn()
        return {
            "name": name,
            "ok": bool(result.get("ok", True)),
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "result": result,
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "duration_seconds": round((datetime.now().astimezone() - started).total_seconds(), 3),
            "error": str(exc)[:2000],
        }


def _owner_summary(steps: list[dict[str, Any]]) -> dict[str, Any]:
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
    model = ((by_name.get("plan_model_autonomy") or {}).get("result") or {})
    strategy = ((by_name.get("strategy_policy_candidates") or {}).get("result") or {})
    cost = ((by_name.get("execution_cost_calibration") or {}).get("result") or {})
    corporate_actions = ((by_name.get("simulation_corporate_actions") or {}).get("result") or {})
    corporate_materialization = (
        corporate_actions.get("materialization")
        if isinstance(corporate_actions.get("materialization"), dict) else {}
    )
    ledger = ((by_name.get("simulation_ledger_audit") or {}).get("result") or {})
    performance_review = ((by_name.get("periodic_performance_review") or {}).get("result") or {})
    cost_active = cost.get("active") if isinstance(cost.get("active"), dict) else {}
    hard_data = ((by_name.get("hard_data_gate") or {}).get("result") or {})
    optimizer = portfolio.get("optimizer") if isinstance(portfolio.get("optimizer"), dict) else {}
    anomalies = [
        {
            "module": str(step["name"]),
            "message": str(step.get("error") or "模块返回未通过"),
        }
        for step in steps if not bool(step.get("ok"))
    ]
    return {
        "schema_version": "alis_owner_daily_summary_v3",
        "generated_at": datetime.now().astimezone().isoformat(),
        "questions": {
            "today_rebalance": {
                "status": str(rebalance.get("status") or "generated"),
                "plan_date": rebalance.get("plan_date") or rebalance.get("trade_date"),
                "summary": rebalance.get("summary") or {},
                "proposal_count": len(portfolio.get("rebalance_proposal") or portfolio.get("proposal") or []),
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
                "strategy_candidates_created": int(strategy.get("created_count") or 0),
                "portfolio_method": optimizer.get("selected_method") or optimizer.get("optimizer"),
                "execution_cost_version": cost_active.get("version"),
                "execution_cost_status": cost.get("status"),
                "execution_cost_observations": int(
                    ((cost.get("metrics") or {}).get("operation_count"))
                    if isinstance(cost.get("metrics"), dict) else 0
                ),
                "hard_data_status": hard_data.get("status"),
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
            },
            "anomalies": anomalies,
        },
        "healthy": not anomalies,
    }


def run_system_autonomy_cycle() -> dict[str, Any]:
    """Run the existing deterministic modules as one auditable daily bus cycle."""
    from app.data_platform.services.oos_hard_data import audit_oos_hard_data
    from app.services.auto_pipeline import run_daily_maintenance
    from app.services.corporate_actions import run_corporate_action_cycle
    from app.services.execution_cost_calibration import calibrate_execution_cost_model
    from app.services.plan_model_auto_optimizer import run_auto_optimization_once
    from app.services.performance_reports import run_periodic_performance_review
    from app.services.profit_loop import _latest_trade_date, build_profit_loop
    from app.services.simulation_ledger_audit import audit_simulation_ledger
    from app.services.strategy_controller import run_strategy_policy_auto_optimization

    steps = [
        _run_step("hard_data_gate", lambda: audit_oos_hard_data(include_dates=False)),
        _run_step("plan_model_autonomy", lambda: run_auto_optimization_once(force=False)),
        _run_step("strategy_policy_candidates", lambda: run_strategy_policy_auto_optimization(force=False)),
        _run_step("execution_cost_calibration", calibrate_execution_cost_model),
        _run_step(
            "simulation_corporate_actions",
            lambda: run_corporate_action_cycle(as_of_date=_latest_trade_date()),
        ),
        _run_step("simulation_ledger_audit", audit_simulation_ledger),
        _run_step("profit_and_portfolio", build_profit_loop),
        _run_step("periodic_performance_review", run_periodic_performance_review),
        _run_step("research_maintenance", lambda: run_daily_maintenance(max_candidates=50)),
    ]
    summary = _owner_summary(steps)
    return {
        "ok": all(bool(step.get("ok")) for step in steps),
        "schema_version": "alis_system_autonomy_cycle_v1",
        "status": "completed_with_anomalies" if not summary["healthy"] else "completed",
        "steps": steps,
        "owner_summary": summary,
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
    if (
        str(summary.get("schema_version") or "") not in {"alis_owner_daily_summary_v2", "alis_owner_daily_summary_v3"}
        and isinstance(result.get("steps"), list)
    ):
        summary = _owner_summary(result["steps"])
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
    except Exception:
        pass
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
        summary["healthy"] = not anomalies
    except Exception as exc:
        questions = summary.setdefault("questions", {})
        anomalies = questions.setdefault("anomalies", [])
        anomalies.append({"module": "simulation_ledger_audit", "message": f"独立账本审计失败：{str(exc)[:300]}"})
        summary["healthy"] = False
    return {
        "ok": True,
        "available": True,
        "task_id": int(item.get("id") or 0),
        "task_status": str(item.get("status") or ""),
        "finished_at": str(item.get("finished_at") or ""),
        "summary": summary,
    }

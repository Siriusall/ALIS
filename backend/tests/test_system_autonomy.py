import threading
import time

from app.services import system_autonomy
from app.services.system_autonomy import _owner_summary, _skip_experimental_step


def test_owner_summary_uses_current_profit_and_autonomy_contracts():
    summary = _owner_summary(
        [
            {
                "name": "hard_data_gate",
                "ok": True,
                "result": {"status": "ready"},
            },
            {
                "name": "decision_data_gate",
                "ok": True,
                "result": {
                    "status": "degraded",
                    "experiment_ready": False,
                    "issues": [{"code": "latest_adj_factor_not_ready"}],
                },
            },
            {
                "name": "plan_model_autonomy",
                "ok": True,
                "result": {
                    "auto_promoted_count": 1,
                    "auto_rejected_count": 2,
                    "started_count": 3,
                },
            },
            {
                    "name": "strategy_policy_candidates",
                    "ok": True,
                    "result": {"created_count": 4, "learning_candidate_count": 2},
            },
            {
                "name": "execution_cost_calibration",
                "ok": True,
                "result": {
                    "status": "insufficient_observations",
                    "metrics": {"operation_count": 16},
                    "active": {"version": "builtin"},
                },
            },
            {
                "name": "simulation_corporate_actions",
                "ok": True,
                "result": {
                    "materialization": {
                        "applied": [
                            {"action_type": "cash_dividend", "cash_amount": 123.45},
                            {"action_type": "stock_dividend", "cash_amount": 0},
                        ]
                    }
                },
            },
                {
                    "name": "profit_and_portfolio",
                "ok": True,
                "result": {
                    "attribution": {
                        "latest_daily_return_pct": 0.12,
                        "latest_monthly_return_pct": 1.34,
                        "unit": "percentage_points",
                        "status": "ready",
                        "hit_rate": 0.6,
                        "components": {"trading_cost": -0.03},
                    },
                    "portfolio_optimization": {
                        "rebalance_proposal": [{"symbol": "000001"}],
                        "optimizer": {"selected_method": "hrp"},
                    },
                    "portfolio_candles": {
                        "series": [{"date": "20260717"}, {"date": "20260723"}],
                        "latest_comparison": {
                            "date": "20260723",
                            "benchmark_return_pct": 2.5,
                            "excess_return_pct": -1.25,
                        },
                    },
                    "rebalance_plan": {"status": "planned", "trade_date": "20260723"},
                    "model_monitor": {
                        "items": [
                            {
                                "model_key": "capital_persistence_multitask_v1",
                                "status": "degraded",
                                "weight_multiplier": 0.55,
                                "reason": "live_ic_below_threshold",
                            },
                            {"model_key": "leader_lambdarank_v1", "status": "production"},
                        ]
                    },
                    },
                },
                {
                    "name": "periodic_performance_review",
                    "ok": True,
                    "result": {
                        "status": "attention",
                        "learning_signal_count": 2,
                        "learning_signals": [
                            {"code": "negative_excess_return", "severity": "warning"},
                            {"code": "high_turnover", "severity": "warning"},
                        ],
                    },
                },
                {
                    "name": "review_data_quality_governance",
                    "ok": True,
                    "result": {
                        "status": "audited_no_actionable_hard_gap",
                        "repair": {"dates": []},
                    },
                },
                {
                    "name": "strategy_policy_maturity",
                    "ok": True,
                    "result": {"counts": {"re_evaluated": 3, "ready_for_review": 1}},
                },
            ]
        )

    assert summary["schema_version"] == "alis_owner_daily_summary_v3"
    assert summary["questions"]["profit"]["daily_return_pct"] == 0.12
    assert summary["questions"]["profit"]["monthly_return_pct"] == 1.34
    assert summary["questions"]["profit"]["trading_cost_contribution"] == -0.03
    assert summary["questions"]["profit"]["benchmark_return"] == 2.5
    assert summary["questions"]["profit"]["excess_return"] == -1.25
    assert summary["questions"]["profit"]["benchmark_period_start"] == "20260717"
    changes = summary["questions"]["system_changes"]
    assert changes["strategy_candidates_created"] == 4
    assert changes["strategy_learning_candidate_count"] == 2
    assert changes["portfolio_method"] == "hrp"
    assert changes["execution_cost_version"] == "builtin"
    assert changes["execution_cost_observations"] == 16
    assert changes["hard_data_status"] == "ready"
    assert changes["decision_data_status"] == "degraded"
    assert changes["decision_data_experiment_ready"] is False
    assert changes["decision_data_issue_codes"] == ["latest_adj_factor_not_ready"]
    assert changes["corporate_actions_applied"] == 2
    assert changes["dividend_cash_applied"] == 123.45
    assert changes["performance_review_status"] == "attention"
    assert changes["performance_learning_signal_count"] == 2
    assert changes["data_quality_governance_status"] == "audited_no_actionable_hard_gap"
    assert changes["data_quality_repair_count"] == 0
    assert changes["strategy_candidates_re_evaluated"] == 3
    assert changes["strategy_candidates_ready_for_review"] == 1
    assert changes["model_degraded_count"] == 1
    assert changes["degraded_models"] == [{
        "model_key": "capital_persistence_multitask_v1",
        "weight_multiplier": 0.55,
        "reason": "live_ic_below_threshold",
    }]


def test_data_gate_skip_keeps_research_mutation_out_of_the_cycle():
    step = _skip_experimental_step("strategy_policy_candidates", {
        "status": "degraded",
        "experiment_ready": False,
        "issues": [{"code": "latest_adj_factor_not_ready"}],
    })

    assert step["ok"] is True
    assert step["health"]["status"] == "observing"
    assert step["result"]["reason"] == "skipped_latest_decision_data_gate"


def test_step_timeout_records_failure_without_blocking_bus(monkeypatch):
    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.02)

    step = system_autonomy._run_step("timeout_test", lambda: (time.sleep(0.08), {"ok": True})[1])

    assert step["ok"] is False
    assert step["result"]["status"] == "timed_out"


def test_timed_out_step_blocks_same_name_reentry_until_original_worker_finishes(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.02)

    first = system_autonomy._run_step(
        "reentry_test",
        lambda: (release.wait(1), {"ok": True})[1],
    )
    second = system_autonomy._run_step("reentry_test", lambda: {"ok": True})

    assert first["result"]["status"] == "timed_out"
    assert second["error"] == "previous_autonomy_step_still_running"
    release.set()
    deadline = time.monotonic() + 1
    while "reentry_test" in system_autonomy._running_steps and time.monotonic() < deadline:
        time.sleep(0.01)
    third = system_autonomy._run_step("reentry_test", lambda: {"ok": True, "status": "completed"})
    assert third["ok"] is True


def test_full_autonomy_bus_continues_to_terminal_result_after_one_step_times_out(monkeypatch):
    release = threading.Event()
    original_run_step = system_autonomy._run_step
    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(
        "app.services.production_strategy.champion_execution_gate",
        lambda: {"status": "normal"},
    )
    monkeypatch.setattr("app.services.incident_supervisor.get_env_value", lambda *_args: "")

    def controlled_run_step(name, fn):
        if name == "hard_data_gate":
            return original_run_step(name, lambda: {"ok": True, "status": "ready"})
        if name == "decision_data_gate":
            return original_run_step(
                name,
                lambda: {"ok": True, "status": "ready", "experiment_ready": True},
            )
        if name == "pmi_demand_proxies":
            return original_run_step(
                name,
                lambda: (release.wait(1), {"ok": True, "status": "completed"})[1],
            )
        if name in {"autonomous_self_healing", "llm_incident_supervisor"}:
            return original_run_step(name, fn)
        return original_run_step(name, lambda: {"ok": True, "status": "completed"})

    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)

    result = system_autonomy.run_system_autonomy_cycle()
    steps = {step["name"]: step for step in result["steps"]}

    assert steps["pmi_demand_proxies"]["result"]["status"] == "timed_out"
    assert steps["cn_ppi_raw"]["ok"] is True
    assert steps["llm_proposal_router"]["ok"] is True
    assert steps["autonomous_self_healing"]["result"]["status"] == "blocked"
    assert steps["llm_incident_supervisor"]["result"]["status"] == "self_managed"
    assert steps["llm_incident_supervisor"]["result"]["owner_action_required_count"] == 0
    assert result["status"] == "completed"
    release.set()

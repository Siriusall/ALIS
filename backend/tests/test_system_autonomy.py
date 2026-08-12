import threading
import time

from app.services import system_autonomy
from app.services.system_autonomy import _owner_rebalance_action, _owner_summary, _skip_decision_step, _skip_experimental_step


def test_owner_rebalance_action_keeps_weights_and_trade_amount():
    action = _owner_rebalance_action({
        "symbol": "TEST001",
        "name": "测试资产",
        "action": "reduce",
        "reason": "字段传递测试",
        "current_weight": 0.12,
        "target_weight": 0.08,
        "weight_change": -0.04,
        "current_amount": 24000.0,
        "target_amount": 16000.0,
        "estimated_trade_amount": 8000.0,
    })

    assert action["current_weight"] == 0.12
    assert action["target_weight"] == 0.08
    assert action["estimated_trade_amount"] == 8000.0


def test_owner_summary_cannot_hide_deterministic_failure_behind_incident_supervisor():
    summary = _owner_summary([
        {
            "name": "theme_revenue_exposure",
            "ok": False,
            "error": "autonomy_step_timeout:120s",
            "health": {"status": "failed", "reason": "timeout"},
        },
        {
            "name": "llm_incident_supervisor",
            "ok": True,
            "result": {"owner_action_required_count": 0},
        },
    ])

    assert summary["healthy"] is False
    assert summary["questions"]["anomalies"][0]["module"] == "theme_revenue_exposure"


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


def test_decision_gate_is_independent_from_experiment_gate():
    gate = {
        "status": "decision_ready_experiment_blocked",
        "decision_ready": True,
        "experiment_ready": False,
        "issues": [{"code": "long_history_not_ready"}],
    }
    decision_skip = _skip_decision_step("profit_and_portfolio", {**gate, "decision_ready": False})
    experiment_skip = _skip_experimental_step("factor_daily_cycle", gate)

    assert decision_skip["result"]["data_gate"]["decision_ready"] is False
    assert decision_skip["result"]["reason"] == "skipped_decision_data_gate"
    assert experiment_skip["result"]["data_gate"]["experiment_ready"] is False


def test_autonomy_runs_production_decision_when_experiments_are_not_ready(monkeypatch):
    invoked = []

    def controlled_run_step(name, fn):
        invoked.append(name)
        if name == "hard_data_gate":
            result = {"ok": True, "status": "ready"}
        elif name == "decision_data_gate":
            result = {
                "ok": True,
                "status": "decision_ready_experiment_blocked",
                "decision_ready": True,
                "experiment_ready": False,
            }
        elif name == "autonomous_self_healing":
            result = {"ok": True, "status": "completed", "components": {}}
        elif name == "llm_incident_supervisor":
            result = {"ok": True, "status": "self_managed", "owner_action_required_count": 0}
        else:
            result = {"ok": True, "status": "completed"}
        return {"name": name, "ok": True, "result": result, "health": {"status": "healthy"}}

    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)
    monkeypatch.setattr(
        "app.services.production_strategy.champion_execution_gate",
        lambda: {"status": "allowed", "mode": "normal"},
    )

    result = system_autonomy.run_system_autonomy_cycle()
    steps = {step["name"]: step for step in result["steps"]}

    assert "profit_and_portfolio" in invoked
    assert steps["profit_and_portfolio"]["result"]["status"] == "completed"
    assert "factor_daily_cycle" not in invoked
    assert steps["factor_daily_cycle"]["result"]["status"] == "observing"


def test_autonomy_freezes_production_plan_before_long_experiments(monkeypatch):
    invoked = []

    def controlled_run_step(name, fn):
        invoked.append(name)
        if name == "hard_data_gate":
            result = {"ok": True, "status": "ready"}
        elif name == "decision_data_gate":
            result = {
                "ok": True,
                "status": "ready",
                "decision_ready": True,
                "experiment_ready": True,
            }
        elif name == "autonomous_self_healing":
            result = {"ok": True, "status": "completed", "components": {}}
        elif name == "llm_incident_supervisor":
            result = {"ok": True, "status": "self_managed", "owner_action_required_count": 0}
        else:
            result = {"ok": True, "status": "completed"}
        return {"name": name, "ok": True, "result": result, "health": {"status": "healthy"}}

    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)
    monkeypatch.setattr(
        "app.services.production_strategy.champion_execution_gate",
        lambda: {"status": "allowed", "mode": "normal"},
    )

    system_autonomy.run_system_autonomy_cycle()

    assert invoked.index("execution_cost_calibration") < invoked.index("profit_and_portfolio")
    assert invoked.index("simulation_corporate_actions") < invoked.index("profit_and_portfolio")
    assert invoked.index("simulation_ledger_audit") < invoked.index("profit_and_portfolio")
    assert invoked.index("profit_and_portfolio") < invoked.index("factor_daily_cycle")
    assert invoked.index("profit_and_portfolio") < invoked.index("plan_model_autonomy")
    assert invoked.index("profit_and_portfolio") < invoked.index("strategy_shadow_recovery")
    assert "data_operations" in invoked
    assert "llm_data_operations" not in invoked


def test_recovery_cycle_retries_strategy_shadow_after_data_becomes_ready(monkeypatch):
    calls = []

    def controlled_run_step(name, fn):
        if name == "hard_data_gate":
            result = {"ok": True, "status": "ready"}
        elif name == "decision_data_gate":
            result = {
                "ok": True,
                "status": "ready",
                "decision_ready": True,
                "experiment_ready": True,
            }
        elif name == "strategy_shadow_recovery":
            result = fn()
        elif name == "autonomous_self_healing":
            result = {"ok": True, "status": "completed", "components": {}}
        elif name == "llm_incident_supervisor":
            result = {"ok": True, "status": "self_managed", "owner_action_required_count": 0}
        else:
            result = {"ok": True, "status": "completed"}
        return {"name": name, "ok": True, "result": result, "health": {"status": "healthy"}}

    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)
    monkeypatch.setattr(
        "app.services.evolution_orchestrator.enqueue_strategy_shadow_daily_task",
        lambda trade_date, **kwargs: calls.append((trade_date, kwargs)) or {"ok": True, "status": "queued"},
    )

    result = system_autonomy.run_system_autonomy_cycle(trade_date="20260810")

    assert calls == [("20260810", {})]
    assert next(
        step for step in result["steps"] if step["name"] == "strategy_shadow_recovery"
    )["result"]["status"] == "queued"


def test_recovery_cycle_does_not_run_strategy_shadow_before_experiment_data_ready(monkeypatch):
    invoked = []

    def controlled_run_step(name, _fn):
        invoked.append(name)
        if name == "hard_data_gate":
            result = {"ok": True, "status": "ready"}
        elif name == "decision_data_gate":
            result = {
                "ok": False,
                "status": "blocked",
                "decision_ready": False,
                "experiment_ready": False,
            }
        elif name == "autonomous_self_healing":
            result = {"ok": True, "status": "completed", "components": {}}
        elif name == "llm_incident_supervisor":
            result = {"ok": True, "status": "self_managed", "owner_action_required_count": 0}
        else:
            result = {"ok": True, "status": "completed"}
        return {"name": name, "ok": True, "result": result, "health": {"status": "healthy"}}

    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)

    result = system_autonomy.run_system_autonomy_cycle(trade_date="20260810")
    step = next(
        item for item in result["steps"] if item["name"] == "strategy_shadow_recovery"
    )

    assert "strategy_shadow_recovery" not in invoked
    assert step["result"]["reason"] == "skipped_latest_decision_data_gate"


def test_recovery_cycle_forwards_frozen_trade_date_to_plan_generation(monkeypatch):
    captured = {}

    def fake_health(trade_date=""):
        captured["health_trade_date"] = trade_date
        return {
            "ok": True,
            "status": "ready",
            "decision_ready": True,
            "experiment_ready": False,
            "readiness": {"target_trade_date": trade_date},
        }

    def fake_profit_loop(*, signal_date="", persist=True):
        captured["plan_trade_date"] = signal_date
        return {"ok": True, "rebalance_plan": {"trade_date": signal_date}}

    def controlled_run_step(name, fn):
        if name in {"decision_data_gate", "profit_and_portfolio"}:
            result = fn()
        elif name == "hard_data_gate":
            result = {"ok": True, "status": "ready"}
        elif name == "autonomous_self_healing":
            result = {"ok": True, "status": "completed", "components": {}}
        elif name == "llm_incident_supervisor":
            result = {"ok": True, "status": "self_managed", "owner_action_required_count": 0}
        else:
            result = {"ok": True, "status": "completed"}
        return {"name": name, "ok": True, "result": result, "health": {"status": "healthy"}}

    monkeypatch.setattr("app.services.data_health.assess_latest_decision_data_health", fake_health)
    monkeypatch.setattr("app.services.profit_loop.build_profit_loop", fake_profit_loop)
    monkeypatch.setattr(system_autonomy, "_run_step", controlled_run_step)

    system_autonomy.run_system_autonomy_cycle(trade_date="20260705")

    assert captured == {
        "health_trade_date": "20260705",
        "plan_trade_date": "20260705",
    }


def _slo_task(task_id: int, trade_date: str, *, status: str = "completed", cash_drift: float = 0.0) -> dict:
    return {
        "id": task_id, "task_type": "system_autonomy_cycle", "status": status,
        "created_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T17:00:00+08:00",
        "payload": {"trade_date": trade_date},
        "result": {"steps": [
            {"name": "hard_data_gate", "ok": True, "result": {"status": "ready"}},
            {"name": "decision_data_gate", "ok": True, "result": {"target_trade_date": trade_date, "decision_ready": True}},
            {"name": "simulation_ledger_audit", "ok": cash_drift == 0, "result": {"cash_drift": cash_drift, "position_deltas": []}},
        ]} if status == "completed" else {},
    }


def test_autonomy_slo_observes_until_twenty_sessions_then_passes():
    tasks = [_slo_task(index, f"202607{index:02d}") for index in range(1, 21)]
    observing = system_autonomy.build_autonomy_slo(tasks[:19])
    passed = system_autonomy.build_autonomy_slo(tasks)
    assert observing["status"] == "observing"
    assert observing["observed_sessions"] == 19
    assert passed["status"] == "passed"
    assert passed["metrics"]["cycle_completion_rate"] == 1.0
    assert passed["metrics"]["duplicate_successful_cycle_count"] == 0


def test_autonomy_slo_fails_duplicate_success_and_ledger_drift():
    tasks = [_slo_task(index, f"202607{index:02d}") for index in range(1, 21)]
    tasks.append(_slo_task(21, "20260720"))
    tasks[0] = _slo_task(1, "20260701", cash_drift=1.0)
    result = system_autonomy.build_autonomy_slo(tasks)
    assert result["status"] == "failed"
    assert result["metrics"]["duplicate_successful_cycle_count"] == 1
    assert result["gates"]["no_duplicate_successful_cycles"] is False
    assert result["gates"]["ledger_zero_drift_rate"] is False


def test_step_timeout_records_failure_without_blocking_bus(monkeypatch):
    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.02)

    step = system_autonomy._run_step("timeout_test", lambda: (time.sleep(0.08), {"ok": True})[1])

    assert step["ok"] is False
    assert step["result"]["status"] == "timed_out"
    assert step["result"]["cancelled_postgres_connections"] == 0
    assert "lease_blocks_cross_worker_reentry" in step["timing"]["policy"]


def test_step_timeout_actively_cancels_postgres_query(monkeypatch):
    from app.db.store import _conn

    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.05)

    def slow_query():
        with _conn() as conn:
            conn.execute("SELECT pg_sleep(5)")
        return {"ok": True}

    step = system_autonomy._run_step("postgres_cancel_test", slow_query)

    assert step["result"]["status"] == "timed_out"
    assert step["result"]["cancelled_postgres_connections"] == 1
    deadline = time.monotonic() + 1
    while "postgres_cancel_test" in system_autonomy._running_steps and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "postgres_cancel_test" not in system_autonomy._running_steps


def test_known_long_step_uses_independent_timeout_budget(monkeypatch):
    monkeypatch.setattr(system_autonomy, "_STEP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(system_autonomy, "_LONG_STEP_TIMEOUT_SECONDS", 0.2)

    step = system_autonomy._run_step(
        "factor_daily_cycle",
        lambda: (time.sleep(0.04), {"ok": True, "status": "completed"})[1],
    )

    assert step["ok"] is True
    assert step["result"]["status"] == "completed"


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
    monkeypatch.setattr(system_autonomy, "_LONG_STEP_TIMEOUT_SECONDS", 0.02)
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
    assert result["status"] == "completed_with_anomalies"
    release.set()

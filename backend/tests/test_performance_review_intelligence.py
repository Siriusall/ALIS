from contextlib import contextmanager

from app.services.performance_reports import enrich_performance_period
from app.services import strategy_controller
from app.services import evolution_orchestrator
from app.services.strategy_controller import _learning_signal_suggestions


def test_strategy_policy_shadow_materialization_reads_decisions_once_for_all_candidates(monkeypatch) -> None:
    queries = []
    inserts = []

    class Result:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            queries.append(sql)
            if "FROM decision_snapshots" in sql:
                return Result([
                    {"id": 10, "decision_time": "2026-08-01T15:00:00", "decision_type": "buy", "research_state_json": '{"score":90}', "target_weight_contract_json": "{}"},
                    {"id": 11, "decision_time": "2026-08-02T15:00:00", "decision_type": "buy", "research_state_json": '{"score":90}', "target_weight_contract_json": "{}"},
                ])
            if "SELECT candidate_version_id,decision_snapshot_id" in sql:
                return Result([{"candidate_version_id": 1, "decision_snapshot_id": 10}])
            if "INSERT INTO strategy_policy_shadow_observations" in sql:
                inserts.append(params)
            return Result()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(strategy_controller, "_ensure_strategy_policy_shadow_tables", lambda: None)
    monkeypatch.setattr(strategy_controller, "business_conn", connection)
    monkeypatch.setattr(strategy_controller, "list_model_versions", lambda *_args, **_kwargs: [
        {"id": 1, "status": "candidate", "is_active": False, "version": "v1", "created_at": "2026-08-01T00:00:00", "config": {"policy": {}}},
        {"id": 2, "status": "candidate", "is_active": False, "version": "v2", "created_at": "2026-08-02T00:00:00", "config": {"policy": {}}},
    ])

    result = strategy_controller.materialize_strategy_policy_shadow_observations()

    assert sum("FROM decision_snapshots" in sql for sql in queries) == 1
    assert sum("SELECT candidate_version_id,decision_snapshot_id" in sql for sql in queries) == 1
    assert {(row[0], row[1]) for row in inserts} == {(1, 11), (2, 11)}
    assert result["observations_written"] == 2


def test_period_review_groups_market_model_cost_and_errors() -> None:
    report = {
        "start_date": "2026-07-01",
        "end_date": "2026-07-02",
        "return_pct": -1.0,
        "benchmark_return_pct": 1.0,
        "turnover_rate": 0.4,
        "traded_amount": 10000.0,
    }
    points = [
        {"date": "2026-07-01", "daily_return_pct": 0.0, "daily_benchmark_return_pct": None},
        {"date": "2026-07-02", "daily_return_pct": -1.0, "daily_benchmark_return_pct": 1.0},
    ]
    operations = [
        {"side": "BUY", "price": 10.0, "quantity": 500, "commission": 5.0, "transfer_fee": 1.0, "stamp_tax": 0.0},
        {"side": "SELL", "price": 10.0, "quantity": 500, "commission": 5.0, "transfer_fee": 1.0, "stamp_tax": 5.0},
    ]
    decisions = [
        {"id": 7, "decision_time": "2026-07-02T15:00:00", "decision_type": "buy", "model_version": "rank-v1", "ts_code": "000001.SZ"}
    ]
    reviews = [
        {"id": 1, "decision_snapshot_id": 7, "review_time": "2026-07-03", "error_type": "模型失效"},
        {"id": 2, "decision_snapshot_id": 7, "review_time": "2026-07-04", "error_type": "数据质量"},
    ]
    states = {
        "20260701": {"state": "S2", "confidence": 0.8, "model_version": "market-v1"},
        "20260702": {"state": "S2", "confidence": 0.6, "model_version": "market-v1"},
    }

    enriched = enrich_performance_period(
        report,
        points=points,
        operations=operations,
        decisions=decisions,
        reviews=reviews,
        market_states=states,
        blocked_events=[{"code": "market_risk_gate"}],
        version_manifest={"manifest_hash": "abc"},
    )

    assert enriched["schema_version"] == "formal_performance_report_v2"
    assert enriched["costs"] == {
        "commission": 10.0, "transfer_fee": 2.0, "stamp_tax": 5.0, "total": 17.0
    }
    assert enriched["market_state_breakdown"]["S2"]["sessions"] == 2
    assert enriched["market_state_breakdown"]["S2"]["return_pct"] == -1.0
    assert enriched["market_state_breakdown"]["S2"]["benchmark_return_pct"] is None
    assert enriched["market_state_breakdown"]["S2"]["benchmark_coverage"]["complete"] is False
    assert enriched["model_version_breakdown"]["rank-v1"]["error_types"] == {"数据质量": 1}
    assert enriched["error_breakdown"]["blocked_signal_codes"] == {"market_risk_gate": 1}
    assert {item["code"] for item in enriched["learning_signals"]} == {
        "negative_excess_return", "high_turnover", "review_errors", "blocked_signals"
    }


def test_learning_signals_create_bounded_strategy_candidates_and_route_data_quality() -> None:
    suggestions, governance = _learning_signal_suggestions(
        {
            "max_monthly_turnover": 0.30,
            "add_position_score": 80.0,
            "loss_review_threshold": -0.06,
        },
        {
            "period": "2026-07",
            "learning_signals": [
                {"code": "high_turnover", "severity": "warning"},
                {"code": "negative_excess_return", "severity": "warning"},
                {"code": "review_errors", "severity": "warning"},
            ],
            "error_breakdown": {"review_error_types": {"模型失效": 1, "数据质量": 2}},
        },
    )

    assert [item["name"] for item in suggestions] == [
        "复盘驱动：收紧月度换手上限",
        "复盘驱动：提高新仓确认门槛",
        "复盘驱动：扩张期满权益暴露",
        "复盘驱动：模型失效更早复核",
    ]
    assert suggestions[0]["patch"]["rebalance_rules"]["max_monthly_turnover"] == 0.25
    assert suggestions[1]["patch"]["signal_thresholds"]["add_position_score"] == 83.0
    assert suggestions[2]["patch"]["market_state_policy"]["risk_expansion"]["max_equity"] == 1.0
    assert suggestions[3]["patch"]["signal_thresholds"]["loss_review_threshold"] == -0.05
    assert suggestions[0]["learning_evidence"]["period"] == "2026-07"
    assert governance == [{
        "target": "data_governance",
        "code": "review_data_quality_errors",
        "count": 2,
        "reason": "数据质量错误不得被转化为交易参数候选；保留给硬数据与数据质量治理。",
    }]


def test_waiting_strategy_candidates_re_evaluate_after_outcomes_materialize(monkeypatch) -> None:
    monkeypatch.setattr(strategy_controller, "materialize_decision_outcomes", lambda **_: {"created": 2, "pending": 0})
    monkeypatch.setattr(strategy_controller, "list_model_versions", lambda *_args, **_kwargs: [
        {"id": 52, "version": "candidate-a", "status": "candidate", "is_active": False,
         "validation": {"status": "blocked_by_data"}, "config": {"learning_evidence": {"period": "2026-07"}}},
        {"id": 99, "version": "active", "status": "active", "is_active": True,
         "validation": {"status": "passed"}, "config": {}},
    ])
    monkeypatch.setattr(strategy_controller, "evaluate_strategy_policy_version", lambda version_id: {
        "status": "passed", "metrics": {"sample_rows": 5, "shadow_sessions": 20}, "validation": {"blocked_reasons": []},
    })
    monkeypatch.setattr(evolution_orchestrator, "create_strategy_controller_oos_task", lambda *_args, **_kwargs: {"task": {"id": 88}})

    result = strategy_controller.advance_waiting_strategy_policy_candidates()

    assert result["outcomes_materialized"] == {"created": 2, "pending": 0}
    assert result["counts"] == {"re_evaluated": 1, "still_waiting": 0, "formal_oos_queued": 1, "rejected": 0}
    assert result["candidates"][0]["version_id"] == 52
    assert result["candidates"][0]["status"] == "passed"
    assert result["candidates"][0]["formal_oos_task_id"] == 88


def test_policy_candidate_cannot_use_champion_outcomes_without_its_own_shadow(monkeypatch) -> None:
    candidate = {
        "id": 52,
        "model_key": "strategy_controller",
        "is_active": False,
        "config": {"policy": strategy_controller.DEFAULT_STRATEGY_POLICY},
    }
    active = {
        "id": 1,
        "model_key": "strategy_controller",
        "is_active": True,
        "config": {"policy": strategy_controller.DEFAULT_STRATEGY_POLICY},
    }
    monkeypatch.setattr(strategy_controller, "get_model_version", lambda _id: candidate)
    monkeypatch.setattr(strategy_controller, "get_active_model_version", lambda _key: active)
    monkeypatch.setattr(strategy_controller, "update_model_version_validation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(strategy_controller, "update_model_version_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        strategy_controller,
        "_candidate_shadow_metrics",
        lambda *_args: strategy_controller._missing_candidate_shadow_metrics(),
    )
    result = strategy_controller.evaluate_strategy_policy_version(52)

    assert result["status"] == "blocked_by_data"
    assert "missing_candidate_specific_shadow_outcomes" in result["validation"]["blocked_reasons"]


def test_policy_shadow_action_uses_candidate_threshold_on_frozen_opportunity() -> None:
    policy = strategy_controller._deep_merge(
        strategy_controller.DEFAULT_STRATEGY_POLICY,
        {"signal_thresholds": {"add_position_score": 83}},
    )
    action, would_trade, score = strategy_controller._shadow_action_for_decision(
        {"decision_type": "BUY", "research": {"score": 82}, "target_weight_contract": {}}, policy,
    )
    assert (action, would_trade, score) == ("buy_candidate", False, 82.0)

    action, would_trade, score = strategy_controller._shadow_action_for_decision(
        {"decision_type": "BUY", "research": {"score": 84}, "target_weight_contract": {}}, policy,
    )
    assert (action, would_trade, score) == ("add_position", True, 84.0)

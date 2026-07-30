import pickle

import numpy as np

from app.services import plan_model_control


class MissingClassClassifier:
    classes_ = np.asarray([0, 1, 2, 4])

    def predict_proba(self, values):
        return np.tile(np.asarray([[0.1, 0.2, 0.3, 0.4]]), (len(values), 1))


class NamedFeatureClassifier:
    classes_ = np.asarray([0, 1])
    feature_names_in_ = np.asarray(["momentum_20", "volatility_20"])

    def predict_proba(self, values):
        assert list(values.columns) == list(self.feature_names_in_)
        return np.tile(np.asarray([[0.25, 0.75]]), (len(values), 1))


def test_canonical_class_probabilities_preserve_fixed_output_contract():
    probabilities = plan_model_control._canonical_class_probabilities(
        MissingClassClassifier(),
        np.zeros((2, 3)),
        class_count=5,
    )

    assert probabilities.shape == (2, 5)
    assert probabilities[0].tolist() == [0.1, 0.2, 0.3, 0.0, 0.4]


def test_predictions_preserve_estimator_feature_names():
    probabilities = plan_model_control._canonical_class_probabilities(
        NamedFeatureClassifier(),
        np.asarray([[0.2, 0.4], [0.1, 0.3]]),
        class_count=2,
    )

    assert probabilities.tolist() == [[0.25, 0.75], [0.25, 0.75]]


class DummyModel:
    def predict(self, rows):
        return [float(rows[0][1]) + 0.25]


class DummyProbabilityModel:
    def predict_proba(self, rows):
        return np.asarray([[0.05, 0.10, 0.15, 0.20, 0.50] for _ in rows])


class DummyBinaryProbabilityModel:
    classes_ = np.asarray([0, 1])

    def predict_proba(self, rows):
        return np.asarray([[0.5, 0.5] for _ in rows])


class DummyConstantModel:
    def __init__(self, value):
        self.value = value

    def predict(self, rows):
        return np.asarray([self.value for _ in rows])


class DummyBatchModel:
    def predict(self, rows):
        return np.asarray([float(row[0]) + float(row[1]) for row in rows])


def test_model_score_returns_one_score_per_input_row():
    values = np.asarray([[0.1, 0.2], [-0.1, 0.3]])

    scores = plan_model_control._model_score(DummyBatchModel(), values)

    assert np.allclose(scores, [0.3, 0.2])


def test_batch_model_scoring_matches_single_symbol_contract():
    artifact = {
        "_model": DummyBatchModel(),
        "features": ["momentum_20", "momentum_60"],
        "model_type": "test_ranker",
        "version": "test-v1",
    }
    features = {
        "000001": {"momentum_20": 0.1, "momentum_60": 0.2},
        "600000": {"momentum_20": -0.1, "momentum_60": 0.3},
    }
    batch = plan_model_control.score_plan_model_feature_batch(
        "medium_horizon_lambdarank_v1",
        features,
        artifact=artifact,
    )

    for symbol, values in features.items():
        single = plan_model_control.score_plan_model_features(
            "medium_horizon_lambdarank_v1",
            values,
            artifact=artifact,
        )
        assert batch[symbol] == single


def test_walk_forward_uses_full_calendar_contract():
    dates = [f"{index:08d}" for index in range(1, 1701)]

    windows = plan_model_control._walk_forward_date_windows(dates)

    assert len(windows) == 5
    for window in windows:
        assert len(window["train"]) >= 756
        assert len(window["validation"]) == 126
        assert len(window["test"]) == 126
        validation_start = dates.index(window["validation"][0])
        test_start = dates.index(window["test"][0])
        assert validation_start - dates.index(window["train"][-1]) - 1 == 60
        assert test_start - dates.index(window["validation"][-1]) - 1 == 5


def test_walk_forward_rejects_short_calendar():
    dates = [f"{index:08d}" for index in range(1, 1577)]

    try:
        plan_model_control._walk_forward_date_windows(dates)
    except RuntimeError as exc:
        assert "至少需要 1577" in str(exc)
    else:
        raise AssertionError("short calendar must not be reported as a valid five-window run")


def test_training_symbol_sample_is_deterministic_and_cross_history_stable():
    symbols = ["600000.SH", "000001.SZ", "000001.SZ", "300001.SZ", ""]

    first = plan_model_control._deterministic_training_symbol_sample(
        symbols,
        limit=3,
    )
    second = plan_model_control._deterministic_training_symbol_sample(
        list(reversed(symbols)),
        limit=3,
    )

    assert first == second
    assert len(first) == 3
    assert len(set(first)) == 3


def test_moneyflow_query_failure_is_not_silently_converted_to_zero_rows():
    class FailingConnection:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("canceling statement due to statement timeout")

    try:
        plan_model_control._load_training_moneyflow_rows(
            FailingConnection(),
            model_key="capital_persistence_multitask_v1",
            start_date="20200101",
            end_date="20260729",
            symbols=["000001.SZ"],
        )
    except plan_model_control.PlanModelTrainingDataError as exc:
        assert exc.code == "capital_moneyflow_query_failed"
        assert "statement timeout" in str(exc)
    else:
        raise AssertionError("moneyflow query failures must stop the training dataset")


def test_training_failure_classifier_separates_retry_backfill_and_code_errors():
    timeout = plan_model_control.classify_plan_model_training_failure(
        "canceling statement due to statement timeout"
    )
    coverage = plan_model_control.classify_plan_model_training_failure(
        "训练数据不足：领域最低 100/504"
    )
    code = plan_model_control.classify_plan_model_training_failure(
        "NameError: _model_score is not defined"
    )

    assert timeout["category"] == "transient_query_timeout"
    assert timeout["retryable"] is True
    assert coverage["recommended_action"] == "delegate_data_backfill"
    assert code["category"] == "code_contract_error"
    assert code["retryable"] is False


def test_training_failure_is_closed_when_existing_candidate_is_observing(monkeypatch):
    captured = {}
    monkeypatch.setattr(plan_model_control, "get_model_train_run", lambda _run_id: {
        "id": 217,
        "status": "failed",
        "error": "statement timeout",
        "metrics": {"model_key": "medium_horizon_lambdarank_v1"},
    })
    monkeypatch.setattr(
        plan_model_control,
        "_candidate_failure_takeover",
        lambda _model_key: {
            "model_version_id": 216,
            "version": "medium-v216",
            "validation_status": "pending_live_observation",
            "shadow_status": "awaiting_first_future_signal",
            "observed_return_sessions": 0,
            "required_return_sessions": 20,
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda run_id, patch, **_kwargs: captured.update({"run_id": run_id, **patch}) or patch,
    )

    result = plan_model_control.process_plan_model_training_failure({
        "failed_run_id": 217,
        "model_key": "medium_horizon_lambdarank_v1",
        "root_run_id": 217,
        "retry_count": 0,
    })

    assert result["status"] == "resolved_by_existing_candidate_observation"
    assert result["candidate_takeover"]["model_version_id"] == 216
    assert captured["status"] == "resolved_by_existing_candidate_observation"


def test_training_failure_uses_bounded_retry_and_preserves_root_run(monkeypatch):
    captured = {}
    monkeypatch.setattr(plan_model_control, "get_model_train_run", lambda _run_id: {
        "id": 218,
        "status": "failed",
        "error": "capital_moneyflow_query_failed: statement timeout",
        "metrics": {"model_key": "capital_persistence_multitask_v1"},
    })
    monkeypatch.setattr(
        plan_model_control,
        "_candidate_failure_takeover",
        lambda _model_key: None,
    )
    monkeypatch.setattr(
        plan_model_control,
        "request_plan_model_training",
        lambda model_key, **kwargs: captured.update({
            "model_key": model_key,
            "context": kwargs["training_context"],
        }) or {
            "ok": True,
            "started": True,
            "run_id": 219,
            "model_key": model_key,
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda _run_id, patch, **_kwargs: patch,
    )

    result = plan_model_control.process_plan_model_training_failure({
        "failed_run_id": 218,
        "model_key": "capital_persistence_multitask_v1",
        "root_run_id": 210,
        "retry_count": 1,
        "max_retries": 2,
    })

    assert result["status"] == "automatic_retry_started"
    recovery = captured["context"]["failure_recovery"]
    assert recovery["root_run_id"] == 210
    assert recovery["previous_run_id"] == 218
    assert recovery["retry_count"] == 2
    assert recovery["max_retries"] == 2


def test_training_failure_delegates_real_coverage_gap_to_backfill(monkeypatch):
    from app.data_platform.services import backfill

    captured = {}
    monkeypatch.setattr(plan_model_control, "get_model_train_run", lambda _run_id: {
        "id": 220,
        "status": "failed",
        "error": "训练数据不足：领域最低 300/504",
        "metrics": {"model_key": "capital_persistence_multitask_v1"},
    })
    monkeypatch.setattr(plan_model_control, "_candidate_failure_takeover", lambda _model_key: None)
    monkeypatch.setattr(
        backfill,
        "ensure_plan_model_data_backfill",
        lambda: {"ok": True, "created": True, "work_order_id": "model-data-1"},
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda run_id, patch, **_kwargs: captured.update({"run_id": run_id, **patch}) or patch,
    )

    result = plan_model_control.process_plan_model_training_failure({
        "failed_run_id": 220,
        "model_key": "capital_persistence_multitask_v1",
    })

    assert result["status"] == "delegated_to_data_backfill"
    assert result["data_backfill"]["created"] is True
    assert captured["status"] == "delegated_to_data_backfill"


def test_training_failure_stops_after_retry_budget_is_exhausted(monkeypatch):
    captured = {}
    monkeypatch.setattr(plan_model_control, "get_model_train_run", lambda _run_id: {
        "id": 221,
        "status": "failed",
        "error": "canceling statement due to statement timeout",
        "metrics": {"model_key": "medium_horizon_lambdarank_v1"},
    })
    monkeypatch.setattr(plan_model_control, "_candidate_failure_takeover", lambda _model_key: None)
    monkeypatch.setattr(
        plan_model_control,
        "request_plan_model_training",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry budget must stop new training")),
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda run_id, patch, **_kwargs: captured.update({"run_id": run_id, **patch}) or patch,
    )

    result = plan_model_control.process_plan_model_training_failure({
        "failed_run_id": 221,
        "model_key": "medium_horizon_lambdarank_v1",
        "retry_count": 2,
        "max_retries": 2,
    })

    assert result["status"] == "automatic_retry_exhausted"
    assert result["owner_intervention_required"] is True
    assert captured["status"] == "automatic_retry_exhausted"


def test_training_code_contract_failure_escalates_without_retry(monkeypatch):
    captured = {}
    monkeypatch.setattr(plan_model_control, "get_model_train_run", lambda _run_id: {
        "id": 222,
        "status": "failed",
        "error": "NameError: model_features is not defined",
        "metrics": {"model_key": "leader_lambdarank_v1"},
    })
    monkeypatch.setattr(plan_model_control, "_candidate_failure_takeover", lambda _model_key: None)
    monkeypatch.setattr(
        plan_model_control,
        "request_plan_model_training",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("code errors must not loop")),
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda run_id, patch, **_kwargs: captured.update({"run_id": run_id, **patch}) or patch,
    )

    result = plan_model_control.process_plan_model_training_failure({
        "failed_run_id": 222,
        "model_key": "leader_lambdarank_v1",
    })

    assert result["status"] == "escalated_owner_review"
    assert result["owner_intervention_required"] is True
    assert captured["classification"]["category"] == "code_contract_error"


def test_training_failure_requeues_when_previous_coordinator_is_terminal(monkeypatch):
    from app.services import evolution_orchestrator

    calls = []
    monkeypatch.setattr(
        evolution_orchestrator,
        "create_evolution_task",
        lambda **kwargs: calls.append(kwargs) or (
            {"id": 501, "status": "completed"}
            if len(calls) == 1
            else {"id": 502, "status": "queued"}
        ),
    )
    monkeypatch.setattr(
        plan_model_control,
        "_update_training_failure_recovery",
        lambda _run_id, patch, **_kwargs: patch,
    )

    result = plan_model_control._enqueue_plan_model_training_recovery(
        model_key="medium_horizon_lambdarank_v1",
        failed_run_id=223,
        root_run_id=223,
        retry_count=0,
        classification={
            "category": "transient_query_timeout",
            "retryable": True,
            "recommended_action": "bounded_retry",
        },
    )

    assert result["task_id"] == 502
    assert result["status"] == "queued"
    assert len(calls) == 2
    assert calls[1]["force"] is True
    queued_recovery = calls[1]["payload"]["training_failure_recovery"]
    assert queued_recovery["previous_recovery_task_id"] == 501
    assert queued_recovery["requeued_after_terminal_coordinator"] is True


def test_moneyflow_observations_require_real_history_and_future_labels():
    dates = [f"20260{index:03d}" for index in range(1, 36)]
    rows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": trade_date,
            "payload_json": {
                "net_mf_amount": float(index - 10),
                "buy_lg_vol": 100.0 + index,
                "sell_lg_vol": 80.0,
            },
        }
        for index, trade_date in enumerate(dates)
    ]

    observations = plan_model_control._moneyflow_observations(rows, dates)

    assert ("000001.SZ", dates[19]) in observations
    assert ("000001.SZ", dates[24]) in observations
    assert ("000001.SZ", dates[18]) not in observations
    assert ("000001.SZ", dates[25]) not in observations
    assert observations[("000001.SZ", dates[24])]["future_persistence"] > 0


def test_event_usable_trade_date_obeys_source_availability_and_close_cutoff():
    dates = ["20260724", "20260727", "20260728"]

    assert plan_model_control._event_usable_trade_date(
        "2026-07-24T10:00:00",
        "2026-07-24T14:00:00",
        dates,
    ) == "20260724"
    assert plan_model_control._event_usable_trade_date(
        "2026-07-24T10:00:00",
        "2026-07-24T16:00:00",
        dates,
    ) == "20260727"
    assert plan_model_control._event_usable_trade_date(
        "2026-07-26T10:00:00",
        "2026-07-26T11:00:00",
        dates,
    ) == "20260727"


def test_news_event_direction_is_deterministic_and_bounded():
    direction, severity, confidence = plan_model_control._event_news_direction(
        "公司中标重大项目并获批，控股股东同步增持"
    )

    assert direction == "positive"
    assert severity == "high"
    assert 0.0 < confidence <= 0.85


def test_plan_model_family_reports_proxy_when_data_not_ready(monkeypatch):
    monkeypatch.setattr(plan_model_control, "_base_coverage_snapshot", lambda: {})
    monkeypatch.setattr(plan_model_control, "_coverage_snapshot", lambda key, base=None: {
        "trade_sessions": 10,
        "theme_snapshot_sessions": 3,
        "event_rows": 0,
        "capital_coverage_slices": 0,
        "effective_sessions": 10,
        "minimum_sessions": plan_model_control.PLAN_MODELS[key]["minimum_sessions"],
        "data_ready": False,
    })
    monkeypatch.setattr(plan_model_control, "list_model_versions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(plan_model_control, "get_active_model_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plan_model_control, "_latest_run_for_model", lambda _key, runs=None: None)

    result = plan_model_control.model_family_status()

    assert result["ok"] is True
    assert result["counts"]["proxy"] == len(plan_model_control.PLAN_MODELS)
    assert {item["model_key"] for item in result["items"]} >= {
        "medium_horizon_lambdarank_v1",
        "capital_persistence_multitask_v1",
        "leader_lambdarank_v1",
    }


def test_request_unknown_plan_model_is_rejected():
    result = plan_model_control.request_plan_model_training("unknown")

    assert result["ok"] is False
    assert result["error"] == "unknown_plan_model"


def test_governance_version_prefers_shadow_candidate_over_newer_version():
    versions = [
        {
            "id": 302,
            "status": "candidate",
            "metrics": {},
            "validation": {"status": "pending_strategy_ab"},
        },
        {
            "id": 216,
            "status": "candidate",
            "metrics": {"strategy_impact_ab": {"status": "passed"}},
            "validation": {
                "status": "pending_live_observation",
                "model_shadow": {"status": "observing"},
            },
        },
    ]

    selected = plan_model_control._select_plan_model_governance_version(versions)

    assert selected is not None
    assert selected["id"] == 216


def test_request_plan_model_training_stops_while_candidate_governance_is_in_progress(
    monkeypatch,
):
    blocker = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "status": "candidate",
    }
    monkeypatch.setattr(
        plan_model_control,
        "_plan_model_candidate_governance_blocker",
        lambda _model_key: blocker,
    )
    monkeypatch.setattr(
        plan_model_control,
        "create_model_train_run",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("blocked governance must not create a training run")
        ),
    )

    result = plan_model_control.request_plan_model_training(
        "medium_horizon_lambdarank_v1"
    )

    assert result == {
        "ok": True,
        "started": False,
        "reason": "candidate_governance_in_progress",
        "model_key": "medium_horizon_lambdarank_v1",
        "model_version_id": 216,
    }


def test_completed_plan_model_version_wakes_continuous_research(monkeypatch):
    from app.services import evolution_orchestrator

    captured = {}
    monkeypatch.setattr(
        evolution_orchestrator,
        "enqueue_factor_strategy_research_event",
        lambda **kwargs: captured.update(kwargs) or {"id": 81, "status": "queued"},
    )

    result = plan_model_control._enqueue_continuous_research_for_model_version(
        model_key="medium_horizon_lambdarank_v1",
        model_version_id=21,
        version="medium-v2",
    )

    assert result == {"status": "queued", "task_id": 81}
    assert captured["trigger"] == "plan_model_training_completed"
    assert captured["evidence_type"] == "model_version"
    assert captured["evidence_id"] == 21
    assert captured["evidence"]["model_key"] == "medium_horizon_lambdarank_v1"


def test_model_evaluation_records_environment_coverage_without_alpha_score():
    result = plan_model_control._observation_counts(
        ["S0"] * 5 + ["S1"] * 4 + ["S2"] * 3,
        expected=["S0", "S1", "S2", "S3"],
    )

    assert result == {"S0": 5, "S1": 4, "S2": 3, "S3": 0}


def test_review_plan_model_cannot_waive_live_observation(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(plan_model_control, "get_model_version", lambda _id: {
        "id": 11,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "medium_horizon_lambdarank_v1-test",
        "metrics": {
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
            "strategy_impact_ab": {"status": "passed"},
        },
        "validation": {
            "status": "passed",
            "production_eligible": False,
            "production_eligibility": {
                "gates": {
                    "data_ready": True,
                    "rolling_oos": True,
                    "cost_after_backplay": True,
                    "baseline_compared": True,
                    "human_review": False,
                    "live_observation_20_sessions": False,
                }
            },
        },
    })
    monkeypatch.setattr(plan_model_control, "update_model_version_validation", lambda model_version_id, *, validation: captured.update({"model_version_id": model_version_id, "validation": validation}))
    monkeypatch.setattr(plan_model_control, "set_active_model_version", lambda model_version_id: captured.update({"active": model_version_id}))

    result = plan_model_control.review_plan_model_version(
        11,
        reviewer="alice",
        decision="approved",
        comment="模拟系统评审通过",
        waive_live_observation=True,
    )

    assert result["ok"] is True
    assert result["production_eligible"] is False
    assert "active" not in captured
    validation = captured["validation"]
    assert validation["production_eligibility"]["gates"]["human_review"] is True
    assert validation["production_eligibility"]["gates"]["live_observation_20_sessions"] is False
    assert validation["production_eligibility"]["gates"]["simulation_only_live_observation_waived"] is False
    assert "live_observation_waiver_forbidden" in validation["production_eligibility"]["blocked_reasons"]
    assert result["review"]["waiver_policy"] == "forbidden"


def test_review_plan_model_activates_after_real_observation(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(plan_model_control, "get_model_version", lambda _id: {
        "id": 12,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "medium-horizon-observed",
        "metrics": {
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
            "strategy_impact_ab": {"status": "passed"},
        },
        "validation": {
            "status": "passed",
            "production_eligible": False,
            "production_eligibility": {
                "gates": {
                    "data_ready": True,
                    "current_model_evaluation_contract": True,
                    "task_metric_passed": True,
                    "task_native_walk_forward_oos": True,
                    "task_native_baseline_outperformed": True,
                    "task_baseline_comparison_executed": True,
                    "environment_coverage_complete": True,
                    "training_protocol": True,
                    "controlled_strategy_ab": True,
                    "human_review": False,
                    "live_observation_20_sessions": True,
                }
            },
        },
    })
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_validation",
        lambda model_version_id, *, validation: captured.update(
            {"model_version_id": model_version_id, "validation": validation}
        ),
    )
    monkeypatch.setattr(
        plan_model_control,
        "set_active_model_version",
        lambda model_version_id: captured.update({"active": model_version_id}),
    )
    monkeypatch.setattr(
        plan_model_control,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": True, "blocked_reasons": []},
    )

    result = plan_model_control.review_plan_model_version(
        12,
        reviewer="deterministic_auto_governance",
        decision="approved",
        comment="真实观察门通过",
    )

    assert result["production_eligible"] is True
    assert captured["active"] == 12


def test_review_plan_model_cannot_activate_when_current_gate_audit_fails(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(plan_model_control, "get_model_version", lambda _id: {
        "id": 13,
        "model_key": "leader_lambdarank_v1",
        "version": "candidate-with-stale-evidence",
        "metrics": {
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
            "strategy_impact_ab": {"status": "passed"},
        },
        "validation": {
            "production_eligible": False,
            "production_eligibility": {
                "gates": {
                    "data_ready": True,
                    "controlled_strategy_ab": True,
                    "live_observation_20_sessions": True,
                    "human_review": False,
                }
            },
        },
    })
    monkeypatch.setattr(
        plan_model_control,
        "audit_plan_model_production_eligibility",
        lambda _version: {
            "passed": False,
            "blocked_reasons": ["task_native_walk_forward_oos"],
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_validation",
        lambda model_version_id, *, validation: captured.update(
            {"model_version_id": model_version_id, "validation": validation}
        ),
    )
    monkeypatch.setattr(
        plan_model_control,
        "set_active_model_version",
        lambda model_version_id: captured.update({"active": model_version_id}),
    )

    result = plan_model_control.review_plan_model_version(
        13,
        reviewer="deterministic_auto_governance",
        decision="approved",
    )

    assert result["production_eligible"] is False
    assert "active" not in captured
    assert (
        "current_production_gate:task_native_walk_forward_oos"
        in result["production_eligibility"]["blocked_reasons"]
    )


def test_plan_model_scorer_returns_numeric_score(monkeypatch):
    monkeypatch.setattr(plan_model_control, "get_active_model_version", lambda _key: None)
    result = plan_model_control.score_plan_model_features(
        "medium_horizon_lambdarank_v1",
        {"momentum_60": 1.0, "volatility_20": 0.5, "capital_persistence": 0.8},
        allow_proxy=True,
    )

    assert result["ok"] is True
    assert isinstance(result["score"], float)
    assert result["model_type"] == "plan_linear_ranker"


def test_plan_model_scorer_does_not_silently_use_proxy(monkeypatch):
    monkeypatch.setattr(plan_model_control, "get_active_model_version", lambda _key: None)

    result = plan_model_control.score_plan_model_features(
        "medium_horizon_lambdarank_v1",
        {"momentum_60": 1.0},
    )

    assert result == {
        "ok": False,
        "model_key": "medium_horizon_lambdarank_v1",
        "score": None,
        "reason": "active_model_artifact_unavailable",
        "model_type": "unavailable",
    }


def test_plan_model_scorer_loads_lightgbm_artifact(tmp_path):
    model_path = tmp_path / "model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(DummyModel(), handle)

    result = plan_model_control.score_plan_model_features(
        "medium_horizon_lambdarank_v1",
        {"momentum_60": 0.75},
        artifact={
            "version": "medium-test",
            "features": plan_model_control.PLAN_MODEL_FEATURES,
            "model_path": str(model_path),
            "model_type": "plan_lightgbm_ranker",
        },
    )

    assert result["ok"] is True
    assert result["model_type"] == "plan_lightgbm_ranker"
    assert result["score"] == 1.0


def test_train_all_only_starts_trainable(monkeypatch):
    monkeypatch.setattr(plan_model_control, "model_family_status", lambda: {
        "items": [
            {"model_key": "medium_horizon_lambdarank_v1", "status": "trainable"},
            {"model_key": "leader_lambdarank_v1", "status": "proxy"},
        ]
    })
    monkeypatch.setattr(plan_model_control, "request_plan_model_training", lambda key: {"ok": True, "started": True, "model_key": key})

    result = plan_model_control.train_all_trainable_plan_models()

    assert result["started_count"] == 1
    assert result["skipped_count"] == 1


def test_current_gate_audit_marks_legacy_active_model_without_current_evidence():
    result = plan_model_control._current_production_gate_audit(
        {
            "metrics": {"walk_forward_lift": -0.01, "walk_forward_windows": 4},
            "validation": {"production_eligible": True},
        },
        {"artifact_ready": True},
    )

    assert result["passed"] is False
    assert result["status"] == "legacy_production_current_gate_not_passed"
    assert "walk_forward_windows" in result["blocked_reasons"]
    assert "current_task_native_evaluation_contract" in result["blocked_reasons"]
    assert "task_native_walk_forward_oos" in result["blocked_reasons"]


def test_current_gate_does_not_confuse_baseline_comparison_with_outperformance():
    result = plan_model_control._current_production_gate_audit(
        {
            "metrics": {
                "model_evaluation_contract": (
                    plan_model_control.MODEL_EVALUATION_CONTRACT
                ),
                "task_metric_passed": True,
                "task_native_oos": {"passed": True},
                "task_baseline": {
                    "comparison_executed": True,
                    "outperformed": False,
                },
                "environment_coverage": {"complete": False},
                "walk_forward_windows": 5,
                "strategy_impact_ab": {"status": "passed"},
            },
            "validation": {"production_eligible": True},
        },
        {"artifact_ready": True},
    )

    assert result["gates"]["task_baseline_comparison_executed"] is True
    assert result["gates"]["task_native_baseline_outperformed"] is False
    assert result["gates"]["environment_coverage_complete"] is False
    assert "task_native_baseline_outperformed" in result["blocked_reasons"]
    assert "environment_coverage_complete" in result["blocked_reasons"]


def test_narrative_current_gate_rejects_strategy_ab_without_current_effect_evidence():
    result = plan_model_control._current_production_gate_audit(
        {
            "model_key": "narrative_transition_multitask_v1",
            "metrics": {
                "model_evaluation_contract": (
                    plan_model_control.MODEL_EVALUATION_CONTRACT
                ),
                "task_metric_passed": True,
                "task_native_oos": {
                    "passed": True,
                    "baseline_outperformed": True,
                },
                "task_baseline": {
                    "comparison_executed": True,
                    "outperformed": True,
                },
                "environment_coverage": {
                    "complete": True,
                    "state_count": 4,
                    "profile_count": 8,
                },
                "walk_forward_windows": 5,
                "strategy_impact_ab": {
                    "status": "passed",
                    "model_effect_contract": (
                        plan_model_control.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                    ),
                    "gates": {
                        "candidate_fold_model_actually_consumed": True,
                        "candidate_fold_model_actually_effective": True,
                    },
                },
            },
            "validation": {
                "production_eligible": True,
                "production_eligibility": {
                    "eligible": True,
                    "gates": {
                        "data_ready": True,
                        "training_protocol": True,
                        "live_observation_20_sessions": True,
                        "human_review": True,
                    },
                },
            },
        },
        {"artifact_ready": True, "model_path_exists": True},
    )

    assert result["gates"]["controlled_strategy_ab"] is False
    assert "controlled_strategy_ab" in result["blocked_reasons"]


def test_reconcile_plan_model_production_gates_deactivates_ineligible_active(monkeypatch):
    legacy = {
        "id": 14,
        "model_key": "leader_lambdarank_v1",
        "version": "legacy-active",
        "metrics": {},
        "validation": {"production_eligible": True},
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_active_model_version",
        lambda key: legacy if key == "leader_lambdarank_v1" else None,
    )
    monkeypatch.setattr(
        plan_model_control,
        "audit_plan_model_production_eligibility",
        lambda version: {
            "passed": False,
            "blocked_reasons": ["current_task_native_evaluation_contract"],
        },
    )
    validation_updates: list[tuple[int, dict]] = []
    status_updates: list[tuple[int, str, bool]] = []
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_validation",
        lambda model_version_id, *, validation: validation_updates.append(
            (model_version_id, validation)
        ),
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_status",
        lambda model_version_id, *, status, is_active: status_updates.append(
            (model_version_id, status, is_active)
        ),
    )

    result = plan_model_control.reconcile_plan_model_production_gates()

    assert result["deactivated_count"] == 1
    assert status_updates == [(14, "retired_legacy", False)]
    assert validation_updates[0][1]["production_eligible"] is False


def test_reconcile_plan_model_production_gates_keeps_fully_eligible_active(monkeypatch):
    eligible = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "version": "current-observed",
        "metrics": {
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
        },
        "validation": {"production_eligible": True},
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_active_model_version",
        lambda key: eligible if key == "medium_horizon_lambdarank_v1" else None,
    )
    monkeypatch.setattr(
        plan_model_control,
        "audit_plan_model_production_eligibility",
        lambda version: {"passed": bool(version), "blocked_reasons": []},
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_validation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("eligible active version must not be rewritten")
        ),
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("eligible active version must not be deactivated")
        ),
    )

    result = plan_model_control.reconcile_plan_model_production_gates()

    assert result["active_count"] == 1
    assert result["deactivated_count"] == 0


def test_active_production_bindings_exclude_active_version_that_fails_current_gate(monkeypatch):
    active = {
        "id": 14,
        "model_key": "leader_lambdarank_v1",
        "version": "legacy-active",
        "status": "active",
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_active_model_version",
        lambda key: active if key == "leader_lambdarank_v1" else None,
    )
    monkeypatch.setattr(
        plan_model_control,
        "audit_plan_model_production_eligibility",
        lambda version: {"passed": False, "blocked_reasons": ["current_contract"]}
        if version
        else {"passed": False, "blocked_reasons": ["no_active_version"]},
    )

    bindings = plan_model_control.active_production_plan_model_bindings()

    assert bindings["leader_lambdarank_v1"] is None
    assert all(binding is None for binding in bindings.values())


def test_reconcile_stale_plan_model_runs_sweeps_all_runs(monkeypatch):
    runs = [
        {"id": 1, "status": "running"},
        {"id": 2, "status": "completed"},
        {"id": 3, "status": "queued"},
    ]
    monkeypatch.setattr(plan_model_control, "list_model_train_runs", lambda limit=1000: runs)
    monkeypatch.setattr(
        plan_model_control,
        "_mark_stale_train_run",
        lambda run, stale_minutes=20: {**run, "error": "stale_plan_model_training_run"},
    )

    result = plan_model_control.reconcile_stale_plan_model_runs()

    assert result["reconciled_run_ids"] == [1, 3]


def test_capital_multihead_scorer_returns_direction_quantiles_and_hazard():
    result = plan_model_control.score_plan_model_features(
        "capital_persistence_multitask_v1",
        {"momentum_20": 0.1},
        artifact={
            "version": "capital-multihead-test",
            "features": plan_model_control.PLAN_MODEL_FEATURES,
            "model_type": "capital_persistence_multihead_v1",
            "_model": {
                "bundle_type": "capital_persistence_multihead_v1",
                "direction": DummyProbabilityModel(),
                "q10": DummyConstantModel(-0.02),
                "q50": DummyConstantModel(0.03),
                "q90": DummyConstantModel(0.08),
                "hazard": DummyBinaryProbabilityModel(),
            },
        },
    )

    assert result["outputs"] == {
        "direction_probability": 0.5,
        "strength_p10": -0.02,
        "strength_p50": 0.03,
        "strength_p90": 0.08,
        "failure_hazard": 0.5,
    }


def test_operations_status_is_lightweight_and_exposes_observation_and_weights(monkeypatch):
    from app.services import runtime_heartbeat

    monkeypatch.setattr(plan_model_control, "list_model_train_runs", lambda limit=200: [{
        "id": 9,
        "status": "completed",
        "metrics": {
            "model_key": "medium_horizon_lambdarank_v1",
            "targeted_training_context": {
                "trigger_reason": "新数据到达",
                "requested_by": "auto_optimizer",
            },
        },
    }])
    monkeypatch.setattr(plan_model_control, "_model_live_health_snapshot", lambda: {
        "medium_horizon_lambdarank_v1": {
            "active_version": "medium-v1",
            "status": "observing",
            "weight_multiplier": 1.0,
            "evidence_maturity": {
                "mature_20d_outcomes": 2,
                "minimum_required": 5,
            },
        },
    })
    monkeypatch.setattr(
        plan_model_control,
        "get_active_model_version",
        lambda key: {"version": "medium-v1"} if key == "medium_horizon_lambdarank_v1" else None,
    )
    monkeypatch.setattr(
        plan_model_control,
        "list_model_versions",
        lambda key, limit=10: [{
            "id": 216,
            "model_key": key,
            "version": "medium-candidate-v216",
            "status": "candidate",
            "metrics": {"strategy_impact_ab": {"status": "passed"}},
            "validation": {
                "status": "pending_live_observation",
                "model_shadow": {
                    "status": "awaiting_first_future_signal",
                    "signal_start_trade_date": "20260730",
                    "observed_return_sessions": 0,
                    "required_return_sessions": 20,
                    "minimum_effect_days": 4,
                    "production_change_forbidden": True,
                    "observations": [],
                },
            },
        }] if key == "medium_horizon_lambdarank_v1" else [],
    )
    monkeypatch.setattr(plan_model_control, "_latest_market_state_code", lambda: "S0")
    monkeypatch.setattr(runtime_heartbeat, "status", lambda: {})
    plan_model_control._operations_cache.update({"at": 0.0, "value": None})

    result = plan_model_control.plan_model_operations_status(cache_seconds=1)
    medium = next(item for item in result["items"] if item["model_key"] == "medium_horizon_lambdarank_v1")

    assert result["schema_version"] == "plan_model_operations_status_v3"
    assert (
        result["evaluation_contracts"]["schema_version"]
        == plan_model_control.MODEL_EVALUATION_CONTRACT
    )
    assert medium["trigger_reason"] == "新数据到达"
    assert medium["evaluation"]["status"] == "outdated"
    assert (
        medium["evaluation"]["definition"]["task"]
        == "daily_cross_sectional_learning_to_rank"
    )
    assert medium["observation"]["mature_20d_outcomes"] == 2
    assert medium["candidate_observation"] == {
        "scope": "candidate_future_shadow",
        "status": "awaiting_first_future_signal",
        "candidate_active": True,
        "model_version_id": 216,
        "version": "medium-candidate-v216",
        "signal_start_trade_date": "20260730",
        "observed_return_sessions": 0,
        "required_return_sessions": 20,
        "minimum_effect_days": 4,
        "candidate_model_effect_days": 0,
        "last_observed_trade_date": None,
        "autonomous_management": True,
        "manager": "model_shadow_observation",
        "production_change_forbidden": True,
    }
    assert medium["production_observation"]["status"] == "observing"
    assert medium["weight_breakdown"]["environment"] == 1.15
    assert medium["weight_breakdown"]["maturity"] == 0.85
    assert medium["weight_breakdown"]["normalized_model_weight"] == 1.0
    assert medium["weight_breakdown"]["final_fusion_weight"] > 0
    inactive = next(item for item in result["items"] if item["model_key"] == "narrative_transition_multitask_v1")
    assert inactive["observation"]["status"] == "not_applicable"
    assert inactive["weight_breakdown"]["final_fusion_weight"] == 0.0


def test_old_truncated_training_run_is_marked_legacy_invalid():
    result = plan_model_control._with_train_timeline({
        "id": 40,
        "status": "completed",
        "metrics": {
            "dataset": {
                "row_count": 120000,
                "trade_date_count": 22,
                "train_trade_date_count": 1,
                "validation_trade_date_count": 2,
                "test_trade_date_count": 3,
            },
        },
    })

    assert result["training_protocol_status"] == "legacy_invalid"
    assert result["labeled_trade_date_count"] == 22


def test_full_walk_forward_training_run_is_current_protocol():
    result = plan_model_control._with_train_timeline({
        "id": 41,
        "status": "completed",
        "metrics": {
            "labeled_trade_date_count": plan_model_control._required_training_sessions(),
            "training_trade_date_count": 756,
            "validation_trade_date_count": 126,
            "test_trade_date_count": 126,
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
            "task_native_oos": {"window_count": 5},
            "evaluation_stage": "formal_five_window",
        },
    })

    assert result["training_protocol_status"] == "current"


def test_failed_current_protocol_attempt_is_not_mislabeled_as_legacy():
    result = plan_model_control._with_train_timeline({
        "id": 52,
        "status": "failed",
        "metrics": {
            "timeline": [{
                "phase": "dataset",
                "message": "日历覆盖 1865 天，起始 20181116，预测周期 60 天",
            }],
        },
    })

    assert result["training_protocol_status"] == "current_failed"

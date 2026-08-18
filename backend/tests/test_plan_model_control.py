import pickle

import numpy as np

from app.services import plan_model_control


def _current_strategy_ab(model_key="medium_horizon_lambdarank_v1"):
    gates = {
        key: True
        for key in plan_model_control.CONTROLLED_STRATEGY_AB_REQUIRED_GATES
    }
    return {
        "schema_version": plan_model_control.CONTROLLED_STRATEGY_AB_CONTRACT,
        "comparison_contract": (
            plan_model_control.CONTROLLED_STRATEGY_AB_COMPARISON_CONTRACT
        ),
        "status": "passed",
        "model_effect_contract": (
            plan_model_control.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
            if model_key == "narrative_transition_multitask_v1"
            else "weighted_model_signal_fusion_v1"
        ),
        "frozen_contract": {
            "execution_cost_contract": {
                "schema_version": (
                    plan_model_control.CONTROLLED_STRATEGY_AB_COST_CONTRACT
                ),
            },
        },
        "windows": [{"fold": index} for index in range(1, 6)],
        "gates": gates,
    }


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


class ShapeCheckingBinaryModel:
    classes_ = np.asarray([0, 1])

    def __init__(self, expected_columns):
        self.expected_columns = expected_columns

    def predict_proba(self, rows):
        assert np.asarray(rows).shape[1] == self.expected_columns
        return np.asarray([[0.4, 0.6] for _ in rows])


class ShapeCheckingRegressor:
    def __init__(self, expected_columns, value):
        self.expected_columns = expected_columns
        self.value = value

    def predict(self, rows):
        assert np.asarray(rows).shape[1] == self.expected_columns
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


def test_event_walk_forward_uses_sparse_event_date_contract():
    dates = [f"2020{index:04d}" for index in range(1, 290)]

    windows = plan_model_control._event_walk_forward_date_windows(dates)

    assert plan_model_control._required_training_sessions("event_impact_assessor_v2") == 270
    assert len(windows) == 5
    for window in windows:
        assert len(window["train"]) >= 200
        assert len(window["validation"]) == 10
        assert len(window["test"]) == 10
        validation_start = dates.index(window["validation"][0])
        test_start = dates.index(window["test"][0])
        assert validation_start - dates.index(window["train"][-1]) - 1 == 5
        assert test_start - dates.index(window["validation"][-1]) - 1 == 5


def _event_audit_dataset(rows=80):
    features = np.zeros((rows, len(plan_model_control.PLAN_MODEL_FEATURES)))
    for feature in (
        "event_magnitude_observed",
        "event_materiality_observed",
        "event_expectation_observed",
        "event_consensus_dispersion_observed",
        "event_consensus_freshness_observed",
        "event_company_size_observed",
        "event_analyst_revision_observed",
        "event_target_price_observed",
    ):
        features[:, plan_model_control.PLAN_MODEL_FEATURES.index(feature)] = (
            np.arange(rows) % 2
        )
    return {
        "x_all": features,
        "y_all_abnormal_return": np.asarray(
            [0.02 if index % 2 else -0.02 for index in range(rows)]
        ),
        "y_all_event_reaction": np.ones(rows),
        "y_all_event_direction_signal": np.ones(rows),
        "all_event_sources": [
            "announcement" if index % 2 else "structured_disclosure"
            for index in range(rows)
        ],
        "all_event_types": [
            "earnings" if index % 2 else "capital_return"
            for index in range(rows)
        ],
        "all_market_states": ["S0" if index % 2 else "S1" for index in range(rows)],
        "all_market_profiles": [
            "P01" if index % 2 else "P02" for index in range(rows)
        ],
    }


def test_event_distribution_audit_reports_zero_for_identical_splits():
    dataset = _event_audit_dataset()
    train_mask = np.asarray([index < 40 for index in range(80)])
    test_mask = ~train_mask

    audit = plan_model_control._event_distribution_label_fold_audit(
        dataset,
        train_mask,
        test_mask,
        fold=1,
    )

    assert audit["contract"] == plan_model_control.EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT
    assert audit["promotion_gate_impact"] == "none"
    assert audit["model_parameter_changes"] is False
    assert audit["evaluation_gate_changes"] is False
    for evidence in audit["distribution_drift"].values():
        assert evidence["total_variation_distance"] == 0.0
        assert evidence["jensen_shannon_divergence_nats"] == 0.0


def test_event_distribution_audit_exposes_category_shift_without_synthesis():
    dataset = _event_audit_dataset()
    dataset["all_event_types"] = ["earnings"] * 40 + ["governance_risk"] * 40
    dataset["all_market_states"] = ["S0"] * 40 + ["S3"] * 40
    train_mask = np.asarray([index < 40 for index in range(80)])
    test_mask = ~train_mask

    audit = plan_model_control._event_distribution_label_fold_audit(
        dataset,
        train_mask,
        test_mask,
        fold=1,
    )

    family = audit["distribution_drift"]["event_family"]
    state = audit["distribution_drift"]["market_state"]
    assert family["total_variation_distance"] == 1.0
    assert family["test_only_categories"] == ["governance_risk"]
    assert state["total_variation_distance"] == 1.0
    assert audit["synthetic_samples"] is False


def test_event_label_audit_flags_sparse_class_without_changing_labels():
    dataset = _event_audit_dataset()
    dataset["y_all_abnormal_return"][40:] = 0.02
    train_mask = np.asarray([index < 40 for index in range(80)])
    test_mask = ~train_mask

    audit = plan_model_control._event_distribution_label_fold_audit(
        dataset,
        train_mask,
        test_mask,
        fold=1,
    )

    actionability = audit["label_prevalence"]["test"]["actionability"]
    assert actionability["positive_count"] == 40
    assert actionability["negative_count"] == 0
    assert actionability["minority_support_warning"] is True
    assert audit["minimum_descriptive_group_support"] == 20


def test_event_read_only_audit_reuses_frozen_training_context(monkeypatch):
    context = {"requested_by": "auto_optimizer", "trigger": "retrain_challenger"}
    written = {}
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda _version_id: {
            "id": 271,
            "model_key": "event_impact_assessor_v2",
            "version": "event-v3",
            "status": "candidate",
            "is_active": False,
            "config": {},
            "metrics": {
                "training_data_signature": "same-signature",
                "targeted_training_context": context,
            },
            "validation": {"production_eligible": False},
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "_build_plan_training_dataset",
        lambda *_args, **_kwargs: {"all_dates": ["20200101"]},
    )

    def signature(_model_key, dataset):
        assert dataset["targeted_training_context"] == context
        return "same-signature"

    monkeypatch.setattr(plan_model_control, "_dataset_signature", signature)
    monkeypatch.setattr(
        plan_model_control,
        "_event_walk_forward_date_windows",
        lambda _dates: [{"train": ["20200101"], "test": ["20200101"]}],
    )
    monkeypatch.setattr(
        plan_model_control,
        "_event_distribution_label_stability_audit",
        lambda _dataset, _windows: {
            "contract": plan_model_control.EVENT_DISTRIBUTION_LABEL_AUDIT_CONTRACT,
            "promotion_gate_impact": "none",
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_metrics",
        lambda version_id, *, metrics: written.update(
            {"version_id": version_id, "metrics": metrics}
        ),
    )

    result = plan_model_control.audit_event_model_distribution_stability(271)

    assert result["persisted"] is True
    assert result["status_unchanged"] == "candidate"
    assert result["active_unchanged"] is False
    assert written["version_id"] == 271
    assert written["metrics"]["targeted_training_context"] == context


def test_event_feature_contribution_audit_refuses_dataset_signature_drift(monkeypatch):
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda _version_id: {
            "id": 272,
            "model_key": "event_impact_assessor_v2",
            "version": "event-v4",
            "status": "candidate",
            "is_active": False,
            "artifact_path": "event-v4-artifact.json",
            "metrics": {
                "training_data_signature": "recorded-signature",
                "targeted_training_context": {"trigger": "formal"},
            },
            "validation": {"production_eligible": False},
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "_load_artifact",
        lambda _path: {
            "model_type": "event_impact_assessor_multihead_v4",
            "walk_forward_fold_artifacts": [
                {"fold": fold} for fold in range(1, 6)
            ],
        },
    )
    monkeypatch.setattr(
        plan_model_control,
        "_build_plan_training_dataset",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        plan_model_control,
        "_dataset_signature",
        lambda *_args, **_kwargs: "rebuilt-signature",
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("signature drift must prevent persistence")
        ),
    )

    result = plan_model_control.audit_event_model_feature_contributions(272)

    assert result["ok"] is False
    assert result["error"] == "event_training_data_signature_mismatch"
    assert result["status_unchanged"] == "candidate"
    assert result["active_unchanged"] is False


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
    shadow = plan_model_control.classify_plan_model_training_failure(
        "影子重放 2026-08-18 缺少相邻两个权益点"
    )

    assert timeout["category"] == "transient_query_timeout"
    assert timeout["retryable"] is True
    assert coverage["recommended_action"] == "delegate_data_backfill"
    assert code["category"] == "code_contract_error"
    assert code["retryable"] is False
    assert shadow["category"] == "shadow_equity_evidence_gap"
    assert shadow["recommended_action"] == "wait_for_next_natural_shadow_observation"


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


def test_narrative_coverage_counts_only_future_label_ready_sessions():
    not_ready = plan_model_control._coverage_snapshot(
        "narrative_transition_multitask_v1",
        base={
            "trade_dates": 2400,
            "themes": 1577,
            "narrative_v3": 1577,
        },
    )
    ready = plan_model_control._coverage_snapshot(
        "narrative_transition_multitask_v1",
        base={
            "trade_dates": 2400,
            "themes": 1580,
            "narrative_v3": 1580,
        },
    )

    assert not_ready["narrative_v3_labeled_sessions"] == 1574
    assert not_ready["data_ready"] is False
    assert ready["narrative_v3_labeled_sessions"] == 1577
    assert ready["narrative_v3_label_horizon_sessions"] == 3
    assert ready["data_ready"] is True


def test_request_unknown_plan_model_is_rejected():
    result = plan_model_control.request_plan_model_training("unknown")

    assert result["ok"] is False
    assert result["error"] == "unknown_plan_model"


def test_event_training_reaches_normal_governance_after_label_contract_repair(monkeypatch):
    monkeypatch.setattr(
        plan_model_control,
        "_plan_model_candidate_governance_blocker",
        lambda _model_key: {"id": 271},
    )

    result = plan_model_control.request_plan_model_training(
        "event_impact_assessor_v2"
    )

    assert result["ok"] is True
    assert result["started"] is False
    assert result["reason"] == "candidate_governance_in_progress"
    assert result["model_version_id"] == 271


def test_event_targeted_candidate_waits_until_formal_oos_cohort_advances(
    monkeypatch,
):
    dates = [f"2026{index:04d}" for index in range(1, 271)]
    windows = plan_model_control._event_walk_forward_date_windows(dates)
    source_metrics = {
        "walk_forward_evidence": [
            {
                "fold": index,
                "test_start": window["test"][0],
                "test_end": window["test"][-1],
                "test_sessions": len(window["test"]),
            }
            for index, window in enumerate(windows, start=1)
        ]
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda version_id: {
            "id": version_id,
            "model_key": "event_impact_assessor_v2",
            "metrics": source_metrics,
        },
    )

    result = plan_model_control._event_targeted_research_cohort_guard(
        "event_impact_assessor_v2",
        {"all_dates": dates},
        {
            "source_model_version_id": 272,
            "model_candidate_spec": {"name": "adaptive-feature-candidate"},
        },
    )

    assert result["allowed"] is False
    assert result["status"] == "waiting_for_natural_oos_cohort_advance"
    assert result["formal_test_labels_reused"] is True


def test_event_targeted_candidate_is_allowed_after_oos_cohort_advances(monkeypatch):
    source_dates = [f"2026{index:04d}" for index in range(1, 271)]
    current_dates = [
        *source_dates,
        *[f"2026{index:04d}" for index in range(271, 281)],
    ]
    source_windows = plan_model_control._event_walk_forward_date_windows(
        source_dates
    )
    source_metrics = {
        "walk_forward_evidence": [
            {
                "fold": index,
                "test_start": window["test"][0],
                "test_end": window["test"][-1],
                "test_sessions": len(window["test"]),
            }
            for index, window in enumerate(source_windows, start=1)
        ]
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda version_id: {
            "id": version_id,
            "model_key": "event_impact_assessor_v2",
            "metrics": source_metrics,
        },
    )

    result = plan_model_control._event_targeted_research_cohort_guard(
        "event_impact_assessor_v2",
        {"all_dates": current_dates},
        {
            "source_model_version_id": 272,
            "model_candidate_spec": {"name": "adaptive-feature-candidate"},
        },
    )

    assert result["allowed"] is True
    assert result["status"] == "fresh_formal_oos_cohort"
    assert result["formal_test_labels_reused"] is False
    assert result["new_event_dates_after_source"] == 10
    assert result["complete_new_test_window"] is True


def test_event_targeted_candidate_waits_when_only_one_new_event_date_exists(
    monkeypatch,
):
    source_dates = [f"2026{index:04d}" for index in range(1, 271)]
    source_windows = plan_model_control._event_walk_forward_date_windows(
        source_dates
    )
    source_metrics = {
        "walk_forward_evidence": [
            {
                "fold": index,
                "test_start": window["test"][0],
                "test_end": window["test"][-1],
                "test_sessions": len(window["test"]),
            }
            for index, window in enumerate(source_windows, start=1)
        ]
    }
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda version_id: {
            "id": version_id,
            "model_key": "event_impact_assessor_v2",
            "metrics": source_metrics,
        },
    )

    result = plan_model_control._event_targeted_research_cohort_guard(
        "event_impact_assessor_v2",
        {"all_dates": [*source_dates, "20260271"]},
        {
            "source_model_version_id": 272,
            "model_candidate_spec": {"name": "adaptive-feature-candidate"},
        },
    )

    assert result["allowed"] is False
    assert result["status"] == "waiting_for_natural_oos_cohort_advance"
    assert result["new_event_dates_after_source"] == 1
    assert result["minimum_new_event_dates"] == 10


def test_narrative_reaudit_requires_final_state_to_beat_persistence(monkeypatch):
    task = {
        "contract": "narrative_two_stage_lifecycle_task_native_v2",
        "model": {"macro_f1": 0.30},
        "baselines": {"state_persistence": {"macro_f1": 0.40}},
        "gates": {
            "class_coverage": True,
            "transition_events_observed": True,
            "transition_detection_nonzero": True,
            "transition_brier_beats_train_only_hazard": True,
            "conditional_destination_coverage": True,
            "conditional_destination_macro_f1_beats_empirical": True,
            "final_state_macro_f1_beats_empirical_transition": True,
            "final_state_log_loss_beats_empirical_transition": True,
            "final_state_brier_beats_empirical_transition": True,
        },
        "passed": True,
        "baseline_outperformed": True,
    }
    version = {
        "id": 237,
        "model_key": "narrative_transition_multitask_v1",
        "status": "candidate",
        "is_active": False,
        "metrics": {
            "walk_forward_evidence": [
                {
                    "fold": fold,
                    "task_native_evaluation": dict(task),
                    "task_native_passed": True,
                }
                for fold in range(1, 6)
            ],
            "task_native_oos": {"passed": True, "baseline_outperformed": True},
            "task_baseline": {"outperformed": True},
        },
        "validation": {
            "production_eligible": False,
            "production_eligibility": {
                "eligible": False,
                "gates": {
                    "task_metric_passed": True,
                    "task_native_walk_forward_oos": True,
                    "task_native_baseline_outperformed": True,
                },
                "blocked_reasons": [],
            },
        },
    }
    persisted_metrics = {}
    persisted_validation = {}
    monkeypatch.setattr(
        plan_model_control,
        "get_model_version",
        lambda _version_id: version,
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_metrics",
        lambda _version_id, *, metrics: persisted_metrics.update(metrics),
    )
    monkeypatch.setattr(
        plan_model_control,
        "update_model_version_validation",
        lambda _version_id, *, validation: persisted_validation.update(validation),
    )

    result = plan_model_control.audit_narrative_strong_baseline_contract(
        237,
        persist=True,
    )

    assert result["audit"]["passed_windows"] == 0
    assert result["audit"]["baseline_outperformed_windows"] == 0
    assert persisted_metrics["task_metric_passed"] is False
    assert persisted_metrics["task_native_oos"]["passed"] is False
    assert persisted_validation["production_eligible"] is False


def test_event_quick_training_uses_repaired_label_contract(monkeypatch):
    dataset = {"x_all": np.zeros((1, 1))}
    monkeypatch.setattr(
        plan_model_control,
        "_fit_lightgbm_plan_model",
        lambda model_key, frozen, **kwargs: {
            "metrics": {
                "model_key": model_key,
                "dataset_reused": frozen is dataset,
                "quick_diagnostic": kwargs["quick_diagnostic"],
            }
        },
    )

    result = plan_model_control.evaluate_plan_model_candidate_quick(
        "event_impact_assessor_v2",
        {},
        dataset=dataset,
    )

    assert result == {
        "model_key": "event_impact_assessor_v2",
        "dataset_reused": True,
        "quick_diagnostic": True,
    }


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
            "strategy_impact_ab": _current_strategy_ab(),
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
            "strategy_impact_ab": _current_strategy_ab(),
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


def test_controlled_strategy_ab_rejects_status_only_and_accepts_complete_cost_after_contract():
    assert plan_model_control.controlled_strategy_ab_passed_current_contract(
        "medium_horizon_lambdarank_v1",
        {"status": "passed"},
    ) is False
    assert plan_model_control.controlled_strategy_ab_passed_current_contract(
        "medium_horizon_lambdarank_v1",
        _current_strategy_ab(),
    ) is True


def test_review_plan_model_cannot_activate_when_current_gate_audit_fails(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(plan_model_control, "get_model_version", lambda _id: {
        "id": 13,
        "model_key": "leader_lambdarank_v1",
        "version": "candidate-with-stale-evidence",
        "metrics": {
            "model_evaluation_contract": plan_model_control.MODEL_EVALUATION_CONTRACT,
            "strategy_impact_ab": _current_strategy_ab("leader_lambdarank_v1"),
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


def test_event_impact_scorer_returns_probabilities_intervals_and_holding_contract():
    result = plan_model_control.score_plan_model_features(
        "event_impact_assessor_v2",
        {
            "momentum_20": 0.1,
            "event_text_confidence": 0.8,
            "event_crowding": 1.5,
        },
        artifact={
            "version": "event-impact-test",
            "features": list(plan_model_control.EVENT_IMPACT_FEATURES),
            "model_type": "event_impact_assessor_multihead_v2",
            "_model": {
                "bundle_type": "event_impact_assessor_multihead_v2",
                "positive_impact": DummyBinaryProbabilityModel(),
                "priced_in": DummyBinaryProbabilityModel(),
                "continuation": DummyBinaryProbabilityModel(),
                "reversal": DummyBinaryProbabilityModel(),
                "q10": DummyConstantModel(-0.01),
                "q50": DummyConstantModel(0.02),
                "q90": DummyConstantModel(0.06),
                "temperature": {},
            },
        },
    )

    assert result["outputs"] == {
        "impact_contract": plan_model_control.EVENT_IMPACT_CONTRACT,
        "event_relevance": 0.8,
        "positive_impact_probability": 0.5,
        "priced_in_probability": 0.5,
        "continuation_probability": 0.5,
        "reversal_probability": 0.5,
        "abnormal_return_q10": -0.01,
        "abnormal_return_q50": 0.02,
        "abnormal_return_q90": 0.06,
        "crowding_risk": 0.5,
        "recommended_holding_horizon_sessions": 2,
        "event_hard_veto": False,
    }


def test_event_impact_v4_scorer_exposes_direction_realization_probability():
    result = plan_model_control.score_plan_model_features(
        "event_impact_assessor_v2",
        {"momentum_20": 0.1, "event_text_confidence": 0.8},
        artifact={
            "version": "event-impact-v4-test",
            "features": list(plan_model_control.EVENT_IMPACT_FEATURES),
            "model_type": "event_impact_assessor_multihead_v4",
            "_model": {
                "bundle_type": "event_impact_assessor_multihead_v4",
                "impact_contract": plan_model_control.EVENT_IMPACT_CONTRACT,
                "positive_impact": DummyBinaryProbabilityModel(),
                "priced_in": DummyBinaryProbabilityModel(),
                "continuation": DummyBinaryProbabilityModel(),
                "reversal": DummyBinaryProbabilityModel(),
                "direction_realization": DummyBinaryProbabilityModel(),
                "q10": DummyConstantModel(-0.01),
                "q50": DummyConstantModel(0.02),
                "q90": DummyConstantModel(0.06),
                "temperature": {},
            },
        },
    )

    assert result["outputs"]["direction_realization_probability"] == 0.5
    assert result["outputs"]["impact_contract"] == (
        "alis_conditional_event_impact_assessment_v4"
    )


def test_event_v4_bundle_routes_each_probability_head_to_its_frozen_subset():
    model = {
        "positive_impact": ShapeCheckingBinaryModel(2),
        "priced_in": ShapeCheckingBinaryModel(1),
        "continuation": ShapeCheckingBinaryModel(2),
        "reversal": ShapeCheckingBinaryModel(3),
        "direction_realization": ShapeCheckingBinaryModel(1),
        "q10": ShapeCheckingRegressor(4, -0.01),
        "q50": ShapeCheckingRegressor(4, 0.01),
        "q90": ShapeCheckingRegressor(4, 0.03),
        "temperature": {},
        "head_feature_indices": {
            "positive_impact": [0, 1],
            "priced_in": [2],
            "continuation": [1, 3],
            "reversal": [0, 2, 3],
            "direction_realization": [3],
        },
    }

    outputs = plan_model_control._event_impact_bundle_outputs(
        model,
        np.zeros((5, 4), dtype=float),
    )

    assert set(outputs) == {
        "positive_impact", "priced_in", "continuation", "reversal",
        "direction_realization", "q10", "q50", "q90",
    }
    assert np.allclose(outputs["direction_realization"], 0.6)


def test_event_head_feature_candidate_covers_exactly_five_heads_without_parameters():
    candidate = plan_model_control.EVENT_HEAD_FEATURE_SUBSET_CANDIDATE_V1

    assert set(candidate["subsets"]) == set(
        plan_model_control.EVENT_PROBABILITY_HEADS
    )
    assert candidate["model_parameter_changes"] is False
    assert candidate["evaluation_gate_changes"] is False
    assert candidate["quantile_head_policy"] == "unchanged_full_event_feature_set"
    for features in candidate["subsets"].values():
        assert len(features) >= 5
        assert set(features) <= set(plan_model_control.EVENT_IMPACT_FEATURES)
        assert "event_expectation_gap" not in features


def test_event_market_state_candidate_adds_point_in_time_context_without_parameters():
    candidate = plan_model_control.EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1

    assert set(candidate["subsets"]) == set(
        plan_model_control.EVENT_PROBABILITY_HEADS
    )
    assert candidate["model_parameter_changes"] is False
    assert candidate["evaluation_gate_changes"] is False
    assert candidate["source_model_version_id"] == 273
    for features in candidate["subsets"].values():
        assert set(plan_model_control.EVENT_MARKET_STATE_CONTEXT_FEATURES) <= set(
            features
        )
        assert set(features) <= set(plan_model_control.EVENT_IMPACT_FEATURES)


def test_event_priced_reversal_refinement_only_changes_audited_heads():
    candidate = plan_model_control.EVENT_PRICED_REVERSAL_REFINEMENT_CANDIDATE_V1
    previous = plan_model_control.EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1

    assert set(candidate["subsets"]) == set(plan_model_control.EVENT_PROBABILITY_HEADS)
    assert candidate["model_parameter_changes"] is False
    assert candidate["evaluation_gate_changes"] is False
    assert candidate["source_model_version_id"] == 274
    for head in ("positive_impact", "continuation", "direction_realization"):
        assert candidate["subsets"][head] == previous["subsets"][head]
    assert not (
        set(candidate["removed_from_priced_in"])
        & set(candidate["subsets"]["priced_in"])
    )
    assert not (
        set(candidate["removed_from_reversal"])
        & set(candidate["subsets"]["reversal"])
    )
    for features in candidate["subsets"].values():
        assert set(plan_model_control.EVENT_MARKET_STATE_CONTEXT_FEATURES) <= set(
            features
        )


def test_event_baseline_residual_candidate_uses_validation_only_and_keeps_gates():
    candidate = plan_model_control.EVENT_BASELINE_RESIDUAL_SHRINKAGE_CANDIDATE_V1

    assert candidate["model_parameter_changes"] is False
    assert candidate["evaluation_gate_changes"] is False
    assert candidate["test_label_selection"] is False
    assert candidate["source_model_version_id"] == 275
    assert candidate["subsets"] == (
        plan_model_control.EVENT_MARKET_STATE_CONTEXT_CANDIDATE_V1["subsets"]
    )


def test_event_head_candidate_selection_is_validation_only_and_bounded():
    candidate = plan_model_control.EVENT_VALIDATION_SELECTED_HEAD_CANDIDATE_V1

    assert candidate["selection_data"] == "validation_only"
    assert candidate["test_label_selection"] is False
    assert candidate["model_parameter_changes"] is False
    assert candidate["evaluation_gate_changes"] is False
    assert set(candidate["alternative_subsets"]) == {"priced_in", "reversal"}
    for head, alternative in candidate["alternative_subsets"].items():
        assert tuple(alternative) != tuple(candidate["primary_subsets"][head])
        assert set(alternative) <= set(plan_model_control.EVENT_IMPACT_FEATURES)


def test_validation_baseline_blend_weight_prefers_safer_lower_weight_on_tie():
    actual = np.asarray([0.0, 1.0])
    baseline = np.asarray([0.25, 0.75])
    assert plan_model_control._validation_baseline_blend_weight(
        actual, baseline, baseline,
    ) == 0.0
    assert plan_model_control._validation_baseline_blend_weight(
        actual, actual, baseline,
    ) == 1.0


def test_event_bundle_blends_frozen_training_baseline_with_model_residual():
    groups = ["announcement:earnings:positive"]
    probability_baseline = plan_model_control.fit_event_probability_baseline(
        np.asarray([0, 0, 1]),
        [groups[0]] * 3,
    )
    quantile_baseline = plan_model_control.fit_event_quantile_baseline(
        np.arange(10, dtype=float),
        [groups[0]] * 10,
        0.5,
    )
    model = {
        "positive_impact": DummyBinaryProbabilityModel(),
        "priced_in": DummyBinaryProbabilityModel(),
        "continuation": DummyBinaryProbabilityModel(),
        "reversal": DummyBinaryProbabilityModel(),
        "q10": DummyConstantModel(1.0),
        "q50": DummyConstantModel(1.0),
        "q90": DummyConstantModel(1.0),
        "temperature": {},
        "event_baseline_models": {
            "probability": {
                name: probability_baseline
                for name in (
                    "positive_impact", "priced_in", "continuation", "reversal"
                )
            },
            "quantile": {
                name: quantile_baseline for name in ("q10", "q50", "q90")
            },
        },
        "event_baseline_blend_weights": {
            "positive_impact": 0.0,
            "priced_in": 0.0,
            "continuation": 0.0,
            "reversal": 0.0,
            "q10": 0.0,
            "q50": 0.0,
            "q90": 0.0,
        },
    }
    outputs = plan_model_control._event_impact_bundle_outputs(
        model,
        np.zeros((1, 1)),
        event_sources=["announcement"],
        event_types=["earnings"],
        event_reaction=np.asarray([0.1]),
        event_direction_signal=np.asarray([1.0]),
    )

    assert outputs["positive_impact"][0] == probability_baseline[
        "group_probabilities"
    ][groups[0]]
    assert outputs["q50"][0] == 4.5


def test_event_quantile_outputs_apply_validation_offsets_and_prevent_crossing():
    outputs = plan_model_control._event_impact_bundle_outputs(
        {
            "positive_impact": DummyBinaryProbabilityModel(),
            "priced_in": DummyBinaryProbabilityModel(),
            "continuation": DummyBinaryProbabilityModel(),
            "reversal": DummyBinaryProbabilityModel(),
            "q10": DummyConstantModel(0.03),
            "q50": DummyConstantModel(0.01),
            "q90": DummyConstantModel(0.02),
            "temperature": {},
            "quantile_offsets": {"q10": -0.01, "q50": 0.0, "q90": 0.02},
        },
        np.asarray([[0.0], [1.0]], dtype=float),
    )

    assert np.allclose(outputs["q10"], [0.01, 0.01])
    assert np.allclose(outputs["q50"], [0.02, 0.02])
    assert np.allclose(outputs["q90"], [0.04, 0.04])


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
    assert "timeline" in medium["latest_train_run"]["metrics"]
    assert "targeted_training_context" not in medium["latest_train_run"]["metrics"]
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


def test_running_current_event_protocol_is_not_mislabeled_as_legacy():
    model_key = "event_impact_assessor_v2"
    result = plan_model_control._with_train_timeline({
        "id": 53,
        "status": "running",
        "phase": "dataset",
        "metrics": {
            "model_key": model_key,
            "contract": plan_model_control.PLAN_MODELS[model_key]["family"],
            "timeline": [{
                "phase": "dataset",
                "message": "正在构造事件影响训练样本",
            }],
        },
    })

    assert result["training_protocol_status"] == "current_running"

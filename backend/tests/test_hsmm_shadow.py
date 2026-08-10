from contextlib import contextmanager
import json

import numpy as np

from app.services import hsmm_shadow
from app.services.hsmm_shadow import (
    FEATURES,
    _decode,
    _duration_log_probabilities,
    _duration_parameters,
    _filtered_terminal_probabilities,
    _fit,
    _fit_hmm_baseline,
    _hmm_forward_log_likelihood,
    _hsmm_forward_log_likelihood,
    _market_regime_axes,
)


def test_hsmm_uses_four_states_student_t_and_bounded_negative_binomial_duration():
    rng = np.random.default_rng(7)
    values = np.vstack([
        rng.normal([1, 1, -1, 1, 1, -1, 1, 1, -1], .2, size=(20, 9)),
        rng.normal([0, 0, 0, 0, 0, 0, 0, 0, 0], .2, size=(20, 9)),
        rng.normal([-1, -1, 1, -1, -1, 1, -1, -1, 1], .2, size=(20, 9)),
        rng.normal([.5, 0, 1, .5, 0, 1, .5, 0, 1], .2, size=(20, 9)),
    ])
    model = _fit(values)
    decoded, likelihood = _decode(values, model)
    assert len(model["emissions"]) == 4
    assert all(item["df"] == 6 for item in model["emissions"])
    assert all(item["r"] > 0 and 0 < item["p"] < 1 for item in model["durations"])
    assert model["training"]["algorithm"] == hsmm_shadow.HSMM_TRAINING_ALGORITHM
    assert all(
        np.allclose(
            np.asarray(item["covariance"]),
            np.diag(np.diag(np.asarray(item["covariance"]))),
        )
        for item in model["emissions"]
    )
    assert len(decoded) == len(values)
    assert np.isfinite(likelihood)


def test_hsmm_duration_support_includes_real_one_week_regimes():
    labels = np.asarray([0, 1, 0, 1, 2, 3], dtype=int)
    model = {"durations": _duration_parameters(labels)}

    probabilities = _duration_log_probabilities(model)

    assert np.all(np.isfinite(probabilities[:, 1]))
    assert np.all(np.isneginf(probabilities[:, 0]))
    assert model["durations"][0]["mean"] < 2.0


def test_hsmm_collapses_correlated_index_copies_to_interpretable_axes():
    raw = np.arange(2 * len(FEATURES), dtype=float).reshape(2, len(FEATURES))
    axes = _market_regime_axes(raw)

    assert axes.shape == (2, 8)
    assert np.allclose(axes[:, 0], np.mean(raw[:, [0, 3, 6]], axis=1))
    assert np.allclose(axes[:, 1], np.mean(raw[:, [1, 4, 7]], axis=1))


def test_hsmm_uses_marginal_sequence_likelihood_not_viterbi_path_score():
    rng = np.random.default_rng(17)
    values = np.vstack([
        rng.normal([1, 1, -1, 1, 1, -1, 1, 1, -1], .2, size=(20, 9)),
        rng.normal([0, 0, 0, 0, 0, 0, 0, 0, 0], .2, size=(20, 9)),
        rng.normal([-1, -1, 1, -1, -1, 1, -1, -1, 1], .2, size=(20, 9)),
        rng.normal([.5, 0, 1, .5, 0, 1, .5, 0, 1], .2, size=(20, 9)),
    ])
    model = _fit(values[:60])
    _decoded, viterbi_score = _decode(values[60:], model)
    marginal_likelihood = _hsmm_forward_log_likelihood(values[60:], model)
    hmm = _fit_hmm_baseline(values[:60], emissions=model["emissions"])
    hmm_likelihood = _hmm_forward_log_likelihood(values[60:], hmm)

    assert np.isfinite(marginal_likelihood)
    assert np.isfinite(hmm_likelihood)
    assert marginal_likelihood >= viterbi_score


def test_hsmm_live_probability_is_filtered_terminal_distribution():
    rng = np.random.default_rng(23)
    values = rng.normal(0, 1, size=(80, 9))
    model = _fit(values[:60])

    probabilities = _filtered_terminal_probabilities(values[60:], model)

    assert probabilities.shape == (4,)
    assert np.all(probabilities >= 0)
    assert np.isclose(float(probabilities.sum()), 1.0)


def test_training_refresh_calls_only_three_compact_index_apis(monkeypatch):
    calls = []
    monkeypatch.setattr("app.data_platform.services.data_center.sync_tushare_api_raw", lambda api, params: calls.append((api, params)) or {"rows_written": 10})
    monkeypatch.setattr("app.services.market_context.materialize_market_training_history", lambda: {"written": 20})
    result = hsmm_shadow.refresh_compact_training_history_from_api(years=10)
    assert len(calls) == 3
    assert {api for api, _ in calls} == {"index_daily"}
    assert {params["ts_code"] for _, params in calls} == {"000300.SH", "000905.SH", "000852.SH"}
    assert result["materialized"]["written"] == 20


def test_hsmm_training_skips_same_contract_without_new_data(monkeypatch):
    monkeypatch.setattr(
        hsmm_shadow,
        "list_model_versions",
        lambda model_key, limit=1: [{
            "id": 262,
            "model_key": model_key,
            "version": "hsmm-shadow-current",
            "validation": {
                "status": "shadow_failed",
                "evaluation_contract": hsmm_shadow.HSMM_EVALUATION_CONTRACT,
                "oos_end": "20260717",
            },
        }],
    )
    monkeypatch.setattr(
        hsmm_shadow,
        "_load_weekly_features",
        lambda: (["20260710", "20260717"], np.zeros((2, 8))),
    )
    monkeypatch.setattr(
        hsmm_shadow,
        "refresh_market_regime_training_history",
        lambda: (_ for _ in ()).throw(
            AssertionError("same-data governance block must run before refresh")
        ),
    )

    result = hsmm_shadow.train_hsmm_shadow()

    assert result["started"] is False
    assert result["reason"] == "current_contract_already_evaluated_without_new_data"
    assert result["model_version_id"] == 262


def test_failed_hsmm_does_not_trigger_strategy_research(monkeypatch):
    monkeypatch.setattr(
        "app.services.evolution_orchestrator.enqueue_factor_strategy_research_event",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed HSMM must not enter strategy research")
        ),
    )

    result = hsmm_shadow._enqueue_hsmm_research_trigger(
        model_version_id=279,
        version="hsmm-shadow-failed",
        validation={"status": "shadow_failed"},
    )

    assert result == {
        "status": "not_enqueued",
        "reason": "hsmm_task_native_evaluation_failed",
    }


def test_passed_hsmm_can_trigger_strategy_research(monkeypatch):
    monkeypatch.setattr(
        "app.services.evolution_orchestrator.enqueue_factor_strategy_research_event",
        lambda **kwargs: {"id": 2301, "status": "queued", "payload": kwargs},
    )

    result = hsmm_shadow._enqueue_hsmm_research_trigger(
        model_version_id=280,
        version="hsmm-shadow-passed",
        validation={"status": "passed"},
    )

    assert result == {"status": "queued", "task_id": 2301}


def test_point_in_time_hsmm_rejects_version_created_after_cutoff(tmp_path):
    artifact = tmp_path / "hsmm.json"
    artifact.write_text("{}", encoding="utf-8")
    hsmm_shadow._infer_hsmm_state_cached.cache_clear()

    result = hsmm_shadow.infer_hsmm_state_as_of(
        {
            "id": 41,
            "artifact_path": str(artifact),
            "created_at": "2026-07-22T10:00:00+08:00",
        },
        as_of_time="2026-07-21T15:00:00+08:00",
    )

    assert result == {
        "ok": False,
        "reason": "hsmm_version_created_after_cutoff",
        "status": "fallback",
    }


def test_point_in_time_hsmm_requires_frozen_artifact(tmp_path):
    hsmm_shadow._infer_hsmm_state_cached.cache_clear()

    result = hsmm_shadow.infer_hsmm_state_as_of(
        {
            "id": 41,
            "artifact_path": str(tmp_path / "missing.json"),
            "created_at": "2026-07-20T10:00:00+08:00",
        },
        as_of_time="2026-07-21T15:00:00+08:00",
    )

    assert result == {
        "ok": False,
        "reason": "hsmm_artifact_unavailable",
        "status": "fallback",
    }


def test_point_in_time_hsmm_uses_frozen_legacy_feature_contract(monkeypatch, tmp_path):
    artifact = tmp_path / "hsmm.json"
    artifact.write_text(json.dumps({
        "features": ["legacy_feature"],
        "states": ["S0", "S1", "S2", "S3"],
        "scaler": {"center": [0.0], "scale": [1.0]},
        "model": {
            "emissions": [
                {"mean": [float(index)], "covariance": [[1.0]], "df": 6}
                for index in range(4)
            ],
        },
    }), encoding="utf-8")

    class Cursor:
        def fetchall(self):
            return [
                {"feature_date": date, "features_json": json.dumps({"legacy_feature": value})}
                for date, value in (
                    ("20260622", 0.4),
                    ("20260615", 0.3),
                    ("20260608", 0.2),
                    ("20260601", 0.1),
                )
            ]

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, _sql, _params=None):
                return Cursor()
        yield Connection()

    monkeypatch.setattr(hsmm_shadow, "data_conn", fake_conn)
    monkeypatch.setattr(
        hsmm_shadow,
        "_decode",
        lambda values, _model: ([2 for _ in values], 0.0),
    )
    monkeypatch.setattr(hsmm_shadow.multivariate_t, "logpdf", lambda *_args, **_kwargs: 0.0)
    hsmm_shadow._infer_hsmm_state_cached.cache_clear()

    result = hsmm_shadow.infer_hsmm_state_as_of(
        {
            "id": 41,
            "artifact_path": str(artifact),
            "created_at": "2026-06-30T10:00:00+08:00",
        },
        as_of_time="2026-07-01T15:00:00+08:00",
    )

    assert result["ok"] is True
    assert result["state"] == "S2"
    assert result["feature_count"] == 1
    assert result["feature_date"] == "20260622"

from contextlib import contextmanager

from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services import model_signal_fusion


def _row(symbol: str, change_pct: float) -> SnapshotRow:
    return SnapshotRow(
        symbol=symbol,
        date="2026-07-22",
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        prev_close=9.9,
        change_pct=change_pct,
        volume=1000,
        turnover=10000,
        name=symbol,
    )


def _history() -> list[HistBar]:
    return [
        HistBar(
            date=f"2026-06-{index + 1:02d}",
            open=10 + index / 10,
            high=10 + index / 10,
            low=10 + index / 10,
            close=10 + index / 10,
            volume=1000,
            turnover=10000 + index * 100,
        )
        for index in range(21)
    ]


def test_production_models_change_ranking_and_block_bottom_capital_signal(monkeypatch):
    rows = [_row("000001", -1.0), _row("000002", 0.0), _row("000003", 1.0)]
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 1, "version": f"{key}-active"},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": True},
    )
    monkeypatch.setattr(model_signal_fusion, "_artifact", lambda version: {"ready": True})
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda versions: {
            key: {"status": "healthy", "weight_multiplier": 1.0, "reason": "test"}
            for key in model_signal_fusion.MODEL_KEYS
        },
    )

    def fake_score(key, features, artifact=None):
        multiplier = {
            "medium_horizon_lambdarank_v1": 1.0,
            "capital_persistence_multitask_v1": 2.0,
            "leader_lambdarank_v1": 3.0,
            "event_impact_assessor_v2": 4.0,
            "narrative_transition_multitask_v1": 5.0,
        }[key]
        result = {"score": features["momentum_20"] * multiplier}
        if key == "capital_persistence_multitask_v1":
            weak = features["momentum_20"] < -0.005
            result["outputs"] = {
                "direction_probability": 0.2 if weak else 0.8,
                "strength_p10": -0.03 if weak else 0.01,
                "failure_hazard": 0.8 if weak else 0.2,
            }
        return result

    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", fake_score)
    ranked, evidence, blocked, status = model_signal_fusion.apply_production_model_signals(
        ranked=[(row, 80.0) for row in rows],
        factor_values={
            row.symbol: {"momentum_20": row.change_pct / 100.0, "momentum_60": 0.2, "volatility_20": 0.01}
            for row in rows
        },
        histories={row.symbol: _history() for row in rows},
        themes=[],
    )

    assert [row.symbol for row, _score in ranked] == ["000003", "000002"]
    assert blocked[0]["symbol"] == "000001"
    assert blocked[0]["code"] == "capital_persistence_gate_failed"
    assert evidence["000003"]["add_position_eligible"] is True
    assert len(evidence["000003"]["trigger_models"]) == 5
    assert status["input_count"] == 3
    assert status["output_count"] == 2


def test_degraded_capital_model_reduces_weight_and_disables_its_trade_gate(monkeypatch):
    rows = [_row("000001", -1.0), _row("000002", 0.0), _row("000003", 1.0)]
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 1, "version": f"{key}-active"},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": True},
    )
    monkeypatch.setattr(model_signal_fusion, "_artifact", lambda version: {"ready": True})
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda versions: {
            key: {
                "status": "degraded" if key == "capital_persistence_multitask_v1" else "healthy",
                "weight_multiplier": 0.55 if key == "capital_persistence_multitask_v1" else 1.0,
                "reason": "live_ic_below_threshold" if key == "capital_persistence_multitask_v1" else "healthy",
            }
            for key in model_signal_fusion.MODEL_KEYS
        },
    )

    def fake_score(key, features, artifact=None):
        multiplier = {
            "medium_horizon_lambdarank_v1": 1.0,
            "capital_persistence_multitask_v1": 2.0,
            "leader_lambdarank_v1": 3.0,
            "event_impact_assessor_v2": 4.0,
            "narrative_transition_multitask_v1": 5.0,
        }[key]
        result = {"score": features["momentum_20"] * multiplier}
        if key == "capital_persistence_multitask_v1":
            result["outputs"] = {
                "direction_probability": 0.2,
                "strength_p10": -0.03,
                "failure_hazard": 0.8,
            }
        return result

    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", fake_score)
    ranked, evidence, blocked, status = model_signal_fusion.apply_production_model_signals(
        ranked=[(row, 80.0) for row in rows],
        factor_values={
            row.symbol: {"momentum_20": row.change_pct / 100.0, "momentum_60": 0.2, "volatility_20": 0.01}
            for row in rows
        },
        histories={row.symbol: _history() for row in rows},
        themes=[],
    )

    assert [row.symbol for row, _score in ranked] == ["000003", "000002", "000001"]
    assert blocked == []
    assert evidence["000001"]["capital_gate_active"] is False
    assert evidence["000001"]["add_position_eligible"] is True
    assert evidence["000001"]["models"]["capital_persistence_multitask_v1"] == {
        "raw_score": evidence["000001"]["models"]["capital_persistence_multitask_v1"]["raw_score"],
        "percentile": 0.0,
        "version_id": 1,
        "version": "capital_persistence_multitask_v1-active",
            "status": "degraded",
            "weight_multiplier": 0.55,
            "health_reason": "live_ic_below_threshold",
                "point_in_time_available": True,
                "availability_reason": "current_runtime",
                "online": True,
                "trade_authorized": True,
                "authorization_scope": "full_production",
                "outputs": {
                "direction_probability": 0.2,
                "strength_p10": -0.03,
                "failure_hazard": 0.8,
            },
            "score_available": True,
            "score_status": "scored",
        }
    assert status["policy"]["effective_model_weights"]["capital_persistence_multitask_v1"] == 0.055
    assert status["policy"]["disabled_gates"] == ["capital_persistence_gate"]
    assert status["production_models"][1]["health_status"] == "degraded"


def test_equal_model_scores_receive_equal_percentiles():
    assert model_signal_fusion._percentiles({"000003": 0.0, "000001": 0.0, "000002": 1.0}) == {
        "000001": 0.25,
        "000003": 0.25,
        "000002": 1.0,
    }


def test_high_confidence_theme_adds_bounded_bonus_without_becoming_global_filter():
    evidence = model_signal_fusion._theme_evidence([
        {
            "theme_name": "算力",
            "lifecycle": "扩散",
            "score": 100,
            "promotable": True,
            "companies": [{"ts_code": "000001.SZ"}],
        },
    ])

    assert evidence["000001"]["rank_bonus"] == 0.08
    assert evidence["000001"]["hard_veto"] is False


def test_risky_or_fading_theme_is_a_hard_new_position_veto():
    evidence = model_signal_fusion._theme_evidence([
        {
            "theme_name": "伪主题",
            "lifecycle": "扩散",
            "score": 100,
            "promotable": True,
            "payload": {"verified": False},
            "companies": [{"ts_code": "000001.SZ"}],
        },
        {
            "theme_name": "退潮主题",
            "lifecycle": "退潮",
            "score": 100,
            "promotable": True,
            "companies": [{"ts_code": "000001.SZ"}],
        },
    ])

    assert evidence["000001"]["hard_veto"] is True
    assert evidence["000001"]["rank_bonus"] == 0.0
    assert evidence["000001"]["veto_reasons"]


def test_supervised_narrative_lifecycle_changes_rank_and_can_veto(monkeypatch):
    rows = [_row("000001", -1.0), _row("000002", 1.0)]
    narrative_key = "narrative_transition_multitask_v1"
    versions = {
        key: (
            {"id": 9, "model_key": key, "version": "narrative-candidate"}
            if key == narrative_key
            else None
        )
        for key in model_signal_fusion.MODEL_KEYS
    }
    monkeypatch.setattr(
        model_signal_fusion,
        "_resolve_model_versions",
        lambda _bindings: versions,
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "_artifact",
        lambda version: {"ready": True} if version else None,
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda _versions, enabled=True: {
            key: {
                "status": "healthy",
                "weight_multiplier": 1.0,
                "reason": "test",
            }
            for key in model_signal_fusion.MODEL_KEYS
        },
    )

    def score(key, features, artifact=None):
        assert key == narrative_key
        if features["momentum_20"] > 0:
            return {
                "score": 0.05,
                "outputs": {
                    "lifecycle_effect_contract": (
                        model_signal_fusion.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                    ),
                    "lifecycle_effect_authorized": True,
                    "next_state_probabilities": {
                        "形成": 0.02,
                        "扩散": 0.03,
                        "成熟/拥挤": 0.25,
                        "衰退": 0.70,
                    },
                    "exit_risk": 0.95,
                    "confidence": 0.90,
                },
                "score_available": True,
                "score_status": "scored",
            }
        return {
            "score": 0.90,
            "outputs": {
                "lifecycle_effect_contract": (
                    model_signal_fusion.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                ),
                "lifecycle_effect_authorized": True,
                "next_state_probabilities": {
                    "形成": 0.25,
                    "扩散": 0.65,
                    "成熟/拥挤": 0.05,
                    "衰退": 0.05,
                },
                "exit_risk": 0.10,
                "confidence": 0.90,
            },
        }

    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", score)
    ranked, evidence, blocked, status = model_signal_fusion.apply_model_signals(
        ranked=[(row, 80.0) for row in rows],
        factor_values={
            row.symbol: {"momentum_20": row.change_pct / 100.0}
            for row in rows
        },
        histories={row.symbol: _history() for row in rows},
        themes=[{
            "theme_name": "机器人",
            "lifecycle": "扩散",
            "score": 90,
            "promotable": True,
            "companies": [
                {"ts_code": "000001.SZ"},
                {"ts_code": "000002.SZ"},
            ],
        }],
        model_bindings={narrative_key: {"id": 9}},
    )

    assert [row.symbol for row, _score in ranked] == ["000001"]
    assert evidence["000001"]["narrative_model"]["rank_adjustment"] > 0
    assert blocked[0]["symbol"] == "000002"
    assert blocked[0]["code"] == "narrative_model_exit_gate"
    narrative_status = next(
        item for item in status["production_models"]
        if item["model_key"] == narrative_key
    )
    assert narrative_status["consumed"] is True
    assert narrative_status["effective"] is True
    assert (
        narrative_status["effect_contract"]
        == model_signal_fusion.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    )
    assert (
        status["policy"]["narrative_model_lifecycle_control"][
            "hard_veto_count"
        ]
        == 1
    )
    assert (
        status["policy"]["narrative_model_lifecycle_control"][
            "effect_contract"
        ]
        == model_signal_fusion.NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
    )

    def unauthorized_score(key, features, artifact=None):
        item = score(key, features, artifact=artifact)
        item["outputs"]["lifecycle_effect_authorized"] = False
        return item

    monkeypatch.setattr(
        model_signal_fusion,
        "score_plan_model_features",
        unauthorized_score,
    )
    ranked, evidence, blocked, status = model_signal_fusion.apply_model_signals(
        ranked=[(row, 80.0) for row in rows],
        factor_values={
            row.symbol: {"momentum_20": row.change_pct / 100.0}
            for row in rows
        },
        histories={row.symbol: _history() for row in rows},
        themes=[{
            "theme_name": "机器人",
            "lifecycle": "扩散",
            "score": 90,
            "promotable": True,
            "companies": [
                {"ts_code": "000001.SZ"},
                {"ts_code": "000002.SZ"},
            ],
        }],
        model_bindings={narrative_key: {"id": 9}},
    )
    assert len(ranked) == 2
    assert blocked == []
    assert all(
        item["narrative_model"]["rank_adjustment"] == 0
        and item["narrative_model"]["effect_authorized"] is False
        for item in evidence.values()
    )
    narrative_status = next(
        item for item in status["production_models"]
        if item["model_key"] == narrative_key
    )
    assert narrative_status["consumed"] is True
    assert narrative_status["effective"] is False


def test_environment_and_maturity_adjust_effective_weights(monkeypatch):
    rows = [_row("000001", 0.0), _row("000002", 1.0)]
    monkeypatch.setattr(model_signal_fusion, "get_active_model_version", lambda key: {"id": 1, "version": f"{key}-active"})
    monkeypatch.setattr(model_signal_fusion, "_artifact", lambda version: {"ready": True})
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda versions: {
            key: {
                "status": "observing" if key == "capital_persistence_multitask_v1" else "healthy",
                "weight_multiplier": 1.0,
                "reason": "test",
            }
            for key in model_signal_fusion.MODEL_KEYS
        },
    )
    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", lambda key, features, artifact=None: {"score": features["momentum_20"]})

    _ranked, evidence, blocked, status = model_signal_fusion.apply_model_signals(
        ranked=[(row, 1.0) for row in rows],
        factor_values={row.symbol: {} for row in rows},
        histories={row.symbol: _history() for row in rows},
        themes=[],
        market_state="S3",
    )

    assert status["policy"]["environment"] == "S3"
    assert status["policy"]["maturity_multipliers"]["capital_persistence_multitask_v1"] == 0.85
    assert status["policy"]["base_rank_weight"] >= 0.45
    assert status["policy"]["disabled_gates"] == ["capital_persistence_gate"]
    assert blocked == []
    assert all(item["capital_gate_active"] is False for item in evidence.values())


def test_event_features_receive_the_same_frozen_market_state_as_fusion(monkeypatch):
    captured: list[dict[str, float]] = []
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 1, "model_key": key, "version": f"{key}-active"},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": True},
    )
    monkeypatch.setattr(model_signal_fusion, "_artifact", lambda _version: {"ready": True})
    monkeypatch.setattr(
        model_signal_fusion,
        "_point_in_time_event_features",
        lambda *_args, **_kwargs: {
            "000001": {"event_text_confidence": 0.9, "event_surprise": 0.2}
        },
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda versions: {
            key: {"status": "healthy", "weight_multiplier": 1.0, "reason": "test"}
            for key in model_signal_fusion.MODEL_KEYS
        },
    )

    def capture(_key, features, artifact=None):
        captured.append(dict(features))
        return {"score": 0.0}

    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", capture)
    model_signal_fusion.apply_model_signals(
        ranked=[(_row("000001", 0.0), 1.0)],
        factor_values={"000001": {}},
        histories={"000001": _history()},
        themes=[],
        market_state="S3",
    )

    assert captured
    assert all(item["market_state_s3"] == 1.0 for item in captured)
    assert all(item["market_state_s1"] == 0.0 for item in captured)


def test_unavailable_models_do_not_consume_fusion_weight(monkeypatch):
    rows = [_row("000001", 0.0), _row("000002", 1.0)]
    only_key = "medium_horizon_lambdarank_v1"
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 1, "model_key": key, "version": f"{key}-active"},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": True},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "_artifact",
        lambda version: {"ready": True} if version["model_key"] == only_key else None,
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "_live_model_controls",
        lambda versions: {
            key: {"status": "healthy", "weight_multiplier": 1.0, "reason": "test"}
            for key in model_signal_fusion.MODEL_KEYS
        },
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "score_plan_model_features",
        lambda key, features, artifact=None: {"score": features["momentum_20"]},
    )

    _ranked, _evidence, _blocked, status = model_signal_fusion.apply_model_signals(
        ranked=[(row, 1.0) for row in rows],
        factor_values={row.symbol: {} for row in rows},
        histories={row.symbol: _history() for row in rows},
        themes=[],
    )

    assert status["policy"]["effective_model_weights"][only_key] == 0.25
    assert status["policy"]["base_rank_weight"] == 0.75
    assert all(
        weight == 0.0
        for key, weight in status["policy"]["effective_model_weights"].items()
        if key != only_key
    )


def test_live_resolution_rejects_active_model_that_fails_production_gate(monkeypatch):
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 14, "model_key": key, "version": "legacy-active"},
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": False},
    )

    versions = model_signal_fusion._resolve_model_versions(None)

    assert all(version is None for version in versions.values())


def test_artifact_rejects_missing_or_hash_mismatched_model(tmp_path):
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        '{"model_key":"medium_horizon_lambdarank_v1",'
        '"model_path":"missing.pkl","model_sha256":"bad"}',
        encoding="utf-8",
    )

    assert model_signal_fusion._artifact({
        "model_key": "medium_horizon_lambdarank_v1",
        "artifact_path": str(artifact_path),
    }) is None


def test_stale_live_health_record_does_not_affect_new_active_version(monkeypatch):
    class Cursor:
        def fetchall(self):
            return [{
                "model_key": "capital_persistence_multitask_v1",
                "active_version": "capital-v1",
                "status": "degraded",
                "weight_multiplier": 0.2,
                "reason": "old_version_failure",
            }]

    class Connection:
        def execute(self, sql):
            assert "model_live_health" in sql
            return Cursor()

    @contextmanager
    def fake_business_conn():
        yield Connection()

    monkeypatch.setattr(model_signal_fusion, "business_conn", fake_business_conn)
    controls = model_signal_fusion._live_model_controls({
        "capital_persistence_multitask_v1": {"version": "capital-v2"},
    })

    assert controls["capital_persistence_multitask_v1"] == {
        "status": "observing",
        "weight_multiplier": 1.0,
        "reason": "live_health_evidence_missing",
    }


def test_unqualified_candidate_can_score_online_without_trade_authority(monkeypatch):
    key = "event_impact_assessor_v2"
    candidate = {
        "id": 280,
        "model_key": key,
        "version": "event-candidate-280",
        "status": "rejected",
        "artifact_path": "/tmp/event-candidate-280.json",
    }
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda model_key: None,
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "list_model_versions",
        lambda model_key, limit=20: [candidate] if model_key == key else [],
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "audit_plan_model_production_eligibility",
        lambda _version: {"passed": False},
    )

    authorized = model_signal_fusion._resolve_model_versions(None)
    online = model_signal_fusion._resolve_online_inference_versions(authorized, None)

    assert authorized[key] is None
    assert online[key] == candidate


def test_core_model_with_strict_ab_evidence_remains_shadow_only(monkeypatch):
    version = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "metrics": {
            "task_metric_passed": True,
            "walk_forward_windows": 5,
            "task_native_oos": {"passed": True},
            "strategy_impact_ab": {"status": "passed"},
        },
    }
    assert model_signal_fusion._limited_live_authorized(version) is False


def test_inconclusive_or_negative_future_shadow_never_gets_live_authority(monkeypatch):
    base = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "metrics": {
            "task_metric_passed": True,
            "walk_forward_windows": 5,
            "task_native_oos": {"passed": True},
            "strategy_impact_ab": {"status": "passed"},
        },
    }
    inconclusive = model_signal_fusion._limited_live_authorization({
        **base,
        "validation": {"model_shadow": {
            "status": "extended_for_inconclusive_evidence",
            "observed_return_sessions": 20,
            "latest_evidence": {"gates": {
                "cumulative_return_delta_positive": True,
                "mean_daily_return_delta_positive": False,
            }},
        }},
    })
    negative = model_signal_fusion._limited_live_authorization({
        **base,
        "validation": {"model_shadow": {
            "status": "extended_for_inconclusive_evidence",
            "observed_return_sessions": 20,
            "latest_evidence": {"gates": {
                "cumulative_return_delta_positive": False,
                "mean_daily_return_delta_positive": False,
            }},
        }},
    })

    assert inconclusive["authorized"] is False
    assert inconclusive["tier"] == "shadow_only"
    assert inconclusive["weight_scale"] == 0.0
    assert negative["authorized"] is False
    assert negative["tier"] == "shadow_only"


def test_passed_future_shadow_still_requires_formal_production_promotion(monkeypatch):
    version = {
        "id": 216,
        "model_key": "medium_horizon_lambdarank_v1",
        "metrics": {
            "task_metric_passed": True,
            "walk_forward_windows": 5,
            "task_native_oos": {"passed": True},
            "strategy_impact_ab": {"status": "passed"},
        },
        "validation": {"model_shadow": {
            "status": "passed",
            "observed_return_sessions": 20,
        }},
    }
    authority = model_signal_fusion._limited_live_authorization(version)

    assert authority["authorized"] is False
    assert authority["tier"] == "shadow_only"
    assert authority["weight_scale"] == 0.0
    assert authority["reason"] == "candidate_requires_complete_production_qualification"


def test_frozen_model_bundle_resolves_exact_versions_without_active_pointer(monkeypatch):
    versions = {
        index: {
            "id": index,
            "model_key": key,
            "version": f"{key}-frozen",
            "status": "candidate",
            "artifact_path": f"/tmp/{key}.json",
        }
        for index, key in enumerate(model_signal_fusion.MODEL_KEYS, start=11)
    }
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("frozen research must never read active model pointers")
        ),
    )
    monkeypatch.setattr(
        model_signal_fusion,
        "get_model_version",
        lambda version_id: versions.get(int(version_id)),
    )

    frozen = model_signal_fusion.freeze_model_bindings({
        key: {"id": version_id}
        for version_id, key in enumerate(model_signal_fusion.MODEL_KEYS, start=11)
    })

    assert set(frozen) == set(model_signal_fusion.MODEL_KEYS)
    assert [frozen[key]["id"] for key in model_signal_fusion.MODEL_KEYS] == list(range(11, 16))
    assert all(frozen[key]["model_key"] == key for key in model_signal_fusion.MODEL_KEYS)


def test_model_created_or_trained_after_replay_cutoff_is_not_consumed():
    created_late = model_signal_fusion._point_in_time_model_availability(
        {"created_at": "2026-07-22T10:00:00+08:00"},
        {"training_dataset": {"validation_end": "20260720"}},
        as_of_time="2026-07-21T15:00:00+08:00",
    )
    trained_late = model_signal_fusion._point_in_time_model_availability(
        {"created_at": "2026-07-20T10:00:00+08:00"},
        {"training_dataset": {"validation_end": "20260722"}},
        as_of_time="2026-07-21T15:00:00+08:00",
    )
    available = model_signal_fusion._point_in_time_model_availability(
        {"created_at": "2026-07-20T10:00:00+08:00"},
        {"training_dataset": {"validation_end": "20260720"}},
        as_of_time="2026-07-21T15:00:00+08:00",
    )

    assert created_late == {"available": False, "reason": "model_version_created_after_cutoff"}
    assert trained_late == {"available": False, "reason": "model_training_evidence_after_cutoff"}
    assert available == {"available": True, "reason": "point_in_time_available"}


def test_explicit_frozen_bindings_are_resolved_once(monkeypatch):
    model_signal_fusion._FROZEN_BINDING_CACHE.clear()
    calls = []
    monkeypatch.setattr(
        model_signal_fusion,
        "get_model_version",
        lambda version_id: calls.append(version_id) or {
            "id": version_id,
            "model_key": "medium_horizon_lambdarank_v1",
            "version": "frozen-v1",
        },
    )
    bindings = {
        key: (
            {"id": 91}
            if key == "medium_horizon_lambdarank_v1"
            else None
        )
        for key in model_signal_fusion.MODEL_KEYS
    }

    first = model_signal_fusion._resolve_model_versions(bindings)
    second = model_signal_fusion._resolve_model_versions(bindings)

    assert calls == [91]
    assert first == second
    assert first is not second

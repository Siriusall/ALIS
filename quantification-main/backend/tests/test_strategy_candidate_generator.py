from app.services import strategy_candidate_generator as generator


def test_selection_allocation_candidate_freezes_evidence_and_only_changes_top_n(monkeypatch):
    source_content = {
        "schema_version": "source-v1",
        "score": {"factors": {"momentum_20": 1.0}},
        "filters": [{"factor": "is_st", "op": "==", "value": 0}],
        "lookback_days": 60,
        "rebalance": {"freq": "daily", "top_n": 20},
        "portfolio_optimizer": "empirical_cvar_slsqp_v2",
        "execution_contract": "t_close_signal_t_plus_1_open_v1",
    }
    source = {
        "id": 56,
        "version": "source-v56",
        "config": {"strategy_id": 11, "selection_strategy": source_content},
    }
    versions = []
    strategies = {}
    created = []

    monkeypatch.setattr(generator, "get_model_version", lambda version_id: source if version_id == 56 else None)
    monkeypatch.setattr(generator, "get_strategy", lambda strategy_id: strategies.get(strategy_id))
    monkeypatch.setattr(generator, "list_model_versions", lambda *_args, **_kwargs: versions)
    monkeypatch.setattr(generator, "list_strategies", lambda: [])
    monkeypatch.setattr(generator, "_ensure_strategy_sequence", lambda: None)
    monkeypatch.setattr(generator, "resolve_production_strategy_champion", lambda: {"champion_version": "champion-v1"})
    monkeypatch.setattr(generator, "active_factor_bindings", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(generator, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(generator, "upsert_experiment_memory", lambda **_kwargs: {})

    def create_strategy(name, strategy_type, content):
        strategies[22] = {"id": 22, "name": name, "content": content}
        return strategies[22]

    def create_model_version(**kwargs):
        created.append(kwargs)
        versions.append({
            "id": 77,
            "version": kwargs["version"],
            "config": kwargs["config"],
            "validation": kwargs["validation"],
            "status": kwargs["status"],
        })
        return 77

    monkeypatch.setattr(generator, "create_strategy", create_strategy)
    monkeypatch.setattr(generator, "create_model_version", create_model_version)
    attribution = {
        "schema_version": "alis_oos_failure_attribution_v1",
        "primary_cause": "selection_or_allocation_residual",
        "confidence": "high",
        "evidence": {"aggregate_residual_gap": -0.12},
        "_routing_source": {"model_version_id": 56},
    }

    result = generator.ensure_selection_allocation_challenger(
        failure_attribution=attribution,
        source_model_version_id=56,
    )

    assert result == {
        "strategy_id": 22,
        "model_version_id": 77,
        "name": "ALIS Challenger 高置信 Top8 选择 V1-源版本56",
        "version": created[0]["version"],
        "status": "pending_oos",
        "generated_by": "automatic_failure_attribution",
    }
    created_content = strategies[22]["content"]
    assert created_content["rebalance"] == {"freq": "daily", "top_n": 8}
    assert created_content["score"] == source_content["score"]
    assert created_content["filters"] == source_content["filters"]
    assert created_content["portfolio_optimizer"] == source_content["portfolio_optimizer"]
    contract = created[0]["config"]["evidence_contract"]
    assert contract["source_model_version_id"] == 56
    assert contract["changed_fields"] == [{"path": "rebalance.top_n", "from": 20, "to": 8}]
    assert contract["experiment_memory"]["hypothesis"]["template"] == "selection_allocation_top_n_v1"
    assert "_routing_source" not in contract["source_failure_attribution"]
    assert created[0]["validation"]["next_gate"] == "quick_diagnostic"

    again = generator.ensure_selection_allocation_challenger(
        failure_attribution=attribution,
        source_model_version_id=56,
    )

    assert again["model_version_id"] == 77
    assert len(created) == 1


def test_selection_allocation_candidate_requires_medium_or_higher_confidence(monkeypatch):
    monkeypatch.setattr(generator, "get_model_version", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read source")))

    result = generator.ensure_selection_allocation_challenger(
        failure_attribution={"primary_cause": "selection_or_allocation_residual", "confidence": "low"},
        source_model_version_id=56,
    )

    assert result is None


def test_selection_allocation_candidate_does_not_retry_formally_rejected_hypothesis(monkeypatch):
    monkeypatch.setattr(generator, "find_rejected_hypothesis", lambda *_args, **_kwargs: {"outcome_stage": "formal_oos"})
    monkeypatch.setattr(generator, "get_model_version", lambda *_args: (_ for _ in ()).throw(AssertionError("must not retry source")))

    result = generator.ensure_selection_allocation_challenger(
        failure_attribution={"primary_cause": "selection_or_allocation_residual", "confidence": "high", "evidence": {"residual": -0.2}},
        source_model_version_id=56,
    )

    assert result is None

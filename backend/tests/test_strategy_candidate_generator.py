from app.services import strategy_candidate_generator as generator


def test_autonomous_lineage_stops_after_root_search_budget(monkeypatch):
    root = {"id": 57, "config": {"strategy_id": 11, "selection_strategy": {"score": {"factors": {"momentum_20": 1}}}}}
    versions = [
        {
            "id": 70 + index,
            "config": {"lineage": {"root_model_version_id": 57, "generation_index": index + 1}},
            "validation": {"status": "formal_oos_failed"},
            "metrics": {"formal_oos": {"status": "failed"}},
        }
        for index in range(generator.MAX_AUTONOMOUS_LINEAGE_TRIALS_PER_ROOT)
    ]
    monkeypatch.setattr(generator, "get_model_version", lambda _id: root)
    monkeypatch.setattr(generator, "get_strategy", lambda _id: {"id": 11, "content": root["config"]["selection_strategy"]})
    monkeypatch.setattr(generator, "list_model_versions", lambda *_args, **_kwargs: versions)

    assert generator.ensure_autonomous_lineage_successor(root_model_version_id=57) is None


def test_adaptive_parameter_search_stops_after_unique_templates(monkeypatch):
    versions = [
        {
            "id": 80 + index,
            "version": f"g{index + 1}",
            "config": {"generation": "automatic_parameter_search", "strategy_id": 20 + index},
            "validation": {"status": "formal_oos_rejected"},
        }
        for index in range(generator.MAX_ADAPTIVE_PARAMETER_TRIALS)
    ]
    monkeypatch.setattr(generator, "get_strategy", lambda strategy_id: {"id": strategy_id, "name": "latest"})

    result = generator._ensure_adaptive_parameter_challenger({}, {"champion_version": "v1"}, versions)

    assert result["status"] == "search_budget_exhausted"
    assert result["search_budget"] == {"trial_count": 4, "maximum_trials": 4}


def test_selection_allocation_candidate_freezes_evidence_and_builds_bounded_research_selector(monkeypatch):
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
        "name": "ALIS Challenger 价值低波反转 V3-源版本56",
        "version": created[0]["version"],
        "status": "pending_oos",
        "generated_by": "automatic_failure_attribution",
    }
    created_content = strategies[22]["content"]
    assert created_content["rebalance"] == {"freq": "weekly", "top_n": 15}
    assert created_content["score"]["normalization"] == "rank_percentile"
    assert created_content["score"]["factors"] == {
        "earnings_yield": .45,
        "downside_volatility_60": -.20,
        "max_drawdown_60": .20,
        "momentum_5": -.15,
    }
    assert created_content["filters"][-1] == {"factor": "earnings_yield", "op": ">", "value": 0}
    assert created_content["universe"]["exclude_smallest_float_mv_pct"] == .30
    assert created_content["challenger_controls"]["buy_rank_max"] == 12
    assert created_content["challenger_controls"]["hold_rank_max"] == 18
    assert created_content["expected_return"] == {"source": "ranking_score", "scale": .06}
    assert created_content["portfolio_optimizer"] == source_content["portfolio_optimizer"]
    contract = created[0]["config"]["evidence_contract"]
    assert contract["source_model_version_id"] == 56
    assert any(item["path"] == "rebalance.top_n" and item["to"] == 15 for item in contract["changed_fields"])
    assert contract["experiment_memory"]["hypothesis"]["template"] == "china_value_lowvol_reversal_v1"
    assert len(contract["research_sources"]) == 3
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


def test_near_miss_candidate_only_changes_diversification_controls(monkeypatch):
    source_content = {
        "schema_version": "value-v3",
        "universe": {"exclude_smallest_float_mv_pct": .30},
        "filters": [{"factor": "earnings_yield", "op": ">", "value": 0}],
        "score": {"normalization": "rank_percentile", "factors": {"earnings_yield": .45}},
        "expected_return": {"source": "ranking_score", "scale": .06},
        "rebalance": {"freq": "weekly", "top_n": 15},
        "challenger_controls": {"max_single_weight": .08, "buy_rank_max": 12, "hold_rank_max": 18},
    }
    source = {
        "id": 56,
        "version": "value-v3",
        "config": {"strategy_id": 10, "selection_strategy": source_content},
        "metrics": {"formal_oos": {
            "status": "failed", "valid_windows": 5, "outperform_windows": 3,
            "aggregate_excess_return": -.0023,
        }},
    }
    versions = []
    strategies = {10: {"id": 10, "name": "source", "content": source_content}}
    created = []
    monkeypatch.setattr(generator, "get_model_version", lambda version_id: source if version_id == 56 else None)
    monkeypatch.setattr(generator, "get_strategy", lambda strategy_id: strategies.get(strategy_id))
    monkeypatch.setattr(generator, "list_model_versions", lambda *_args, **_kwargs: versions)
    monkeypatch.setattr(generator, "list_strategies", lambda: list(strategies.values()))
    monkeypatch.setattr(generator, "_ensure_strategy_sequence", lambda: None)
    monkeypatch.setattr(generator, "resolve_production_strategy_champion", lambda: {"champion_version": "champion-v1"})
    monkeypatch.setattr(generator, "active_factor_bindings", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generator, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(generator, "upsert_experiment_memory", lambda **_kwargs: {})

    def create_strategy(name, strategy_type, content):
        strategies[11] = {"id": 11, "name": name, "content": content}
        return strategies[11]

    def create_model_version(**kwargs):
        created.append(kwargs)
        versions.append({
            "id": 57, "version": kwargs["version"], "config": kwargs["config"],
            "validation": kwargs["validation"], "status": kwargs["status"],
        })
        return 57

    monkeypatch.setattr(generator, "create_strategy", create_strategy)
    monkeypatch.setattr(generator, "create_model_version", create_model_version)

    result = generator.ensure_diversified_selection_challenger(source_model_version_id=56)

    assert result["model_version_id"] == 57
    content = strategies[11]["content"]
    assert content["score"] == source_content["score"]
    assert content["filters"] == source_content["filters"]
    assert content["rebalance"]["top_n"] == 30
    assert content["challenger_controls"]["max_single_weight"] == .04
    assert content["challenger_controls"]["buy_rank_max"] == 24
    assert content["challenger_controls"]["hold_rank_max"] == 36
    assert created[0]["validation"]["next_gate"] == "quick_diagnostic"


def test_passed_selector_creates_three_bounded_regime_risk_hypotheses(monkeypatch):
    source_content = {
        "schema_version": "value-v4",
        "universe": {"exclude_smallest_float_mv_pct": .30},
        "filters": [{"factor": "earnings_yield", "op": ">", "value": 0}],
        "score": {"normalization": "rank_percentile", "factors": {"earnings_yield": .45}},
        "rebalance": {"freq": "weekly", "top_n": 30},
        "challenger_controls": {"max_single_weight": .04, "rationale": ["source"]},
    }
    source = {
        "id": 57,
        "version": "value-v4",
        "config": {"strategy_id": 11, "selection_strategy": source_content},
        "metrics": {"formal_oos": {"status": "passed", "valid_windows": 5, "aggregate_excess_return": .0047}},
    }
    versions = []
    strategies = {11: {"id": 11, "name": "source", "content": source_content}}
    created_versions = []
    monkeypatch.setattr(generator, "get_model_version", lambda version_id: source if version_id == 57 else None)
    monkeypatch.setattr(generator, "get_strategy", lambda strategy_id: strategies.get(strategy_id))
    monkeypatch.setattr(generator, "list_model_versions", lambda *_args, **_kwargs: versions)
    monkeypatch.setattr(generator, "list_strategies", lambda: list(strategies.values()))
    monkeypatch.setattr(generator, "_ensure_strategy_sequence", lambda: None)
    monkeypatch.setattr(generator, "resolve_production_strategy_champion", lambda: {"champion_version": "champion-v1"})
    monkeypatch.setattr(generator, "active_factor_bindings", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generator, "find_rejected_hypothesis", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(generator, "upsert_experiment_memory", lambda **_kwargs: {})

    def create_strategy(name, strategy_type, content):
        strategy_id = 20 + len(strategies)
        strategies[strategy_id] = {"id": strategy_id, "name": name, "content": content}
        return strategies[strategy_id]

    def create_model_version(**kwargs):
        version_id = 70 + len(versions)
        item = {
            "id": version_id,
            "version": kwargs["version"],
            "config": kwargs["config"],
            "validation": kwargs["validation"],
            "status": kwargs["status"],
        }
        versions.append(item)
        created_versions.append(kwargs)
        return version_id

    monkeypatch.setattr(generator, "create_strategy", create_strategy)
    monkeypatch.setattr(generator, "create_model_version", create_model_version)

    outcomes = generator.ensure_regime_aware_selection_challengers(source_model_version_id=57)

    assert len(outcomes) == 3
    policies = [item["config"]["selection_strategy"]["dynamic_market_exposure"] for item in created_versions]
    assert {policy["schema_version"] for policy in policies} == {
        "transparent_four_state_profile_ceiling_v1",
        "dual_trend_change_alarm_v1",
        "regime_volatility_managed_v1",
    }
    assert all(policy["maximum_equity_weight"] == 1.0 for policy in policies)
    assert all(policy["etf_fallback"] is False for policy in policies)
    assert all(item["config"]["selection_strategy"]["score"] == source_content["score"] for item in created_versions)
    assert all(len(item["config"]["evidence_contract"]["research_sources"]) == 4 for item in created_versions)
    assert all(item["validation"]["next_gate"] == "quick_diagnostic" for item in created_versions)

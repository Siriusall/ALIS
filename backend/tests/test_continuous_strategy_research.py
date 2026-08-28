from app.services import continuous_strategy_research as research
from app.services.strategy_config import (
    get_strategy_expert_route,
    get_strategy_factor_names,
    get_strategy_score_for_market_state,
)
import json
import pytest
import uuid


def test_dynamic_factor_complexity_is_not_a_fixed_range():
    small = research.dynamic_factor_complexity(
        universe_size=66,
        seed_material={"snapshot": "a", "proposal": 1},
    )
    expanded = research.dynamic_factor_complexity(
        universe_size=66,
        seed_material={"snapshot": "a"},
        parent_factor_count=12,
        marginal_contribution=0.01,
        redundancy_ratio=0.0,
    )
    contracted = research.dynamic_factor_complexity(
        universe_size=66,
        seed_material={"snapshot": "a"},
        parent_factor_count=12,
        marginal_contribution=-0.01,
        redundancy_ratio=0.5,
    )
    assert small["fixed_factor_count"] is False
    assert expanded["target_factor_count"] > 8
    assert contracted["target_factor_count"] < expanded["target_factor_count"]
    assert expanded["target_factor_count"] <= expanded["resource_safety_ceiling"]


def test_dynamic_market_environments_are_evidence_driven_and_include_stress():
    rows = research.build_dynamic_market_environments(
        evidence_hash="evidence-1",
        historical_windows=[{"start": "2026-01-01", "end": "2026-01-31"}],
        observed_dimensions={
            "trend": ["up", "down", "sideways"],
            "liquidity": ["expanding", "contracting"],
            "style": ["large_cap", "small_cap"],
        },
        extreme_windows=[{
            "start": "2015-06-15",
            "end": "2015-08-31",
            "selection_rule": "historical_drawdown_tail",
        }],
    )
    kinds = {item["environment_type"] for item in rows}
    assert kinds == {
        "historical_rolling",
        "observed_state_slice",
        "extreme_historical_replay",
        "calibrated_stress",
    }
    assert len({item["environment_id"] for item in rows}) == len(rows)
    stress = [item for item in rows if item["environment_type"] == "calibrated_stress"]
    assert stress
    assert all(item["source_contract"]["calibration_required"] for item in stress)
    assert all(item["source_contract"]["price_or_return_fabrication_forbidden"] for item in stress)


def test_strategy_failure_routes_to_targeted_model_training():
    request = research.targeted_model_training_request(
        strategy_hash="strategy-1",
        environment_id="liquidity-contracting",
        attribution={
            "primary_gap": "capital_persistence_failure",
            "reason": "capital signal decayed after entry",
        },
        data_cutoff="2026-07-28",
    )
    assert request is not None
    assert request["model_key"] == "capital_persistence_multitask_v1"
    assert request["production_change_forbidden"] is True
    assert request["training_objective"]["target_environment"] == "liquidity-contracting"


def test_unknown_failure_stays_in_memory_without_arbitrary_training():
    assert research.targeted_model_training_request(
        strategy_hash="strategy-1",
        environment_id=None,
        attribution={"primary_gap": "unknown"},
        data_cutoff="2026-07-28",
    ) is None


def test_candidate_batch_runs_all_four_modes_with_dynamic_sizes():
    factors = [
        {"factor_key": f"factor_{index}", "category": f"category_{index % 7}", "direction": 1}
        for index in range(66)
    ]
    prototypes = [
        {
            "prototype_id": "prototype-a",
            "factor_weights": {f"factor_{index}": 0.1 for index in range(12)},
            "marginal_contribution": 0.01,
        },
        {
            "prototype_id": "prototype-b",
            "factor_weights": {f"factor_{index}": 0.1 for index in range(8, 22)},
            "marginal_contribution": 0.005,
        },
    ]
    rows = research.generate_candidate_batch(
        snapshot_id="snapshot-v10",
        factors=factors,
        count=4,
        prototypes=prototypes,
        failure_memories=[{"preferred_factor_categories": ["category_6"]}],
    )
    assert {item["generation_mode"] for item in rows} == set(research.GENERATION_MODES)
    assert len({item["candidate_hash"] for item in rows}) == 4
    assert any(item["factor_count"] > 8 for item in rows)
    assert all(item["factor_complexity"]["fixed_factor_count"] is False for item in rows)
    assert all(item["holding_period_sessions"] in {3, 5, 10, 20, 40, 60} for item in rows)


def test_model_candidate_binding_changes_complete_candidate_identity(monkeypatch):
    monkeypatch.setattr(
        research,
        "generate_candidate_batch",
        lambda **kwargs: [{
            "candidate_id": "strategy-research-candidate-base",
            "candidate_hash": "base",
            "snapshot_id": kwargs["snapshot_id"],
            "factor_weights": {"momentum_20": 1.0},
            "holding_period_sessions": 20,
        }],
    )
    monkeypatch.setattr(research, "_persist_research_wave", lambda **kwargs: None)
    monkeypatch.setattr(research, "enqueue_research_event", lambda **kwargs: {"status": "queued"})

    result = research.run_candidate_generation_line(
        snapshot_id="snapshot-model-candidate",
        factors=[{"factor_key": "momentum_20"}],
        environment_ids=["environment-1"],
        batch_size=1,
        model_bindings_override={
            "medium_horizon_lambdarank_v1": {"id": 99, "version": "challenger-v2"},
        },
    )

    candidate = result["generated"][0]
    assert candidate["candidate_hash"] != "base"
    assert candidate["model_bindings"]["medium_horizon_lambdarank_v1"]["id"] == 99


def test_candidate_batch_nonce_advances_random_search():
    factors = [
        {"factor_key": f"factor_{index}", "category": f"category_{index % 7}", "direction": 1}
        for index in range(66)
    ]

    first = research.generate_candidate_batch(
        snapshot_id="snapshot-v10",
        factors=factors,
        count=4,
        generation_nonce=0,
    )
    second = research.generate_candidate_batch(
        snapshot_id="snapshot-v10",
        factors=factors,
        count=4,
        generation_nonce=4,
    )

    assert {item["candidate_hash"] for item in first}.isdisjoint(
        item["candidate_hash"] for item in second
    )


def test_candidate_generation_preserves_random_exploration_and_applies_guide(monkeypatch):
    factors = [
        {"factor_key": f"factor_{index}", "category": f"category_{index % 4}", "direction": 1}
        for index in range(20)
    ]
    monkeypatch.setattr(
        research,
        "factor_combination_guidance",
        lambda limit=200: {
            "sampling_probabilities": {
                "factor_19": 0.90,
                **{f"factor_{index}": 0.10 / 19 for index in range(19)},
            }
        },
    )

    rows = research.generate_candidate_batch(
        snapshot_id="guided-snapshot",
        factors=factors,
        count=4,
    )

    random_row = next(row for row in rows if row["generation_mode"] == "random_exploration")
    guided_rows = [row for row in rows if row["generation_mode"] != "random_exploration"]
    assert random_row["guidance_contract"]["pure_random"] is True
    assert random_row["guidance_contract"]["applied"] is False
    assert guided_rows
    assert all(row["guidance_contract"]["applied"] for row in guided_rows)


def test_replay_cache_contract_changes_with_environment_version():
    candidate = {"candidate_hash": "candidate-hash"}
    content = {"factor_weights": {"momentum_20": 1.0}}
    spec = {"factor_bindings": ["momentum_20"], "model_bindings": []}
    first, first_contract = research._replay_cache_key(
        candidate=candidate,
        environment={"environment_id": "env-a", "evidence_hash": "v1"},
        snapshot_id="snapshot-a",
        content=content,
        spec=spec,
    )
    second, second_contract = research._replay_cache_key(
        candidate=candidate,
        environment={"environment_id": "env-a", "evidence_hash": "v2"},
        snapshot_id="snapshot-a",
        content=content,
        spec=spec,
    )
    repeated, repeated_contract = research._replay_cache_key(
        candidate=candidate,
        environment={"environment_id": "env-a", "evidence_hash": "v1"},
        snapshot_id="snapshot-a",
        content=content,
        spec=spec,
    )
    assert first == repeated
    assert first_contract == repeated_contract
    assert first != second
    assert first_contract != second_contract


def test_environment_matrix_reuses_cache_and_writes_factor_learning(monkeypatch, request):
    from app.services import continuous_factor_research as factor_research

    nonce = uuid.uuid4().hex
    snapshot_id = f"cache-snapshot-{nonce}"
    snapshot = {"snapshot_id": snapshot_id, "data_as_of": "20260728"}
    candidate_ids = [f"cache-candidate-{nonce}-a", f"cache-candidate-{nonce}-b"]

    def cleanup():
        with research._conn() as conn:
            conn.execute(
                "DELETE FROM strategy_research_runtime_metrics WHERE candidate_id IN (?,?)",
                tuple(candidate_ids),
            )
            conn.execute(
                "DELETE FROM strategy_factor_contributions WHERE candidate_id IN (?,?)",
                tuple(candidate_ids),
            )
            conn.execute(
                "DELETE FROM strategy_factor_learning_runs WHERE candidate_id IN (?,?)",
                tuple(candidate_ids),
            )
            conn.execute(
                "DELETE FROM strategy_environment_backtests WHERE candidate_id IN (?,?)",
                tuple(candidate_ids),
            )
            conn.execute(
                "DELETE FROM strategy_environment_replay_cache WHERE candidate_hash=?",
                (f"cache-shared-hash-{nonce}",),
            )
            conn.execute(
                "DELETE FROM strategy_factor_interactions WHERE factor_left='factor_a' AND factor_right='factor_b'"
            )
            conn.execute(
                "DELETE FROM dynamic_market_environments WHERE environment_id LIKE ?",
                (f"cache-environment-{nonce}-%",),
            )

    request.addfinalizer(cleanup)
    monkeypatch.setattr(factor_research, "_active_research_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        factor_research,
        "_baseline_research_spec",
        lambda _snapshot: {
            "factor_bindings": ["factor_a", "factor_b"],
            "model_bindings": [],
            "market_state_binding": {},
            "strategy_controller_version_id": 1,
        },
    )
    monkeypatch.setattr(
        factor_research,
        "_strategy_content",
        lambda weights, _spec: {"factor_weights": weights},
    )
    environments = [
        {
            "environment_id": f"cache-environment-{nonce}-{index}",
            "evidence_hash": f"cache-environment-version-{nonce}-{index}",
            "environment_type": "historical_rolling",
            "name": f"cache window {index}",
            "specification": {"start": "2026-01-01", "end": "2026-01-31"},
            "source_contract": {},
        }
        for index in range(2)
    ]
    research.persist_market_environments(environments)
    calls = []

    def replay_runner(**kwargs):
        calls.append(kwargs["start_date"])
        return {
            "metrics": {
                "total_return": 0.03,
                "benchmark_return": 0.01,
                "excess_return": 0.02,
                "max_drawdown": -0.04,
                "turnover_rate": 0.2,
            },
            "data_quality": {"point_in_time": True},
            "equity_series": [],
        }

    candidate = {
        "candidate_id": candidate_ids[0],
        "candidate_hash": f"cache-shared-hash-{nonce}",
        "snapshot_id": snapshot_id,
        "factor_weights": {"factor_a": 0.6, "factor_b": 0.4},
        "environment_ids": [item["environment_id"] for item in environments],
    }
    first = research.run_candidate_environment_matrix(
        candidate=candidate,
        replay_runner=replay_runner,
    )
    second = research.run_candidate_environment_matrix(
        candidate={**candidate, "candidate_id": candidate_ids[1]},
        replay_runner=replay_runner,
    )

    assert first["runtime"]["cache_misses"] == 2
    assert second["runtime"]["cache_hits"] == 2
    assert len(calls) == 2
    guidance = research.factor_combination_guidance()
    assert {row["factor_key"] for row in guidance["factor_scores"]} >= {
        "factor_a", "factor_b",
    }
    assert any(
        {row["factor_left"], row["factor_right"]} == {"factor_a", "factor_b"}
        for row in guidance["interactions"]
    )


def test_candidate_generation_binds_event_and_wave_to_candidate(monkeypatch):
    candidate = {
        "candidate_id": "candidate-a",
        "candidate_hash": "hash-a",
        "factor_weights": {"momentum_20": 1.0},
    }
    monkeypatch.setattr(
        research,
        "generate_candidate_batch",
        lambda **_kwargs: [dict(candidate)],
    )

    def persist_wave(*, snapshot_id, candidates):
        assert snapshot_id == "snapshot-a"
        candidates[0]["research_wave_id"] = "wave-a"
        return {"wave_id": "wave-a", "status": "queued"}

    captured = []
    monkeypatch.setattr(research, "_persist_research_wave", persist_wave)
    monkeypatch.setattr(
        research,
        "enqueue_research_event",
        lambda **kwargs: captured.append(kwargs) or {"event_id": "event-a"},
    )
    monkeypatch.setattr(
        research,
        "refresh_research_wave",
        lambda wave_id: {"wave_id": wave_id, "status": "queued"},
    )

    result = research.run_candidate_generation_line(
        snapshot_id="snapshot-a",
        factors=[{"factor_key": "momentum_20"}],
        environment_ids=["environment-a"],
    )

    assert result["research_wave_id"] == "wave-a"
    assert result["generated"][0]["research_wave_id"] == "wave-a"
    assert captured[0]["source_id"] == "candidate-a"
    assert captured[0]["payload"]["environment_ids"] == ["environment-a"]


def test_active_candidate_generation_stops_at_pending_backlog_limit(monkeypatch):
    monkeypatch.setattr(research, "MAX_PENDING_CANDIDATES", 4)
    monkeypatch.setattr(
        research,
        "_candidate_backlog_summary",
        lambda **_kwargs: {
            "active": [{"candidate_id": f"candidate-{index}"} for index in range(4)],
            "active_count": 4,
            "quarantined": [],
            "quarantined_count": 0,
            "duplicate_completed": [],
            "duplicate_completed_count": 0,
        },
    )
    from app.services import continuous_factor_research as factor_research

    monkeypatch.setattr(
        factor_research,
        "_active_research_snapshot",
        lambda: {"snapshot_id": "snapshot-current"},
    )

    result = research.run_active_candidate_generation_line(batch_size=4)

    assert result["status"] == "backpressured"
    assert result["generated"] == []
    assert result["pending_candidate_count"] == 4
    assert result["maximum_pending_candidates"] == 4
    assert result["pipeline_health"]["status"] == "backpressured"


def test_active_candidate_generation_continues_when_blocked_candidates_are_quarantined(monkeypatch):
    monkeypatch.setattr(research, "MAX_PENDING_CANDIDATES", 4)
    monkeypatch.setattr(
        research,
        "_candidate_backlog_summary",
        lambda **_kwargs: {
            "active": [],
            "active_count": 0,
            "quarantined": [{"candidate_id": "blocked-candidate"}],
            "quarantined_count": 1,
            "duplicate_completed": [{"candidate_id": "completed-candidate"}],
            "duplicate_completed_count": 1,
        },
    )
    from app.services import continuous_factor_research as factor_research

    monkeypatch.setattr(
        factor_research,
        "_active_research_snapshot",
        lambda: {
            "snapshot_id": "snapshot-current",
            "contract": {"evidence_signature": "evidence"},
            "research_windows": [],
            "factors": [],
            "data_as_of": "20260828",
        },
    )
    monkeypatch.setattr(research, "backfill_factor_learning", lambda: None)
    monkeypatch.setattr(research, "build_dynamic_market_environments", lambda **_kwargs: [])
    monkeypatch.setattr(research, "persist_market_environments", lambda _items: None)
    monkeypatch.setattr(research, "analyze_factor_pool", lambda _items: {"eligible_factors": []})
    monkeypatch.setattr(research, "_current_snapshot_prototypes", lambda _snapshot: [])
    monkeypatch.setattr(research, "_load_failure_memories", lambda **_kwargs: [])
    monkeypatch.setattr(research, "_candidate_event_count", lambda: 0)
    monkeypatch.setattr(
        research,
        "run_candidate_generation_line",
        lambda **_kwargs: {"ok": True, "generated": [{"candidate_id": "new-candidate"}]},
    )

    result = research.run_active_candidate_generation_line(batch_size=4)

    assert result["status"] == "generated"
    assert result["generated"][0]["candidate_id"] == "new-candidate"
    assert result["pipeline_health"]["status"] == "degraded_continuing"
    assert result["pipeline_health"]["quarantined_count"] == 1
    assert result["pipeline_health"]["duplicate_completed_count"] == 1


def test_current_snapshot_prototypes_exclude_retired_model_contracts(monkeypatch):
    snapshot = {
        "snapshot_id": "snapshot-current",
        "contract": {
            "research_package": {
                "model_bindings": {
                    key: None for key in research.MODEL_KEYS
                },
            },
        },
    }
    monkeypatch.setattr(
        research,
        "_load_prototypes",
        lambda limit=200: [
            {
                "prototype_id": "old-snapshot",
                "research_snapshot_id": "snapshot-old",
                "model_bindings": {key: None for key in research.MODEL_KEYS},
            },
            {
                "prototype_id": "retired-model",
                "research_snapshot_id": "snapshot-current",
                "model_bindings": {
                    key: (
                        {"id": 16}
                        if key == "medium_horizon_lambdarank_v1"
                        else None
                    )
                    for key in research.MODEL_KEYS
                },
            },
            {
                "prototype_id": "current",
                "research_snapshot_id": "snapshot-current",
                "model_bindings": {key: None for key in research.MODEL_KEYS},
            },
        ],
    )

    rows = research._current_snapshot_prototypes(snapshot)

    assert [item["prototype_id"] for item in rows] == ["current"]


def test_cluster_distillation_blocks_stale_research_snapshot(monkeypatch):
    from app.services import continuous_factor_research as factor_research

    monkeypatch.setattr(
        factor_research,
        "_active_research_snapshot",
        lambda: {"snapshot_id": "snapshot-current"},
    )

    result = research.run_cluster_distillation_line(
        snapshot_id="snapshot-old",
        research_wave_id="wave-old",
    )

    assert result["status"] == "blocked_stale_research_snapshot"
    assert result["generated"] == []
    assert result["production_change_forbidden"] is True


def test_environment_backtest_requires_explicit_candidate_binding(monkeypatch):
    monkeypatch.setattr(research, "ensure_strategy_research_tables", lambda: None)

    result = research.run_next_environment_backtest(
        replay_runner=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("replay must not run")
        )
    )

    assert result["status"] == "blocked_by_missing_candidate_id"


def test_environment_backtest_rejects_candidate_binding_mismatch(monkeypatch):
    monkeypatch.setattr(research, "ensure_strategy_research_tables", lambda: None)
    monkeypatch.setattr(
        research,
        "_candidate_event",
        lambda _candidate_id: {
            "event_id": "event-a",
            "status": "queued",
            "payload_json": json.dumps({"candidate_id": "candidate-b"}),
        },
    )

    with pytest.raises(ValueError, match="candidate binding mismatch"):
        research.run_next_environment_backtest(
            candidate_id="candidate-a",
            replay_runner=lambda **_kwargs: {},
        )


def test_failure_training_dispatch_uses_mapped_trainer(monkeypatch):
    rows = [{
        "memory_id": "memory-1",
        "model_training_request_json": '{"model_key":"event_impact_assessor_v2"}',
        "evidence_json": '{"research_snapshot_id":"snapshot-current"}',
    }]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, query, _params=()):
            if "SELECT * FROM strategy_failure_memories" in query:
                class Result:
                    def fetchall(self): return rows
                return Result()
            return None

    monkeypatch.setattr(research, "ensure_strategy_research_tables", lambda: None)
    monkeypatch.setattr(research, "_conn", lambda: Conn())
    from app.services import continuous_factor_research as factor_research

    monkeypatch.setattr(
        factor_research,
        "_active_research_snapshot",
        lambda: {"snapshot_id": "snapshot-current"},
    )
    called = []
    result = research.dispatch_targeted_model_training(
        plan_trainer=lambda model_key: called.append(model_key) or {"ok": True, "started": True},
        regime_trainer=lambda: {"ok": True},
    )
    assert called == ["event_impact_assessor_v2"]
    assert result["dispatched"][0]["status"] == "training_dispatched"


def test_failure_training_ignores_stale_research_snapshot(monkeypatch):
    rows = [{
        "memory_id": "memory-old",
        "model_training_request_json": '{"model_key":"market_regime_hsmm_v1"}',
        "evidence_json": '{"research_snapshot_id":"snapshot-old"}',
    }]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, query, _params=()):
            if "SELECT * FROM strategy_failure_memories" in query:
                class Result:
                    def fetchall(self): return rows
                return Result()
            return None

    monkeypatch.setattr(research, "ensure_strategy_research_tables", lambda: None)
    monkeypatch.setattr(research, "_conn", lambda: Conn())
    from app.services import continuous_factor_research as factor_research

    monkeypatch.setattr(
        factor_research,
        "_active_research_snapshot",
        lambda: {"snapshot_id": "snapshot-current"},
    )

    result = research.dispatch_targeted_model_training(
        plan_trainer=lambda _model_key: (_ for _ in ()).throw(
            AssertionError("stale model request must not run")
        ),
        regime_trainer=lambda: (_ for _ in ()).throw(
            AssertionError("stale HSMM request must not run")
        ),
    )

    assert result["dispatched"] == []
    assert result["stale_request_count"] == 1


def test_compact_replay_evidence_keeps_returns_and_holdings():
    compact = research._compact_replay_evidence({
        "equity_series": [
            {"date": "2026-01-01", "total_value": 100.0},
            {"date": "2026-01-02", "total_value": 102.0},
        ],
        "decisions": [{
            "signal_date": "2026-01-01",
            "target_weights": {"000001.SZ": 0.5, "000002.SZ": 0.0},
        }],
        "metrics": {"turnover_rate": 0.25},
        "blocked_events": [{"reason": "limit_up"}],
    })
    assert compact["daily_returns"] == {"2026-01-02": 0.02}
    assert compact["holding_sets"] == {"2026-01-01": ["000001.SZ"]}
    assert compact["turnover_rate"] == 0.25
    assert compact["blocked_trade_count"] == 1


def test_environment_fitness_separates_return_and_risk_only_environments():
    ranked = research.environment_fitness({
        "status": "completed",
        "metrics": {
            "excess_return": 0.03,
            "max_drawdown": -0.10,
            "turnover_rate": 0.40,
        },
    })
    risk_only = research.environment_fitness({
        "status": "completed",
        "method": "observed_capacity_contraction_v1",
        "blocked_trade_count": 2,
    })
    assert ranked["rankable"] is True
    assert ranked["fitness"] == 0.014
    assert ranked["absolute_pass_threshold"] is None
    assert risk_only["rankable"] is False
    assert risk_only["reason"] == "risk_only_environment"


def test_environment_expert_router_is_consumed_by_strategy_scoring(monkeypatch):
    environments = {
        "env-up": {"name": "trend:up"},
        "env-down": {"name": "trend:down"},
    }
    monkeypatch.setattr(research, "_load_environment", lambda key: environments.get(key))
    prototypes = [
        {
            "prototype_id": "expert-up",
            "cluster_id": "cluster-a",
            "factor_weights": {"momentum_20": 1.0},
            "evidence": {"representative_environment_results": [{
                "environment_id": "env-up",
                "environment_version": "market-v1",
                "status": "completed",
                "environment_fitness": {"rankable": True, "fitness": 0.08},
            }]},
        },
        {
            "prototype_id": "expert-down",
            "cluster_id": "cluster-b",
            "factor_weights": {"volatility_20": -1.0},
            "evidence": {"representative_environment_results": [{
                "environment_id": "env-down",
                "environment_version": "market-v1",
                "status": "completed",
                "environment_fitness": {"rankable": True, "fitness": 0.05},
            }]},
        },
    ]
    router = research.build_environment_expert_router(prototypes)
    content = {
        "score": {"factors": {"volume": 1.0}},
        "environment_expert_router": router,
    }
    assert router["state_experts"]["S0"]["prototype_id"] == "expert-up"
    assert router["state_experts"]["S2"]["prototype_id"] == "expert-down"
    assert get_strategy_score_for_market_state(content, "formula", "S0") == {
        "momentum_20": 1.0,
    }
    assert get_strategy_score_for_market_state(content, "formula", "S2") == {
        "volatility_20": -1.0,
    }
    assert get_strategy_expert_route(content, "S2")["prototype_id"] == "expert-down"
    assert get_strategy_factor_names(content, "formula") >= {
        "volume", "momentum_20", "volatility_20",
    }


def test_composite_router_replay_freezes_the_exact_materialization_content(monkeypatch):
    from app.services import continuous_factor_research

    monkeypatch.setattr(
        continuous_factor_research,
        "_baseline_research_spec",
        lambda _snapshot: {
            "factor_bindings": {},
            "model_bindings": {},
            "market_state_binding": {},
            "strategy_controller_version_id": 7,
        },
    )
    monkeypatch.setattr(
        continuous_factor_research,
        "_strategy_content",
        lambda weights, _spec: {
            "score": {"normalization": "rank_percentile", "factors": weights},
            "rebalance": {"freq": "daily", "top_n": 20},
        },
    )
    prototypes = [
        {
            "prototype_id": "expert-a",
            "cluster_id": "cluster-a",
            "factor_weights": {"momentum_20": 1.0},
        },
        {
            "prototype_id": "expert-b",
            "cluster_id": "cluster-b",
            "factor_weights": {"volatility_20": -1.0},
        },
    ]
    router = {
        "schema_version": "alis_environment_expert_router_v1",
        "environment_versions": ["market-v1"],
        "experts": {
            "expert-a": {
                "prototype_id": "expert-a",
                "factor_weights": {"momentum_20": 1.0},
            },
            "expert-b": {
                "prototype_id": "expert-b",
                "factor_weights": {"volatility_20": -1.0},
            },
        },
        "state_experts": {
            "S0": {"prototype_id": "expert-a"},
            "S2": {"prototype_id": "expert-b"},
        },
        "fallback_prototype_id": "expert-a",
    }
    observed_content = []

    def replay_runner(**kwargs):
        observed_content.append(kwargs["strategy_content"])
        return {
            "metrics": {"total_return": 0.01, "benchmark_return": 0.0, "excess_return": 0.01},
            "data_quality": {"status": "passed"},
            "equity_series": [
                {"date": "2026-01-01", "total_value": 100.0},
                {"date": "2026-01-02", "total_value": 101.0},
            ],
            "decisions": [],
            "blocked_events": [],
        }

    result = research._run_composite_router_backtest(
        snapshot={
            "snapshot_id": "snapshot-v10",
            "research_windows": [{"start": "2026-01-01", "end": "2026-01-31"}],
        },
        prototypes=prototypes,
        router=router,
        replay_runner=replay_runner,
    )
    assert result["status"] == "completed"
    assert result["evaluated_object_equals_materialized_object"] is True
    assert observed_content == [result["strategy_content"]]
    assert result["strategy_content"]["environment_expert_router"] == router
    assert result["strategy_content"]["regime_scores"]["S2"]["prototype_id"] == "expert-b"

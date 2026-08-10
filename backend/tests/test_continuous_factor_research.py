import json

from app.services import continuous_factor_research as research


def _factors():
    return [
        {
            "id": index,
            "factor_key": key,
            "version": 1,
            "status": "candidate",
            "category": category,
            "direction": direction,
            "formula": {"op": "field", "field": key},
        }
        for index, (key, category, direction) in enumerate(
            (
                ("momentum_20", "趋势", 1),
                ("volatility_20", "波动", -1),
                ("pb", "估值", -1),
                ("turnover", "流动性", 1),
                ("volume_ratio_5", "量价", 1),
                ("beta_60", "风险", -1),
            ),
            start=1,
        )
    ]


def test_proposals_mutate_the_best_completed_population(monkeypatch):
    snapshot = {"snapshot_id": "frozen-1", "factors": _factors()}
    monkeypatch.setattr(
        research,
        "_experiment_rows",
        lambda _snapshot_id: [
            {
                "experiment_id": "parent-best",
                "generation": 0,
                "status": "completed",
                "strategy_hash": "old",
                "fitness": 0.2,
                "factor_weights": {
                    "momentum_20": 0.4,
                    "volatility_20": -0.3,
                    "pb": -0.3,
                },
            }
        ],
    )
    monkeypatch.setattr(research, "_cluster_representative_rows", lambda _snapshot_id: [])

    proposal = research.propose_next_combinations(snapshot, count=1)[0]

    assert proposal["parent_experiment_id"] == "parent-best"
    assert proposal["mutation_type"] != "diverse_seed"
    assert len(proposal["factor_weights"]) >= 3
    assert abs(sum(abs(value) for value in proposal["factor_weights"].values()) - 1.0) < 1e-8


def test_strategy_search_produces_distinct_factor_combinations(monkeypatch):
    snapshot = {"snapshot_id": "frozen-distinct", "factors": _factors()}
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: [])

    proposals = research.propose_next_combinations(snapshot, count=3)

    assert len(proposals) == 3
    assert len({proposal["strategy_hash"] for proposal in proposals}) == 3
    assert len({tuple(proposal["factor_weights"].items()) for proposal in proposals}) == 3
    assert all(len(proposal["factor_weights"]) >= 3 for proposal in proposals)


def test_frozen_factor_bindings_include_fixed_strategy_constraints(monkeypatch):
    factors = _factors()
    monkeypatch.setattr(
        research,
        "registered_factor_bindings",
        lambda keys: {
            key: {
                "definition_id": 100 + index,
                "factor_key": key,
                "version": "v1",
                "status": "active",
                "formula": {"expression": key},
                "available_time_rule": "T_close_available_by_T_15_00",
                "leakage_status": "passed",
            }
            for index, key in enumerate(sorted(keys))
        },
    )
    selection_strategy = {
        "type": "formula",
        "content": {
            "score": {"factors": {factors[0]["factor_key"]: 1.0}},
            "filters": [
                {"factor": "is_st", "op": "==", "value": 0},
                {"factor": "is_suspended", "op": "==", "value": 0},
            ],
        },
    }

    bindings = research._frozen_factor_bindings(factors, selection_strategy)

    assert "is_st" in bindings
    assert "is_suspended" in bindings
    assert set(item["factor_key"] for item in factors).issubset(bindings)
    assert "is_st" not in {item["factor_key"] for item in factors}


def test_model_candidate_pool_excludes_archived_failed_and_legacy_versions(monkeypatch):
    model_key = research.MODEL_KEYS[0]
    versions = [
        {
            "id": 10,
            "model_key": model_key,
            "version": "current-candidate",
            "status": "candidate",
            "artifact_path": "/models/current.json",
            "metrics": {
                "model_evaluation_contract": research.MODEL_EVALUATION_CONTRACT,
                "task_native_oos": {
                    "passed": True,
                    "baseline_outperformed": True,
                },
            },
            "validation": {"status": "pending_live_observation"},
        },
        {
            "id": 11,
            "model_key": model_key,
            "version": "archived",
            "status": "evaluation_archived",
            "artifact_path": "/models/archived.json",
        },
        {
            "id": 12,
            "model_key": model_key,
            "version": "failed",
            "status": "candidate",
            "artifact_path": "/models/failed.json",
            "metrics": {
                "model_evaluation_contract": research.MODEL_EVALUATION_CONTRACT,
                "task_native_oos": {
                    "passed": False,
                    "baseline_outperformed": False,
                },
            },
            "validation": {
                "status": "failed",
                "review": {"status": "rejected"},
            },
        },
        {
            "id": 13,
            "model_key": model_key,
            "version": "legacy-contract",
            "status": "candidate",
            "artifact_path": "/models/legacy.json",
            "metrics": {
                "model_evaluation_contract": "legacy_model_evaluation_v1",
                "task_native_oos": {
                    "passed": True,
                    "baseline_outperformed": True,
                },
            },
            "validation": {"status": "pending_live_observation"},
        },
    ]
    monkeypatch.setattr(
        research,
        "list_model_versions",
        lambda key, limit=10: versions if key == model_key else [],
    )
    monkeypatch.setattr(research, "model_artifact_exists", lambda _path: True)

    candidates = research._model_candidate_pool(
        {key: None for key in research.MODEL_KEYS}
    )

    assert [item["id"] for item in candidates] == [10]


def test_model_action_summary_uses_causal_attribution_contract():
    replay = {
        "decisions": [{
            "strategy_action_contracts": [
                {
                    "action": "buy_candidate",
                    "trigger_models": [
                        "medium_horizon_lambdarank_v1",
                        "portfolio_risk_engine_v1",
                    ],
                },
                {
                    "action": "sell_exit",
                    "trigger_models": ["portfolio_risk_engine_v1"],
                },
            ],
        }],
    }

    summary = research._model_action_summary(replay)

    assert summary["schema_version"] == "alis_research_model_action_contribution_v2"
    assert summary["action_count"] == 2
    assert summary["by_model"] == {
        "medium_horizon_lambdarank_v1": {"buy_candidate": 1},
        "portfolio_risk_engine_v1": {
            "buy_candidate": 1,
            "sell_exit": 1,
        },
    }


def test_research_generates_factor_model_and_strategy_control_lanes(monkeypatch):
    factor_bindings = {
        item["factor_key"]: {
            "definition_id": item["id"],
            "factor_key": item["factor_key"],
            "version": str(item["version"]),
            "formula": item["formula"],
            "leakage_status": "passed",
        }
        for item in _factors()
    }
    baseline_models = {
        key: None for key in research.MODEL_KEYS
    }
    snapshot = {
        "snapshot_id": "frozen-lanes",
        "factors": _factors(),
        "contract": {
            "research_package": {
                "factor_bindings": factor_bindings,
                "model_bindings": baseline_models,
                "market_state_binding": {
                    "production_model": "transparent_four_state_v1",
                    "change_model": "bocpd_gaussian_v1",
                },
                "strategy_controller": {"id": 31, "version": "controller-v1"},
                "portfolio_optimizer": {"implementation": "empirical_cvar_slsqp_v2"},
                "execution_contract": {"signal": "T_close", "execution": "T_plus_1_open"},
            },
            "model_candidate_pool": [{
                "id": 21,
                "model_key": "medium_horizon_lambdarank_v1",
                "version": "medium-candidate-v2",
            }],
            "controller_candidate_pool": [],
            "market_state_candidate_pool": [{
                "production_model": "market_regime_hsmm_v1",
                "change_model": "bocpd_gaussian_v1",
                "hsmm_shadow": {"id": 41, "model_key": "market_regime", "version": "hsmm-v2"},
            }],
            "portfolio_candidate_pool": ["equal_weight", "hrp"],
        },
    }
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: [])

    proposals = research.propose_next_combinations(snapshot, count=9)

    by_lane = {
        item["research_spec"]["optimization_lane"]: item
        for item in proposals
        if item["research_spec"]["optimization_lane"] != "strategy_control"
    }
    assert {item["research_spec"]["optimization_lane"] for item in proposals} == {
        "factor", "model", "strategy_control",
    }
    assert (
        by_lane["model"]["research_spec"]["model_bindings"]["medium_horizon_lambdarank_v1"]["id"]
        == 21
    )
    strategy_proposals = [
        item for item in proposals
        if item["research_spec"]["optimization_lane"] == "strategy_control"
    ]
    assert any(item["research_spec"]["portfolio_method"] == "equal_weight" for item in strategy_proposals)
    assert any(item["research_spec"]["portfolio_method"] == "hrp" for item in strategy_proposals)
    assert any(
        item["research_spec"]["market_state_binding"]["production_model"]
        == "market_regime_hsmm_v1"
        for item in strategy_proposals
    )
    assert all(item["research_spec"]["factor_bindings"] == factor_bindings for item in proposals)
    assert len({item["research_spec"]["research_package_hash"] for item in proposals}) == 9


def test_continuous_lane_balance_executes_all_model_families(monkeypatch):
    model_candidates = [
        {
            "id": 100 + index,
            "model_key": model_key,
            "version": f"{model_key}-candidate",
        }
        for index, model_key in enumerate(research.MODEL_KEYS)
    ]
    factor_bindings = {
        item["factor_key"]: {
            "definition_id": item["id"],
            "factor_key": item["factor_key"],
            "version": str(item["version"]),
            "formula": item["formula"],
            "leakage_status": "passed",
        }
        for item in _factors()
    }
    snapshot = {
        "snapshot_id": "frozen-balanced-lanes",
        "factors": _factors(),
        "contract": {
            "research_package": {
                "factor_bindings": factor_bindings,
                "model_bindings": {key: None for key in research.MODEL_KEYS},
                "market_state_binding": {
                    "production_model": "transparent_four_state_v1",
                },
                "strategy_controller": {"id": 31, "version": "controller-v1"},
            },
            "model_candidate_pool": model_candidates,
            "controller_candidate_pool": [{"id": 32, "version": "controller-v2"}],
            "portfolio_candidate_pool": ["equal_weight", "hrp"],
        },
    }
    rows = []
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: rows)
    monkeypatch.setattr(
        research,
        "_cluster_representative_rows",
        lambda _snapshot_id: [
            item for item in rows
            if item["research_spec"]["optimization_lane"] == "factor"
        ],
    )

    for _index in range(research.QUICK_SCREEN_TARGET):
        proposal = research.propose_next_combinations(snapshot, count=1)[0]
        rows.append({
            **proposal,
            "experiment_id": f"experiment-{len(rows)}",
            "status": "completed",
            "fitness": 0.0,
            "metrics": {
                "research_spec": proposal["research_spec"],
                "aggregate": {
                    "research_disposition": {"retain": True},
                },
            },
        })

    lane_counts = {
        lane: sum(
            item["research_spec"]["optimization_lane"] == lane
            for item in rows
        )
        for lane in ("factor", "model", "strategy_control")
    }
    replaced_models = {
        item["mutation_type"].split(":", 1)[1]
        for item in rows
        if item["mutation_type"].startswith("replace_model:")
    }

    assert lane_counts == {"factor": 8, "model": 8, "strategy_control": 8}
    assert replaced_models == set(research.MODEL_KEYS)
    factor_rows = [
        item for item in rows
        if item["research_spec"]["optimization_lane"] == "factor"
    ]
    exploration_rows = [
        item for item in factor_rows
        if item["research_spec"]["factor_search_mode"].startswith(
            "deterministic_random_exploration"
        )
    ]
    exploitation_rows = [item for item in factor_rows if item not in exploration_rows]

    assert len(exploration_rows) == 4
    assert len(exploitation_rows) == 4
    assert len({item["research_spec"]["factor_set_hash"] for item in exploration_rows}) == 4
    assert any(item["mutation_type"] == "cross_cluster_recombine" for item in exploitation_rows)
    for index, left in enumerate(exploration_rows):
        for right in exploration_rows[index + 1:]:
            assert research._factor_set_similarity(
                left["factor_weights"], right["factor_weights"]
            ) <= research.EXPLORATION_FACTOR_SET_SIMILARITY_LIMIT


def test_deterministic_factor_exploration_is_replayable():
    left, left_seed = research._deterministic_exploration_weights(
        snapshot_id="snapshot-replayable",
        factors=_factors(),
        exploration_index=2,
        attempt=7,
    )
    right, right_seed = research._deterministic_exploration_weights(
        snapshot_id="snapshot-replayable",
        factors=_factors(),
        exploration_index=2,
        attempt=7,
    )

    assert left == right
    assert left_seed == right_seed
    assert research.MIN_FACTOR_COMBINATION_SIZE <= len(left) <= research.MAX_FACTOR_COMBINATION_SIZE


def test_research_clusters_use_factor_and_behavior_evidence(monkeypatch):
    def row(experiment_id, weights, excess, drawdown, turnover, fitness):
        return {
            "experiment_id": experiment_id,
            "strategy_hash": f"hash-{experiment_id}",
            "research_stage": "quick_screen",
            "status": "completed",
            "fitness": fitness,
            "factor_weights": weights,
            "metrics": {
                "folds": [
                    {"excess_return": excess, "turnover_rate": turnover},
                    {"excess_return": excess * 0.9, "turnover_rate": turnover},
                ],
                "aggregate": {
                    "mean_excess_return": excess,
                    "worst_drawdown": drawdown,
                    "market_states": {
                        state: {"mean_excess_return": excess}
                        for state in ("S0", "S1", "S2", "S3")
                    },
                    "research_disposition": {"retain": True},
                },
            },
        }

    rows = [
        row("a", {"f1": 0.5, "f2": 0.3, "f3": 0.2}, 0.03, -0.08, 0.4, 0.10),
        row("b", {"f1": 0.45, "f2": 0.35, "f3": 0.2}, 0.025, -0.09, 0.45, 0.08),
        row("c", {"f4": 0.4, "f5": 0.3, "f6": 0.3}, -0.12, -0.35, 1.8, -0.20),
    ]
    writes = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            writes.append((sql, params))
            return self

    monkeypatch.setattr(research, "ensure_continuous_research_tables", lambda: None)
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: rows)
    monkeypatch.setattr(research, "_conn", lambda: Connection())

    clusters = research.refresh_research_clusters("snapshot-clusters")

    assert len(clusters) == 2
    assert sorted(item["member_count"] for item in clusters) == [1, 2]
    two_member = next(item for item in clusters if item["member_count"] == 2)
    assert set(two_member["evidence"]["member_experiment_ids"]) == {"a", "b"}
    assert two_member["factor_centroid"]["intersection"] == ["f1", "f2", "f3"]
    assert set(two_member["factor_centroid"]["distilled_weights"]) == {"f1", "f2", "f3"}
    assert (
        two_member["evidence"]["distillation_method"]
        == "fitness_softmax_weighted_factor_centroid_v1"
    )


def test_confirmation_prefers_distinct_cluster_representatives(monkeypatch):
    quick = [
        {
            "experiment_id": experiment_id,
            "research_stage": "quick_screen",
            "status": "completed",
            "fitness": fitness,
            "generation": index,
            "factor_weights": {"a": 0.4, "b": 0.3, "c": 0.3},
            "metrics": {"aggregate": {"research_disposition": {"retain": True}}},
            "research_spec": {},
        }
        for index, (experiment_id, fitness) in enumerate(
            (("fitness-first", 0.3), ("cluster-first", 0.2), ("cluster-second", 0.1))
        )
    ]
    rows = list(quick)
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: rows)
    monkeypatch.setattr(research, "refresh_research_clusters", lambda _snapshot_id: [
        {"representative_experiment_id": "cluster-first"},
        {"representative_experiment_id": "cluster-second"},
    ])
    monkeypatch.setattr(research, "_confirmation_windows", lambda _data_as_of: [])
    snapshot = {
        "snapshot_id": "snapshot-confirm-clusters",
        "data_as_of": "2026-07-28",
        "contract": {},
    }

    first = research.propose_confirmation_candidate(snapshot)
    rows.append({
        "experiment_id": "confirmation-one",
        "research_stage": "confirmation",
        "status": "completed",
        "parent_experiment_id": first["parent_experiment_id"],
    })
    second = research.propose_confirmation_candidate(snapshot)

    assert first["parent_experiment_id"] == "cluster-first"
    assert second["parent_experiment_id"] == "cluster-second"


def test_market_state_candidate_pool_requires_passed_validation_and_artifact(monkeypatch, tmp_path):
    artifact = tmp_path / "hsmm.json"
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(research, "list_model_versions", lambda model_key, limit: [
        {
            "id": 41,
            "model_key": "market_regime",
            "version": "hsmm-passed",
            "status": "shadow_validated",
            "artifact_path": str(artifact),
            "created_at": "2026-07-20T10:00:00+08:00",
            "validation": {"status": "passed"},
        },
        {
            "id": 40,
            "model_key": "market_regime",
            "version": "hsmm-failed",
            "status": "shadow_failed",
            "artifact_path": str(artifact),
            "validation": {"status": "failed"},
        },
        {
            "id": 39,
            "model_key": "market_regime",
            "version": "hsmm-missing",
            "status": "shadow_validated",
            "artifact_path": str(tmp_path / "missing.json"),
            "validation": {"status": "passed"},
        },
    ])

    candidates = research._market_state_candidate_pool()

    assert len(candidates) == 1
    assert candidates[0]["production_model"] == "market_regime_hsmm_v1"
    assert candidates[0]["hsmm_shadow"]["id"] == 41
    assert candidates[0]["hsmm_shadow"]["validation_status"] == "passed"


def test_research_package_hash_ignores_lineage_but_not_behavior():
    snapshot = {"snapshot_id": "snapshot-hash"}
    base = {
        "factor_bindings": {},
        "model_bindings": {},
        "market_state_binding": {"production_model": "transparent_four_state_v1"},
        "strategy_controller_version_id": 31,
        "portfolio_method": "cvar",
        "optimization_lane": "factor",
        "source_experiment_id": "parent-a",
    }
    left_hash, _left = research._proposal_identity(
        snapshot,
        weights={"a": 0.4, "b": 0.3, "c": 0.3},
        research_spec=base,
        research_stage="quick_screen",
    )
    right_hash, _right = research._proposal_identity(
        snapshot,
        weights={"a": 0.4, "b": 0.3, "c": 0.3},
        research_spec={
            **base,
            "optimization_lane": "model",
            "source_experiment_id": "parent-b",
            "research_package_hash": "old-parent-hash",
        },
        research_stage="quick_screen",
    )
    changed_hash, _changed = research._proposal_identity(
        snapshot,
        weights={"a": 0.5, "b": 0.25, "c": 0.25},
        research_spec=base,
        research_stage="quick_screen",
    )

    assert left_hash == right_hash
    assert changed_hash != left_hash


def test_population_does_not_collapse_distinct_model_bundles_with_same_factors():
    base = {
        "factor_weights": {"a": 0.4, "b": 0.3, "c": 0.3},
        "research_spec": {
            "model_bindings": {"medium_horizon_lambdarank_v1": {"id": 1}},
            "market_state_binding": {"production_model": "transparent_four_state_v1"},
            "strategy_controller_version_id": 31,
            "portfolio_method": "cvar",
        },
    }
    changed_model = {
        **base,
        "research_spec": {
            **base["research_spec"],
            "model_bindings": {"medium_horizon_lambdarank_v1": {"id": 2}},
        },
    }

    assert research._candidate_similarity(base, base) == 1.0
    assert research._candidate_similarity(base, changed_model) == 0.0


def test_weak_first_generation_is_incubated_instead_of_rejected():
    disposition = research._research_disposition(
        pit_passed=True,
        aggregate={
            "mean_total_return": -0.08,
            "mean_excess_return": -0.05,
            "worst_drawdown": -0.15,
        },
        market_states={
            "S0": {"observations": 10, "mean_excess_return": -0.03, "worst_drawdown": -0.12},
            "S1": {"observations": 8, "mean_excess_return": -0.04, "worst_drawdown": -0.10},
        },
    )

    assert disposition == {
        "status": "incubating",
        "retain": True,
        "reason": "weak_first_generation_kept_for_learning",
    }


def test_same_evidence_rolls_over_only_within_epoch_budget():
    population = [{"experiment_id": "survivor"}]

    assert research._next_epoch_allowed({
        "contract": {"epoch_no": 2, "maximum_epochs_per_evidence": 3},
    }, population) is True
    assert research._next_epoch_allowed({
        "contract": {"epoch_no": 3, "maximum_epochs_per_evidence": 3},
    }, population) is False


def test_research_contract_version_changes_evidence_signature(monkeypatch):
    identity = {"data_as_of": "2026-07-27", "factor_universe_hash": "factors"}
    current = research._research_evidence_signature(identity)

    monkeypatch.setattr(
        research,
        "RESEARCH_SCHEMA_VERSION",
        "alis_continuous_factor_model_strategy_research_v_next",
    )

    assert research._research_evidence_signature(identity) != current


def test_new_contract_supersedes_incompatible_running_experiments(monkeypatch):
    updates = []

    class Result:
        def fetchall(self):
            return [
                {
                    "experiment_id": "old-running",
                    "contract_json": '{"schema_version":"old-contract"}',
                },
                {
                    "experiment_id": "current-running",
                    "contract_json": (
                        '{"schema_version":"'
                        + research.RESEARCH_SCHEMA_VERSION
                        + '"}'
                    ),
                },
            ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT"):
                return Result()
            updates.append((sql, params))
            return Result()

    monkeypatch.setattr(research, "_conn", lambda: Connection())

    superseded = research._supersede_incompatible_running_experiments()

    assert superseded == ["old-running"]
    assert len(updates) == 1
    assert updates[0][1][0] == "superseded_by_research_contract_v9"
    assert updates[0][1][3] == "old-running"


def test_running_experiment_is_resumed_before_new_proposal(monkeypatch):
    running = {
        "experiment_id": "running-1",
        "strategy_hash": "hash-running-1",
        "status": "running",
        "generation": 3,
        "parent_experiment_id": "parent-1",
        "mutation_type": "replace_factor",
        "research_stage": "quick_screen",
        "factor_weights": {"a": 0.4, "b": 0.3, "c": 0.3},
        "windows": [{"index": 1, "start": "2026-01-01", "end": "2026-01-02"}],
        "research_spec": {"research_package_hash": "package-1"},
        "created_at": "2026-07-27T10:00:00+08:00",
    }
    rows = [running]
    monkeypatch.setattr(research, "_active_research_snapshot", lambda: {
        "snapshot_id": "snapshot-resume",
        "research_windows": running["windows"],
        "contract": {"epoch_no": 1, "maximum_epochs_per_evidence": 3},
        "factors": _factors(),
    })
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: rows)
    monkeypatch.setattr(
        research,
        "propose_next_combinations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must resume the interrupted experiment")
        ),
    )
    monkeypatch.setattr(research, "refresh_research_population", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(research, "refresh_research_clusters", lambda *_args, **_kwargs: [])
    captured = {}

    def resume(_snapshot, proposal, **_kwargs):
        captured.update(proposal)
        rows[0] = {**running, "status": "completed", "fitness": 0.01}
        return rows[0]

    monkeypatch.setattr(research, "run_combination_experiment", resume)

    result = research.run_continuous_factor_research_cycle()

    assert captured["strategy_hash"] == "hash-running-1"
    assert captured["research_spec"] == {"research_package_hash": "package-1"}
    assert result["quick_screen_completed"] == 1


def test_research_trial_count_includes_current_row_once(monkeypatch):
    monkeypatch.setattr(
        research,
        "_experiment_rows",
        lambda _snapshot_id: [{"experiment_id": "current"}],
    )

    assert research._research_trial_count("snapshot-1") == 1


def test_evidence_event_refreshes_snapshot_before_reusing_active_epoch(monkeypatch):
    captured = {}
    monkeypatch.setattr(research, "ensure_continuous_research_tables", lambda: None)
    monkeypatch.setattr(
        research,
        "freeze_research_snapshot",
        lambda data_source=None, **kwargs: captured.update(kwargs) or {"snapshot_id": "new-evidence"},
    )

    snapshot = research._active_research_snapshot(refresh_evidence=True)

    assert snapshot["snapshot_id"] == "new-evidence"
    assert captured["require_new_evidence"] is True


def test_active_snapshot_is_replaced_when_production_model_contract_changed(monkeypatch):
    stored_contract = {
        "schema_version": research.RESEARCH_SCHEMA_VERSION,
        "factor_universe": _factors(),
        "research_package": {
            "model_bindings": {
                key: ({"id": 16} if key == "medium_horizon_lambdarank_v1" else None)
                for key in research.MODEL_KEYS
            },
        },
    }
    row = {
        "snapshot_id": "snapshot-with-retired-model",
        "data_as_of": "2026-07-27",
        "factor_universe_hash": "factors",
        "factor_count": len(_factors()),
        "research_windows_json": json.dumps([
            {"index": 1, "sessions": research.RESEARCH_FOLD_SESSIONS},
            {"index": 2, "sessions": research.RESEARCH_FOLD_SESSIONS},
        ]),
        "contract_json": json.dumps(stored_contract),
    }

    class Result:
        def fetchone(self):
            return row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=()):
            return Result()

    monkeypatch.setattr(research, "ensure_continuous_research_tables", lambda: None)
    monkeypatch.setattr(research, "_conn", lambda: Connection())
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: [])
    monkeypatch.setattr(
        research,
        "_research_model_bundle",
        lambda _champion: {key: None for key in research.MODEL_KEYS},
    )
    monkeypatch.setattr(
        research,
        "resolve_production_strategy_champion",
        lambda: {"model_bindings": {}},
    )
    monkeypatch.setattr(
        research,
        "freeze_research_snapshot",
        lambda data_source=None: {"snapshot_id": "snapshot-current-contract"},
    )

    snapshot = research._active_research_snapshot()

    assert snapshot["snapshot_id"] == "snapshot-current-contract"


def test_research_model_bundle_ignores_champion_historical_bindings(monkeypatch):
    calls = []
    current = {key: None for key in research.MODEL_KEYS}
    monkeypatch.setattr(
        research,
        "freeze_model_bindings",
        lambda bindings=None: calls.append(bindings) or current,
    )
    historical = {
        key: ({"id": 16} if index == 0 else None)
        for index, key in enumerate(research.MODEL_KEYS)
    }

    result = research._research_model_bundle({"model_bindings": historical})

    assert result == current
    assert calls == [None]


def test_multiple_testing_penalty_increases_with_trials():
    folds = [
        {
            "excess_return": 0.03,
            "total_return": 0.05,
            "max_drawdown": -0.08,
            "total_fees": 100.0,
        },
        {
            "excess_return": 0.02,
            "total_return": 0.04,
            "max_drawdown": -0.06,
            "total_fees": 100.0,
        },
    ]

    early_score, early = research._fitness(folds, trial_count=1)
    late_score, late = research._fitness(folds, trial_count=100)

    assert late["multiple_testing_penalty"] > early["multiple_testing_penalty"]
    assert late_score < early_score


def test_research_strategy_is_explicitly_non_production(monkeypatch):
    monkeypatch.setattr(
        research,
        "resolve_production_strategy_champion",
        lambda: {"strategy": {"content": {"lookback_days": 20, "rebalance": {}}}},
    )

    content = research._strategy_content(
        {"momentum_20": 0.6, "volatility_20": -0.4}
    )

    assert content["research_only"] is True
    assert content["lookback_days"] == 120
    assert content["score"]["factors"]["momentum_20"] == 0.6


def test_research_strategy_uses_frozen_selection_content(monkeypatch):
    monkeypatch.setattr(
        research,
        "resolve_production_strategy_champion",
        lambda: (_ for _ in ()).throw(
            AssertionError("frozen research must not reread the current Champion")
        ),
    )

    content = research._strategy_content(
        {"momentum_20": 0.6, "volatility_20": -0.4},
        {
            "selection_strategy": {
                "id": 7,
                "type": "formula",
                "content": {
                    "lookback_days": 180,
                    "filters": [{"factor": "pb", "op": ">", "value": 0}],
                    "rebalance": {"freq": "monthly", "top_n": 12},
                },
            },
            "portfolio_method": "hrp",
        },
    )

    assert content["filters"] == [{"factor": "pb", "op": ">", "value": 0}]
    assert content["lookback_days"] == 180
    assert content["portfolio_method"] == "hrp"
    assert content["rebalance"] == {"freq": "weekly", "top_n": 30}


def test_population_keeps_distinct_risk_and_diversity_elites():
    def row(experiment_id, fitness, drawdown, instability, weights, stage="quick_screen"):
        return {
            "experiment_id": experiment_id,
            "fitness": fitness,
            "factor_weights": weights,
            "research_stage": stage,
            "metrics": {"aggregate": {
                "worst_drawdown": drawdown,
                "excess_instability": instability,
            }},
        }

    selected = research._diverse_population([
        row("fitness", 0.10, -0.20, 0.10, {"a": 0.5, "b": 0.5}),
        row("drawdown", 0.05, -0.03, 0.08, {"a": 0.48, "b": 0.52}),
        row("stable", 0.04, -0.08, 0.01, {"c": 0.5, "d": 0.5}),
        row("confirmed", 0.03, -0.06, 0.03, {"e": 0.5, "f": 0.5}, "confirmation"),
    ], limit=4)

    roles = {item["experiment_id"]: role for item, role in selected}
    assert roles["confirmed"] == "confirmed_elite"
    assert roles["fitness"] == "fitness_elite"
    assert roles["drawdown"] == "drawdown_elite"
    assert roles["stable"] == "stability_elite"


def test_epoch_requires_quick_screen_and_confirmation():
    quick = [
        {"research_stage": "quick_screen", "status": "completed"}
        for _ in range(research.QUICK_SCREEN_TARGET)
    ]
    confirmations = [
        {"research_stage": "confirmation", "status": "completed"}
        for _ in range(research.CONFIRMATION_ELITE_COUNT)
    ]

    assert research._research_epoch_complete(quick) is False
    assert research._research_epoch_complete(quick + confirmations) is True
    assert research.MAX_GENERATIONS_PER_SNAPSHOT >= research.QUICK_SCREEN_TARGET - 1


def test_epoch_is_not_terminal_while_confirmation_is_running():
    quick = [
        {
            "experiment_id": f"quick-{index}",
            "research_stage": "quick_screen",
            "status": "completed",
            "metrics": {"aggregate": {"research_disposition": {"retain": True}}},
        }
        for index in range(research.QUICK_SCREEN_TARGET)
    ]
    confirmations = [
        {
            "experiment_id": f"confirm-{index}",
            "parent_experiment_id": f"quick-{index}",
            "research_stage": "confirmation",
            "status": "completed" if index < 2 else "running",
        }
        for index in range(research.CONFIRMATION_ELITE_COUNT)
    ]

    assert research._research_epoch_complete(quick + confirmations) is False
    assert research._research_epoch_terminal(quick + confirmations) is False


def test_completed_epoch_stops_continuation_and_waits_for_new_data(monkeypatch):
    completed = [
        {"research_stage": "quick_screen", "status": "completed", "generation": index}
        for index in range(research.QUICK_SCREEN_TARGET)
    ] + [
        {"research_stage": "confirmation", "status": "completed", "generation": index}
        for index in range(research.CONFIRMATION_ELITE_COUNT)
    ]
    monkeypatch.setattr(research, "_active_research_snapshot", lambda: {
        "snapshot_id": "snapshot-1", "factors": _factors(), "research_windows": [],
    })
    monkeypatch.setattr(research, "_experiment_rows", lambda _snapshot_id: completed)
    monkeypatch.setattr(research, "refresh_research_population", lambda _snapshot_id: [])
    monkeypatch.setattr(research, "refresh_research_clusters", lambda _snapshot_id: [])
    monkeypatch.setattr(
        "app.services.factor_strategy_evolution.distill_confirmed_research_experiments",
        lambda **_kwargs: {"ok": True},
    )

    result = research.run_continuous_factor_research_cycle(replay_runner=lambda **_kwargs: {})

    assert result["status"] == "awaiting_new_data"
    assert result["continue_research"] is False
    assert result["rollover_epoch"] is False


def test_final_confirmation_completes_epoch_distills_and_stops(monkeypatch):
    quick = [
        {
            "experiment_id": f"quick-{index}",
            "research_stage": "quick_screen",
            "status": "completed",
            "generation": index,
            "strategy_hash": f"quick-hash-{index}",
            "fitness": 0.01 + index / 1000,
            "factor_weights": {"momentum_20": 0.4, "pb": -0.3, "turnover": 0.3},
            "metrics": {"aggregate": {"excess_return": 0.01}},
            "mutation_type": "diverse_seed",
        }
        for index in range(research.QUICK_SCREEN_TARGET)
    ]
    confirmation_existing = [
        {
            "experiment_id": f"confirm-{index}",
            "research_stage": "confirmation",
            "status": "completed",
            "generation": index,
            "strategy_hash": f"confirm-hash-{index}",
            "fitness": 0.05 + index / 100,
            "factor_weights": {"momentum_20": 0.5, "pb": -0.25, "turnover": 0.25},
            "metrics": {"aggregate": {"excess_return": 0.03}},
            "mutation_type": "confirmation_replay",
        }
        for index in range(research.CONFIRMATION_ELITE_COUNT - 1)
    ]
    final_confirmation = {
        "experiment_id": "confirm-final",
        "research_stage": "confirmation",
        "status": "completed",
        "generation": research.CONFIRMATION_ELITE_COUNT,
        "strategy_hash": "confirm-hash-final",
        "fitness": 0.09,
        "factor_weights": {"momentum_20": 0.55, "pb": -0.25, "turnover": 0.20},
        "metrics": {"aggregate": {"excess_return": 0.04}},
        "mutation_type": "confirmation_replay",
    }
    before_final = quick + confirmation_existing
    after_final = before_final + [final_confirmation]
    calls = {"distill": 0}

    monkeypatch.setattr(research, "_active_research_snapshot", lambda: {
        "snapshot_id": "snapshot-final",
        "factors": _factors(),
        "research_windows": [],
        "data_as_of": "20260727",
    })
    monkeypatch.setattr(research, "refresh_research_population", lambda _snapshot_id: [])
    monkeypatch.setattr(research, "refresh_research_clusters", lambda _snapshot_id: [])
    monkeypatch.setattr(research, "propose_confirmation_candidate", lambda _snapshot: {
        "strategy_hash": final_confirmation["strategy_hash"],
        "factor_weights": final_confirmation["factor_weights"],
        "research_stage": "confirmation",
        "mutation_type": "confirmation_replay",
    })
    monkeypatch.setattr(
        research,
        "run_combination_experiment",
        lambda *_args, **_kwargs: final_confirmation,
    )

    def fake_distill(**kwargs):
        calls["distill"] += 1
        return {
            "ok": True,
            "distillation": {
                "generated": True,
                "source_snapshot_id": kwargs["snapshot_id"],
            },
        }

    monkeypatch.setattr(
        "app.services.factor_strategy_evolution.distill_confirmed_research_experiments",
        fake_distill,
    )
    rows_by_call = iter([before_final, after_final, after_final])
    monkeypatch.setattr(
        research,
        "_experiment_rows",
        lambda _snapshot_id: next(rows_by_call),
    )

    result = research.run_continuous_factor_research_cycle(
        replay_runner=lambda **_kwargs: {}
    )

    assert result["status"] == "completed"
    assert result["quick_screen_completed"] == research.QUICK_SCREEN_TARGET
    assert result["confirmation_completed"] == research.CONFIRMATION_ELITE_COUNT
    assert result["continue_research"] is False
    assert result["rollover_epoch"] is False
    assert result["distillation"]["distillation"]["generated"] is True
    assert calls["distill"] == 1

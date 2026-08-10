from contextlib import contextmanager
import json

from app.services import factor_strategy_evolution as evolution


class _Cursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_combination_waits_until_factors_have_mature_evidence(monkeypatch):
    monkeypatch.setattr(evolution, "_ensure_tables", lambda: None)
    monkeypatch.setattr(evolution, "_factor_evidence", lambda: [
        {
            "definition_id": 1, "factor_key": "alpha_a", "category": "动量",
            "direction": 1, "evaluation_count": 10, "average_rank_ic": 0.04,
            "average_cost_after": 0.01, "average_coverage": 0.9,
            "latest_end_date": "20260724",
        },
    ])
    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, sql, params=None):
                return _Cursor()
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", fake_conn)

    result = evolution.build_factor_combination_candidates()

    assert result["status"] == "observing"
    assert result["combinations"][0]["status"] == "observing"


def test_eligible_combination_requires_diverse_mature_factors(monkeypatch):
    monkeypatch.setattr(evolution, "_ensure_tables", lambda: None)
    monkeypatch.setattr(evolution, "_factor_evidence", lambda: [
        {
            "definition_id": index, "factor_key": f"alpha_{index}", "category": category,
            "direction": 1, "evaluation_count": 25, "average_rank_ic": 0.03 + index / 1000,
            "average_cost_after": 0.01, "average_coverage": 0.9,
            "latest_end_date": "20260724",
        }
        for index, category in enumerate(("动量", "价值", "流动性"), start=1)
    ])
    calls = []

    class Connection:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))
            return _Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", fake_conn)
    result = evolution.build_factor_combination_candidates()

    combination = result["combinations"][0]
    assert combination["status"] == "production_eligible"
    assert len(combination["members"]) == 3
    assert abs(sum(item["weight"] for item in combination["members"]) - 1.0) < 1e-8
    assert any(sql.startswith("INSERT INTO factor_combinations") for sql, _ in calls)


def test_confirmed_continuous_research_winner_is_distilled(monkeypatch):
    monkeypatch.setattr(evolution, "_ensure_tables", lambda: None)
    calls = []
    experiment = {
        "experiment_id": "confirmed-1",
        "strategy_hash": "hash-1",
        "factor_weights_json": json.dumps({"alpha_a": 0.4, "alpha_b": 0.35, "alpha_c": 0.25}),
        "metrics_json": json.dumps({
            "folds": [
                {"point_in_time_passed": True},
                {"point_in_time_passed": True},
                {"point_in_time_passed": True},
            ],
            "aggregate": {
                "mean_excess_return": 0.02,
                "positive_excess_window_ratio": 2 / 3,
                "worst_drawdown": -0.10,
            },
            "research_spec": {
                "research_package_hash": "package-hash-1",
                "factor_bindings": {
                    "alpha_a": {"definition_id": 101, "factor_key": "alpha_a", "version": "v1"},
                    "alpha_b": {"definition_id": 102, "factor_key": "alpha_b", "version": "v1"},
                    "alpha_c": {"definition_id": 103, "factor_key": "alpha_c", "version": "v1"},
                },
                "model_bindings": {
                    "medium_horizon_lambdarank_v1": {
                        "id": 21,
                        "model_key": "medium_horizon_lambdarank_v1",
                        "version": "medium-v2",
                    },
                },
                "market_state_binding": {
                    "production_model": "transparent_four_state_v1",
                    "change_model": "bocpd_gaussian_v1",
                },
                "strategy_controller_version_id": 31,
                "portfolio_method": "cvar",
            },
        }),
        "fitness": 0.03,
        "generation": 4,
        "parent_experiment_id": "quick-1",
        "finished_at": "2026-07-26T16:00:00+08:00",
    }

    class Connection:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            calls.append((normalized, params))
            if "FROM factor_research_snapshots" in normalized:
                return _Cursor([{"data_as_of": "20260724"}])
            if "FROM factor_combination_experiments" in normalized:
                return _Cursor([experiment])
            return _Cursor()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", fake_conn)
    captured = {}

    def fake_distill(payload):
        captured.update(payload)
        return {"generated": [{"model_version_id": 99}], "reused": []}

    monkeypatch.setattr(evolution, "distill_factor_combinations_to_strategies", fake_distill)
    result = evolution.distill_confirmed_research_experiments(snapshot_id="snapshot-1")

    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["status"] == "research_confirmed"
    assert result["accepted"][0]["frozen_after"] == "20260724"
    assert result["accepted"][0]["research_spec"]["research_package_hash"] == "package-hash-1"
    assert captured["combinations"][0]["research_spec"]["model_bindings"]["medium_horizon_lambdarank_v1"]["id"] == 21
    assert captured["combinations"][0]["members"][0]["factor_key"] == "alpha_a"
    assert captured["combinations"][0]["members"][0]["definition_id"] == 101
    assert any(sql.startswith("INSERT INTO factor_combinations") for sql, _ in calls)
    insert_params = next(
        params for sql, params in calls
        if sql.startswith("INSERT INTO factor_combinations")
    )
    lineage = json.loads(insert_params[6])
    assert lineage["source"] == evolution.CONTINUOUS_RESEARCH_CONFIRMATION_SOURCE
    assert lineage["identity"]["source"] == evolution.CONTINUOUS_RESEARCH_CONFIRMATION_SOURCE


def test_strategy_distillation_preserves_frozen_factor_bindings(monkeypatch):
    monkeypatch.setattr(evolution, "_ensure_tables", lambda: None)
    monkeypatch.setattr(evolution, "resolve_production_strategy_champion", lambda: {
        "champion_id": 50,
        "champion_version": "champion-v50",
        "strategy": {"content": {"score": {"factors": {"old": 1.0}}}},
    })
    monkeypatch.setattr(evolution, "list_model_versions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(evolution, "list_strategies", lambda: [])
    monkeypatch.setattr(evolution, "create_strategy", lambda name, type_, content: {
        "id": 7, "name": name, "type": type_, "content": content,
    })
    frozen_keys = {
        "alpha_a", "alpha_b", "alpha_c",
        "momentum_20", "momentum_60", "volatility_20",
    }
    frozen_bindings = {
        key: {
            "definition_id": index,
            "factor_key": key,
            "version": "frozen-v1",
            "formula": {"engine": "alis_factor_dsl_v1", "expression": {"op": "field", "name": "close"}},
            "leakage_status": "passed",
        }
        for index, key in enumerate(sorted(frozen_keys), start=1)
    }
    captured = {}
    monkeypatch.setattr(
        evolution,
        "create_model_version",
        lambda **kwargs: captured.update(kwargs) or 99,
    )

    @contextmanager
    def fake_conn():
        class Connection:
            def execute(self, _sql, _params=None):
                return _Cursor()
        yield Connection()

    monkeypatch.setattr(evolution, "_conn", fake_conn)
    monkeypatch.setattr(
        evolution,
        "_frozen_factor_bindings",
        lambda _keys: (_ for _ in ()).throw(
            AssertionError("continuous research bindings must not be rebound from the current registry")
        ),
    )

    result = evolution.distill_factor_combinations_to_strategies({
        "combinations": [{
            "combination_key": "factor-combination-frozen",
            "version": "v-frozen",
            "status": "research_confirmed",
            "frozen_after": "20260724",
            "members": [
                {"factor_key": "alpha_a", "weight": 0.4},
                {"factor_key": "alpha_b", "weight": 0.35},
                {"factor_key": "alpha_c", "weight": 0.25},
            ],
            "research_spec": {
                "schema_version": "alis_factor_model_strategy_candidate_v1",
                "factor_bindings": frozen_bindings,
                "model_bindings": {},
                "market_state_binding": {"production_model": "transparent_four_state_v1"},
                "strategy_controller_version_id": 31,
                "portfolio_method": "cvar",
                "research_package_hash": "package-hash-frozen",
            },
        }],
    })

    assert result["generated"][0]["model_version_id"] == 99
    assert captured["config"]["factor_bindings"] == frozen_bindings

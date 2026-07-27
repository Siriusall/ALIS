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
            if "SELECT id,factor_key FROM factor_definitions" in normalized:
                return _Cursor([
                    {"id": 1, "factor_key": "alpha_a"},
                    {"id": 2, "factor_key": "alpha_b"},
                    {"id": 3, "factor_key": "alpha_c"},
                ])
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
    assert captured["combinations"][0]["members"][0]["factor_key"] == "alpha_a"
    assert any(sql.startswith("INSERT INTO factor_combinations") for sql, _ in calls)

from contextlib import contextmanager
import json

from app.services import model_registry, review_learning


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, _params=()):
        if "SELECT label,sample_weight,error_type,source" in sql:
            return _Result(self.rows)
        return _Result()


def _fake_conn(rows):
    @contextmanager
    def connect():
        yield _Connection(rows)
    return connect


def test_feedback_gate_requires_mature_outcomes_not_human_reviews(monkeypatch):
    rows = [
        {"label": 1, "sample_weight": 1.5, "error_type": None, "source": "human_review"},
        {"label": 1, "sample_weight": .5, "error_type": None, "source": "outcome_5d"},
    ]
    monkeypatch.setattr(review_learning, "_conn", _fake_conn(rows))
    result = review_learning.model_feedback_gate("v1")
    assert result["passed"] is False
    assert result["blocked_reasons"] == ["minimum_20d_outcomes", "outcome_accuracy"]


def test_feedback_gate_accepts_automatic_mature_outcomes_without_human_reviews(monkeypatch):
    rows = [{"label": 1, "sample_weight": .5, "error_type": None, "source": "outcome_20d"} for _ in range(5)]
    monkeypatch.setattr(review_learning, "_conn", _fake_conn(rows))
    result = review_learning.model_feedback_gate("v1")
    assert result["passed"] is True
    assert result["human_reviews"] == 0


def test_feedback_gate_can_degrade_poor_production_model(monkeypatch):
    rows = [
        *[{"label": 0, "sample_weight": 1.5, "error_type": "模型失效", "source": "human_review"} for _ in range(5)],
        *[{"label": 0, "sample_weight": .5, "error_type": None, "source": "outcome_20d"} for _ in range(10)],
    ]
    monkeypatch.setattr(review_learning, "_conn", _fake_conn(rows))
    result = review_learning.model_feedback_gate("v1")
    assert result["should_degrade"] is True
    assert result["passed"] is False


def test_materialized_outcome_persists_numeric_directional_return(monkeypatch):
    inserts = []

    class Result:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class BusinessConnection:
        def execute(self, sql, params=()):
            if "FROM decision_snapshots" in sql and "ORDER BY decision_time" in sql:
                return Result([{"id": 7, "ts_code": "000001.SZ", "decision_time": "2026-01-02T15:00:00", "decision_type": "buy"}])
            if "SELECT decision_snapshot_id,horizon_sessions" in sql:
                return Result()
            if "INSERT INTO decision_outcome_attributions" in sql:
                inserts.append(params)
            return Result()

    class DataConnection:
        def execute(self, sql, params=()):
            if "WITH requested(decision_id,code,normalized_code,start_date)" in sql:
                return Result([
                    {"decision_id": 7, "trade_date": f"202601{day:02d}", "close": 10.0 + day, "industry": "银行"}
                    for day in range(1, 62)
                ])
            if "dc_tushare_raw_records" in sql:
                return Result([
                    {"trade_date": trade_date, "payload_json": json.dumps({"close": 100.0 + index})}
                    for index, trade_date in enumerate(params)
                ])
            if "WITH requested(industry,entry_date,exit_date)" in sql:
                return Result([
                    {"industry": params[index], "entry_date": params[index + 1], "exit_date": params[index + 2], "value": 0.05}
                    for index in range(0, len(params), 3)
                ])
            return Result()

    @contextmanager
    def business_conn():
        yield BusinessConnection()

    @contextmanager
    def data_conn():
        yield DataConnection()

    monkeypatch.setattr(review_learning, "_ensure_tables", lambda: None)
    monkeypatch.setattr(review_learning, "_conn", business_conn)
    monkeypatch.setattr(review_learning, "data_conn", data_conn)
    monkeypatch.setattr(review_learning, "materialize_training_feedback", lambda: {"ok": True})

    review_learning.materialize_decision_outcomes()

    assert len(inserts) == 3
    assert all(isinstance(values[5], float) for values in inserts)
    assert inserts[0][5] == inserts[0][4]


def test_retired_generic_challenger_comparison_cannot_change_production():
    champion = {"id": 1, "version": "old", "metrics": {"lift": .01, "walk_forward_lift": .01, "hit_rate": .5}}
    challenger = {"id": 2, "version": "new", "metrics": {"lift": .03, "walk_forward_lift": .02, "hit_rate": .55}, "validation": {"status": "passed"}}
    result = model_registry.compare_champion_challenger(champion, challenger)
    assert result["decision"] == "no_action"
    assert result["status"] == "retired_legacy_generic_model_protocol"
    assert result["production_change_forbidden"] is True

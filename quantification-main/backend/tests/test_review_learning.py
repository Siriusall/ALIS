from contextlib import contextmanager

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


def test_feedback_gate_requires_human_and_mature_outcomes(monkeypatch):
    rows = [
        {"label": 1, "sample_weight": 1.5, "error_type": None, "source": "human_review"},
        {"label": 1, "sample_weight": .5, "error_type": None, "source": "outcome_5d"},
    ]
    monkeypatch.setattr(review_learning, "_conn", _fake_conn(rows))
    result = review_learning.model_feedback_gate("v1")
    assert result["passed"] is False
    assert result["blocked_reasons"] == ["minimum_human_reviews", "minimum_20d_outcomes"]


def test_feedback_gate_can_degrade_poor_production_model(monkeypatch):
    rows = [
        *[{"label": 0, "sample_weight": 1.5, "error_type": "模型失效", "source": "human_review"} for _ in range(5)],
        *[{"label": 0, "sample_weight": .5, "error_type": None, "source": "outcome_20d"} for _ in range(10)],
    ]
    monkeypatch.setattr(review_learning, "_conn", _fake_conn(rows))
    result = review_learning.model_feedback_gate("v1")
    assert result["should_degrade"] is True
    assert result["passed"] is False


def test_challenger_comparison_consumes_review_feedback(monkeypatch):
    monkeypatch.setattr(review_learning, "model_feedback_gate", lambda _version: {"passed": False, "blocked_reasons": ["minimum_20d_outcomes"]})
    champion = {"id": 1, "version": "old", "metrics": {"lift": .01, "walk_forward_lift": .01, "hit_rate": .5}}
    challenger = {"id": 2, "version": "new", "metrics": {"lift": .03, "walk_forward_lift": .02, "hit_rate": .55}, "validation": {"status": "passed"}}
    result = model_registry.compare_champion_challenger(champion, challenger)
    assert result["decision"] == "keep_champion"
    assert result["gates"]["review_feedback_passed"] is False

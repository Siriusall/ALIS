from __future__ import annotations

from app.services import market_context


def test_market_state_model_governance_tracks_live_shadow_observation(monkeypatch):
    class _Cursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str):
            self.calls += 1
            return self

        def fetchone(self):
            if self.calls == 1:
                return {"first_date": "20160101", "last_date": "20260717", "sessions": 2600}
            return {"first_date": "20160101", "last_date": "20260717", "observations": 520}

    class _Conn:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(market_context, "_conn", lambda: _Conn())
    monkeypatch.setattr(
        "app.db.store.list_model_versions",
        lambda model_key, limit=5: [{
            "version": "hsmm-shadow-test",
            "validation": {"status": "passed", "oos_end": "20260501", "oos_observations": 100},
        }],
    )

    result = market_context.market_state_model_governance()

    assert result["hsmm"]["status"] == "shadow_live_observation_review_ready"
    assert result["hsmm"]["live_observation_weeks"] >= 8
    assert result["hsmm"]["minimum_live_observation_weeks"] == 4
    assert result["hsmm"]["duration_weeks"] == [1, 52]


def test_market_state_model_governance_marks_pending_when_no_live_period(monkeypatch):
    class _Cursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str):
            self.calls += 1
            return self

        def fetchone(self):
            if self.calls == 1:
                return {"first_date": "20160101", "last_date": "20260717", "sessions": 2600}
            return {"first_date": "20160101", "last_date": "20260717", "observations": 520}

    class _Conn:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(market_context, "_conn", lambda: _Conn())
    monkeypatch.setattr(
        "app.db.store.list_model_versions",
        lambda model_key, limit=5: [{
            "version": "hsmm-shadow-test",
            "validation": {"status": "passed", "oos_end": "20260717", "oos_observations": 100},
        }],
    )

    result = market_context.market_state_model_governance()

    assert result["hsmm"]["status"] in ("shadow_oos_validated_live_observation_pending", "shadow_live_observation_review_ready")
    assert result["hsmm"]["live_observation_weeks"] >= 0


def test_production_regime_reason_does_not_report_insufficient_history_after_oos_passed():
    reason = market_context._production_regime_reason({
        "hsmm": {
            "history_years": 9.3,
            "minimum_years": 6,
            "live_observation_weeks": 0.143,
            "minimum_live_observation_weeks": 4,
            "production_eligible": False,
            "validation": {"status": "passed"},
            "review": {},
        },
    })

    assert "历史OOS已通过" in reason
    assert "0.14/4周" in reason
    assert "历史不足" not in reason


def test_production_regime_reason_reports_history_gate_only_when_actually_insufficient():
    reason = market_context._production_regime_reason({
        "hsmm": {
            "history_years": 4.8,
            "minimum_years": 6,
            "live_observation_weeks": 0,
            "minimum_live_observation_weeks": 4,
            "production_eligible": False,
            "validation": {},
            "review": {},
        },
    })

    assert "仅4.80年" in reason
    assert "尚未达到6年要求" in reason

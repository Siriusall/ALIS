from datetime import datetime

from app.services import model_governance_scheduler as scheduler


NOW = datetime(2026, 7, 18, 8, 0, 0)


def _version(*, trained_through: str = "20260717", created_at: str = "2026-07-10T08:00:00") -> dict:
    return {
        "id": 6,
        "created_at": created_at,
        "config": {
            "training_input": {
                "cross_sectional_history": {"latest_trade_date": trained_through},
            },
        },
    }


def _completed_run(*, finished_at: str = "2026-07-10T08:00:00") -> dict:
    return {
        "id": 7,
        "status": "completed",
        "finished_at": finished_at,
        "updated_at": finished_at,
    }


def test_retraining_waits_when_market_data_has_not_advanced():
    result = scheduler.automatic_retraining_decision(
        latest_run=_completed_run(), latest_version=_version(),
        current_trade_date="20260717", now=NOW,
    )
    assert result["due"] is False
    assert result["reason"] == "no_new_market_data"


def test_retraining_waits_for_interval_even_with_new_market_data():
    result = scheduler.automatic_retraining_decision(
        latest_run=_completed_run(finished_at="2026-07-17T08:00:00"),
        latest_version=_version(created_at="2026-07-17T08:00:00"),
        current_trade_date="20260718", now=NOW,
    )
    assert result["due"] is False
    assert result["reason"] == "retrain_interval_not_reached"


def test_retraining_starts_after_new_data_and_interval():
    result = scheduler.automatic_retraining_decision(
        latest_run=_completed_run(), latest_version=_version(),
        current_trade_date="20260718", now=NOW,
    )
    assert result["due"] is True
    assert result["reason"] == "new_market_data_and_interval_reached"


def test_failed_training_retries_only_after_cooldown():
    cooling = scheduler.automatic_retraining_decision(
        latest_run={"id": 8, "status": "failed", "updated_at": "2026-07-18T07:30:00"},
        latest_version=_version(), current_trade_date="20260718", now=NOW,
        failed_retry_hours=1,
    )
    retry = scheduler.automatic_retraining_decision(
        latest_run={"id": 8, "status": "failed", "updated_at": "2026-07-18T06:00:00"},
        latest_version=_version(), current_trade_date="20260718", now=NOW,
        failed_retry_hours=1,
    )
    assert cooling == {"due": False, "reason": "failed_retry_cooldown"}
    assert retry == {"due": True, "reason": "retry_failed_training"}


def test_governance_promotes_first_fully_eligible_model(monkeypatch):
    candidate = {
        "id": 9,
        "status": "candidate",
        "is_active": False,
        "metrics": {"lift": 0.01, "walk_forward_lift": 0.02},
        "validation": {"status": "passed", "production_eligible": True},
    }
    promoted = []
    monkeypatch.setattr(scheduler, "materialize_market_training_history", lambda: {"ok": True})
    monkeypatch.setattr(scheduler, "get_active_model_version", lambda *_: None)
    monkeypatch.setattr(
        scheduler, "list_model_versions",
        lambda model_key, limit=20: [candidate] if model_key == "main" else [{"version": "hsmm-shadow-202607-existing"}],
    )
    monkeypatch.setattr(scheduler, "set_active_model_version", lambda model_id: promoted.append(model_id))
    monkeypatch.setattr(scheduler, "write_runtime_log", lambda *args, **kwargs: None)

    result = scheduler.auto_tune_models_once(allow_retrain=False)

    assert result["decision"] == "promoted_initial"
    assert promoted == [9]
    assert result["retraining"]["reason"] == "post_training_evaluation"

from datetime import datetime, timedelta, timezone

from app.services import plan_model_control as control


def test_parse_dt_preserves_business_timezone_for_legacy_values():
    parsed = control._parse_dt("2026-08-12T15:00:00")
    assert parsed is not None
    assert parsed.tzinfo == control._BUSINESS_TZ


def test_parse_dt_normalizes_aware_values():
    parsed = control._parse_dt("2026-08-12T07:00:00+00:00")
    assert parsed is not None
    assert parsed.hour == 15
    assert parsed.tzinfo == control._BUSINESS_TZ


def test_mark_stale_accepts_aware_timestamp_without_type_error(monkeypatch):
    old = (datetime.now(control._BUSINESS_TZ) - timedelta(minutes=30)).isoformat()
    run = {"id": 1, "status": "running", "updated_at": old, "metrics": {}}
    monkeypatch.setattr(control, "update_model_train_run", lambda *args, **kwargs: None)
    result = control._mark_stale_train_run(run)
    assert result["status"] == "failed"

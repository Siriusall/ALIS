from __future__ import annotations

import json
from datetime import timezone
from datetime import datetime, timedelta

from app.data_platform.services import daily_update


def test_daily_update_status_exposes_latest_decision_ready_trade_date(monkeypatch):
    monkeypatch.setattr(daily_update, "_state", {"running": False, "phase": "completed", "message": "ok"})

    class _Conn:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(daily_update, "_conn", lambda: _Conn())
    monkeypatch.setattr(daily_update, "_latest_usable_snapshot_date", lambda c: "20260717")
    monkeypatch.setattr(daily_update, "_latest_decision_ready_trade_date", lambda c: "20260717")

    result = daily_update.daily_update_status()

    assert result["latest_usable_snapshot_date"] == "20260717"
    assert result["latest_decision_ready_trade_date"] == "20260717"


def test_persisted_data_failure_suppresses_minute_level_retry() -> None:
    finished_at = (datetime(2026, 7, 24, 17, 0, 0) - timedelta(minutes=5)).isoformat(timespec="seconds")

    class _Conn:
        def execute(self, _sql, _params=()):
            class _Result:
                @staticmethod
                def fetchone():
                    return {
                        "finished_at": finished_at,
                        "meta_json": json.dumps({
                            "trade_date": "20260724",
                            "failure_category": "upstream_not_ready",
                            "retry_after_seconds": 600,
                        }),
                    }

            return _Result()

    result = daily_update._daily_update_retry_policy(
        _Conn(), "20260724", now=datetime(2026, 7, 24, 17, 0, 0),
    )

    assert result["allowed"] is False
    assert result["reason"] == "data_refresh_cooldown"
    assert result["retry_after_seconds"] == 300


def test_scheduler_retries_an_overdue_trade_date_before_today_close() -> None:
    assert daily_update._scheduler_should_check(
        datetime(2026, 7, 29, 9, 30, 0),
        "20260728",
    )


def test_scheduler_retries_an_overdue_trade_date_on_weekend() -> None:
    assert daily_update._scheduler_should_check(
        datetime(2026, 8, 1, 9, 30, 0),
        "20260731",
    )


def test_scheduler_starts_lightweight_discovery_immediately_after_close() -> None:
    assert not daily_update._scheduler_should_check(
        datetime(2026, 7, 29, 15, 0, 0),
        "20260729",
    )
    assert daily_update._scheduler_should_check(
        datetime(2026, 7, 29, 15, 1, 0),
        "20260729",
    )


def test_scheduler_uses_utc8_when_given_utc_clock() -> None:
    # 07:01 UTC is exactly 15:01 in Shanghai.
    assert daily_update._scheduler_should_check(
        datetime(2026, 7, 29, 7, 1, 0, tzinfo=timezone.utc),
        "20260729",
    )


def test_retry_cooldown_accepts_aware_now_with_legacy_naive_timestamp() -> None:
    finished_at = "2026-07-24T16:55:00"

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _Result:
                @staticmethod
                def fetchone():
                    return {
                        "finished_at": finished_at,
                        "meta_json": json.dumps({
                            "trade_date": "20260724",
                            "failure_category": "upstream_not_ready",
                            "retry_after_seconds": 600,
                        }),
                    }
            return _Result()

    result = daily_update._daily_update_retry_policy(
        _Conn(), "20260724", now=datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
    )

    assert result["allowed"] is False
    assert result["retry_after_seconds"] == 300


def test_scheduler_rejects_invalid_or_future_target_date() -> None:
    now = datetime(2026, 7, 29, 17, 0, 0)
    assert not daily_update._scheduler_should_check(now, "")
    assert not daily_update._scheduler_should_check(now, "20260730")

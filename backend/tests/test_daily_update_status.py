from __future__ import annotations

import json
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

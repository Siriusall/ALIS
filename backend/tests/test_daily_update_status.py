from __future__ import annotations

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

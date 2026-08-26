from pathlib import Path

from app.data_platform import db
from app.data_platform.services import asset_watermarks as watermarks
from app.data_platform.services.asset_watermarks import (
    apply_terminal_states,
    asset_watermark_status,
    mark_asset_terminal,
    persist_asset_watermarks,
)


def test_watermark_terminal_state_survives_same_coverage(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = Path(tmp_path) / "watermarks.db"
    try:
        db.init_db()
        payload = {"items": [{
            "key": "financial_research_fields", "status": "partial", "coverage": 0.8,
            "coverage_basis": 8, "coverage_expected": 10, "source": "test",
            "period_details": [{"period": "20250331", "checks": 10, "complete_checks": 8}],
        }]}
        persist_asset_watermarks(payload)
        mark_asset_terminal("financial_research_fields", "20250331", "upstream_unavailable", "no progress")
        applied = apply_terminal_states(payload)
        assert applied["items"][0]["period_details"][0]["status"] == "upstream_unavailable"
        persist_asset_watermarks(applied)
        row = next(
            x for x in asset_watermark_status(trigger_refresh_if_stale=False)["items"]
            if x["slice_key"] == "20250331"
        )
        assert row["status"] == "upstream_unavailable"
        assert row["terminal_reason"] == "no progress"
    finally:
        db.DB_PATH = original


def test_watermark_status_prefers_data_max_date_and_flags_stale_audit(tmp_path, monkeypatch):
    original = db.DB_PATH
    db.DB_PATH = Path(tmp_path) / "watermarks.db"
    try:
        db.init_db()
        persist_asset_watermarks({"items": [{
            "key": "daily",
            "name": "日线行情",
            "status": "complete",
            "coverage": 1.0,
            "coverage_basis": 10,
            "coverage_expected": 10,
            "source": "test",
            "max_date": "20260826",
            "window_end": "20260826",
            "period_details": [],
        }]})
        monkeypatch.setattr(watermarks, "_live_market_max_trade_date", lambda: "20260826")
        refresh_calls = {"n": 0}

        def _fake_refresh(**_kwargs):
            refresh_calls["n"] += 1
            return {"started": True, "running": True}

        monkeypatch.setattr(watermarks, "request_data_assets_refresh", _fake_refresh)

        with db._conn() as conn:
            conn.execute(
                "UPDATE dc_asset_watermark SET checked_at=?, updated_at=? WHERE asset_key=? AND slice_key=?",
                ("2026-07-26T17:48:24", "2026-07-26T17:48:24", "daily", "overall"),
            )

        status = asset_watermark_status(trigger_refresh_if_stale=True)
        daily = next(item for item in status["items"] if item["asset_key"] == "daily")
        assert daily["watermark"] == "20260826"
        assert daily["data_max_date"] == "20260826"
        assert daily["audit_checked_at"] == "2026-07-26T17:48:24"
        assert status["freshness"]["stale"] is True
        assert status["freshness"]["live_max_trade_date"] == "20260826"
        assert status["refresh"]["triggered"] is True
        assert refresh_calls["n"] == 1
    finally:
        db.DB_PATH = original

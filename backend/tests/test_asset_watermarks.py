from pathlib import Path

from app.data_platform import db
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
        row = next(x for x in asset_watermark_status()["items"] if x["slice_key"] == "20250331")
        assert row["status"] == "upstream_unavailable"
        assert row["terminal_reason"] == "no progress"
    finally:
        db.DB_PATH = original

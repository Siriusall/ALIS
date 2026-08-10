import pytest

from app.services.model_training import _require_compact_api_training_history


def test_training_requires_a_successful_compact_api_refresh() -> None:
    refresh = {
        "ok": True,
        "range": ["20160701", "20260701"],
        "contract": "full_market_daily_aggregates_plus_broad_index_anchors_v2",
        "sources": [
            {"ts_code": "000300.SH", "ok": True, "rows_written": 2400},
            {"ts_code": "000905.SH", "ok": True, "rows_written": 2400},
            {"ts_code": "000852.SH", "ok": True, "rows_written": 2400},
        ],
        "materialized": {"ok": True, "written": 2200},
    }
    result = _require_compact_api_training_history(refresh)
    assert result["full_market_history_persisted"] is True
    assert result["features_written"] == 2200


def test_training_refuses_to_fall_back_when_api_refresh_failed() -> None:
    with pytest.raises(RuntimeError, match="API刷新失败"):
        _require_compact_api_training_history({"ok": False, "sources": [], "materialized": {"written": 0}})

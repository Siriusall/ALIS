from __future__ import annotations

from app.data_platform.services.data_center import _decision_source_observation_ready


def _observed(api_name: str) -> dict[str, str]:
    return {
        "api_name": api_name,
        "observed_at": "2026-08-14T16:02:00+08:00",
        "first_observed_at": "2026-08-14T16:02:00+08:00",
        "economic_available_time": "2026-08-14T15:00:00+08:00",
        "created_at": "2026-08-14T16:02:00+08:00",
        "source_code": "tushare",
        "observation_provenance": "tushare:direct_fetch",
        "content_hash": f"hash-{api_name}",
    }


def test_decision_snapshot_provenance_does_not_require_late_adj_factor() -> None:
    sources = [_observed(api) for api in ("daily", "daily_basic", "stk_limit")]

    assert _decision_source_observation_ready(sources) is True


def test_decision_snapshot_provenance_rejects_missing_or_legacy_core_input() -> None:
    sources = [_observed(api) for api in ("daily", "daily_basic", "stk_limit")]
    sources[1]["observation_provenance"] = "legacy_import"

    assert _decision_source_observation_ready(sources) is False

    assert _decision_source_observation_ready([
        _observed("daily"), _observed("daily_basic"), _observed("adj_factor"),
    ]) is False

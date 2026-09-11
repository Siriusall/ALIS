"""Unit tests for Compact Feature Store and local dataset caching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.compact_feature_store import (
    FEATURE_BUNDLE_CONTRACT,
    export_market_features_bundle,
    feature_cache_root,
    is_local_feature_cache_enabled,
    load_snapshot_bundle_from_local_cache,
    save_snapshot_bundle_to_local_cache,
)


def test_save_and_load_snapshot_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIS_FEATURE_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("ALIS_LOCAL_FEATURE_CACHE_ENABLED", "1")

    sample_records = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260828",
            "close": 10.5,
            "open": 10.1,
            "high": 10.8,
            "low": 10.0,
            "volume": 50000.0,
            "observed_at": "2026-08-28T15:30:00+08:00",
        },
        {
            "ts_code": "600000.SH",
            "trade_date": "20260828",
            "close": 8.2,
            "open": 8.0,
            "high": 8.3,
            "low": 7.9,
            "volume": 30000.0,
            "observed_at": "2026-08-28T15:30:00+08:00",
        },
    ]

    saved_path = save_snapshot_bundle_to_local_cache("20260828", sample_records)
    assert saved_path.exists()
    assert saved_path.suffix == ".parquet"
    assert saved_path.with_name(saved_path.name + ".manifest.json").exists()

    loaded = load_snapshot_bundle_from_local_cache("20260828")
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0]["ts_code"] == "000001.SZ"

    # Test point-in-time filtering
    earlier_cutoff = "2026-08-28T12:00:00+08:00"
    pit_filtered = load_snapshot_bundle_from_local_cache("20260828", as_of_time=earlier_cutoff)
    assert pit_filtered == []

    valid_cutoff = "2026-08-28T16:00:00+08:00"
    pit_valid = load_snapshot_bundle_from_local_cache("20260828", as_of_time=valid_cutoff)
    assert len(pit_valid) == 2


def test_disabled_cache_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIS_FEATURE_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("ALIS_LOCAL_FEATURE_CACHE_ENABLED", "0")

    sample_records = [{
        "ts_code": "000001.SZ",
        "trade_date": "20260828",
        "close": 10.5,
        "observed_at": "2026-08-28T15:30:00+08:00",
    }]
    save_snapshot_bundle_to_local_cache("20260828", sample_records)

    loaded = load_snapshot_bundle_from_local_cache("20260828")
    assert loaded is None


def test_save_and_load_text_event_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIS_FEATURE_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("ALIS_LOCAL_FEATURE_CACHE_ENABLED", "1")

    from app.services.compact_feature_store import (
        save_text_event_features_to_local_cache,
        load_text_event_features_from_local_cache,
    )

    event_records = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260828",
            "event_type": "earnings_forecast",
            "expected_growth": 0.85,
            "surprise_gap": 0.12,
            "observed_at": "2026-08-28T18:00:00+08:00",
        }
    ]

    saved = save_text_event_features_to_local_cache("20260828", event_records)
    assert saved.exists()
    loaded = load_text_event_features_from_local_cache("20260828")
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["event_type"] == "earnings_forecast"


def test_cache_rejects_missing_or_naive_availability_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIS_FEATURE_CACHE_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="missing available_time"):
        save_snapshot_bundle_to_local_cache(
            "20260828", [{"ts_code": "000001.SZ", "trade_date": "20260828"}]
        )
    with pytest.raises(ValueError, match="include timezone"):
        save_snapshot_bundle_to_local_cache(
            "20260828",
            [{"ts_code": "000001.SZ", "trade_date": "20260828", "observed_at": "2026-08-28T15:30:00"}],
        )


def test_cache_rejects_tampered_parquet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIS_FEATURE_CACHE_ROOT", str(tmp_path))
    saved = save_snapshot_bundle_to_local_cache(
        "20260828",
        [{"ts_code": "000001.SZ", "trade_date": "20260828", "observed_at": "2026-08-28T15:30:00+08:00"}],
    )
    saved.write_bytes(saved.read_bytes() + b"tampered")
    assert load_snapshot_bundle_from_local_cache("20260828") is None

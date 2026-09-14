from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.market_dataset import (
    CONTRACT,
    FEATURE_FIELDS,
    METADATA_FIELDS,
    DatasetUnavailable,
    MarketDataset,
    _dataset_schemas,
    _derived_row,
    _identity,
    configured_dataset,
    sync_market_dataset,
)


def _make_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    objects = source / "objects"
    manifests = source / "manifests"
    objects.mkdir(parents=True)
    manifests.mkdir()
    # One valid partition per required category for one month.
    import pyarrow as pa
    import pyarrow.parquet as pq
    from app.services.market_dataset import RAW_APIS

    parts = []
    for category in ("snapshot", *RAW_APIS):
        feature = (
            "close"
            if "close" in FEATURE_FIELDS[category]
            else FEATURE_FIELDS[category][0]
        )
        path = objects / f"{category}.parquet"
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260828",
                        feature: 10,
                        "source_code": "test",
                        "economic_available_time": "2026-08-28T15:00:00+08:00",
                        "observed_at": "2026-08-28T15:30:00+08:00",
                        "first_observed_at": "2026-08-28T15:30:00+08:00",
                        "observation_provenance": "test",
                        "content_hash": "a" * 64,
                        "created_at": "2026-08-28T15:30:00+08:00",
                    }
                ]
            ),
            path,
            compression="zstd",
        )
        import hashlib

        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        target = objects / f"{sha}.parquet"
        path.replace(target)
        parts.append(
            {
                "category": category,
                "start": "20260801",
                "end": "20260831",
                "sha256": sha,
                "rows": 1,
                "bytes": target.stat().st_size,
            }
        )
    manifest = {
        "contract": CONTRACT,
        "schemas": _dataset_schemas(),
        "start": "20260801",
        "end": "20260831",
        "trade_dates": ["20260828"],
        "partitions": parts,
    }
    version = _identity(manifest)
    (manifests / f"{version}.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "latest.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    return source, version


def test_sync_reuses_content_addressed_partitions(tmp_path: Path):
    source, version = _make_source(tmp_path)
    target = tmp_path / "target"
    first = sync_market_dataset(source=str(source), root=target)
    assert first["downloaded_partitions"] + first["reused_partitions"] == 3
    second = sync_market_dataset(source=str(source), version=version, root=target)
    assert second["downloaded_partitions"] == 0
    assert second["reused_partitions"] == 3
    dataset = MarketDataset(version, root=target)
    assert dataset.dates("20260801", "20260831") == ["20260828"]
    assert (
        len(
            dataset.rows("snapshot", cutoff="2026-08-28T16:00:00+08:00", ymd="20260828")
        )
        == 1
    )


def test_explicit_historical_sync_does_not_roll_back_latest_pointer(tmp_path: Path):
    source, old_version = _make_source(tmp_path)
    target = tmp_path / "target"
    sync_market_dataset(source=str(source), root=target)
    old_manifest = json.loads((source / "manifests" / f"{old_version}.json").read_text())
    september = [
        {**part, "start": "20260901", "end": "20260930"}
        for part in old_manifest["partitions"]
    ]
    new_manifest = {
        **old_manifest,
        "end": "20260930",
        "trade_dates": [*old_manifest["trade_dates"], "20260901"],
        "partitions": [*old_manifest["partitions"], *september],
    }
    new_version = _identity(new_manifest)
    (source / "manifests" / f"{new_version}.json").write_text(json.dumps(new_manifest))
    (source / "latest.json").write_text(json.dumps({"version": new_version}))

    followed = sync_market_dataset(source=str(source), root=target)
    replayed = sync_market_dataset(source=str(source), version=old_version, root=target)

    assert followed["latest_pointer_updated"] is True
    assert replayed["latest_pointer_updated"] is False
    assert json.loads((target / "latest.json").read_text())["version"] == new_version


def test_follow_latest_rejects_pointer_rollback(tmp_path: Path):
    source, old_version = _make_source(tmp_path)
    target = tmp_path / "target"
    old_manifest = json.loads((source / "manifests" / f"{old_version}.json").read_text())
    september = [
        {**part, "start": "20260901", "end": "20260930"}
        for part in old_manifest["partitions"]
    ]
    new_manifest = {
        **old_manifest,
        "end": "20260930",
        "trade_dates": [*old_manifest["trade_dates"], "20260901"],
        "partitions": [*old_manifest["partitions"], *september],
    }
    new_version = _identity(new_manifest)
    (source / "manifests" / f"{new_version}.json").write_text(json.dumps(new_manifest))
    (source / "latest.json").write_text(json.dumps({"version": new_version}))
    sync_market_dataset(source=str(source), root=target)
    (source / "latest.json").write_text(json.dumps({"version": old_version}))

    with pytest.raises(DatasetUnavailable, match="backwards"):
        sync_market_dataset(source=str(source), root=target)


def test_derived_row_whitelists_research_fields_and_drops_raw_payload():
    import json

    row = {
        "ts_code": "000001.SZ",
        "trade_date": "20260828",
        "source_code": "test",
        "economic_available_time": "2026-08-28T15:00:00+08:00",
        "observed_at": "2026-08-28T15:30:00+08:00",
        "first_observed_at": "2026-08-28T15:30:00+08:00",
        "observation_provenance": "test",
        "content_hash": "a" * 64,
        "created_at": "2026-08-28T15:30:00+08:00",
        "payload_json": json.dumps(
            {"close": 10, "provider_secret_field": "must-not-cross"}
        ),
    }
    derived = _derived_row("index_daily", row)
    assert derived["close"] == 10
    assert "payload_json" not in derived
    assert "provider_secret_field" not in derived
    assert set(derived) <= set(METADATA_FIELDS) | set(FEATURE_FIELDS["index_daily"])


def test_sync_rejects_parquet_with_raw_payload_column(tmp_path: Path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source, _ = _make_source(tmp_path)
    manifest_path = next((source / "manifests").iterdir())
    manifest = __import__("json").loads(manifest_path.read_text())
    part = manifest["partitions"][0]
    object_path = source / "objects" / f"{part['sha256']}.parquet"
    table = pq.read_table(object_path).append_column("payload_json", pa.array(["{}"]))
    pq.write_table(table, object_path, compression="zstd")
    import hashlib

    part["sha256"] = hashlib.sha256(object_path.read_bytes()).hexdigest()
    part["bytes"] = object_path.stat().st_size
    tainted = source / "objects" / f"{part['sha256']}.parquet"
    object_path.replace(tainted)
    version = _identity(manifest)
    (source / "manifests" / f"{version}.json").write_text(
        __import__("json").dumps(manifest)
    )
    (source / "latest.json").write_text(__import__("json").dumps({"version": version}))
    with pytest.raises(DatasetUnavailable, match="raw payload columns forbidden"):
        sync_market_dataset(source=str(source), root=tmp_path / "target")


def test_required_dataset_never_falls_back_to_remote_sql(monkeypatch):
    monkeypatch.delenv("ALIS_MARKET_DATASET_VERSION", raising=False)
    monkeypatch.setenv("ALIS_MARKET_DATASET_REQUIRED", "1")
    with pytest.raises(DatasetUnavailable, match="remote SQL fallback is forbidden"):
        configured_dataset()

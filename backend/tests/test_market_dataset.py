from __future__ import annotations

import json
from pathlib import Path

from app.services.market_dataset import CONTRACT, MarketDataset, _identity, sync_market_dataset


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
        path = objects / f"{category}.parquet"
        pq.write_table(pa.Table.from_pylist([{
            "ts_code": "000001.SZ", "trade_date": "20260828",
            "payload_json": json.dumps({"close": 10}), "source_code": "test",
            "economic_available_time": "2026-08-28T15:00:00+08:00",
            "observed_at": "2026-08-28T15:30:00+08:00",
            "first_observed_at": "2026-08-28T15:30:00+08:00",
            "observation_provenance": "test", "content_hash": "a" * 64,
            "created_at": "2026-08-28T15:30:00+08:00",
        }]), path, compression="zstd")
        import hashlib
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        target = objects / f"{sha}.parquet"
        path.replace(target)
        parts.append({"category": category, "start": "20260801", "end": "20260831",
                      "sha256": sha, "rows": 1, "bytes": target.stat().st_size})
    manifest = {"contract": CONTRACT, "start": "20260801", "end": "20260831",
                "trade_dates": ["20260828"], "partitions": parts}
    version = _identity(manifest)
    (manifests / f"{version}.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "latest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return source, version


def test_sync_reuses_content_addressed_partitions(tmp_path: Path):
    source, version = _make_source(tmp_path)
    target = tmp_path / "target"
    first = sync_market_dataset(source=str(source), root=target)
    assert first["downloaded_partitions"] == 1
    assert first["reused_partitions"] == 6
    second = sync_market_dataset(source=str(source), version=version, root=target)
    assert second["downloaded_partitions"] == 0
    assert second["reused_partitions"] == 7
    dataset = MarketDataset(version, root=target)
    assert dataset.dates("20260801", "20260831") == ["20260828"]
    assert len(dataset.rows("snapshot", cutoff="2026-08-28T16:00:00+08:00", ymd="20260828")) == 1

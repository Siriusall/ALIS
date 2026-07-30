from app.services import tushare_snapshot


def test_latest_cached_snapshot_respects_point_in_time_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(tushare_snapshot, "_SNAPSHOT_DISK_CACHE_DIR", tmp_path)
    for trade_date in ("20240102", "20240105", "20240110"):
        (tmp_path / f"{trade_date}.json").write_text("{}", encoding="utf-8")

    assert tushare_snapshot.get_latest_cached_snapshot_date() == "20240110"
    assert (
        tushare_snapshot.get_latest_cached_snapshot_date(on_or_before="2024-01-07")
        == "20240105"
    )
    assert tushare_snapshot.get_latest_cached_snapshot_date(on_or_before="20240101") is None

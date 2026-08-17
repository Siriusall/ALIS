import pytest

from app.services import tushare_snapshot


def test_exact_date_snapshot_never_falls_back_to_an_earlier_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(tushare_snapshot, "_SNAPSHOT_LOCK_DIR", tmp_path)
    monkeypatch.setattr(tushare_snapshot, "_snapshot_cache", {})
    monkeypatch.setattr(tushare_snapshot, "_snapshot_fail_until", {})
    monkeypatch.setattr(tushare_snapshot, "_resolve_recent_trade_date", lambda _date: "20260814")
    monkeypatch.setattr(tushare_snapshot, "_load_snapshot_from_postgres", lambda _date: None)
    calls: list[str] = []

    def unavailable(trade_date: str):
        calls.append(trade_date)
        raise tushare_snapshot.DailySnapshotUnavailableError("market_bundle_not_ready")

    monkeypatch.setattr(tushare_snapshot, "_build_snapshot_for_date", unavailable)

    with pytest.raises(tushare_snapshot.DailySnapshotUnavailableError):
        tushare_snapshot.get_daily_snapshot("20260814")

    assert calls == ["20260814"]

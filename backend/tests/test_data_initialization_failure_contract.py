from __future__ import annotations

from app.data_platform import db as data_db
from app.data_platform.services import data_center
from app.services import init_service


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=()):
        class _Result:
            @staticmethod
            def fetchone():
                return {"cnt": 5001}

        return _Result()


def test_non_strict_initialization_does_not_hide_market_bundle_failure(monkeypatch):
    monkeypatch.setattr(init_service, "get_last_trade_date_str", lambda **_kwargs: "20260814")
    monkeypatch.setattr(init_service, "is_strict_first_load_enabled", lambda: False)
    monkeypatch.setattr(init_service, "has_consolidated_for_date", lambda _date: False)
    monkeypatch.setattr(init_service, "run_startup_connectivity_checks", lambda: {"tushare_ok": True, "errors": []})
    monkeypatch.setattr(init_service, "write_init_status", lambda _payload: None)
    monkeypatch.setattr(data_db, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(data_center, "sync_stock_basic", lambda: {"rows_written": 5001})
    monkeypatch.setattr(
        data_center,
        "sync_market_bundle",
        lambda _date: {"ok": False, "failure_category": "upstream_not_published"},
    )
    monkeypatch.setattr(data_center, "build_stock_daily_snapshot", lambda _date: {"rows_written": 5001})
    monkeypatch.setattr(data_center, "run_data_quality_checks", lambda _date: {"ok": True})

    result = init_service.run_data_initialization(strict=False)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["market_bundle_failure_category"] == "upstream_not_published"
    assert any("Market bundle incomplete" in item for item in result["quality"]["errors"])

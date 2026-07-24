from __future__ import annotations

from contextlib import contextmanager

import app.data_platform.db as db
import app.services.cache_consolidation as cache_consolidation


def test_available_trade_dates_use_materialized_table_before_raw_records(monkeypatch):
    executed: list[str] = []

    class FakeConn:
        def execute(self, sql, params=None):  # noqa: ANN001
            executed.append(str(sql))
            if "dc_available_trade_dates" in str(sql):
                return FakeRows([{"trade_date": "20260714"}, {"trade_date": "20260715"}])
            if "dc_tushare_raw_records" in str(sql):
                raise AssertionError("raw records should not be scanned on the hot path")
            return FakeRows([])

    class FakeRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    @contextmanager
    def fake_conn():
        yield FakeConn()

    monkeypatch.setattr(db, "_conn", fake_conn)
    monkeypatch.setattr(cache_consolidation, "_available_trade_dates_cache", (0.0, []))

    assert cache_consolidation.list_available_trade_dates() == ["20260714", "20260715"]
    assert any("dc_available_trade_dates" in sql for sql in executed)
    assert not any("dc_tushare_raw_records" in sql for sql in executed)

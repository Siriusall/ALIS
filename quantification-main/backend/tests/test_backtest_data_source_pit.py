from contextlib import contextmanager

from app.services import backtest_data_source as data_source_module
from app.services.backtest_data_source import BacktestDataSource, HistBar


def test_point_in_time_audit_accumulates_repeated_source_batches():
    source = BacktestDataSource()
    source.set_as_of_time("2026-07-22T15:00:00")

    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [
            {"available_time": "2026-07-21T15:00:00"},
            {"available_time": "2026-07-22T15:00:00"},
        ],
    )
    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [{"available_time": "2026-07-20T15:00:00"}],
    )

    audit = source.point_in_time_sources()[0]
    assert audit["read_count"] == 3
    assert audit["batch_count"] == 2
    assert audit["max_available_time"] == "2026-07-22T15:00:00"
    assert audit["passed"] is True


def test_forward_history_cache_only_loads_newly_visible_session_and_clears_on_rewind(monkeypatch):
    source = BacktestDataSource()
    source.set_as_of_time("2026-01-02T15:00:00")
    source._hist_cache["000001"] = [  # noqa: SLF001 - cache safety contract
        HistBar("2026-01-02", 10, 10, 10, 10, 1, 1),
    ]
    source._hist_cache_through["000001"] = "20260102"  # noqa: SLF001 - cache safety contract
    source.set_as_of_time("2026-01-03T15:00:00")

    executed: list[tuple[str, tuple]] = []

    class Result:
        def __init__(self, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, params=()):
            executed.append((sql, params))
            if "MIN(trade_date)" in sql:
                return Result(row={"start_date": "20251201"})
            code = str(params[0])
            return Result(rows=[{
                "ts_code": code, "trade_date": "20260103", "close": 11,
                "volume": 1, "turnover": 1, "available_time": "2026-01-03T15:00:00", "adj_factor": 1,
            }])

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(data_source_module, "_conn", fake_conn)
    source.preload_histories(["000001"], end_date="2026-01-03", lookback=60)

    history_params = executed[-1][1]
    assert history_params[1:3] == ("20260102", "20260103")
    assert [bar.date for bar in source.get_hist_window("000001", "2026-01-03", 5)] == ["2026-01-02", "2026-01-03"]

    source.set_as_of_time("2026-01-04T15:00:00")
    source.preload_histories(["000001", "000002"], end_date="2026-01-04", lookback=60)
    history_batches = [params for sql, params in executed if "snapshot_rows" in sql]
    assert history_batches[-2][0] == "000001.SZ"
    assert history_batches[-2][1:3] == ("20260103", "20260104")
    assert history_batches[-1][0] == "000002.SZ"
    assert history_batches[-1][1:3] == ("20251201", "20260104")

    source.set_as_of_time("2026-01-02T15:00:00")
    assert source._hist_cache == {}  # noqa: SLF001 - rewind must discard future-visible bars

from contextlib import contextmanager
import sqlite3

from app.services import backtest_data_source as data_source_module
from app.services.backtest_data_source import BacktestDataSource, HistBar


def test_derived_dataset_fields_are_consumed_without_rebuilding_raw_json():
    row = {
        "ts_code": "510300.SH",
        "trade_date": "20260831",
        "open": 4.0,
        "high": 4.2,
        "low": 3.9,
        "close": 4.1,
        "pre_close": 4.0,
        "vol": 100.0,
        "amount": 410.0,
    }

    assert BacktestDataSource._payload_fields(row) == row  # noqa: SLF001
    decoded = BacktestDataSource._decode_versioned_snapshot_row(row)  # noqa: SLF001
    assert decoded["close"] == 4.1
    assert decoded["daily_open"] == 4.0
    assert "payload_json" not in decoded


def test_point_in_time_audit_accumulates_repeated_source_batches():
    source = BacktestDataSource()
    source.set_as_of_time("2026-07-22T15:00:00+08:00")

    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [
            {"available_time": "2026-07-21T15:00:00+08:00"},
            {"available_time": "2026-07-22T15:00:00+08:00"},
        ],
    )
    source._record_pit_read(  # noqa: SLF001 - audit aggregation contract
        "snapshot.history",
        [{"available_time": "2026-07-20T15:00:00+08:00"}],
    )

    audit = source.point_in_time_sources()[0]
    assert audit["read_count"] == 3
    assert audit["batch_count"] == 2
    assert audit["max_available_time"] == "2026-07-22T15:00:00+08:00"
    assert audit["passed"] is True


def test_forward_history_cache_only_loads_newly_visible_session_and_clears_on_rewind(monkeypatch):
    source = BacktestDataSource()
    source.set_as_of_time("2026-01-02T15:00:00+08:00")
    source._hist_cache["000001"] = [  # noqa: SLF001 - cache safety contract
        HistBar("2026-01-02", 10, 10, 10, 10, 1, 1),
    ]
    source._hist_cache_through["000001"] = "20260102"  # noqa: SLF001 - cache safety contract
    source.set_as_of_time("2026-01-03T15:00:00+08:00")

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
                "volume": 1, "turnover": 1, "available_time": "2026-01-03T15:00:00+08:00", "adj_factor": 1,
            }])

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(data_source_module, "_conn", fake_conn)
    source.preload_histories(["000001"], end_date="2026-01-03", lookback=60)

    history_params = executed[-1][1]
    assert history_params[1:3] == ("20260102", "20260103")
    assert [bar.date for bar in source.get_hist_window("000001", "2026-01-03", 5)] == ["2026-01-02", "2026-01-03"]

    source.set_as_of_time("2026-01-04T15:00:00+08:00")
    source.preload_histories(["000001", "000002"], end_date="2026-01-04", lookback=60)
    history_batches = [
        params for sql, params in executed
        if "dc_stock_daily_snapshot_version" in sql
    ]
    assert history_batches[-2][0] == "000001.SZ"
    assert history_batches[-2][1:3] == ("20260103", "20260104")
    assert history_batches[-1][0] == "000002.SZ"
    assert history_batches[-1][1:3] == ("20251201", "20260104")

    source.set_as_of_time("2026-01-02T15:00:00+08:00")
    assert source._hist_cache == {}  # noqa: SLF001 - rewind must discard future-visible bars


def test_postgres_replay_never_falls_back_to_current_daily_projection(monkeypatch):
    source = BacktestDataSource()
    source.set_as_of_time("2026-01-03T15:00:00+08:00")

    class UnexpectedConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            raise AssertionError("current projection must not be read when PIT versions are unavailable")

    monkeypatch.setattr(data_source_module, "_is_postgres", lambda: True)
    monkeypatch.setattr(source, "_versioned_raw_rows", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_source_module, "_conn", lambda: UnexpectedConnection())

    assert source._postgres_daily_rows("20260103") == []  # noqa: SLF001
    audit = source.point_in_time_sources()[0]
    assert audit["passed"] is False


def test_postgres_replay_never_falls_back_to_current_fund_projection(monkeypatch):
    source = BacktestDataSource()
    source.set_as_of_time("2026-01-03T15:00:00+08:00")

    class UnexpectedConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            raise AssertionError("current projection must not be read when PIT versions are unavailable")

    monkeypatch.setattr(data_source_module, "_is_postgres", lambda: True)
    monkeypatch.setattr(source, "_versioned_raw_rows", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(data_source_module, "_conn", lambda: UnexpectedConnection())

    assert source.load_fund_snapshot("20260103", ["510300.SH"]) == []
    audit = source.point_in_time_sources()[0]
    assert audit["passed"] is False


def test_empty_versioned_read_is_a_hard_pit_failure(monkeypatch):
    source = BacktestDataSource()
    source.set_as_of_time("2026-01-03T15:00:00+08:00")

    monkeypatch.setattr(data_source_module, "_is_postgres", lambda: True)
    monkeypatch.setattr(source, "_versioned_snapshot_rows", lambda *_args, **_kwargs: [])

    assert source.load_snapshot("20260103") == []
    audit = source.point_in_time_sources()[0]
    assert audit["read_count"] == 0
    assert audit["passed"] is False


def test_immutable_readers_keep_projection_selected_provider(monkeypatch):
    """A slower provider must not replace the publication-race winner in replay."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE dc_tushare_raw_record_version(
            id INTEGER PRIMARY KEY, api_name TEXT, record_key TEXT, ts_code TEXT,
            trade_date TEXT, payload_json TEXT, source_code TEXT,
            economic_available_time TEXT, observed_at TEXT, first_observed_at TEXT,
            observation_provenance TEXT, content_hash TEXT, created_at TEXT
        );
        CREATE TABLE dc_tushare_raw_records(
            api_name TEXT, record_key TEXT, content_hash TEXT,
            PRIMARY KEY(api_name, record_key)
        );
        CREATE TABLE dc_stock_daily_snapshot_version(
            id INTEGER PRIMARY KEY, ts_code TEXT, trade_date TEXT, payload_json TEXT,
            source_code TEXT, economic_available_time TEXT, observed_at TEXT,
            first_observed_at TEXT, observation_provenance TEXT, content_hash TEXT,
            created_at TEXT
        );
        CREATE TABLE dc_stock_daily_snapshot(
            ts_code TEXT, trade_date TEXT, content_hash TEXT,
            PRIMARY KEY(ts_code, trade_date)
        );
        """
    )
    early = "2026-08-14T15:01:00+08:00"
    late = "2026-08-14T15:02:00+08:00"
    connection.executemany(
        """INSERT INTO dc_tushare_raw_record_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                1, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814",
                '{"close":10}', "tencent_market_snapshot", early, early, early,
                "tencent_market_snapshot:validated", "fallback", early,
            ),
            (
                2, "daily", "daily:000001.SZ:20260814", "000001.SZ", "20260814",
                '{"close":11}', "tushare", late, late, late,
                "tushare:direct_fetch", "primary", late,
            ),
        ],
    )
    connection.executemany(
        """INSERT INTO dc_stock_daily_snapshot_version
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                1, "000001.SZ", "20260814",
                '{"open":10,"high":10,"low":10,"close":10,"pre_close":10,"vol":1,"amount":1}',
                "tencent_market_snapshot", early, early, early,
                "tencent_market_snapshot:validated", "fallback", early,
            ),
            (
                2, "000001.SZ", "20260814",
                '{"open":11,"high":11,"low":11,"close":11,"pre_close":11,"vol":1,"amount":1}',
                "tushare", late, late, late, "tushare:direct_fetch", "primary", late,
            ),
        ],
    )
    connection.execute(
        "INSERT INTO dc_tushare_raw_records VALUES(?,?,?)",
        ("daily", "daily:000001.SZ:20260814", "fallback"),
    )
    connection.execute(
        "INSERT INTO dc_stock_daily_snapshot VALUES(?,?,?)",
        ("000001.SZ", "20260814", "fallback"),
    )

    @contextmanager
    def fake_conn():
        yield connection

    monkeypatch.setattr(data_source_module, "_conn", fake_conn)
    monkeypatch.setattr(data_source_module, "_is_postgres", lambda: False)
    source = BacktestDataSource()
    source.set_as_of_time("2026-08-14T15:05:00+08:00")

    raw = source._versioned_raw_rows("daily")  # noqa: SLF001 - PIT reader contract
    snapshot = source._versioned_snapshot_rows("20260814")  # noqa: SLF001
    history = source._versioned_snapshot_history_rows(["000001.SZ"])  # noqa: SLF001

    assert [(item["source_code"], item["content_hash"]) for item in raw or []] == [
        ("tencent_market_snapshot", "fallback"),
    ]
    assert [(item["source_code"], item["content_hash"]) for item in snapshot or []] == [
        ("tencent_market_snapshot", "fallback"),
    ]
    assert [(item["source_code"], item["content_hash"]) for item in history or []] == [
        ("tencent_market_snapshot", "fallback"),
    ]

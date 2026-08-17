from __future__ import annotations

from datetime import date, timedelta
from contextlib import contextmanager
import json
import sqlite3

from app.data_platform.services import oos_hard_data, snapshot_health


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        if "api_name='index_daily'" in sql:
            return FakeRows([
                {
                    "trade_date": value,
                    "max_available_time": (
                        f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00"
                    ),
                }
                for value in ("20260721", "20260722")
            ])
        if "FROM dc_oos_hard_api_stats_live" in sql:
            return FakeRows([{
                "api_name": api, "trade_date": value, "point_in_time_rows": 1000,
            } for api in oos_hard_data.HARD_APIS for value in (
                ["20260721", "20260722"] if api != "adj_factor" else ["20260721"]
            )])
        if "FROM dc_trade_calendar" in sql:
            return FakeRows([{"cal_date": "20260722"}, {"cal_date": "20260721"}])
        if "FROM dc_stock_daily_snapshot" in sql:
            return FakeRows([
                {"trade_date": "20260721", "rows_total": 1000, "market_rows": 950, "min_available_time": "2026-07-21T15:00:00", "max_available_time": "2026-07-21T15:00:00"},
                {"trade_date": "20260722", "rows_total": 1000, "market_rows": 950, "min_available_time": "2026-07-22T15:00:00", "max_available_time": "2026-07-22T15:00:00"},
            ])
        raise AssertionError(sql)


def test_partial_snapshot_summary_falls_back_to_normalized_rows():
    class Result:
        def __init__(self, rows=(), row=None):
            self.rows = list(rows)
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class PartialSummaryConnection:
        _is_postgres = True

        def __init__(self):
            self.queries = []

        def execute(self, sql, params=()):
            self.queries.append(sql)
            if "to_regclass" in sql:
                return Result(row={"table_name": "dc_stock_daily_snapshot_stats"})
            if "FROM dc_stock_daily_snapshot_stats" in sql:
                return Result(rows=[{
                    "trade_date": "20260803", "rows_total": 5522,
                    "market_rows": 5516, "min_available_time": "2026-08-03T15:00:00",
                    "max_available_time": "2026-08-03T15:00:00",
                }])
            if "FROM dc_stock_daily_snapshot" in sql:
                return Result(rows=[
                    {
                        "trade_date": value, "rows_total": 5522,
                        "market_rows": 5516, "min_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00",
                        "max_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00",
                    }
                    for value in ("20260803", "20260804")
                ])
            raise AssertionError(sql)

    conn = PartialSummaryConnection()
    result = snapshot_health.snapshot_stats_for_range(
        conn,
        "20260803",
        "20260804",
        expected_dates=["20260803", "20260804"],
    )

    assert sorted(result or {}) == ["20260803", "20260804"]
    assert any("FROM dc_stock_daily_snapshot" in sql for sql in conn.queries)


def test_snapshot_summary_reads_live_generation_before_legacy():
    class Result:
        def __init__(self, rows=(), row=None):
            self.rows, self.row = list(rows), row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        _is_postgres = True

        def __init__(self):
            self.summary_sql = ""

        def execute(self, sql, params=()):
            if "to_regclass" in sql:
                return Result(row={"table_name": params[0]})
            if "DISTINCT ON" in sql:
                self.summary_sql = sql
                return Result(rows=[{
                    "trade_date": "20260811", "rows_total": 5539,
                    "market_rows": 5539, "min_available_time": "2026-08-11T15:00:00",
                    "max_available_time": "2026-08-11T15:00:00",
                }])
            raise AssertionError(sql)

    conn = Connection()
    result = snapshot_health.snapshot_stats_for_range(
        conn, "20260811", "20260811", expected_dates=["20260811"],
    )

    assert result and result["20260811"]["rows_total"] == 5539
    assert "dc_stock_daily_snapshot_stats_live" in conn.summary_sql
    assert conn.summary_sql.index("dc_stock_daily_snapshot_stats_live") < conn.summary_sql.index("dc_stock_daily_snapshot_stats WHERE")


class FakeRows(list):
    def fetchall(self):
        return self


def test_strict_versions_rank_only_after_filtering_to_the_trade_cutoff(monkeypatch):
    """A late repair must not displace the version actually visible at T15."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
            CREATE TABLE dc_tushare_raw_record_version(
                id INTEGER PRIMARY KEY, api_name TEXT, record_key TEXT, ts_code TEXT,
                trade_date TEXT, economic_available_time TEXT, observed_at TEXT,
                first_observed_at TEXT, created_at TEXT, content_hash TEXT,
                observation_provenance TEXT
            );
            CREATE TABLE dc_stock_daily_snapshot_version(
                id INTEGER PRIMARY KEY, ts_code TEXT, trade_date TEXT, payload_json TEXT,
                source_code TEXT, economic_available_time TEXT, observed_at TEXT,
                first_observed_at TEXT, created_at TEXT, content_hash TEXT,
                observation_provenance TEXT
            );
    """)
    trade_date = "20260813"
    before = "2026-08-13T14:30:00+08:00"
    after = "2026-08-13T16:00:00+08:00"
    connection.executemany(
            """INSERT INTO dc_tushare_raw_record_version
               (id,api_name,record_key,ts_code,trade_date,economic_available_time,observed_at,first_observed_at,created_at,content_hash,observation_provenance)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (1, "daily", "daily:000001.SZ:20260813", "000001.SZ", trade_date, "2026-08-13T15:00:00+08:00", before, before, before, "before", "tushare:direct_fetch"),
                # The row was stored before T15, but its economic availability
                # was recorded after the close. It is not PIT evidence.
                (2, "daily", "daily:000001.SZ:20260813", "000001.SZ", trade_date, after, before, before, before, "after", "tushare:direct_fetch"),
        ],
    )
    complete = {
        "close": 10, "volume": 100, "turnover": 1000, "adj_factor": 1,
        "total_mv": 100, "circ_mv": 90, "pe_ttm": 10, "pb": 1,
        "turnover_rate": 2, "up_limit": 11, "down_limit": 9,
    }
    connection.executemany(
            """INSERT INTO dc_stock_daily_snapshot_version
               (id,ts_code,trade_date,payload_json,source_code,economic_available_time,observed_at,first_observed_at,created_at,content_hash,observation_provenance)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (1, "000001.SZ", trade_date, json.dumps(complete), "tushare", "2026-08-13T15:00:00+08:00", before, before, before, "before", "tushare:normalized"),
                # An economically late partial rebuild would make the date fail
                # if ranking were done before all four T15 clock filters.
                (2, "000001.SZ", trade_date, json.dumps({"close": 10}), "tushare", after, before, before, before, "after", "tushare:normalized"),
        ],
    )

    class Connection:
        _is_postgres = False

        def execute(self, sql, params=()):
            return connection.execute(sql, params)

    @contextmanager
    def open_connection():
        yield Connection()

    monkeypatch.setattr(oos_hard_data, "_conn", open_connection)
    monkeypatch.setattr(oos_hard_data, "MINIMUM_HARD_API_ROWS", 1)

    api_dates, evidence = oos_hard_data._strict_date_api_sets(trade_date, trade_date)
    snapshots = oos_hard_data._strict_snapshot_stats(trade_date, trade_date)

    assert api_dates["daily"] == {trade_date}
    assert evidence[("daily", trade_date)]["first_observed_at"] == before
    assert snapshots[trade_date]["rows_total"] == 1
    assert snapshots[trade_date]["market_rows"] == 1
    assert snapshots[trade_date]["max_observed_at"] == before


def test_strict_versions_exclude_blank_provenance_before_coverage_and_rank(monkeypatch):
    """Blank provenance cannot replace auditable evidence or satisfy OOS rows."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
            CREATE TABLE dc_tushare_raw_record_version(
                id INTEGER PRIMARY KEY, api_name TEXT, record_key TEXT, ts_code TEXT,
                trade_date TEXT, payload_json TEXT, economic_available_time TEXT,
                observed_at TEXT, first_observed_at TEXT, created_at TEXT,
                content_hash TEXT, observation_provenance TEXT
            );
            CREATE TABLE dc_stock_daily_snapshot_version(
                id INTEGER PRIMARY KEY, ts_code TEXT, trade_date TEXT, payload_json TEXT,
                source_code TEXT, economic_available_time TEXT, observed_at TEXT,
                first_observed_at TEXT, created_at TEXT, content_hash TEXT,
                observation_provenance TEXT
            );
    """)
    trade_date = "20260813"
    observed = "2026-08-13T14:30:00+08:00"
    connection.executemany(
            """INSERT INTO dc_tushare_raw_record_version
               (id,api_name,record_key,ts_code,trade_date,payload_json,economic_available_time,observed_at,first_observed_at,created_at,content_hash,observation_provenance)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (1, "daily", "daily:000001.SZ:20260813", "000001.SZ", trade_date, "{}", "2026-08-13T15:00:00+08:00", observed, observed, observed, "valid", "tushare:direct_fetch"),
                (2, "daily", "daily:000001.SZ:20260813", "000001.SZ", trade_date, "{}", "2026-08-13T15:00:00+08:00", observed, observed, observed, "blank", " "),
                (3, "daily", "daily:000002.SZ:20260813", "000002.SZ", trade_date, "{}", "2026-08-13T15:00:00+08:00", observed, observed, observed, "blank-only", ""),
        ],
    )
    fields = {
        "close": 10, "volume": 100, "turnover": 1000, "adj_factor": 1,
        "total_mv": 100, "circ_mv": 90, "pe_ttm": 10, "pb": 1,
        "turnover_rate": 2, "up_limit": 11, "down_limit": 9,
    }
    connection.executemany(
            """INSERT INTO dc_stock_daily_snapshot_version
               (id,ts_code,trade_date,payload_json,source_code,economic_available_time,observed_at,first_observed_at,created_at,content_hash,observation_provenance)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (1, "000001.SZ", trade_date, json.dumps(fields), "tushare", "2026-08-13T15:00:00+08:00", observed, observed, observed, "valid", "tushare:normalized"),
                (2, "000001.SZ", trade_date, json.dumps(fields), "tushare", "2026-08-13T15:00:00+08:00", observed, observed, observed, "blank", "  "),
                (3, "000002.SZ", trade_date, json.dumps(fields), "tushare", "2026-08-13T15:00:00+08:00", observed, observed, observed, "blank-only", ""),
        ],
    )

    class Connection:
        _is_postgres = False

        def execute(self, sql, params=()):
            return connection.execute(sql, params)

    @contextmanager
    def open_connection():
        yield Connection()

    monkeypatch.setattr(oos_hard_data, "_conn", open_connection)
    monkeypatch.setattr(oos_hard_data, "MINIMUM_HARD_API_ROWS", 1)
    monkeypatch.setattr(oos_hard_data, "MINIMUM_DAILY_ROWS", 1)

    api_dates, evidence = oos_hard_data._strict_date_api_sets(trade_date, trade_date)
    snapshots = oos_hard_data._strict_snapshot_stats(trade_date, trade_date)

    assert api_dates["daily"] == {trade_date}
    assert evidence[("daily", trade_date)]["point_in_time_rows"] == 1
    assert snapshots[trade_date]["rows_total"] == 1
    assert snapshots[trade_date]["market_rows"] == 1


def test_explicit_utc8_cutoff_accepts_close_and_rejects_late_or_naive_clock():
    cutoff = oos_hard_data._business_cutoff("20260814")

    assert cutoff == "2026-08-14T15:00:00+08:00"
    assert oos_hard_data._timestamp_at_or_before("2026-08-14T15:00:00+08:00", cutoff)
    assert oos_hard_data._timestamp_at_or_before("2026-08-14T07:00:00Z", cutoff)
    assert not oos_hard_data._timestamp_at_or_before("2026-08-14T15:00:01+08:00", cutoff)
    assert not oos_hard_data._timestamp_at_or_before("2026-08-14T15:00:00", cutoff)


def test_strict_sql_casts_timestamp_with_timezone_and_created_at():
    observed = oos_hard_data._timestamp_at_or_before_sql(
        "observed_at", oos_hard_data._cutoff_expression("trade_date"), is_postgres=True,
    )
    created = oos_hard_data._timestamp_at_or_before_sql(
        "created_at", oos_hard_data._cutoff_expression("trade_date"), is_postgres=True,
    )

    assert "::timestamptz" in observed
    assert "::timestamptz" in created
    assert "+08:00" in oos_hard_data._cutoff_expression("trade_date")


def test_late_snapshot_rebuild_is_non_repairable():
    details = [{"trade_date": "20260814", "eligible": False}]
    late = oos_hard_data._classify_snapshot_late_arrivals(details, {
        "20260814": {"raw_rows": 1000, "rows_total": 0, "late_rows": 1000},
    })

    assert late == {"20260814"}


def test_repairable_classification_allows_only_an_open_cutoff_and_rejects_historical_absence():
    current_gap = {
        "trade_date": "20260814",
        "missing_apis": ["adj_factor", "index_daily:000300.SH"],
        "trade_calendar_present": True,
    }
    repairable, allowed, permanently_late = oos_hard_data._classify_repairable_missing_apis(
        current_gap,
        strict_evidence_backend_available=True,
        historical_cutoff_elapsed=False,
    )
    assert repairable == ["adj_factor", "index_daily:000300.SH"]
    assert allowed is True
    assert permanently_late is False

    late_history = {
        "trade_date": "20200701",
        "missing_apis": ["daily", "index_daily:000300.SH"],
        "trade_calendar_present": True,
    }
    repairable, allowed, permanently_late = oos_hard_data._classify_repairable_missing_apis(
        late_history,
        late_hard_apis={"daily"},
        strict_evidence_backend_available=True,
        historical_cutoff_elapsed=True,
    )
    assert repairable == []
    assert allowed is False
    assert permanently_late is True

    absent_history = {
        "trade_date": "20200702",
        "missing_apis": ["daily", "index_daily:000300.SH"],
        "trade_calendar_present": True,
    }
    repairable, allowed, permanently_late = oos_hard_data._classify_repairable_missing_apis(
        absent_history,
        strict_evidence_backend_available=True,
        historical_cutoff_elapsed=True,
    )
    assert repairable == []
    assert allowed is False
    assert permanently_late is False

    calendar_only = {
        "trade_date": "20200701", "missing_apis": [], "trade_calendar_present": False,
    }
    assert oos_hard_data._classify_repairable_missing_apis(
        calendar_only,
        strict_evidence_backend_available=True,
    ) == ([], False, False)

    assert oos_hard_data._classify_repairable_missing_apis(
        absent_history,
        strict_evidence_backend_available=False,
        historical_cutoff_elapsed=True,
    ) == ([], False, False)


def test_repair_entry_rejects_non_repairable_or_unclassified_dates():
    result = oos_hard_data.repair_oos_hard_data_batch(
        ["20260814", "20260813"],
        date_gaps={
            "20260814": {"repairable": False, "late_arrival_apis": ["adj_factor"]},
        },
    )

    assert result["ok"] is False
    assert result["status"] == "no_repairable_dates"
    assert result["rejected_dates"] == {
        "20260814": "not_repairable_by_strict_pit_audit",
        "20260813": "missing_strict_audit_gap",
    }


def test_oos_hard_contract_requires_five_formal_windows(monkeypatch):
    assert oos_hard_data.REQUIRED_SESSIONS == 1537
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: FakeConn())
    monkeypatch.setattr(oos_hard_data, "_late_arrival_hard_api_sets", lambda _details: {})
    result = oos_hard_data.audit_oos_hard_data(end_date="20260722")
    assert result["status"] == "blocked"
    assert result["eligible_sessions"] == 0
    assert result["calendar_missing_sessions"] == 1535
    assert "historical_observation_evidence_missing" in result["dates"][1]["reasons"]
    assert result["hard_requirement"] is True


def test_oos_benchmark_set_filters_late_revisions_in_sql(monkeypatch):
    monkeypatch.setattr(
        oos_hard_data,
        "_strict_benchmark_date_set",
        lambda start, end: {"20260722"},
    )
    result = oos_hard_data._benchmark_date_set("20260722", "20260722")

    assert result == {"20260722"}


def test_oos_hard_api_sets_read_persisted_point_in_time_coverage(monkeypatch):
    queries: list[tuple[str, tuple]] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            queries.append((sql, params))
            return FakeRows([{"api_name": api, "trade_date": "20260722", "point_in_time_rows": 500} for api in oos_hard_data.HARD_APIS])

    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Conn())
    result = oos_hard_data._date_api_sets("20260722", "20260722")

    assert all(values == {"20260722"} for values in result.values())
    assert len(queries) == 1
    assert "dc_oos_hard_api_stats_live" in queries[0][0]


def test_late_complete_hard_slices_are_blocked_without_repair(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            assert "dc_tushare_raw_record_version" in sql
            assert "first_observed_at" in sql
            assert "economic_available_time" in sql
            return FakeRows([{
                "api_name": "daily", "trade_date": "20260722", "raw_rows": 1000,
                "point_in_time_rows": 0, "late_rows": 1000,
                "first_observed_at": "2026-07-23T09:00:00+08:00",
                "first_economic_available_time": "2026-07-23T09:00:00+08:00",
                "first_created_at": "2026-07-23T09:00:00+08:00",
            }])

    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Conn())
    late = oos_hard_data._late_arrival_hard_api_sets([{
        "trade_date": "20260722", "eligible": False, "missing_apis": ["daily"],
    }])
    assert late == {"20260722": {"daily"}}


def test_late_arrival_classification_uses_immutable_observation_versions(monkeypatch):
    queries: list[str] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            queries.append(sql)
            return FakeRows([])

    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Conn())
    assert oos_hard_data._late_arrival_hard_api_sets([{
        "trade_date": "20260722", "eligible": False, "missing_apis": ["daily"],
    }]) == {}
    assert "dc_tushare_raw_record_version" in queries[0]


def test_oos_asset_is_permanent_blocking_asset(monkeypatch):
    monkeypatch.setattr(oos_hard_data, "audit_oos_hard_data", lambda include_dates=False: {
        "consumer": "Champion", "window_start": "20200101", "window_end": "20260722",
        "eligible_sessions": 1000, "required_sessions": 1537, "coverage": .650618, "status": "blocked",
    })
    asset = oos_hard_data.oos_hard_data_asset()
    assert asset["key"] == "oos_hard_data"
    assert asset["hard_requirement"] is True
    assert asset["status"] == "partial"
    assert asset["weight"] == 100


def test_default_audit_keeps_last_complete_frozen_window_when_cutoff_is_open(monkeypatch):
    # The audit validates a real timezone-aware close timestamp, so use real
    # calendar dates rather than syntactically invalid YYYY0001 placeholders.
    anchor = date(2020, 1, 1)
    dates = [
        (anchor + timedelta(days=index)).strftime("%Y%m%d")
        for index in range(oos_hard_data.REQUIRED_SESSIONS + 1)
    ]
    pending = dates[-1]

    class RollingConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            if "FROM dc_trade_calendar" in sql:
                return FakeRows([{"cal_date": value} for value in reversed(dates)])
            if "FROM dc_stock_daily_snapshot" in sql:
                return FakeRows([
                    {
                        "trade_date": value,
                        "rows_total": 1000,
                        "market_rows": 950,
                        "min_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00+08:00",
                        "max_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00+08:00",
                    }
                    for value in dates[:-1]
                ])
            raise AssertionError(sql)

    complete = set(dates[:-1])
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: RollingConn())
    monkeypatch.setattr(oos_hard_data, "target_trade_date", lambda conn: pending)
    monkeypatch.setattr(
        oos_hard_data,
        "_trade_cutoff_elapsed",
        lambda trade_date: trade_date != pending,
    )
    monkeypatch.setattr(
        oos_hard_data,
        "_date_api_sets",
        lambda start, end: {api: set(complete) for api in oos_hard_data.HARD_APIS},
    )
    monkeypatch.setattr(
        oos_hard_data,
        "_benchmark_date_set",
        lambda start, end: set(complete),
    )
    monkeypatch.setattr(
        oos_hard_data,
        "_strict_date_api_sets",
        lambda start, end: (
            {api: set(complete) for api in oos_hard_data.HARD_APIS},
            {(api, value): {"point_in_time_rows": 1000, "raw_rows": 1000,
                            "first_observed_at": f"{value[:4]}-{value[4:6]}-{value[6:]}T14:00:00+08:00"}
             for api in oos_hard_data.HARD_APIS for value in complete},
        ),
    )
    monkeypatch.setattr(oos_hard_data, "_strict_benchmark_date_set", lambda start, end: set(complete))
    monkeypatch.setattr(oos_hard_data, "_strict_snapshot_stats", lambda start, end: {
        value: {"rows_total": 1000, "market_rows": 950,
                "min_observed_at": f"{value[:4]}-{value[4:6]}-{value[6:]}T14:00:00+08:00",
                "max_observed_at": f"{value[:4]}-{value[4:6]}-{value[6:]}T14:00:00+08:00"}
        for value in complete
    })
    monkeypatch.setattr(oos_hard_data, "_late_arrival_hard_api_sets", lambda _details: {})
    result = oos_hard_data.audit_oos_hard_data(include_dates=False)
    assert result["status"] == "ready"
    assert result["window_end"] == dates[-2]
    assert result["trailing_pending_dates"] == [pending]
    assert result["eligible_sessions"] == oos_hard_data.REQUIRED_SESSIONS


def test_default_audit_keeps_elapsed_historical_evidence_gaps_visible(monkeypatch):
    dates = ["20200102", "20200103"]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            if "FROM dc_trade_calendar" in sql:
                return FakeRows([{"cal_date": value} for value in reversed(dates)])
            if "FROM dc_stock_daily_snapshot" in sql:
                return FakeRows([])
            raise AssertionError(sql)

    monkeypatch.setattr(oos_hard_data, "REQUIRED_SESSIONS", 2)
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Connection())
    monkeypatch.setattr(oos_hard_data, "target_trade_date", lambda _conn: dates[-1])
    monkeypatch.setattr(
        oos_hard_data,
        "_strict_date_api_sets",
        lambda *_args: ({api: set() for api in oos_hard_data.HARD_APIS}, {}),
    )
    monkeypatch.setattr(oos_hard_data, "_strict_observation_schema_available", lambda: True)
    monkeypatch.setattr(oos_hard_data, "_strict_benchmark_with_evidence", lambda *_args: (set(), {}))
    monkeypatch.setattr(oos_hard_data, "_strict_snapshot_stats", lambda *_args: {})
    monkeypatch.setattr(oos_hard_data, "_late_arrival_hard_api_sets", lambda _details: {})

    result = oos_hard_data.audit_oos_hard_data(include_dates=False)

    assert result["status"] == "blocked"
    assert result["trailing_pending_dates"] == []
    assert result["non_repairable_dates"] == dates


def test_compact_audit_returns_nonrepairable_count_with_bounded_date_samples(monkeypatch):
    dates = ["20200102", "20200103", "20200106"]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            if "FROM dc_trade_calendar" in sql:
                return FakeRows([{"cal_date": value} for value in reversed(dates)])
            if "FROM dc_stock_daily_snapshot" in sql:
                return FakeRows([])
            raise AssertionError(sql)

    monkeypatch.setattr(oos_hard_data, "REQUIRED_SESSIONS", 3)
    monkeypatch.setattr(oos_hard_data, "MAX_COMPACT_NON_REPAIRABLE_DATE_SAMPLES", 2)
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Connection())
    monkeypatch.setattr(oos_hard_data, "target_trade_date", lambda _conn: dates[-1])
    monkeypatch.setattr(
        oos_hard_data,
        "_strict_date_api_sets",
        lambda *_args: ({api: set() for api in oos_hard_data.HARD_APIS}, {}),
    )
    monkeypatch.setattr(oos_hard_data, "_strict_observation_schema_available", lambda: True)
    monkeypatch.setattr(oos_hard_data, "_strict_benchmark_with_evidence", lambda *_args: (set(), {}))
    monkeypatch.setattr(oos_hard_data, "_strict_snapshot_stats", lambda *_args: {})
    monkeypatch.setattr(oos_hard_data, "_late_arrival_hard_api_sets", lambda _details: {})

    result = oos_hard_data.audit_oos_hard_data(include_dates=False)

    assert result["non_repairable_dates"] == dates[:2]
    assert result["non_repairable_sessions"] == 3
    assert result["non_repairable_dates_truncated"] is True

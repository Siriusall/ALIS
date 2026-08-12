from __future__ import annotations

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


def test_oos_hard_contract_requires_five_formal_windows(monkeypatch):
    assert oos_hard_data.REQUIRED_SESSIONS == 1537
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: FakeConn())
    result = oos_hard_data.audit_oos_hard_data(end_date="20260722")
    assert result["status"] == "blocked"
    assert result["eligible_sessions"] == 1
    assert result["calendar_missing_sessions"] == 1535
    assert result["dates"][1]["missing_apis"] == ["adj_factor"]
    assert result["hard_requirement"] is True


def test_oos_benchmark_set_filters_late_revisions_in_sql(monkeypatch):
    queries: list[tuple[str, tuple]] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            queries.append((sql, params))
            # Only the close-time record remains after the SQL filter.
            return FakeRows([{
                "trade_date": "20260722",
                "max_available_time": "2026-07-22T15:00:00",
            }])

    monkeypatch.setattr(oos_hard_data, "_conn", lambda: Conn())
    result = oos_hard_data._benchmark_date_set("20260722", "20260722")

    assert result == {"20260722"}
    assert "available_time<=" in queries[0][0]


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


def test_default_audit_keeps_last_complete_frozen_window_when_today_is_pending(monkeypatch):
    dates = [f"2025{index:04d}" for index in range(1, oos_hard_data.REQUIRED_SESSIONS + 2)]
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
                        "min_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00",
                        "max_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00",
                    }
                    for value in dates[:-1]
                ])
            raise AssertionError(sql)

    complete = set(dates[:-1])
    monkeypatch.setattr(oos_hard_data, "_conn", lambda: RollingConn())
    monkeypatch.setattr(oos_hard_data, "target_trade_date", lambda conn: pending)
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
    result = oos_hard_data.audit_oos_hard_data(include_dates=False)
    assert result["status"] == "ready"
    assert result["window_end"] == dates[-2]
    assert result["trailing_pending_dates"] == [pending]
    assert result["eligible_sessions"] == oos_hard_data.REQUIRED_SESSIONS

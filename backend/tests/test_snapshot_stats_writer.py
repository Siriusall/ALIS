from app.data_platform.services.data_center import _refresh_snapshot_stats


def test_snapshot_stats_uses_live_table_when_legacy_is_read_only():
    calls = []

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        _is_postgres = True

        def execute(self, sql, params=()):
            calls.append((sql, params))
            if "has_table_privilege" in sql:
                return Result({"can_write": False})
            if "FROM dc_stock_daily_snapshot" in sql:
                return Result({
                    "rows_total": 5539, "market_rows": 5539,
                    "min_available_time": "2026-08-11T15:00:00",
                    "max_available_time": "2026-08-11T15:00:00",
                })
            return Result(None)

    _refresh_snapshot_stats(Connection(), "20260811", "2026-08-11T17:00:00")

    write_sql = calls[-1][0]
    assert "INSERT INTO dc_stock_daily_snapshot_stats_live" in write_sql

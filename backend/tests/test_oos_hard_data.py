from __future__ import annotations

from app.data_platform.services import oos_hard_data


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
        if "FROM dc_tushare_raw_records" in sql or "FROM dc_tushare_request_coverage" in sql:
            api = params[0] if params[0] in oos_hard_data.HARD_APIS else params[-1]
            dates = ["20260721", "20260722"] if api != "adj_factor" else ["20260721"]
            return FakeRows([{
                "trade_date": value, "row_count": 1000,
                "max_available_time": f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00",
            } for value in dates])
        if "FROM dc_trade_calendar" in sql:
            return FakeRows([{"cal_date": "20260722"}, {"cal_date": "20260721"}])
        if "FROM dc_stock_daily_snapshot" in sql:
            return FakeRows([
                {"trade_date": "20260721", "rows_total": 1000, "market_rows": 950, "min_available_time": "2026-07-21T15:00:00", "max_available_time": "2026-07-21T15:00:00"},
                {"trade_date": "20260722", "rows_total": 1000, "market_rows": 950, "min_available_time": "2026-07-22T15:00:00", "max_available_time": "2026-07-22T15:00:00"},
            ])
        raise AssertionError(sql)


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

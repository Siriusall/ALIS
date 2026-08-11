from app.services.theme_revenue_exposure import (
    _company_exposures,
    _execute_many,
    _initial_company_rows,
    _latest_segment_rows,
    _matching_themes,
)


def test_execute_many_uses_public_raw_connection_contract():
    calls = []

    class Cursor:
        def executemany(self, sql, rows):
            calls.append((sql, rows))

    class RawConnection:
        def cursor(self):
            return Cursor()

    class Connection:
        _is_postgres = True
        raw = RawConnection()

    _execute_many(Connection(), "INSERT INTO sample VALUES(?,?)", [(1, 2)])

    assert calls == [("INSERT INTO sample VALUES(%s,%s)", [(1, 2)])]


def test_business_segment_theme_matching_keeps_multiple_supported_themes():
    themes = _matching_themes("人工智能芯片及集成电路产品")
    assert "人工智能" in themes
    assert "半导体" in themes


def test_business_segment_theme_matching_ignores_unrelated_text():
    assert _matching_themes("物业管理服务") == []


def test_company_exposure_is_deterministic_and_uses_latest_period_segments():
    rows = [
        {"id": 2, "period": "20251231", "available_time": "2026-03-01T09:00:00", "payload_json": '{"bz_item":"人工智能芯片","bz_sales":60}'},
        {"id": 3, "period": "20251231", "available_time": "2026-03-01T09:00:00", "payload_json": '{"bz_item":"物业服务","bz_sales":40}'},
    ]

    exposures, fingerprint = _company_exposures(rows)
    same_exposures, same_fingerprint = _company_exposures(rows)

    assert exposures["revenue:人工智能"]["revenue_share"] == 0.6
    assert exposures["revenue:半导体"]["matched_sales"] == 60.0
    assert fingerprint == same_fingerprint
    assert exposures == same_exposures


def test_initial_company_discovery_uses_bounded_distinct_index_query():
    captured = {}

    class Cursor:
        def fetchall(self):
            return [{"ts_code": "000001.SZ"}]

    class Connection:
        def execute(self, sql, params):
            captured.update({"sql": sql, "params": params})
            return Cursor()

    rows = _initial_company_rows(Connection(), limit=8000)

    assert rows == [{"ts_code": "000001.SZ"}]
    assert "SELECT DISTINCT ts_code" in captured["sql"]
    assert "GROUP BY" not in captured["sql"]
    assert captured["params"] == (8001,)


def test_latest_segments_are_split_into_bounded_company_batches():
    calls = []

    class Cursor:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Cursor()

    _latest_segment_rows(
        Connection(), [f"{index:06d}.SZ" for index in range(501)],
        cutoff="2026-08-11T15:00:00", batch_size=250,
    )

    assert len(calls) == 3
    assert [len(params) for _sql, params in calls] == [252, 252, 3]

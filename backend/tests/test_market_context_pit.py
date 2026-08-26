from __future__ import annotations

from contextlib import contextmanager
import json

from app.services import market_context


def test_financial_enrichment_forwards_bounded_period_read(monkeypatch) -> None:
    calls: list[dict] = []

    def bounded_loader(_conn, _api_name, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(market_context, "load_raw_record_versions", bounded_loader)

    result = market_context._strict_raw_payload_rows(
        object(), "fina_indicator", cutoff="2026-08-14T15:00:00+08:00", period_limit=2,
    )

    assert result == []
    assert calls == [{
        "cutoff": "2026-08-14T15:00:00+08:00",
        "ts_codes": None,
        "cutoff_by_trade_date": False,
        "trade_date_start": "",
        "trade_date_end": "",
        "period_limit": 2,
    }]


def test_market_training_requires_trade_date_pit_cutoff(monkeypatch) -> None:
    calls: list[dict] = []

    def strict_rows(_conn, _api_name, **kwargs):
        calls.append(kwargs)
        return None

    @contextmanager
    def fake_conn():
        yield object()

    monkeypatch.setattr(market_context, "_read_conn", fake_conn)
    monkeypatch.setattr(market_context, "load_raw_record_versions", strict_rows)

    result = market_context.materialize_market_training_history()

    assert calls == [{"ts_codes": ["000300.SH", "000905.SH", "000852.SH"], "cutoff_by_trade_date": True}]
    assert result == {
        "ok": False,
        "status": "blocked",
        "reason": "observation_clock_unavailable",
        "written": 0,
    }


def test_market_training_uses_accepted_snapshot_hash_before_aggregation(monkeypatch) -> None:
    """The full-market aggregate must not rank the raw version archive."""
    statements: list[str] = []

    class Result:
        def fetchall(self):
            return []

    class Connection:
        _is_postgres = False

        def execute(self, sql, _params=()):
            statements.append(sql)
            if "FROM dc_stock_daily_snapshot p" in sql:
                return Result()
            raise AssertionError(f"unexpected query: {sql}")

    @contextmanager
    def fake_conn():
        yield Connection()

    clock_columns = {
        "id", "ts_code", "trade_date", "payload_json", "source_code",
        "economic_available_time", "observed_at", "first_observed_at",
        "observation_provenance", "content_hash", "created_at",
    }
    monkeypatch.setattr(market_context, "_read_conn", fake_conn)
    monkeypatch.setattr(
        market_context,
        "_table_columns",
        lambda _conn, table: (
            clock_columns if table == "dc_stock_daily_snapshot_version"
            else {"ts_code", "trade_date", "content_hash"}
            if table == "dc_stock_daily_snapshot" else set()
        ),
    )
    monkeypatch.setattr(
        market_context,
        "load_raw_record_versions",
        lambda *_args, **_kwargs: [
            {"ts_code": code, "trade_date": "20260814", "payload_json": json.dumps({"close": 10})}
            for code in ("000300.SH", "000905.SH", "000852.SH")
        ],
    )

    result = market_context.materialize_market_training_history()

    assert result["market_sessions"] == 0
    aggregate_sql = next(sql for sql in statements if "FROM dc_stock_daily_snapshot p" in sql)
    assert "v.content_hash=p.content_hash" in aggregate_sql
    assert "ROW_NUMBER() OVER" not in aggregate_sql


def test_history_rebuild_blocks_instead_of_reading_mutable_raw_projection(monkeypatch) -> None:
    class Result:
        def fetchall(self):
            return [{"trade_date": "20260814"}, {"trade_date": "20260813"}]

    class Connection:
        def execute(self, sql, _params=()):
            assert "dc_available_trade_dates" in sql
            return Result()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(market_context, "_conn", fake_conn)
    monkeypatch.setattr(market_context, "_strict_raw_payload_rows", lambda *_args, **_kwargs: None)

    result = market_context.rebuild_market_context_history()

    assert result == {
        "ok": False,
        "status": "blocked",
        "reason": "observation_clock_unavailable",
        "dates_considered": 0,
        "snapshots_written": 0,
    }


def test_market_context_as_of_rejects_naive_cutoff_and_naive_snapshot_clock(monkeypatch) -> None:
    class Result:
        def fetchall(self):
            return [{
                "trade_date": "20260814",
                "result_json": json.dumps({"ok": True, "market_state": {"state": "S1"}}),
                "model_version": "transparent_four_state_v1",
                "available_time": "2026-08-14T14:59:00",
                "created_at": "2026-08-14T14:59:00",
                "updated_at": "2026-08-14T14:59:00",
            }]

    class Connection:
        def execute(self, _sql, _params=()):
            return Result()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(market_context, "_conn", fake_conn)

    invalid_cutoff = market_context.market_context_as_of("2026-08-14T15:00:00")
    assert invalid_cutoff["status"] == "blocked"
    assert invalid_cutoff["reason"] == "invalid_market_context_observation_cutoff"

    invalid_snapshot = market_context.market_context_as_of("2026-08-14T15:00:00+08:00")
    assert invalid_snapshot["status"] == "blocked"
    assert invalid_snapshot["reason"] == "no_market_context_visible_at_cutoff"


def test_live_market_context_rebuild_uses_immutable_aggregates_after_cache_miss(monkeypatch) -> None:
    dates = [f"202608{day:02d}" for day in range(14, 8, -1)]
    executed: list[str] = []

    class Result:
        def __init__(self, rows=None, row=None):
            self._rows = rows or []
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, _params=()):
            executed.append(sql)
            if "FROM dc_available_trade_dates" in sql and "LIMIT 6" in sql:
                return Result(rows=[{"trade_date": value} for value in dates])
            if "FROM dc_market_context_snapshot" in sql and "result_json" in sql:
                return Result(row=None)
            if "FROM dc_stock_master" in sql:
                return Result(row={"cnt": 0})
            if "FROM dc_market_context_snapshot" in sql and "market_state" in sql:
                return Result(row=None)
            if "FROM dc_market_context_snapshot" in sql and "observations_json" in sql:
                return Result(rows=[])
            return Result(row=None)

    @contextmanager
    def fake_conn():
        yield Connection()

    aggregates = {
        date: {
            "trade_date": date,
            "rows_total": 1,
            "market_rows": 1,
            "breadth": 1.0,
            "average_return": 0.01,
            "amount": 100.0,
            "limit_up_ratio": 0.0,
            "limit_down_ratio": 0.0,
        }
        for date in dates
    }
    monkeypatch.setattr(market_context, "_read_conn", fake_conn)
    monkeypatch.setattr(market_context, "target_trade_date", lambda _conn: dates[0])
    monkeypatch.setattr(market_context, "get_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(market_context, "load_snapshot_market_aggregates", lambda *_args, **_kwargs: aggregates)
    monkeypatch.setattr(
        market_context,
        "load_snapshot_versions",
        lambda *_args, **_kwargs: [
            {"payload_json": json.dumps({"turnover": 100.0})},
        ],
    )
    monkeypatch.setattr(
        market_context,
        "_market_enrichment_observations",
        lambda _date: {"coverage": {}},
    )

    result = market_context.live_market_context()

    assert result["ok"] is True
    assert result["trade_date"] == dates[0]
    assert result["observations"]["breadth"] == 1.0
    assert not any("FROM dc_stock_daily_snapshot " in sql for sql in executed)


def test_live_market_context_reuses_valid_durable_snapshot_after_redis_miss(monkeypatch) -> None:
    dates = ["20260814", "20260813"]
    snapshot_time = "2026-08-14T16:01:00+08:00"
    payload = {
        "ok": True,
        "status": "ready",
        "trade_date": dates[0],
        "available_time": snapshot_time,
        "market_state": {"state": "S1"},
    }
    redis_writes: list[tuple[str, dict, int]] = []

    class Result:
        def __init__(self, *, rows=None, row=None):
            self._rows = rows or []
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, sql, _params=()):
            if "FROM dc_available_trade_dates" in sql and "LIMIT 6" in sql:
                return Result(rows=[{"trade_date": value} for value in dates])
            if "FROM dc_market_context_snapshot" in sql and "result_json,created_at,updated_at" in sql:
                return Result(row={
                    "result_json": json.dumps(payload),
                    "created_at": snapshot_time,
                    "updated_at": snapshot_time,
                })
            if "SELECT updated_at FROM dc_available_trade_dates" in sql:
                return Result(row={"updated_at": "2026-08-14T16:00:00+08:00"})
            raise AssertionError(f"durable market-context cache should avoid rebuild query: {sql}")

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(market_context, "_read_conn", fake_conn)
    monkeypatch.setattr(market_context, "target_trade_date", lambda _conn: dates[0])
    monkeypatch.setattr(market_context, "get_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        market_context,
        "set_json",
        lambda key, value, ttl: redis_writes.append((key, value, ttl)),
    )
    monkeypatch.setattr(
        market_context,
        "load_snapshot_market_aggregates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must use durable snapshot")),
    )

    result = market_context.live_market_context()

    assert result == payload
    assert redis_writes == [("alis:market-context:live:v5", payload, 300)]


def test_live_market_context_rejects_stale_cache_when_target_session_is_missing(monkeypatch) -> None:
    class Result:
        def fetchall(self):
            return [{"trade_date": "20260814"}, {"trade_date": "20260813"}]

    class Connection:
        def execute(self, sql, _params=()):
            assert "dc_available_trade_dates" in sql
            return Result()

    @contextmanager
    def fake_conn():
        yield Connection()

    monkeypatch.setattr(market_context, "_read_conn", fake_conn)
    monkeypatch.setattr(market_context, "target_trade_date", lambda _conn: "20260815")
    monkeypatch.setattr(
        market_context,
        "get_json",
        lambda *_args, **_kwargs: {"ok": True, "trade_date": "20260814", "market_state": {"state": "S1"}},
    )

    result = market_context.live_market_context()

    assert result == {
        "ok": False,
        "status": "blocked",
        "reason": "target_market_context_snapshot_missing",
        "trade_date": "20260815",
        "latest_available_trade_date": "20260814",
    }

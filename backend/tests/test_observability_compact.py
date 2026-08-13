from app.data_platform.services import observability


def test_compact_observability_omits_ingestion_run_meta(monkeypatch):
    class Result:
        def __init__(self, one=None, rows=None):
            self.one = one
            self.rows = rows or []
        def fetchone(self):
            return self.one
        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, _params=()):
            if "dc_stock_daily_snapshot" in sql:
                return Result({"total": 1, "quality_pass": 1, "avg_quality": 100})
            if "data_ingestion_runs ORDER BY" in sql:
                return Result(rows=[{"id": 1, "meta_json": '{"large":"omit"}'}])
            if "tushare_api_catalog" in sql:
                return Result(rows=[])
            if "source_code,COUNT" in sql:
                return Result(rows=[])
            return Result(rows=[])
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(observability, "_conn", lambda: Conn())
    monkeypatch.setattr(observability, "target_trade_date", lambda _c: "20260812")
    monkeypatch.setattr(observability, "_quality_audit", lambda _c: {})
    monkeypatch.setattr(observability, "list_runtime_logs", lambda _limit: [])
    monkeypatch.setattr(observability, "redis_health", lambda: {})
    monkeypatch.setattr(observability, "stream_health", lambda: {})

    full = observability.get_observability()
    compact = observability.get_observability(compact=True)

    assert full["logs"] == [{"id": 1, "meta": {"large": "omit"}}]
    assert compact["logs"] == []

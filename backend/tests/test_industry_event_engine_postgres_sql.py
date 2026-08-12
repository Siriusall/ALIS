from app.services import industry_event_engine as engine


class _Rows(list):
    def fetchall(self):
        return self


class _Connection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "dc_industry_state_snapshot" in sql:
            return _Rows([{
                "industry_name": "电子", "as_of_date": "20260811", "model_version": "v1",
                "state_json": "{}", "evidence_json": "{}", "available_time": "2026-08-11T15:00:00",
            }])
        if "dc_industry_metric_definition" in sql:
            return _Rows([])
        if "dc_stock_master" in sql:
            return _Rows([])
        raise AssertionError(sql)


def test_materialized_industry_snapshot_parameterizes_percent_like_pattern(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(engine, "_conn", lambda: conn)

    result = engine._latest_industry_snapshot("20260811")

    assert result and result["ok"] is True
    definitions = next((call for call in conn.calls if "dc_industry_metric_definition" in call[0]), None)
    assert definitions is not None
    assert "NOT LIKE ?" in definitions[0]
    assert definitions[1] == ("%transmission_direction=%",)

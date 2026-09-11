from app.services import industry_event_engine as engine


def test_current_industries_observe_updates_from_another_worker(monkeypatch):
    current = {"date": "20260828"}
    monkeypatch.setattr(engine, "_latest_industry_snapshot", lambda *a, **k: {
        "ok": True, "items": [{"state_as_of_date": current["date"]}],
    })
    engine.clear_industry_cache()
    for compact in (False, True):
        current["date"] = "20260828"
        assert engine.list_industries(compact=compact)["items"][0]["state_as_of_date"] == "20260828"
        current["date"] = "20260911"
        assert engine.list_industries(compact=compact)["items"][0]["state_as_of_date"] == "20260911"


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


def test_materialized_industry_snapshot_blocks_late_observation_for_formal_cutoff(monkeypatch):
    conn = _Connection()
    conn.execute = lambda sql, params=(): (
        conn.calls.append((sql, params)) or _Rows([{
            "industry_name": "电子", "as_of_date": "20260811", "model_version": "v1",
            "state_json": "{}", "evidence_json": "{}",
            "available_time": "2026-08-11T16:00:00+08:00",
            "created_at": "2026-08-11T16:00:00+08:00",
            "updated_at": "2026-08-11T16:00:00+08:00",
        }]) if "dc_industry_state_snapshot" in sql else (
            _Rows([]) if "dc_industry_metric_definition" in sql or "dc_stock_master" in sql
            else (_ for _ in ()).throw(AssertionError(sql))
        )
    )
    monkeypatch.setattr(engine, "_conn", lambda: conn)

    result = engine._latest_industry_snapshot(
        "20260811", as_of_time="2026-08-11T15:00:00+08:00",
    )

    assert result is None


def test_list_industries_does_not_fallback_to_mutable_metrics_at_cutoff(monkeypatch):
    monkeypatch.setattr(engine, "_latest_industry_snapshot", lambda *args, **kwargs: None)
    result = engine._list_industries_cached("2026-08-11T15:00:00+08:00", False)
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "no_industry_state_visible_at_cutoff"

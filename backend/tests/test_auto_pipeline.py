from app.services import auto_pipeline


def test_daily_maintenance_surfaces_internal_position_failures(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001.SZ"}])
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **kwargs: {"ok": True, "theses_created": 0})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *args, **kwargs: {"ok": False, "error": "missing evidence"})
    monkeypatch.setattr(auto_pipeline, "transition_position", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("state write failed")))

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["error_count"] == 2
    assert any("position_thesis_000001.SZ" in value for value in result["errors"])
    assert any("position_state_000001.SZ" in value for value in result["errors"])


def test_daily_maintenance_discards_failed_exchange_guess_after_success(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001"}])

    def research(code, **_kwargs):
        if code.endswith(".SH"):
            raise RuntimeError("not listed here")
        return {"ok": True}

    monkeypatch.setattr(auto_pipeline, "build_stock_research", research)
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **kwargs: {"ok": True, "theses_created": 0})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *args, **kwargs: {"ok": True})
    transitions = []
    monkeypatch.setattr(auto_pipeline, "transition_position", lambda **kwargs: transitions.append(kwargs))

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["results"]["research"] == 1
    assert transitions[0]["ts_code"] == "000001.SZ"

from app.services import auto_pipeline


def test_daily_maintenance_surfaces_internal_position_failures(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001.SZ"}])
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **kwargs: {"ok": True, "theses_created": 0})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *args, **kwargs: {"ok": False, "error": "missing evidence"})
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state audit failed")))

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["error_count"] == 2
    assert any("position_thesis_000001.SZ" in value for value in result["errors"])
    assert any("position_state_audit_000001.SZ" in value for value in result["errors"])


def test_daily_maintenance_discards_failed_exchange_guess_after_success(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001"}])

    def research(code, **_kwargs):
        if code.endswith(".SH"):
            raise RuntimeError("not listed here")
        return {"ok": True}

    monkeypatch.setattr(auto_pipeline, "build_stock_research", research)
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **kwargs: {"ok": True, "theses_created": 0})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda _code: {"state": "holding"})

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["results"]["research"] == 1
    assert result["results"]["position_states"] == 1


def test_daily_maintenance_preserves_north_exchange_code(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "430001"}])
    seen = []
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda code, **_: seen.append(code) or {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **_: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda code, **_: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda _code: {"state": "holding"})
    result = auto_pipeline.run_daily_maintenance()
    assert result["ok"] is True
    assert seen == ["430001.BJ"]
    assert result["results"]["position_states"] == 1


def test_daily_maintenance_skips_etf_stock_research_but_keeps_position_state(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "510880.SH"}])
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ETF must not use stock research")))
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ETF must not create stock thesis")))
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **_kwargs: {"ok": True, "generated_or_reviewed": []})
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda _code: {"state": "holding"})

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is True
    assert result["results"]["skipped_etf_research"] == 1
    assert result["results"]["research"] == 0
    assert result["results"]["position_theses"] == 0
    assert result["results"]["position_states"] == 1


def test_daily_maintenance_only_audits_missing_position_state(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001.SZ"}])
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **_kwargs: {"ok": True, "generated_or_reviewed": []})
    monkeypatch.setattr(auto_pipeline, "generate_thesis_for_stock", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda _code: None)

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is True
    assert result["results"]["position_states"] == 0
    assert result["results"]["missing_position_states"] == 1


def test_daily_maintenance_records_risk_veto_without_failing(monkeypatch):
    monkeypatch.setattr(auto_pipeline, "get_positions", lambda: [{"symbol": "000001.SZ"}])
    monkeypatch.setattr(auto_pipeline, "build_stock_research", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(auto_pipeline, "generate_watch_pool_theses", lambda **_kwargs: {"ok": True, "generated_or_reviewed": []})
    monkeypatch.setattr(
        auto_pipeline,
        "generate_thesis_for_stock",
        lambda *_args, **_kwargs: {"ok": False, "status": "blocked", "reason": "risk_hard_veto"},
    )
    monkeypatch.setattr(auto_pipeline, "get_position_state", lambda _code: {"state": "holding"})

    result = auto_pipeline.run_daily_maintenance()

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["results"]["blocked_position_theses"] == 1

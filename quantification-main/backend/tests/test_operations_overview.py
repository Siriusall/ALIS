from app.services import operations_overview


def test_governance_overview_degrades_one_failed_domain(monkeypatch):
    monkeypatch.setattr(operations_overview, "build_data_health_report", lambda **_: {"ok": True, "issues": []})
    monkeypatch.setattr(operations_overview, "_cached_readiness_overview", lambda: (_ for _ in ()).throw(RuntimeError("redis unavailable")))
    monkeypatch.setattr(operations_overview, "get_observability", lambda **_: {"logs": []})
    monkeypatch.setattr(operations_overview, "get_model_status", lambda: {"active_version": {"version": "v1"}, "latest_train_run": None})
    monkeypatch.setattr(operations_overview, "capability_registry", lambda: [{"status": "ready"}])
    monkeypatch.setattr(operations_overview, "list_quant_run_tasks", lambda **_: [])
    monkeypatch.setattr(operations_overview, "list_model_versions", lambda **_: [])

    result = operations_overview.build_governance_overview()

    assert result["ok"] is False
    assert result["errors"] == [{"domain": "data_readiness", "error": "redis unavailable"}]
    readiness_gate = next(item for item in result["gates"] if item["key"] == "data_readiness")
    assert readiness_gate["status"] == "unknown"

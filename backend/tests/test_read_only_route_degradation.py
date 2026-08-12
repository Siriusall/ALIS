from app.api.routes import model_strategy, research


def test_evidence_verification_degrades_on_database_failure(monkeypatch):
    def fail(_snapshot_id):
        raise TimeoutError("database connection timeout")

    monkeypatch.setattr(research, "verify_decision_snapshot", fail)

    result = research.research_evidence_verify(47)

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert result["component"] == "decision_evidence_verification"
    assert result["id"] == 47
    assert result["verified"] is False
    assert result["error"]["code"] == "database_query_timeout"


def test_market_context_history_degrades_on_database_failure(monkeypatch):
    def fail(*, limit):
        raise ConnectionError("database connection refused")

    monkeypatch.setattr(model_strategy, "market_context_history", fail)

    result = model_strategy.model_strategy_market_context_history(120)

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert result["component"] == "market_context_history"
    assert result["items"] == []
    assert result["transitions"] == []
    assert result["count"] == 0
    assert result["error"]["code"] == "database_unavailable"


def test_research_data_assets_degrades_without_http_500(monkeypatch):
    from app.services import research_data_assets

    def fail():
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(research_data_assets, "audit_research_data_assets", fail)

    result = model_strategy.model_strategy_research_data_assets()

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert result["data_available"] is False
    assert result["component"] == "research_data_assets"
    assert result["error"]["code"] == "database_query_timeout"


def test_research_get_routes_explicitly_disable_persistence(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        research,
        "build_stock_research",
        lambda code, **kwargs: calls.setdefault("stock", (code, kwargs)) or {},
    )
    monkeypatch.setattr(
        research,
        "build_expectation_state",
        lambda code, **kwargs: calls.setdefault("expectation", (code, kwargs)) or {},
    )
    monkeypatch.setattr(
        research,
        "build_valuation_state",
        lambda code, **kwargs: calls.setdefault("valuation", (code, kwargs)) or {},
    )
    monkeypatch.setattr(
        research,
        "evaluate_core_pool_eligibility",
        lambda code, **kwargs: calls.setdefault("eligibility", (code, kwargs)) or {},
    )

    research.research_stock("000001.SZ")
    research.research_expectation("000001.SZ")
    research.research_valuation("000001.SZ")
    research.research_pool_eligibility("000001.SZ")

    assert calls["stock"][1] == {"persist": False}
    assert calls["expectation"][1] == {"persist_baseline": False}
    assert calls["valuation"][1] == {"persist": False}
    assert calls["eligibility"][1] == {"persist_research": False}


def test_model_status_get_routes_disable_governance_and_snapshot_writes(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        model_strategy,
        "build_auto_optimization_status",
        lambda **kwargs: calls.setdefault("optimizer", kwargs) or {},
    )
    monkeypatch.setattr(
        model_strategy,
        "live_market_context",
        lambda **kwargs: calls.setdefault("market", kwargs) or {"ok": True},
    )

    model_strategy.model_strategy_auto_optimization()
    model_strategy.model_strategy_live_market_context()

    assert calls == {
        "optimizer": {"apply_auto_reject": False},
        "market": {"persist": False},
    }
def test_plan_model_console_projection_keeps_governance_summary_without_fold_payload(monkeypatch):
    payload = {
        "items": [{
            "model_key": "medium_horizon_lambdarank_v1",
            "name": "中期排序",
            "latest_version": {
                "id": 216,
                "version": "candidate-v216",
                "status": "candidate",
                "metrics": {
                    "task_native_oos": {"window_count": 5, "passed_windows": 5},
                    "strategy_impact_ab": {"status": "passed", "windows": [{"fold": 1}]},
                    "walk_forward_evidence": [{"large": "omit"}],
                },
                "validation": {
                    "status": "pending_live_observation",
                    "model_shadow": {"status": "observing", "observations": [{"large": "omit"}]},
                },
            },
            "versions": [{"large": "omit"}],
        }],
    }
    monkeypatch.setattr(model_strategy, "model_family_status", lambda: payload)

    result = model_strategy.model_strategy_plan_models(console=True)
    version = result["items"][0]["latest_version"]

    assert version["id"] == 216
    assert version["metrics"]["task_native_oos"]["passed_windows"] == 5
    assert version["metrics"]["strategy_impact_ab"]["windows"] == [{"fold": 1}]
    assert "walk_forward_evidence" not in version["metrics"]
    assert "observations" not in version["validation"]["model_shadow"]
    assert "versions" not in result["items"][0]


def test_full_plan_model_route_keeps_complete_version_evidence(monkeypatch):
    payload = {"items": [{"model_key": "m", "latest_version": {"metrics": {"walk_forward_evidence": [1]}}}]}
    monkeypatch.setattr(model_strategy, "model_family_status", lambda: payload)

    assert model_strategy.model_strategy_plan_models(console=False) == payload

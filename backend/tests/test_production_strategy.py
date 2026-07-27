from __future__ import annotations

from app.services import production_strategy


def _bundle() -> dict:
    return {
        "schema_version": production_strategy.CHAMPION_SCHEMA_VERSION,
        "name": "ALIS Champion 综合生产策略",
        "role": "champion",
        "strategy_id": 1,
        "selection_strategy": {"id": 1, "name": "ALIS Champion 综合策略", "type": "formula", "content": {}},
        "strategy_controller": {"id": 31, "version": "controller-v1"},
        "model_bindings": {},
        "portfolio_optimizer": {"implementation": "empirical_cvar_slsqp_v2"},
        "execution_contract": {"signal": "T_close", "execution": "T_plus_1_open"},
    }


def test_ensure_champion_reuses_identical_active_binding(monkeypatch):
    active = {"id": 40, "version": "champion-v1", "config": _bundle(), "is_active": True}
    monkeypatch.setattr(production_strategy, "_build_bundle_config", _bundle)
    monkeypatch.setattr(production_strategy, "get_active_model_version", lambda key: active)
    monkeypatch.setattr(production_strategy, "create_model_version", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))

    assert production_strategy.ensure_production_strategy_champion()["id"] == 40


def test_force_rebind_creates_new_champion_version(monkeypatch):
    old = _bundle()
    old["strategy_controller"] = {"id": 30, "version": "controller-v0"}
    monkeypatch.setattr(production_strategy, "_build_bundle_config", _bundle)
    monkeypatch.setattr(production_strategy, "get_active_model_version", lambda key: {"id": 39, "config": old})
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return 41

    monkeypatch.setattr(production_strategy, "create_model_version", create)
    monkeypatch.setattr(production_strategy, "get_model_version", lambda version_id: {"id": version_id, "version": captured["version"], "config": captured["config"], "is_active": True})
    monkeypatch.setattr(production_strategy, "update_model_version_status", lambda *args, **kwargs: None)

    result = production_strategy.ensure_production_strategy_champion(force_rebind=True)
    assert result["id"] == 41
    assert captured["model_key"] == production_strategy.CHAMPION_MODEL_KEY
    assert captured["is_active"] is True
    assert captured["config"]["strategy_controller"]["id"] == 31


def test_failed_formal_oos_quarantines_only_the_bound_active_champion(monkeypatch):
    champion = {
        "champion_id": 50,
        "champion_version": "champion-v1",
        "metrics": {"formal_oos": {
            "status": "failed", "evaluation_role": "champion",
            "production_strategy_version_id": 50,
            "summary": {"valid_window_count": 5, "outperform_window_count": 0},
        }},
    }
    monkeypatch.setattr(production_strategy, "resolve_production_strategy_champion", lambda: champion)

    gate = production_strategy.champion_execution_gate()

    assert gate["status"] == "quarantined"
    assert gate["mode"] == "research_only_sell_reduce"
    assert gate["formal_oos"]["valid_window_count"] == 5
    assert gate["defensive_continuity"] == {
        "enabled": True,
        "maximum_total_weight": 0.30,
        "asset_scope": "registered_defensive_etf_only",
        "individual_equity_buys_allowed": False,
    }


def test_old_or_unbound_oos_failure_does_not_quarantine_champion(monkeypatch):
    champion = {
        "champion_id": 50,
        "champion_version": "champion-v2",
        "metrics": {"formal_oos": {
            "status": "failed", "evaluation_role": "champion",
            "production_strategy_version_id": 49,
        }},
    }
    monkeypatch.setattr(production_strategy, "resolve_production_strategy_champion", lambda: champion)

    assert production_strategy.champion_execution_gate()["status"] == "allowed"


def test_record_champion_formal_oos_persists_failure_and_returns_gate(monkeypatch):
    current = {"id": 50, "metrics": {"previous": 1}, "validation": {"production_eligible": True}}
    champion = {"champion_id": 50, "champion_version": "champion-v1", "metrics": current["metrics"]}
    written = {}
    monkeypatch.setattr(production_strategy, "resolve_production_strategy_champion", lambda: champion)
    monkeypatch.setattr(production_strategy, "get_model_version", lambda _version_id: current)
    monkeypatch.setattr(production_strategy, "update_model_version_metrics", lambda _id, *, metrics: written.setdefault("metrics", metrics))
    monkeypatch.setattr(production_strategy, "update_model_version_validation", lambda _id, *, validation: written.setdefault("validation", validation))

    result = production_strategy.record_champion_formal_oos_result({
        "status": "failed", "evaluation_role": "champion", "production_strategy_version_id": 50,
        "summary": {"valid_window_count": 5, "outperform_window_count": 0},
        "blocking_reasons": ["aggregate_cost_after_excess_positive"],
    })

    assert result["recorded"] is True
    assert written["metrics"]["formal_oos"]["status"] == "failed"
    assert written["validation"]["execution_mode"] == "research_only_sell_reduce"


def test_candidate_promotion_is_idempotent_after_champion_was_created(monkeypatch):
    candidate = {
        "id": 11,
        "model_key": "alis_strategy_challenger",
        "validation": {"status": "shadow_passed", "production_eligible": True},
    }
    active = {
        "id": 99,
        "version": "champion-v99",
        "config": {
            "promotion": {
                "candidate_model_version_id": 11,
                "from_champion_version_id": 50,
            },
        },
    }
    monkeypatch.setattr(production_strategy, "get_model_version", lambda _model_id: candidate)
    monkeypatch.setattr(production_strategy, "ensure_production_strategy_champion", lambda: active)
    monkeypatch.setattr(
        production_strategy,
        "create_model_version",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not create a duplicate Champion")),
    )
    monkeypatch.setattr(
        production_strategy,
        "get_strategy",
        lambda _strategy_id: (_ for _ in ()).throw(AssertionError("must return before rebuilding the bundle")),
    )

    result = production_strategy.promote_strategy_candidate(11, evidence={"retry": True})

    assert result == {
        "ok": True,
        "event": "already_promoted",
        "from_champion_version_id": 50,
        "to_champion_version_id": 99,
        "candidate_model_version_id": 11,
        "version": "champion-v99",
    }

from app.services import (
    decision_store,
    defensive_assets,
    market_context,
    quant_run_research_gates as gates,
    research_workflow,
)


def test_normalize_ts_code_preserves_suffix_and_maps_exchange_prefixes():
    assert gates.normalize_ts_code("600000") == "600000.SH"
    assert gates.normalize_ts_code("510300") == "510300.SH"
    assert gates.normalize_ts_code("000001") == "000001.SZ"
    assert gates.normalize_ts_code("300001.sz") == "300001.SZ"
    assert gates.normalize_ts_code("430001") == "430001.BJ"
    assert gates.normalize_ts_code("830001") == "830001.BJ"


def test_unsupported_market_model_falls_back_without_changing_transparent_state(monkeypatch):
    monkeypatch.setattr(market_context, "market_context_as_of", lambda _cutoff: {
        "market_state": {"state": "S3", "model": "transparent_four_state_v1"},
        "observations": {},
    })

    result = gates.production_market_context_as_of(
        "2026-07-31T15:00:00+08:00",
        market_state_binding={"production_model": "unregistered_model"},
    )

    assert result["market_state"]["state"] == "S3"
    assert result["market_state_runtime"]["effective_model"] == "transparent_four_state_v1"
    assert result["market_state_runtime"]["fallback_reason"] == (
        "unsupported_market_state_model:unregistered_model"
    )


def test_dynamic_gate_blocks_all_candidates_when_market_context_pit_is_missing(monkeypatch):
    monkeypatch.setattr(market_context, "market_context_as_of", lambda _cutoff: {
        "ok": False,
        "status": "blocked",
        "reason": "market_context_exact_trade_date_missing",
    })
    monkeypatch.setattr(
        defensive_assets,
        "defensive_allocation_for_state",
        lambda *_args, **_kwargs: {"target_weights": {}},
    )

    admitted, blocked, summary = gates.apply_dynamic_strategy_gate(
        ["000001", "600000"],
        as_of_time="2026-08-14T15:00:00+08:00",
    )

    assert admitted == []
    assert [item["code"] for item in blocked] == [
        "market_context_pit_unavailable",
        "market_context_pit_unavailable",
    ]
    assert summary["status"] == "blocked"
    assert summary["risk_budget_multiplier"] == 0.0


def test_research_admission_excludes_pool_members_entered_after_cutoff(monkeypatch):
    monkeypatch.setattr(decision_store, "list_pool_members", lambda _pool: [
        {"ts_code": "600000.SH", "entered_at": "2026-08-01T09:00:00+08:00"},
        {"ts_code": "000001.SZ", "entered_at": "2026-07-01T09:00:00+08:00"},
    ])
    monkeypatch.setattr(
        research_workflow,
        "evaluate_core_pool_eligibility",
        lambda code, as_of_time=None: {"eligible_for_core": True, "gates": []},
    )

    admitted, blocked = gates.apply_research_admission(
        ["600000", "000001"],
        as_of_time="2026-07-31T15:00:00+08:00",
    )

    assert admitted == ["000001"]
    assert blocked == [{
        "symbol": "600000",
        "ts_code": "600000.SH",
        "side": "BUY",
        "code": "not_in_core_pool",
        "message": "模型排名不能绕过核心研究池准入",
    }]

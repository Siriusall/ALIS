from app.services import unified_stock_selection


def test_unified_selection_freezes_core_restricted_and_hard_blocked_tiers(monkeypatch):
    monkeypatch.setattr(
        unified_stock_selection,
        "resolve_production_strategy_champion",
        lambda: {"strategy_id": 1, "champion_id": 50, "champion_version": "v50"},
    )
    monkeypatch.setattr(
        unified_stock_selection,
        "BacktestDataSource",
        lambda: type("DS", (), {"set_as_of_time": lambda self, value: None})(),
    )
    symbols = ["000001", "000002", "000003"]
    monkeypatch.setattr(
        unified_stock_selection,
        "_rank_full_market",
        lambda **_kwargs: (
            symbols,
            {
                "selections": [{"symbol": symbol, "score": 90} for symbol in symbols],
                "filter_summary": {"universe": 500},
            },
        ),
    )

    def research(code, **_kwargs):
        bare = code.split(".")[0]
        gates = [
            {"key": "stock_master", "passed": True},
            {"key": "risk", "passed": bare != "000003"},
            {"key": "financial_fields", "passed": True},
            {"key": "research", "passed": True},
            {"key": "valuation", "passed": True},
            {"key": "thesis", "passed": bare != "000002"},
        ]
        return {"ok": True}, {
            "eligible_for_core": all(item["passed"] for item in gates),
            "gates": gates,
            "research_dimensions": {},
            "raw_metrics": {},
            "valuation": {},
        }

    monkeypatch.setattr(unified_stock_selection, "_prepare_research_admission", research)
    monkeypatch.setattr(
        unified_stock_selection,
        "_apply_dynamic_admission",
        lambda values, **kwargs: (
            ["000001"],
            [{"symbol": "000002", "code": "dynamic_strategy_gate_failed"}],
            {
                "status": "ready",
                "as_of_time": kwargs.get("as_of_time"),
                "items": [
                    {"symbol": "000001", "gate": {"admitted": True}},
                    {"symbol": "000002", "gate": {
                        "admitted": False,
                        "hard_blockers": [],
                        "research_blockers": ["研究置信度低于0.60"],
                    }},
                ],
            },
        ),
    )

    result = unified_stock_selection.build_unified_stock_selection(signal_date="20260917")

    assert result["schema_version"] == "alis_unified_stock_selection_v2"
    assert result["core_symbols"] == ["000001"]
    assert result["restricted_symbols"] == ["000002"]
    assert result["hard_blocked_symbols"] == ["000003"]
    assert result["tradable_symbols"] == ["000001", "000002"]
    assert result["research_admitted_symbols"] == ["000001"]
    assert result["selection_contract"]["research_tier_contract"]["model_promotion_evidence_tiers"] == ["core"]


def test_holding_research_is_frozen_separately_from_top_k(monkeypatch):
    monkeypatch.setattr(
        unified_stock_selection,
        "resolve_production_strategy_champion",
        lambda: {"strategy_id": 1, "champion_id": 50, "champion_version": "v50"},
    )
    monkeypatch.setattr(unified_stock_selection, "BacktestDataSource", lambda: type("DS", (), {"set_as_of_time": lambda self, value: None})())
    monkeypatch.setattr(
        unified_stock_selection,
        "_rank_full_market",
        lambda **kwargs: (
            ["000001"],
            {
                "selections": [{"symbol": "000001", "score": 90}],
                "filter_summary": {"universe": 500},
            },
        ),
    )
    monkeypatch.setattr(
        unified_stock_selection,
        "_prepare_research_admission",
        lambda code, **kwargs: (
            {"ok": True, "status": "ready"},
            {
                "eligible_for_core": True,
                "gates": [],
                "thesis_count": 1,
                "research_dimensions": {},
                "raw_metrics": {},
                "valuation": {},
            },
        ),
    )
    monkeypatch.setattr(
        unified_stock_selection,
        "_apply_dynamic_admission",
        lambda symbols, **kwargs: (list(symbols), [], {
            "status": "ready", "as_of_time": kwargs.get("as_of_time"),
            "items": [{"symbol": symbol, "gate": {"admitted": True}} for symbol in symbols],
        }),
    )

    result = unified_stock_selection.build_unified_stock_selection(
        signal_date="20260724",
        holding_symbols=["600000"],
    )

    assert result["admitted_symbols"] == ["000001"]
    assert result["holding_admitted_symbols"] == ["600000"]
    assert set(result["research_evidence"]) == {"000001", "600000"}


def test_research_cross_section_error_ratio_above_twenty_percent_blocks(monkeypatch):
    monkeypatch.setattr(
        unified_stock_selection,
        "resolve_production_strategy_champion",
        lambda: {"strategy_id": 1, "champion_id": 50, "champion_version": "v50"},
    )
    monkeypatch.setattr(unified_stock_selection, "BacktestDataSource", lambda: type("DS", (), {"set_as_of_time": lambda self, value: None})())
    symbols = [f"{index:06d}" for index in range(1, 6)]
    monkeypatch.setattr(
        unified_stock_selection,
        "_rank_full_market",
        lambda **kwargs: (
            symbols,
            {
                "selections": [{"symbol": symbol, "score": 100 - index} for index, symbol in enumerate(symbols)],
                "filter_summary": {"universe": 500},
            },
        ),
    )

    def research(code, **kwargs):
        if code.startswith(("000001", "000002")):
            raise RuntimeError("research unavailable")
        return {"ok": True}, {
            "eligible_for_core": True,
            "gates": [],
            "research_dimensions": {},
            "raw_metrics": {},
            "valuation": {},
        }

    monkeypatch.setattr(unified_stock_selection, "_prepare_research_admission", research)
    monkeypatch.setattr(
        unified_stock_selection,
        "_apply_dynamic_admission",
        lambda admitted, **kwargs: (list(admitted), [], {
            "status": "ready", "as_of_time": kwargs.get("as_of_time"),
            "items": [{"symbol": symbol, "gate": {"admitted": True}} for symbol in admitted],
        }),
    )

    result = unified_stock_selection.build_unified_stock_selection(signal_date="20260724")

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["selection_contract"]["coverage"]["research_error_ratio"] == 0.4
    assert result["research_evidence"]["000001"]["evaluation_status"] == "error"
    assert result["research_evidence"]["000001"]["eligible_for_core"] is False


def test_dynamic_gate_freeze_removes_refresh_clocks_but_keeps_point_in_time_evidence():
    frozen = unified_stock_selection._freeze_dynamic_gate({
        "updated_at": "runtime",
        "items": [{
            "theme": {
                "created_at": "runtime",
                "generated_at": "runtime",
                "available_time": "2026-07-27T15:00:00+08:00",
                "score": 72,
                "lifecycle": "扩散",
            },
        }],
    })

    assert frozen == {
        "items": [{
            "theme": {
                "available_time": "2026-07-27T15:00:00+08:00",
                "score": 72,
                "lifecycle": "扩散",
            },
        }],
    }


def test_live_selection_uses_the_same_observation_cutoff_for_market_and_model_inputs(monkeypatch):
    observed = {}
    cutoff = "2026-07-24T18:10:00+08:00"

    monkeypatch.setattr(
        unified_stock_selection,
        "resolve_production_strategy_champion",
        lambda: {"strategy_id": 1, "champion_id": 50, "champion_version": "v50"},
    )
    monkeypatch.setattr(
        unified_stock_selection,
        "BacktestDataSource",
        lambda: type("DS", (), {"set_as_of_time": lambda self, value: observed.setdefault("data_source", value)})(),
    )

    def rank(**kwargs):
        observed["ranker"] = kwargs.get("as_of_time")
        return ["000001"], {
            "selections": [{"symbol": "000001", "score": 90}],
            "filter_summary": {"universe": 500},
        }

    monkeypatch.setattr(unified_stock_selection, "_rank_full_market", rank)

    def research(_code, **kwargs):
        observed["research"] = kwargs.get("as_of_time")
        return {"ok": True}, {
            "eligible_for_core": True,
            "gates": [],
            "research_dimensions": {},
            "raw_metrics": {},
            "valuation": {},
        }

    monkeypatch.setattr(unified_stock_selection, "_prepare_research_admission", research)

    def dynamic(symbols, **kwargs):
        observed["dynamic"] = kwargs.get("as_of_time")
        return list(symbols), [], {
            "status": "ready", "as_of_time": kwargs.get("as_of_time"),
            "items": [{"symbol": symbol, "gate": {"admitted": True}} for symbol in symbols],
        }

    monkeypatch.setattr(unified_stock_selection, "_apply_dynamic_admission", dynamic)

    result = unified_stock_selection.build_unified_stock_selection(
        signal_date="20260724",
        observation_cutoff=cutoff,
    )

    assert result["data_as_of_time"] == cutoff
    assert {key: observed[key] for key in ("data_source", "ranker", "research", "dynamic")} == {
        "data_source": cutoff,
        "ranker": cutoff,
        "research": cutoff,
        "dynamic": cutoff,
    }

from app.services import research_engine, thesis_store


def _research_result(*, eligible=True, ready=3):
    dimensions = {
        key: {"status": "ready" if index < ready else "insufficient_data", "score": 70 + index}
        for index, key in enumerate(("quality", "growth", "cashflow", "governance", "industry", "expectation", "valuation"))
    }
    return {
        "ok": True,
        "profile": {"name": "测试公司"},
        "risk": {"eligible": eligible},
        "dimensions": dimensions,
        "research_state": {"classification": "研究观察"},
        "raw_metrics": {"revenue_growth": 12, "core_profit_growth": 8, "cash_conversion": .9, "debt_to_assets": 45, "valuation_base_upside": .2},
    }


def test_generate_thesis_creates_machine_checkable_hypotheses(monkeypatch):
    created = []
    monkeypatch.setattr(research_engine, "build_stock_research", lambda _code, persist=True: _research_result())
    monkeypatch.setattr(thesis_store, "list_theses", lambda **_kwargs: [])
    monkeypatch.setattr(thesis_store, "create_thesis", lambda payload: created.append(payload) or {"id": 17})
    monkeypatch.setattr(thesis_store, "review_thesis", lambda thesis_id: {"id": thesis_id, "derived_status": "valid"})

    result = thesis_store.generate_thesis_for_stock("000001.sz")

    assert result["created"] is True
    assert result["item"]["derived_status"] == "valid"
    assert created[0]["thesis_type"] == "system_research_v1"
    assert len(created[0]["hypotheses"]) == 5
    assert all(item["metric"] and item["expected"] for item in created[0]["hypotheses"])


def test_generate_thesis_reviews_existing_system_thesis(monkeypatch):
    monkeypatch.setattr(research_engine, "build_stock_research", lambda _code, persist=True: _research_result())
    monkeypatch.setattr(thesis_store, "list_theses", lambda **_kwargs: [{"id": 8, "thesis_type": "system_research_v1", "derived_status": "valid"}])
    monkeypatch.setattr(thesis_store, "review_thesis", lambda thesis_id: {"id": thesis_id, "derived_status": "warning"})

    result = thesis_store.generate_thesis_for_stock("000001.SZ")

    assert result["created"] is False
    assert result["status"] == "reviewed_existing"
    assert result["item"]["derived_status"] == "warning"


def test_generate_thesis_blocks_hard_risk_and_weak_evidence(monkeypatch):
    monkeypatch.setattr(research_engine, "build_stock_research", lambda _code, persist=True: _research_result(eligible=False))
    risk_result = thesis_store.generate_thesis_for_stock("000001.SZ")
    monkeypatch.setattr(research_engine, "build_stock_research", lambda _code, persist=True: _research_result(ready=2))
    coverage_result = thesis_store.generate_thesis_for_stock("000001.SZ")

    assert risk_result["reason"] == "risk_hard_veto"
    assert coverage_result["reason"] == "fewer_than_three_ready_dimensions"


def test_necessary_failure_invalidates_and_cannot_auto_reenter(monkeypatch):
    status, confidence = thesis_store._derive_thesis_state([
        {"necessary": True, "status": "failed"},
        {"necessary": False, "status": "verified"},
    ])
    assert status == "invalidated"
    assert confidence <= 40

    monkeypatch.setattr(research_engine, "build_stock_research", lambda _code, persist=True: _research_result())
    monkeypatch.setattr(thesis_store, "list_theses", lambda **_kwargs: [{"id": 9, "thesis_type": "system_research_v1", "derived_status": "invalidated"}])
    result = thesis_store.generate_thesis_for_stock("000001.SZ")
    assert result["ok"] is False
    assert result["reason"] == "invalidated_thesis_requires_manual_reset"

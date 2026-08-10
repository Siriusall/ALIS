from app.services import research_engine
from app.data_platform.services.data_center import FINANCIAL_SECTOR_RESEARCH_REQUIRED_FIELDS
from app.services.research_engine import _dcf_per_share, _financial_template_inputs, _nonfinancial_template_inputs, classify_company, evaluate_risk, materialize_pool_research_snapshots, research_state, score_dimension


def test_hard_risk_cannot_be_averaged_away() -> None:
    result = evaluate_risk({"major_investigation": True, "high_pledge": True})
    assert result["eligible"] is False
    assert result["max_position"] == 0
    assert "重大立案调查" in result["hard_reasons"]


def test_missing_evidence_is_not_scored_as_complete() -> None:
    result = score_dimension("quality", {"profitability": 90, "capital_efficiency": 80})
    assert result["status"] == "insufficient_data"
    assert result["coverage"] == 0.4
    assert "cash_conversion" in result["missing"]


def test_weights_are_renormalized_only_with_disclosed_coverage() -> None:
    result = score_dimension("growth", {
        "revenue_growth": 80,
        "core_profit_growth": 60,
        "three_year_cagr": 70,
        "growth_acceleration": 50,
        "growth_quality": 90,
        "evidence_confidence": 80,
    })
    assert result["status"] == "ready"
    assert result["score"] == 71.0
    assert result["confidence"] == 0.8


def test_company_classification_uses_industry_and_growth() -> None:
    result = classify_company(industry="银行", metrics={"revenue_growth": 10, "core_profit_growth": 8})
    assert result["business_model"] == "金融杠杆型"
    assert result["lifecycle"] == "稳定成长"
    assert result["research_template"] == "financial_institution_v1"


def test_industry_templates_are_not_a_single_general_threshold_set() -> None:
    cyclical = classify_company(industry="煤炭开采", metrics={})
    platform = classify_company(industry="软件服务", metrics={})
    assert cyclical["research_template"] == "cyclical_resource_v1"
    assert cyclical["cycle_sensitivity"] == "高"
    assert platform["research_template"] == "platform_service_v1"
    assert platform["capital_intensity"] == "低"

    inputs = _nonfinancial_template_inputs(
        "cyclical_resource_v1",
        netprofit_margin=10,
        roe=10,
        debt_ratio=50,
        cash_conversion=1,
        revenue_growth=20,
        profit_growth=20,
        revenue_cagr=10,
        receivable_to_revenue=.1,
        cash_debt_coverage=1,
    )
    assert inputs["dimensions"]["growth"]["revenue_growth"] != 0
    assert inputs["limitations"]


def test_research_state_respects_risk_gate() -> None:
    dimensions = {name: {"score": 90, "confidence": .9} for name in ("quality", "growth", "cashflow", "governance", "industry", "expectation", "valuation")}
    result = research_state(dimensions, evaluate_risk({"fraud": True}))
    assert result["classification"] == "风险恶化"
    assert result["eligible"] is False


def test_missing_governance_evidence_does_not_mean_risk_deterioration() -> None:
    dimensions = {name: {"score": 0, "confidence": 0, "status": "insufficient_data"} for name in ("quality", "growth", "cashflow", "governance", "industry", "expectation", "valuation")}
    result = research_state(dimensions, evaluate_risk({}))
    assert result["classification"] == "研究观察"
    assert result["eligible"] is True


def test_dcf_per_share_is_monotonic_and_rejects_invalid_assumptions() -> None:
    base = _dcf_per_share(
        free_cashflow=100, growth=.05, discount_rate=.10, terminal_growth=.02,
        net_cash=20, shares=10,
    )
    bull = _dcf_per_share(
        free_cashflow=100, growth=.08, discount_rate=.09, terminal_growth=.03,
        net_cash=20, shares=10,
    )
    assert base is not None and bull is not None and bull > base > 0
    assert _dcf_per_share(
        free_cashflow=100, growth=.05, discount_rate=.02, terminal_growth=.03,
        net_cash=0, shares=10,
    ) is None


def test_pool_research_snapshot_materialization_deduplicates_and_bounds(monkeypatch) -> None:
    pools = {
        "candidate": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"}],
        "watch": [{"ts_code": "000002.SZ"}],
        "core": [{"ts_code": "600000.SH"}],
    }
    persisted = []
    monkeypatch.setattr("app.services.decision_store.list_pool_members", lambda pool_type: pools[pool_type])
    monkeypatch.setattr(research_engine, "build_stock_research", lambda code, **kwargs: persisted.append((code, kwargs)) or {"ok": True, "research_state": {"confidence": .7}})

    result = materialize_pool_research_snapshots(limit=2)

    assert result["requested"] == 2
    assert result["skipped_by_limit"] == 1
    assert [item[0] for item in persisted] == ["000001.SZ", "000002.SZ"]
    assert all(item[1]["persist"] is True for item in persisted)


def test_financial_template_does_not_treat_bank_cashflow_as_general_free_cashflow() -> None:
    result = _financial_template_inputs(roe=12, equity=120, total_assets=1000)

    assert result["cashflow"] == {}
    assert result["quality"]["profitability"] is not None
    assert result["facts"]["equity_to_assets_proxy"] == .12




def test_cyclical_lifecycle_detects_cycle_position() -> None:
    """Cyclical resource companies should get cycle-aware lifecycle labels."""
    cyclical = classify_company(
        industry="煤炭开采",
        metrics={"revenue_growth": 30, "core_profit_growth": 40, "roe": 18, "cash_conversion": 1.2, "revenue_cagr_3y": 20},
    )
    assert cyclical["research_template"] == "cyclical_resource_v1"
    assert cyclical["cycle_sensitivity"] == "高"
    # High growth + high ROE at cyclical peak industry state -> could be 周期顶部 or 高速成长
    assert cyclical["lifecycle"] in ("高速成长", "周期顶部")


def test_healthcare_template_is_separate_from_consumer() -> None:
    pharma = classify_company(industry="生物医药", metrics={"revenue_growth": 15, "core_profit_growth": 12})
    assert pharma["research_template"] == "healthcare_rd_v1"
    assert pharma["business_model"] == "研发驱动型"


def test_semiconductor_template() -> None:
    chip = classify_company(industry="半导体", metrics={"revenue_growth": 30, "core_profit_growth": 25})
    assert chip["research_template"] == "semiconductor_v1"
    assert chip["business_model"] == "技术密集制造型"


def test_utility_infrastructure() -> None:
    utility = classify_company(industry="电力", metrics={"revenue_growth": 5, "core_profit_growth": 3})
    assert utility["research_template"] == "utility_infrastructure_v1"
    assert utility["business_model"] == "公用事业或特许经营型"


def test_cash_cow_lifecycle_detected() -> None:
    cow = classify_company(
        industry="食品加工",
        metrics={"revenue_growth": 3, "core_profit_growth": 2, "roe": 18, "cash_conversion": 1.0},
    )
    assert cow["lifecycle"] == "成熟现金牛"


def test_decline_lifecycle_detected() -> None:
    decline = classify_company(
        industry="商业贸易",
        metrics={"revenue_growth": -35, "core_profit_growth": -45, "roe": -5},
    )
    assert decline["lifecycle"] == "衰退"

def test_financial_sector_readiness_does_not_require_industrial_cashflow_statement() -> None:
    assert "cashflow" not in FINANCIAL_SECTOR_RESEARCH_REQUIRED_FIELDS

from app.services.research_template_metrics import build_professional_template_data


def _payloads():
    return {
        "income": {
            "total_revenue": 1_000,
            "n_income": 100,
            "sell_exp": 80,
            "admin_exp": 50,
            "rd_exp": 60,
        },
        "balance": {
            "total_assets": 2_000,
            "fix_assets_total": 500,
            "cip_total": 100,
            "invest_real_estate": 100,
            "intan_assets": 100,
            "goodwill": 50,
            "inventories": 200,
            "accounts_receiv": 150,
        },
        "cashflow": {
            "n_cashflow_act": 120,
            "c_pay_acq_const_fiolta": 100,
        },
        "indicator": {
            "grossprofit_margin": 32,
            "roic": 12,
            "debt_to_assets": 48,
            "assets_turn": 0.8,
            "inv_turn": 1.2,
            "ar_turn": 2.4,
        },
        "derived": {
            "revenue_growth": 15,
            "core_profit_growth": 20,
            "cash_conversion": 1.2,
            "debt_to_assets": 48,
        },
        "industry_observations": {
            "demand_orders_sales": 65,
            "product_price": 70,
            "inventory_cycle": 55,
            "capacity_utilization": None,
        },
        "source_rows": {
            "income": {
                "period": "20260331",
                "available_time": "2026-04-30T23:59:59+08:00",
                "payload": {"total_revenue": 1_000},
            },
        },
    }


def _build(template_kind: str):
    return build_professional_template_data(template_kind, **_payloads())


def test_cyclical_template_keeps_missing_capacity_unobserved():
    result = _build("cyclical_resource_v1")

    assert result["status"] == "financial_statement_evidence_ready"
    assert result["metrics"]["industry_product_price_score"]["value"] == 70
    assert result["metrics"]["industry_capacity_utilization_score"]["status"] == "unobserved"
    assert result["production_score_consumed"] is False
    assert "公司产能利用率" in result["missing_external_operating_metrics"]


def test_asset_template_uses_reported_assets_without_calling_it_nav():
    result = _build("asset_heavy_v1")

    assert result["metrics"]["productive_asset_share_pct"]["value"] == 35
    assert result["metrics"]["free_cashflow_to_assets_pct"]["value"] == 1
    assert "重估NAV与重置成本" in result["missing_external_operating_metrics"]


def test_platform_template_exposes_rd_and_intangible_observations():
    result = _build("platform_service_v1")

    assert result["metrics"]["rd_expense_ratio_pct"]["value"] == 6
    assert result["metrics"]["intangible_goodwill_share_pct"]["value"] == 7.5
    assert "留存、续费和流失率" in result["missing_external_operating_metrics"]


def test_consumer_template_distinguishes_reported_inventory_from_channel_inventory():
    result = _build("consumer_brand_v1")

    assert result["metrics"]["inventory_to_revenue_pct"]["value"] == 20
    assert result["metrics"]["inventory_turnover_ratio"]["value"] == 1.2
    assert "不等同于渠道库存" in result["metrics"]["inventory_to_revenue_pct"]["meaning"]
    assert "渠道库存与动销" in result["missing_external_operating_metrics"]


def test_non_specialized_template_is_explicitly_not_applicable():
    result = _build("general_corporate_v1")

    assert result["status"] == "not_applicable_or_unobserved"
    assert result["metrics"] == {}
    assert result["coverage"] == 0

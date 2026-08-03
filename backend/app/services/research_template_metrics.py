"""Point-in-time, business-model-specific research observations.

The metrics in this module only interpret fields already present in frozen
financial statements and industry observations.  They are research evidence,
not a new production score, and missing operating KPIs are never imputed.
"""

from __future__ import annotations

from typing import Any


CONTRACT_VERSION = "alis_professional_research_template_data_v1"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in {float("inf"), float("-inf")} else None


def _ratio(numerator: Any, denominator: Any, *, percent: bool = False) -> float | None:
    top = _number(numerator)
    bottom = _number(denominator)
    if top is None or bottom in {None, 0.0}:
        return None
    result = top / abs(bottom)
    return result * 100.0 if percent else result


def _sum_observed(*values: Any) -> float | None:
    observed = [_number(value) for value in values]
    present = [value for value in observed if value is not None]
    return sum(present) if present else None


def _metric(
    value: Any,
    *,
    unit: str,
    sources: list[str],
    meaning: str,
) -> dict[str, Any]:
    number = _number(value)
    return {
        "status": "observed" if number is not None else "unobserved",
        "value": round(number, 6) if number is not None else None,
        "unit": unit,
        "source_fields": sources,
        "meaning": meaning,
    }


def _source_evidence(source_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "api": api,
            "period": row.get("period"),
            "available_time": row.get("available_time"),
        }
        for api, row in source_rows.items()
        if isinstance(row, dict) and row.get("payload")
    ]


def build_professional_template_data(
    template_kind: str,
    *,
    income: dict[str, Any],
    balance: dict[str, Any],
    cashflow: dict[str, Any],
    indicator: dict[str, Any],
    derived: dict[str, Any],
    industry_observations: dict[str, Any] | None = None,
    source_rows: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return observed template metrics without changing any decision score."""

    revenue = income.get("total_revenue") or income.get("revenue")
    total_assets = balance.get("total_assets")
    fixed_assets = balance.get("fix_assets_total") or balance.get("fix_assets")
    productive_assets = _sum_observed(
        fixed_assets,
        balance.get("cip_total") or balance.get("cip"),
        balance.get("invest_real_estate"),
    )
    intangible_assets = _sum_observed(balance.get("intan_assets"), balance.get("goodwill"))
    capex = cashflow.get("c_pay_acq_const_fiolta")
    free_cashflow = cashflow.get("free_cashflow")
    if free_cashflow is None:
        operating_cash = _number(cashflow.get("n_cashflow_act"))
        observed_capex = _number(capex)
        free_cashflow = (
            operating_cash - observed_capex
            if operating_cash is not None and observed_capex is not None
            else None
        )
    industry = industry_observations if isinstance(industry_observations, dict) else {}

    common = {
        "gross_margin_pct": _metric(
            indicator.get("grossprofit_margin"),
            unit="pct",
            sources=["fina_indicator.grossprofit_margin"],
            meaning="毛利率，观察产品盈利与价格传导能力",
        ),
        "roic_pct": _metric(
            indicator.get("roic"),
            unit="pct",
            sources=["fina_indicator.roic"],
            meaning="投入资本回报率",
        ),
        "revenue_growth_pct": _metric(
            derived.get("revenue_growth"),
            unit="pct",
            sources=["income.total_revenue", "prior_year.income.total_revenue"],
            meaning="同报告期营业收入同比增速",
        ),
        "core_profit_growth_pct": _metric(
            derived.get("core_profit_growth"),
            unit="pct",
            sources=["income.n_income", "prior_year.income.n_income"],
            meaning="同报告期净利润同比增速代理",
        ),
        "cash_conversion_ratio": _metric(
            derived.get("cash_conversion"),
            unit="ratio",
            sources=["cashflow.n_cashflow_act", "income.n_income"],
            meaning="经营现金流对净利润的覆盖",
        ),
        "capex_to_revenue_pct": _metric(
            _ratio(capex, revenue, percent=True),
            unit="pct",
            sources=["cashflow.c_pay_acq_const_fiolta", "income.total_revenue"],
            meaning="资本开支相对营业收入强度",
        ),
        "asset_turnover_ratio": _metric(
            indicator.get("assets_turn"),
            unit="times",
            sources=["fina_indicator.assets_turn"],
            meaning="报告期累计总资产周转率",
        ),
        "inventory_turnover_ratio": _metric(
            indicator.get("inv_turn"),
            unit="times",
            sources=["fina_indicator.inv_turn"],
            meaning="报告期累计存货周转率",
        ),
        "receivable_turnover_ratio": _metric(
            indicator.get("ar_turn"),
            unit="times",
            sources=["fina_indicator.ar_turn"],
            meaning="报告期累计应收账款周转率",
        ),
        "inventory_to_revenue_pct": _metric(
            _ratio(balance.get("inventories"), revenue, percent=True),
            unit="pct",
            sources=["balancesheet.inventories", "income.total_revenue"],
            meaning="报表存货相对营业收入规模；不等同于渠道库存",
        ),
        "receivable_to_revenue_pct": _metric(
            _ratio(balance.get("accounts_receiv"), revenue, percent=True),
            unit="pct",
            sources=["balancesheet.accounts_receiv", "income.total_revenue"],
            meaning="应收账款相对营业收入规模",
        ),
        "selling_expense_ratio_pct": _metric(
            _ratio(income.get("sell_exp"), revenue, percent=True),
            unit="pct",
            sources=["income.sell_exp", "income.total_revenue"],
            meaning="销售费用率",
        ),
        "admin_expense_ratio_pct": _metric(
            _ratio(income.get("admin_exp"), revenue, percent=True),
            unit="pct",
            sources=["income.admin_exp", "income.total_revenue"],
            meaning="管理费用率",
        ),
        "rd_expense_ratio_pct": _metric(
            _ratio(income.get("rd_exp"), revenue, percent=True),
            unit="pct",
            sources=["income.rd_exp", "income.total_revenue"],
            meaning="研发费用率",
        ),
        "productive_asset_share_pct": _metric(
            _ratio(productive_assets, total_assets, percent=True),
            unit="pct",
            sources=[
                "balancesheet.fix_assets_total",
                "balancesheet.cip_total",
                "balancesheet.invest_real_estate",
                "balancesheet.total_assets",
            ],
            meaning="固定资产、在建工程与投资性房地产占总资产比例",
        ),
        "intangible_goodwill_share_pct": _metric(
            _ratio(intangible_assets, total_assets, percent=True),
            unit="pct",
            sources=[
                "balancesheet.intan_assets",
                "balancesheet.goodwill",
                "balancesheet.total_assets",
            ],
            meaning="无形资产和商誉占总资产比例",
        ),
        "debt_to_assets_pct": _metric(
            indicator.get("debt_to_assets") or derived.get("debt_to_assets"),
            unit="pct",
            sources=["fina_indicator.debt_to_assets"],
            meaning="资产负债率",
        ),
        "free_cashflow_to_assets_pct": _metric(
            _ratio(free_cashflow, total_assets, percent=True),
            unit="pct",
            sources=[
                "cashflow.free_cashflow",
                "cashflow.n_cashflow_act",
                "cashflow.c_pay_acq_const_fiolta",
                "balancesheet.total_assets",
            ],
            meaning="自由现金流相对总资产规模",
        ),
        "industry_demand_score": _metric(
            industry.get("demand_orders_sales"),
            unit="normalized_score",
            sources=["industry_state.demand_orders_sales"],
            meaning="点时行业需求/订单/销量归一化得分",
        ),
        "industry_product_price_score": _metric(
            industry.get("product_price"),
            unit="normalized_score",
            sources=["industry_state.product_price"],
            meaning="点时行业产品价格归一化得分",
        ),
        "industry_inventory_cycle_score": _metric(
            industry.get("inventory_cycle"),
            unit="normalized_score",
            sources=["industry_state.inventory_cycle"],
            meaning="点时行业库存周期归一化得分",
        ),
        "industry_capacity_utilization_score": _metric(
            industry.get("capacity_utilization"),
            unit="normalized_score",
            sources=["industry_state.capacity_utilization"],
            meaning="点时行业产能利用率归一化得分；缺源时保持不可观测",
        ),
    }

    template_metrics = {
        "cyclical_resource_v1": [
            "gross_margin_pct",
            "roic_pct",
            "revenue_growth_pct",
            "core_profit_growth_pct",
            "cash_conversion_ratio",
            "capex_to_revenue_pct",
            "inventory_to_revenue_pct",
            "industry_demand_score",
            "industry_product_price_score",
            "industry_inventory_cycle_score",
            "industry_capacity_utilization_score",
        ],
        "asset_heavy_v1": [
            "productive_asset_share_pct",
            "capex_to_revenue_pct",
            "debt_to_assets_pct",
            "free_cashflow_to_assets_pct",
            "roic_pct",
            "asset_turnover_ratio",
            "cash_conversion_ratio",
            "receivable_to_revenue_pct",
            "industry_demand_score",
            "industry_capacity_utilization_score",
        ],
        "platform_service_v1": [
            "gross_margin_pct",
            "rd_expense_ratio_pct",
            "selling_expense_ratio_pct",
            "admin_expense_ratio_pct",
            "intangible_goodwill_share_pct",
            "receivable_to_revenue_pct",
            "cash_conversion_ratio",
            "revenue_growth_pct",
        ],
        "consumer_brand_v1": [
            "gross_margin_pct",
            "selling_expense_ratio_pct",
            "inventory_turnover_ratio",
            "inventory_to_revenue_pct",
            "receivable_turnover_ratio",
            "receivable_to_revenue_pct",
            "cash_conversion_ratio",
            "revenue_growth_pct",
        ],
    }
    external_missing = {
        "cyclical_resource_v1": ["公司产品价格与产销量", "公司产能利用率", "细分品种社会库存"],
        "asset_heavy_v1": ["重估NAV与重置成本", "特许经营剩余期限", "项目级租售/通行/入住率"],
        "platform_service_v1": ["活跃用户与付费用户", "留存、续费和流失率", "单位获客成本与生命周期价值"],
        "consumer_brand_v1": ["渠道库存与动销", "同店销售", "品牌份额与终端价格"],
    }
    selected = template_metrics.get(str(template_kind), [])
    metrics = {key: common[key] for key in selected}
    observed = sum(1 for item in metrics.values() if item["status"] == "observed")
    total = len(metrics)
    coverage = observed / total if total else 0.0
    return {
        "schema_version": CONTRACT_VERSION,
        "template_kind": str(template_kind),
        "status": (
            "financial_statement_evidence_ready"
            if total and coverage >= 0.60
            else "partial_financial_statement_evidence"
            if observed
            else "not_applicable_or_unobserved"
        ),
        "observed_metrics": observed,
        "total_metrics": total,
        "coverage": round(coverage, 6),
        "metrics": metrics,
        "point_in_time_evidence": _source_evidence(source_rows or {}),
        "missing_external_operating_metrics": external_missing.get(str(template_kind), []),
        "production_score_consumed": False,
        "limitations": [
            "该合同只展示截止时点已披露的报表和行业证据，不推断未披露经营指标",
            "季度累计报表比率不可直接与单季经营比率混用",
            "尚未经过独立增量收益验证，因此不进入选股、Controller 或组合权重",
        ],
    }

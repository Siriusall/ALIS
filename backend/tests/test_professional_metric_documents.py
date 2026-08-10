from app.services.professional_metric_documents import (
    _report_period,
    _safe_cninfo_pdf_url,
    _supported_report_title,
    extract_metric_candidates,
)


def test_only_full_cninfo_annual_reports_are_supported():
    assert _supported_report_title("某公司2025年年度报告") is True
    assert _supported_report_title("某公司2025年年度报告（修订版）") is True
    assert _supported_report_title("某公司2025年年度报告摘要") is False
    assert _supported_report_title("关于2025年年度报告的问询函回复") is False
    assert _report_period("某公司2025年年度报告") == "20251231"


def test_pdf_url_is_restricted_to_official_cninfo_static_host():
    assert _safe_cninfo_pdf_url(
        "https://static.cninfo.com.cn/finalpage/2026-01-01/123.PDF"
    ) is True
    assert _safe_cninfo_pdf_url("https://example.com/finalpage/123.pdf") is False
    assert _safe_cninfo_pdf_url("https://static.cninfo.com.cn/file.txt") is False


def test_platform_candidates_bind_each_keyword_to_nearest_number():
    candidates = extract_metric_candidates(
        ["本年度月活跃用户达到1,234万人，付费用户达到56万人，续费率为85.5%。"],
        template_kind="platform_service_v1",
        report_period="20251231",
    )
    by_key = {item["metric_key"]: item for item in candidates}

    assert by_key["monthly_active_users"]["value"] == 1234
    assert by_key["monthly_active_users"]["unit"] == "万人"
    assert by_key["paying_users"]["value"] == 56
    assert by_key["renewal_rate"]["value"] == 85.5
    assert all(item["validation_status"] == "pending_manual_verification" for item in candidates)
    assert all(item["production_eligible"] is False for item in candidates)


def test_template_specific_terms_do_not_cross_create_other_template_metrics():
    candidates = extract_metric_candidates(
        ["公司核定产能为500万吨/年，报告期产品产量为420万吨。"],
        template_kind="cyclical_resource_v1",
        report_period="20251231",
    )
    by_key = {item["metric_key"]: item for item in candidates}

    assert by_key["company_production_capacity"]["value"] == 500
    assert by_key["company_production_capacity"]["unit"] == "万吨/年"
    assert by_key["company_production_volume"]["value"] == 420
    assert not {"monthly_active_users", "same_store_sales_growth"} & set(by_key)


def test_number_without_explicit_unit_is_not_a_candidate():
    candidates = extract_metric_candidates(
        ["公司月活跃用户达到123456，统计口径发生调整。"],
        template_kind="platform_service_v1",
        report_period="20251231",
    )
    assert candidates == []


def test_percentage_change_is_not_mislabeled_as_physical_volume():
    candidates = extract_metric_candidates(
        ["生产量 KG 388,277.44 306,163.72 26.82%；销售量 KG 315,326.80 301,915.01 4.44%。"],
        template_kind="cyclical_resource_v1",
        report_period="20251231",
    )
    assert candidates == []


def test_standard_cninfo_production_sales_table_preserves_product_and_unit():
    candidates = extract_metric_candidates(
        [
            "(2). 产销量情况分析表\n主要产品 单 位 生产量 销售量 库存量",
            "长链二元酸 吨 11,461.75 12,790.33 233.48 179.79 432.69\n"
            "活性炭 吨 15,969.76 16,626.53 3,867.87 -5.06 -5.59",
        ],
        template_kind="cyclical_resource_v1",
        report_period="20221231",
    )
    rows = {
        (item["metric_key"], item["product"]): item
        for item in candidates
        if item["extraction_method"] == "cninfo_production_sales_table_row_v1"
    }

    assert rows[("company_production_volume", "长链二元酸")]["value"] == 11461.75
    assert rows[("company_sales_volume", "长链二元酸")]["value"] == 12790.33
    assert rows[("company_production_volume", "活性炭")]["unit"] == "吨"
    assert all(item["production_eligible"] is False for item in rows.values())


def test_sales_column_follows_optional_external_purchase_column():
    candidates = extract_metric_candidates(
        [
            "(2). 产销量情况分析表\n主要产品 单位 生产量 外购量 销售量 库存量\n"
            "活性炭 吨 16,222.47 1,755.75 17,116.45 4,677.64 1.58 2.95",
        ],
        template_kind="cyclical_resource_v1",
        report_period="20231231",
    )
    by_key = {item["metric_key"]: item for item in candidates}

    assert by_key["company_production_volume"]["value"] == 16222.47
    assert by_key["company_sales_volume"]["value"] == 17116.45

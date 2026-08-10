import json

from app.services.event_impact_features import (
    EVENT_FEATURE_CONTRACT,
    analyst_net_profit_cny,
    build_analyst_consensus_index,
    disclosure_expectation_gap,
    event_type_group,
    extract_text_amount_cny,
    materialize_event_features,
    merge_event_candidates,
    point_in_time_analyst_consensus,
    point_in_time_analyst_consensus_bundle,
    report_period_key,
    structured_event_candidate,
)


def test_structured_forecast_keeps_real_guidance_gap_and_event_type():
    candidate = structured_event_candidate(
        source="structured_disclosure",
        api_name="forecast",
        confidence=0.98,
        payload={
            "p_change_min": 40,
            "p_change_max": 60,
            "net_profit_min": 100_000_000,
            "net_profit_max": 120_000_000,
        },
        expectation_gap=0.20,
        expectation_basis="company_guidance_revision_same_period",
    )

    assert EVENT_FEATURE_CONTRACT.endswith("_v3")
    assert candidate["event_type_group"] == "earnings"
    assert candidate["direction"] == "positive"
    assert candidate["magnitude"] == 1.0
    assert candidate["expectation_gap"] == 0.20
    assert candidate["expectation_observed"] is True


def test_report_rc_consensus_is_normalized_to_cny_and_period_key():
    assert report_period_key("20260331") == "2026Q1"
    assert report_period_key("2026Q4") == "2026Q4"
    assert report_period_key("FY2026E") == "2026Q4"
    assert report_period_key("2026年半年度") == "2026Q2"
    assert report_period_key("2026Q5") == ""
    assert analyst_net_profit_cny({"np": 261700}) == 2_617_000_000


def test_consensus_uses_latest_report_per_independent_broker():
    rows = []
    for org, report_date, estimate in (
        ("A券商", "20260301", 100),
        ("A券商", "20260320", 120),
        ("B券商", "20260310", 110),
        ("C券商", "20260315", 130),
    ):
        rows.append({
            "ts_code": "000001.SZ",
            "trade_date": report_date,
            "available_time": f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:]}T23:59:59",
            "payload_json": json.dumps({
                "ts_code": "000001.SZ",
                "report_date": report_date,
                "org_name": org,
                "quarter": "2026Q4",
                "np": estimate,
            }),
        })
    index = build_analyst_consensus_index(rows)
    consensus = point_in_time_analyst_consensus(
        index,
        symbol="000001.SZ",
        period="20261231",
        as_of_date="20260331",
    )

    assert consensus["observed"] is True
    assert consensus["organization_count"] == 3
    assert consensus["estimate_cny"] == 1_200_000
    assert consensus["dispersion"] is not None
    assert consensus["freshness_days"] == 11


def test_consensus_requires_three_brokers_and_exact_report_period():
    rows = [
        {
            "ts_code": "000001.SZ",
            "trade_date": "20260320",
            "available_time": "2026-03-20T23:59:59",
            "payload_json": json.dumps({
                "ts_code": "000001.SZ",
                "report_date": "20260320",
                "org_name": org,
                "quarter": "2026Q4",
                "np": estimate,
            }),
        }
        for org, estimate in (("A券商", 100), ("B券商", 110))
    ]
    index = build_analyst_consensus_index(rows)

    too_few = point_in_time_analyst_consensus(
        index, symbol="000001.SZ", period="2026Q4", as_of_date="20260331"
    )
    wrong_period = point_in_time_analyst_consensus(
        index, symbol="000001.SZ", period="2026Q1", as_of_date="20260331"
    )

    assert too_few["observed"] is False
    assert too_few["organization_count"] == 2
    assert too_few["estimate_cny"] is None
    assert wrong_period["organization_count"] == 0


def test_multi_metric_consensus_uses_independent_brokers_and_point_in_time_revisions():
    rows = []
    for report_date, multiplier in (("20260220", 1.0), ("20260320", 1.1)):
        for index, org in enumerate(("A券商", "B券商", "C券商")):
            rows.append({
                "ts_code": "000001.SZ",
                "trade_date": report_date,
                "available_time": (
                    f"{report_date[:4]}-{report_date[4:6]}-"
                    f"{report_date[6:]}T23:59:59"
                ),
                "payload_json": json.dumps({
                    "ts_code": "000001.SZ",
                    "report_date": report_date,
                    "org_name": org,
                    "quarter": "2026Q4",
                    "np": (100 + index * 10) * multiplier,
                    "op_rt": (1_000 + index * 100) * multiplier,
                    "op_pr": (150 + index * 10) * multiplier,
                    "eps": (1.0 + index * 0.1) * multiplier,
                }),
            })
    bundle = point_in_time_analyst_consensus_bundle(
        build_analyst_consensus_index(rows),
        symbol="000001.SZ",
        period="2026Q4",
        as_of_date="20260331",
        revision_days=28,
    )

    assert bundle["observed_metric_count"] == 4
    assert round(bundle["metrics"]["net_profit_cny"]["estimate"], 2) == 1_210_000
    assert round(bundle["metrics"]["revenue_cny"]["estimate"], 2) == 12_100_000
    assert round(bundle["metrics"]["eps_cny_per_share"]["estimate"], 6) == 1.21
    assert round(bundle["metrics"]["net_profit_cny"]["revision"]["relative"], 6) == 0.1


def test_disclosure_gap_compares_same_period_consensus_in_cny():
    gap, basis, current = disclosure_expectation_gap(
        api_name="forecast",
        payload={"net_profit_min": 120, "net_profit_max": 120},
        consensus={"observed": True, "estimate_cny": 1_000_000},
    )

    assert gap == 0.2
    assert basis.endswith("independent_analyst_consensus_np_v2")
    assert current["profit_cny"] == 1_200_000


def test_repurchase_and_holder_trade_keep_observable_quantities():
    repurchase = structured_event_candidate(
        source="structured_disclosure",
        api_name="repurchase",
        payload={"proc": "实施", "amount": 60_920_385.3, "vol": 1_204_217},
    )
    reduction = structured_event_candidate(
        source="structured_disclosure",
        api_name="stk_holdertrade",
        payload={"in_de": "DE", "change_ratio": 1.2, "change_vol": 2_000_000},
    )

    assert repurchase["direction"] == "positive"
    assert repurchase["amount_observed"] is True
    assert repurchase["magnitude_observed"] is True
    assert reduction["direction"] == "negative"
    assert reduction["event_type"] == "shareholder_reduction"
    assert reduction["direct_materiality"] > 0


def test_materiality_uses_reported_amount_over_point_in_time_market_cap():
    candidate = structured_event_candidate(
        source="major_news",
        direction="positive",
        severity="high",
        confidence=0.8,
        title="签订10亿元重大合同",
    )
    features = materialize_event_features(
        candidate,
        total_mv=1_000_000,
        close=20,
        momentum_20=0.10,
        volatility_20=0.03,
        market_reaction=0.04,
        crowding=1.0,
    )

    assert extract_text_amount_cny("签订10亿元合同") == 1_000_000_000
    assert features["event_type_contract_operation"] == 1.0
    assert features["event_materiality_observed"] == 1.0
    assert 0.0 < features["event_materiality"] < 1.0
    assert features["event_company_size_observed"] == 1.0
    assert features["event_priced_in_proxy"] > 0.0


def test_missing_expectation_is_explicitly_unobserved_not_fabricated():
    candidate = structured_event_candidate(
        source="major_news",
        direction="neutral",
        severity="medium",
        confidence=0.6,
        title="公司发布经营动态",
    )
    features = materialize_event_features(
        candidate,
        total_mv=None,
        close=10,
        momentum_20=0,
        volatility_20=0.02,
        market_reaction=0,
        crowding=0,
    )

    assert features["event_expectation_gap"] == 0.0
    assert features["event_expectation_observed"] == 0.0
    assert features["event_company_size_observed"] == 0.0
    assert features["event_materiality_observed"] == 0.0


def test_merge_prefers_structured_disclosure_and_keeps_all_type_evidence():
    news = structured_event_candidate(
        source="major_news",
        direction="positive",
        severity="medium",
        confidence=0.95,
        title="获得重大订单",
    )
    disclosure = structured_event_candidate(
        source="structured_disclosure",
        api_name="forecast",
        confidence=0.8,
        payload={"p_change_min": 30, "p_change_max": 50},
    )

    merged = merge_event_candidates(news, disclosure)

    assert merged["source"] == "structured_disclosure"
    assert merged["event_type_group"] == "earnings"
    assert set(merged["event_type_groups"]) == {"earnings", "contract_operation"}
    assert set(merged["observed_sources"]) == {"major_news", "structured_disclosure"}
    assert merged["event_count"] == 2


def test_equal_rank_event_merge_is_independent_of_database_row_order():
    first = structured_event_candidate(
        source="major_news",
        direction="positive",
        severity="medium",
        confidence=0.8,
        title="获得重大订单",
    )
    second = structured_event_candidate(
        source="major_news",
        direction="negative",
        severity="medium",
        confidence=0.8,
        title="收到监管问询",
    )

    forward = merge_event_candidates(first, second)
    reverse = merge_event_candidates(second, first)

    assert forward == reverse
    assert forward["event_count"] == 2
    assert set(forward["event_type_groups"]) == {
        "contract_operation",
        "governance_risk",
    }


def test_event_type_group_maps_governance_risk_before_other():
    assert event_type_group(text="收到证监会立案调查通知") == "governance_risk"

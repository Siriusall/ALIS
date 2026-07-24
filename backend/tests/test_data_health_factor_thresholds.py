from app.services.data_health import _low_core_factor_coverage


def test_pe_ttm_uses_structural_null_aware_threshold():
    low = _low_core_factor_coverage({
        "close": 0.999,
        "volume": 0.999,
        "turnover": 0.999,
        "pe_ttm": 0.72,
    })

    assert low == {}


def test_market_bar_coverage_still_requires_eighty_percent():
    low = _low_core_factor_coverage({"volume": 0.79, "turnover": 0.75})

    assert low == {"volume": 0.79, "turnover": 0.75}

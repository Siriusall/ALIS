from app.data_platform.services.derived_limit_price import calculate_limit_prices


def _calculate(**overrides):
    values = {
        "ts_code": "600000.SH",
        "trade_date": "20260728",
        "pre_close": "10.00",
        "list_date": "19991110",
        "listing_open_session_number": 100,
        "is_st": False,
        "name": "浦发银行",
    }
    values.update(overrides)
    return calculate_limit_prices(**values)


def test_main_board_uses_ten_percent_after_2026_rule_change_even_when_st():
    result = _calculate(pre_close="35.05", is_st=True, name="ST示例")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.1
    assert result["up_limit"] == 38.56
    assert result["down_limit"] == 31.55


def test_main_board_st_uses_five_percent_before_2026_rule_change():
    result = _calculate(trade_date="20260703", is_st=True, name="*ST示例")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.05
    assert result["up_limit"] == 10.5
    assert result["down_limit"] == 9.5


def test_growth_star_and_beijing_boards_use_their_exchange_rates():
    assert _calculate(ts_code="300001.SZ")["limit_rate"] == 0.2
    assert _calculate(ts_code="688001.SH")["limit_rate"] == 0.2
    assert _calculate(ts_code="920001.BJ")["limit_rate"] == 0.3


def test_legacy_s_share_keeps_five_percent_rate():
    result = _calculate(ts_code="600182.SH", name="S佳通")

    assert result["ok"] is True
    assert result["limit_rate"] == 0.05


def test_initial_no_limit_window_and_unknown_board_are_rejected():
    assert _calculate(listing_open_session_number=5)["reason"] == "initial_no_limit_window"
    assert _calculate(ts_code="900001.SH")["reason"] == "unsupported_security_board"

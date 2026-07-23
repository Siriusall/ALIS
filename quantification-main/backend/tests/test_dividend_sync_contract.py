from app.data_platform.services import data_center


def test_dividend_apis_accept_a_successful_empty_event_set():
    assert "dividend" in data_center.VALID_EMPTY_TUSHARE_APIS
    assert "fund_div" in data_center.VALID_EMPTY_TUSHARE_APIS

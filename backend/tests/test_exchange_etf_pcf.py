from app.data_platform.services import exchange_etf_pcf as pcf


def test_parse_szse_pcf_keeps_header_and_components():
    text = """
基金名称：  创业板ETF易方达
基金管理公司名称：  易方达基金管理有限公司
目标指数代码：  399006
是否需要公布IOPV：  是
最小申购、赎回单位：  1000000份
全部申购赎回组合证券只数：  2只
组合信息内容
 300001                               特锐德                                      400         允许               73.00%                                          0.0000           0.0000     深圳市场
 300002                               神州泰岳                                    500         必须               10.00%                20.00%                  123.0000         45.0000     深圳市场
"""
    result = pcf._parse_szse_text(text, "159915", "20260722")

    assert result["ok"] is True
    assert result["index_code"] == "399006"
    assert result["creation_redemption_unit"] == 1_000_000
    assert result["component_count"] == 2
    assert [item["component_code"] for item in result["components"]] == ["300001", "300002"]


def test_sse_pcf_rejects_latest_data_for_requested_old_date(monkeypatch):
    responses = iter([
        {"result": [{"TRADING_DAY": "20260722", "FUND_NAME": "沪深300ETF", "RECORD_NUM": "1"}]},
        {"result": [{"INSTRUMENT_ID": "000001", "INSTRUMENT_NAME": "平安银行", "QUANTITY": "100"}]},
    ])
    monkeypatch.setattr(pcf, "_request_json", lambda *args, **kwargs: next(responses))

    result = pcf.fetch_sse_pcf("510300", "20260721")

    assert result["ok"] is False
    assert result["status"] == "historical_unavailable"
    assert result["latest_available_date"] == "20260722"


def test_sse_pcf_normalizes_percentage_fields(monkeypatch):
    responses = iter([
        {"result": [{"TRADING_DAY": "20260722", "FUND_NAME": "沪深300ETF", "RECORD_NUM": "1", "PUBLISH_IOPV": "是"}]},
        {"result": [{"INSTRUMENT_ID": "000001", "INSTRUMENT_NAME": "平安银行", "QUANTITY": "1600",
                     "SUBSTITUTION_FLAG": "1", "CREATION_PREMIUM_RATE": "10%",
                     "REDEMPTION_DISCOUNT_RATE": "20%", "SUBSTITUTION_CASH_AMOUNT": "17344.000"}]},
    ])
    monkeypatch.setattr(pcf, "_request_json", lambda *args, **kwargs: next(responses))

    result = pcf.fetch_sse_pcf("510300", "20260722")

    assert result["ok"] is True
    assert result["components"][0]["creation_premium_rate"] == 0.1
    assert result["components"][0]["redemption_discount_rate"] == 0.2

from app.data_platform.services import market_data_discovery as discovery


def test_defensive_fund_discovery_includes_all_configured_tushare_compatible_sources(monkeypatch):
    from app.data_platform.tushare_client import tushare_compatible_source_codes

    providers = discovery.DATASET_PROVIDERS["defensive_fund_daily"]
    assert providers[0] == "tencent_etf_quote"
    assert set(tushare_compatible_source_codes()).issubset(providers)


def test_core_market_discovery_includes_all_configured_tushare_compatible_sources():
    from app.data_platform.tushare_client import tushare_compatible_source_codes

    compatible = set(tushare_compatible_source_codes())
    for dataset in ("daily", "daily_basic", "stk_limit", "index_daily", "adj_factor"):
        assert compatible.issubset(discovery.DATASET_PROVIDERS[dataset])


def test_tencent_etf_probe_fetches_once_and_rejects_partial_execution_bar(monkeypatch):
    calls = []

    def fetch(codes, trade_date):
        calls.append((codes, trade_date))
        return {
            "510300.SH": {
                "trade_date": trade_date,
                "open": 4.0,
                "high": 4.1,
                "low": 3.9,
                "close": 4.0,
                "pre_close": 4.0,
                "vol": 1_000,
            }
        }

    monkeypatch.setattr(
        "app.data_platform.services.tencent_etf_daily_fallback.fetch_tencent_etf_quotes",
        fetch,
    )

    result = discovery._tencent_etf_probe("20260813")

    assert calls == [(["510300.SH"], "20260813")]
    assert result["available"] is False
    assert result["missing_execution_fields"] == ["amount"]

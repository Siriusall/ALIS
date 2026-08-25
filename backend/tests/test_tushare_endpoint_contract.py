from __future__ import annotations

from pathlib import Path

from app.data_platform import tushare_client


class _FakePro:
    pass


def test_primary_tushare_endpoint_defaults_to_configured_proxy(monkeypatch):
    fake = _FakePro()
    monkeypatch.setattr(tushare_client.ts, "pro_api", lambda _token: fake)
    monkeypatch.delenv("TUSHARE_HTTP_URL", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    result = tushare_client.create_strict_tushare_pro("token")

    assert result is fake
    assert result._DataApi__http_url == "http://lianghua.nanyangqiankun.top"


def test_probe_endpoints_default_to_primary_proxy_only(monkeypatch):
    monkeypatch.delenv("TUSHARE_PROBE_ENDPOINTS", raising=False)
    monkeypatch.setenv("TUSHARE_HTTP_URL", "http://lianghua.nanyangqiankun.top")

    endpoints = dict(tushare_client.configured_tushare_probe_endpoints())

    assert endpoints == {"tushare_proxy": "http://lianghua.nanyangqiankun.top"}
    assert tushare_client.active_tushare_provider_codes() == ("tushare_proxy",)
    assert tushare_client.primary_tushare_provider_code() == "tushare_proxy"
    assert tushare_client.tushare_compatible_source_codes() == ("tushare_proxy", "tushare")


def test_probe_endpoints_allow_explicit_compatible_fallbacks(monkeypatch):
    monkeypatch.setenv(
        "TUSHARE_PROBE_ENDPOINTS",
        "tushare_proxy=http://proxy.test,tushare_backup=https://backup.test/",
    )

    assert dict(tushare_client.configured_tushare_probe_endpoints()) == {
        "tushare_proxy": "http://proxy.test",
        "tushare_backup": "https://backup.test",
    }


def test_ingestion_run_retains_the_winning_provider_source(tmp_path: Path):
    from app.data_platform import db
    from app.data_platform.services import data_center

    previous_path = db.DB_PATH
    db.DB_PATH = tmp_path / "provider-source.db"
    try:
        db.init_db()
        run_id = data_center.start_ingestion_run(
            "tushare_proxy.adj_factor",
            {"api": "adj_factor", "provider": "tushare_proxy"},
            source_code="tushare_proxy",
        )
        with db._conn() as conn:
            row = conn.execute(
                "SELECT source_code,meta_json FROM data_ingestion_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        assert dict(row)["source_code"] == "tushare_proxy"
        assert "tushare_proxy" in dict(row)["meta_json"]
    finally:
        db.DB_PATH = previous_path

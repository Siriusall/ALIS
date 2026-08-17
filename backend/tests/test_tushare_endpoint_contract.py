from __future__ import annotations

from pathlib import Path

from app.data_platform import tushare_client


class _FakePro:
    pass


def test_primary_tushare_endpoint_is_configurable_and_does_not_bypass_proxy(monkeypatch):
    fake = _FakePro()
    monkeypatch.setattr(tushare_client.ts, "pro_api", lambda _token: fake)
    monkeypatch.delenv("TUSHARE_HTTP_URL", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    result = tushare_client.create_strict_tushare_pro("token")

    assert result is fake
    assert result._DataApi__http_url == "https://api.tushare.pro"
    assert "api.tushare.pro" not in str(tushare_client.os.getenv("NO_PROXY"))


def test_probe_endpoints_include_independent_official_and_fallback_sources(monkeypatch):
    monkeypatch.delenv("TUSHARE_PROBE_ENDPOINTS", raising=False)

    endpoints = dict(tushare_client.configured_tushare_probe_endpoints())

    assert endpoints["tushare_official"] == "https://api.tushare.pro"
    assert "tushare_waditu" in endpoints
    assert "tushare_proxy" in endpoints


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

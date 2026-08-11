from contextlib import contextmanager
from datetime import date
import time

import pandas as pd

from app.data_platform.services import data_center
from app.data_platform.services import research_source_discovery as discovery


def test_first_valid_research_source_wins_without_waiting_for_slow_peer(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "_eligible_endpoints", lambda *_args: [
        ("fast", "http://fast"), ("slow", "http://slow"),
    ])
    monkeypatch.setattr(discovery, "_record", lambda *_args, **_kwargs: None)

    def probe(provider, endpoint, *_args):
        if provider == "slow":
            time.sleep(0.08)
        return {
            "provider": provider, "endpoint": endpoint, "status": "available",
            "available": True, "rows": 1,
            "frame": pd.DataFrame([{"month": "202607"}]),
            "latency_ms": 1 if provider == "fast" else 80, "error": "",
        }

    monkeypatch.setattr(discovery, "_probe", probe)

    started = time.monotonic()
    result = discovery.race_tushare_sources(
        dataset="cn_ppi", target_key="202607", api_name="cn_ppi", params={},
        validator=lambda frame: not frame.empty,
    )

    assert result["winner"] == "fast"
    assert "slow" in result["pending_providers"]
    assert time.monotonic() - started < 0.06


def test_ppi_refresh_uses_local_month_without_any_network_probe(monkeypatch) -> None:
    class Result:
        def fetchone(self):
            return {"rows": 1}

    class Conn:
        def execute(self, *_args):
            return Result()

    @contextmanager
    def connection():
        yield Conn()

    monkeypatch.setattr(data_center, "_conn", connection)
    monkeypatch.setattr(
        "app.data_platform.services.research_source_discovery.race_tushare_sources",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network probe must not run")),
    )

    result = data_center.refresh_cn_ppi_from_first_source(today=date(2026, 8, 11))

    assert result["status"] == "already_current"
    assert result["target_month"] == "202607"
    assert result["network_requests"] == 0


def test_ppi_refresh_persists_first_valid_provider(monkeypatch) -> None:
    class Result:
        def fetchone(self):
            return {"rows": 0}

    class Conn:
        def execute(self, *_args):
            return Result()

    @contextmanager
    def connection():
        yield Conn()

    finished = []
    monkeypatch.setattr(data_center, "_conn", connection)
    monkeypatch.setattr(
        "app.data_platform.services.research_source_discovery.race_tushare_sources",
        lambda **_kwargs: {
            "ok": True, "status": "available", "winner": "tushare_official",
            "endpoint": "http://official", "probe_contract": "v1",
            "frame": pd.DataFrame([{"month": "202607", "ppi_yoy": 1.2}]),
            "completed": [], "pending_providers": ["tushare_proxy"],
        },
    )
    monkeypatch.setattr(data_center, "start_ingestion_run", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(data_center, "finish_ingestion_run", lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr(data_center, "_upsert_raw_records", lambda *_args: 1)
    monkeypatch.setattr(data_center, "_record_probe", lambda *_args: None)
    monkeypatch.setattr(data_center, "_record_request_coverage", lambda *_args: None)
    monkeypatch.setattr(data_center, "_resolve_failed_tushare_request", lambda *_args: None)
    monkeypatch.setattr(data_center, "_invalidate_asset_views", lambda: None)

    result = data_center.refresh_cn_ppi_from_first_source(today=date(2026, 8, 11))

    assert result["status"] == "completed"
    assert result["winner"] == "tushare_official"
    assert result["rows_written"] == 1
    assert finished

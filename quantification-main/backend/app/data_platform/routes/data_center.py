from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.data_platform.services.data_center import (
    build_stock_daily_snapshot,
    ensure_data_center_seeded,
    estimate_data_center_runtime,
    enrich_financial_research_fields,
    enrich_financial_research_batch,
    financial_research_completeness,
    get_data_center_status,
    import_industry_metrics,
    list_failed_tushare_requests,
    resume_failed_tushare_requests,
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_expectation_bundle,
    sync_financial_batch,
    sync_financial_bundle,
    sync_financial_period_bundle,
    sync_governance_bundle,
    sync_index_bundle,
    sync_market_bundle,
    sync_capital_structure_bundle,
    backfill_capital_structure_history,
    backfill_etf_constituents_history,
    backfill_etf_price_history,
    backfill_major_news_history,
    get_vip_backfill_status,
    sync_stock_basic,
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_date_batch,
    sync_tushare_stock_batch,
    sync_tushare_catalog_from_probe,
)
from app.data_platform.services.web_fallback import (
    get_web_fallback_crawl_status,
    import_legacy_news_db,
    list_web_factors,
    start_web_fallback_crawl,
)
from app.data_platform.services.daily_update import daily_update_status, simple_health, start_daily_update
from app.data_platform.services.observability import get_observability
from app.data_platform.services.runtime_logs import list_runtime_logs_since
from app.data_platform.services.readiness import readiness_overview
from app.data_platform.services.assets import get_data_assets
from app.data_platform.services.backfill import build_backfill_plan, get_backfill_status, start_backfill
from app.data_platform.services.cold_archive import archive_raw_records, cold_archive_status

router = APIRouter(tags=["data-center"])


class ColdArchiveRequest(BaseModel):
    api_names: list[str] = Field(min_length=1, max_length=20)
    start_date: str = Field(min_length=8, max_length=10)
    end_date: str = Field(min_length=8, max_length=10)
    max_rows: int = Field(default=250_000, ge=1, le=1_000_000)


@router.get("/data-center/simple-health")
def data_center_simple_health() -> dict[str, Any]:
    return simple_health()


@router.get("/data-center/observability")
def data_center_observability(limit_logs: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return get_observability(limit_logs=limit_logs)


@router.get("/data-center/runtime-logs")
def data_center_runtime_logs(
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    wait_seconds: float = Query(default=0.0, ge=0.0, le=30.0),
) -> dict[str, Any]:
    deadline = time.monotonic() + float(wait_seconds or 0)
    items = list_runtime_logs_since(after_id=after_id, limit=limit)
    while not items and wait_seconds > 0 and time.monotonic() < deadline:
        time.sleep(0.5)
        items = list_runtime_logs_since(after_id=after_id, limit=limit)
    return {"ok": True, "after_id": after_id, "total": len(items), "items": items}


@router.get("/data-center/readiness")
def data_center_readiness() -> dict[str, Any]:
    return readiness_overview()


@router.get("/data-center/assets")
def data_center_assets() -> dict[str, Any]:
    return get_data_assets()


@router.get("/data-center/cold-archive")
def data_center_cold_archive_status(limit: int = Query(default=30, ge=1, le=200)) -> dict[str, Any]:
    return cold_archive_status(limit=limit)


@router.post("/data-center/cold-archive/raw")
def data_center_archive_raw(
    req: ColdArchiveRequest,
) -> dict[str, Any]:
    try:
        return archive_raw_records(
            api_names=req.api_names, start_date=req.start_date, end_date=req.end_date, max_rows=req.max_rows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/data-center/daily-update")
def data_center_start_daily_update() -> dict[str, Any]:
    result = start_daily_update()
    if not result.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result)
    return result


@router.get("/data-center/daily-update/status")
def data_center_daily_update_status() -> dict[str, Any]:
    return daily_update_status()


@router.get("/data-center/backfill/plan")
def data_center_backfill_plan(
    market_batch_days: int = Query(default=30, ge=1, le=80),
    financial_batch_size: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    """Generate a historical completion plan from audit-window gaps."""
    return build_backfill_plan(
        market_batch_days=market_batch_days,
        financial_batch_size=financial_batch_size,
    )


@router.post("/data-center/backfill/start")
def data_center_backfill_start(
    max_steps: int = Query(default=0, ge=0, le=500),
    market_batch_days: int = Query(default=30, ge=1, le=80),
    financial_batch_size: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    """Execute the historical plan. max_steps=0 runs all auto-runnable steps."""
    result = start_backfill(
        max_steps=max_steps,
        market_batch_days=market_batch_days,
        financial_batch_size=financial_batch_size,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result)
    return result


@router.get("/data-center/backfill/status")
def data_center_backfill_status() -> dict[str, Any]:
    return get_backfill_status()


@router.post("/data-center/web-fallback/import-legacy")
def data_center_import_legacy_news(
    path: str = Query(default="../news/data/news.db", description="Legacy news.db path."),
) -> dict[str, Any]:
    """Import the latest legacy news crawl into the unified data-center database."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    try:
        return import_legacy_news_db(p)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/data-center/web-fallback/factors")
def data_center_web_factors(
    q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return list_web_factors(q=q, limit=limit, offset=offset)


@router.post("/data-center/web-fallback/crawl")
def data_center_start_web_crawl(
    codes: str = Query(default="", description="Comma-separated stock codes; empty uses the web universe."),
    limit: int = Query(default=20, ge=1, le=5000),
) -> dict[str, Any]:
    code_list = [item.strip() for item in codes.split(",") if item.strip()]
    result = start_web_fallback_crawl(codes=code_list, limit=limit)
    if not result.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result)
    return result


@router.get("/data-center/web-fallback/crawl/status")
def data_center_web_crawl_status() -> dict[str, Any]:
    return get_web_fallback_crawl_status()


@router.get("/data-center/status")
def data_center_status(limit_runs: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    """Return data-center source/catalog coverage and latest ingestion runs."""

    return get_data_center_status(limit_runs=limit_runs)


@router.post("/data-center/seed")
def data_center_seed() -> dict[str, Any]:
    """Seed TuShare source metadata and API catalog."""

    return ensure_data_center_seeded()


@router.post("/data-center/sync/stock-basic")
def data_center_sync_stock_basic() -> dict[str, Any]:
    """Sync TuShare stock_basic into dc_stock_master."""

    try:
        return sync_stock_basic()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "stock_basic_sync_failed", "message": str(exc)[:500]},
        ) from exc


@router.post("/data-center/sync/trade-calendar")
def data_center_sync_trade_calendar(
    start_date: str | None = Query(default=None, description="YYYYMMDD, defaults to recent window."),
    end_date: str | None = Query(default=None, description="YYYYMMDD, defaults to recent window."),
    exchange: str = Query(default="SSE"),
) -> dict[str, Any]:
    """Sync TuShare trade_cal into dc_trade_calendar."""

    try:
        return sync_trade_calendar(start_date=start_date, end_date=end_date, exchange=exchange)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "trade_calendar_sync_failed", "message": str(exc)[:500]},
        ) from exc


@router.post("/data-center/tushare/probe-result")
def data_center_import_probe_result(path: str = Query(..., description="Local probe JSON path.")) -> dict[str, Any]:
    """Import scripts/probe_tushare_permissions.py JSON result into the catalog status columns."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        backend_root = Path(__file__).resolve().parents[2]
        p = backend_root / p
    if not p.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "probe_result_not_found", "path": str(p)},
        )
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "probe_result_invalid", "path": str(p), "message": str(exc)[:300]},
        ) from exc
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "probe_result_not_list", "path": str(p)},
        )
    return sync_tushare_catalog_from_probe(payload)


@router.post("/data-center/sync/tushare-api/{api_name}")
def data_center_sync_tushare_api(
    api_name: str,
    params_json: str = Query(default="{}", description="JSON params for TuShare API."),
    limit_rows: int = Query(default=0, ge=0, le=100000),
) -> dict[str, Any]:
    """Sync any catalogued TuShare API into raw records."""

    try:
        params = json.loads(params_json or "{}")
        if not isinstance(params, dict):
            raise ValueError("params_json must decode to an object")
        return sync_tushare_api_raw(api_name, params=params, limit_rows=limit_rows or None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "tushare_api_sync_failed", "api": api_name, "message": str(exc)[:500]},
        ) from exc


@router.get("/data-center/tushare/failed-requests")
def data_center_failed_tushare_requests(
    request_status: str = Query(default="pending", alias="status", pattern="^(pending|resuming|resolved)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """List persisted failed calls available for selective recovery."""
    return list_failed_tushare_requests(status=request_status, limit=limit)


@router.post("/data-center/tushare/failed-requests/resume")
def data_center_resume_failed_tushare_requests(
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Retry only persisted failed TuShare calls; successful calls are not replayed."""
    return resume_failed_tushare_requests(limit=limit)


@router.post("/data-center/sync/tushare-stock-batch/{api_name}")
def data_center_sync_tushare_stock_batch(
    api_name: str,
    period: str = Query(default=""),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, Any]:
    return sync_tushare_stock_batch(api_name, period=period, start_date=start_date, end_date=end_date, limit=limit)


@router.post("/data-center/sync/tushare-date-batch/{api_name}")
def data_center_sync_tushare_date_batch(
    api_name: str,
    trade_dates: str = Query(..., description="Comma-separated YYYYMMDD dates"),
    limit: int = Query(default=10, ge=1, le=60),
) -> dict[str, Any]:
    return sync_tushare_date_batch(api_name, trade_dates=[x.strip() for x in trade_dates.split(",") if x.strip()], limit=limit)


@router.post("/data-center/sync/market")
def data_center_sync_market(
    trade_date: str = Query(..., description="YYYYMMDD"),
    include_suspend: bool = Query(default=True),
) -> dict[str, Any]:
    """Sync daily market bundle and build snapshot for one trade date."""

    return sync_market_bundle(trade_date, include_suspend=include_suspend)


@router.post("/data-center/sync/capital-structure")
def data_center_sync_capital_structure(
    trade_date: str = Query(..., description="YYYYMMDD"),
) -> dict[str, Any]:
    """Sync northbound, margin, Dragon-Tiger and eligibility evidence."""
    return sync_capital_structure_bundle(trade_date)


@router.post("/data-center/backfill/capital-structure")
def data_center_backfill_capital_structure(
    start_date: str = Query(..., description="YYYYMMDD"),
    end_date: str = Query(..., description="YYYYMMDD"),
    cursor: str = Query(default="", description="Last completed YYYYMMDD"),
    limit_dates: int = Query(default=10, ge=1, le=60),
) -> dict[str, Any]:
    """Run one resumable historical capital-structure slice."""
    return backfill_capital_structure_history(
        start_date=start_date, end_date=end_date, cursor=cursor, limit_dates=limit_dates
    )


@router.post("/data-center/backfill/etf-history")
def data_center_backfill_etf_history(
    start_date: str = Query(..., description="YYYYMMDD"),
    end_date: str = Query(..., description="YYYYMMDD"),
    cursor: str = Query(default="", description="Last completed ETF ts_code"),
    limit_funds: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Run one resumable ETF price/NAV history slice."""
    return backfill_etf_price_history(
        start_date=start_date, end_date=end_date, cursor=cursor, limit_funds=limit_funds
    )


@router.post("/data-center/backfill/etf-constituents")
def data_center_backfill_etf_constituents(
    start_date: str = Query(..., description="YYYYMMDD"),
    end_date: str = Query(..., description="YYYYMMDD"),
    cursor: str = Query(default="", description="Last completed YYYYMMDD"),
    limit_dates: int = Query(default=20, ge=1, le=60),
) -> dict[str, Any]:
    """Run one resumable Shanghai/Shenzhen ETF basket history slice."""
    return backfill_etf_constituents_history(
        start_date=start_date, end_date=end_date, cursor=cursor, limit_dates=limit_dates
    )


@router.post("/data-center/backfill/major-news")
def data_center_backfill_major_news(
    start_date: str = Query(..., description="YYYYMMDD"),
    end_date: str = Query(..., description="YYYYMMDD"),
    cursor: str = Query(default="", description="Last completed YYYYMMDD"),
    limit_days: int = Query(default=7, ge=1, le=31),
) -> dict[str, Any]:
    """Run one resumable source-by-day long-form news slice."""
    return backfill_major_news_history(
        start_date=start_date, end_date=end_date, cursor=cursor, limit_days=limit_days
    )


@router.get("/data-center/backfill/vip-status")
def data_center_vip_backfill_status(
    capital_start: str = Query(default="20200101", description="YYYYMMDD"),
    news_start: str = Query(default="20230719", description="YYYYMMDD"),
    end_date: str = Query(..., description="YYYYMMDD"),
) -> dict[str, Any]:
    """Inspect exact request-slice completion for VIP datasets."""
    return get_vip_backfill_status(
        capital_start=capital_start, news_start=news_start, end_date=end_date
    )


@router.post("/data-center/sync/financial")
def data_center_sync_financial(
    ts_code: str = Query(...),
    period: str = Query(..., description="YYYYMMDD report period"),
    include_yearly: bool = Query(default=True),
) -> dict[str, Any]:
    """Sync financial statement/indicator bundle for one stock and report period."""

    return sync_financial_bundle(ts_code, period, include_yearly=include_yearly)


@router.get("/data-center/financial-research-completeness/{ts_code}")
def data_center_financial_research_completeness(
    ts_code: str, period_limit: int = Query(default=8, ge=1, le=12),
) -> dict[str, Any]:
    return financial_research_completeness(ts_code, period_limit=period_limit)


@router.post("/data-center/financial-research-enrich/{ts_code}")
def data_center_financial_research_enrich(
    ts_code: str, period_limit: int = Query(default=8, ge=1, le=12),
    request_limit: int = Query(default=32, ge=1, le=64),
) -> dict[str, Any]:
    return enrich_financial_research_fields(ts_code, period_limit=period_limit, request_limit=request_limit)


@router.post("/data-center/financial-research-enrich-batch")
def data_center_financial_research_enrich_batch(
    period: str = Query(..., description="YYYYMMDD report period"),
    limit: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    return enrich_financial_research_batch(period, limit=limit)


@router.post("/data-center/sync/financial-period")
def data_center_sync_financial_period(
    period: str = Query(..., description="YYYYMMDD report period"),
    include_yearly: bool = Query(default=False),
) -> dict[str, Any]:
    """Bulk sync financial bundle for all stocks in one report period."""

    return sync_financial_period_bundle(period, include_yearly=include_yearly)


@router.post("/data-center/sync/financial-batch")
def data_center_sync_financial_batch(
    period: str = Query(..., description="YYYYMMDD report period"),
    codes: str = Query(default="", description="Comma-separated ts_codes."),
    limit: int = Query(default=20, ge=1, le=500),
    include_yearly: bool = Query(default=False),
) -> dict[str, Any]:
    """Sync financial bundle for a staged stock batch."""

    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    return sync_financial_batch(period, codes=code_list, limit=limit, include_yearly=include_yearly)


@router.post("/data-center/sync/governance")
def data_center_sync_governance(
    ts_code: str = Query(...),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """Sync shareholder/dividend/pledge/disclosure bundle for one stock."""

    return sync_governance_bundle(ts_code, period=period)


@router.post("/data-center/sync/expectation")
def data_center_sync_expectation(
    ts_code: str = Query(...),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """Sync forecast/express bundle for one stock."""

    return sync_expectation_bundle(ts_code, period=period)


@router.post("/data-center/sync/index")
def data_center_sync_index(
    index_code: str = Query(default="000300.SH"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
) -> dict[str, Any]:
    """Sync index metadata/daily/weight bundle."""

    return sync_index_bundle(index_code=index_code, start_date=start_date, end_date=end_date)


@router.post("/data-center/snapshot")
def data_center_build_snapshot(trade_date: str = Query(..., description="YYYYMMDD")) -> dict[str, Any]:
    """Build one-day stock snapshot from raw records."""

    return build_stock_daily_snapshot(trade_date)


@router.post("/data-center/quality")
def data_center_quality(trade_date: str | None = Query(default=None)) -> dict[str, Any]:
    """Run basic data-center quality checks."""

    return run_data_quality_checks(trade_date=trade_date)


@router.post("/data-center/sync/announcements")
def data_center_sync_announcements(
    start_date: str = Query(..., description="YYYYMMDD or YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYYMMDD or YYYY-MM-DD"),
    ts_code: str | None = Query(default=None),
    page_size: int = Query(default=30, ge=1, le=50),
    max_pages: int = Query(default=1, ge=1, le=20),
) -> dict[str, Any]:
    """Sync CNINFO announcement metadata and coarse rule-based events."""

    try:
        return sync_cninfo_announcements(
            start_date=start_date,
            end_date=end_date,
            ts_code=ts_code,
            page_size=page_size,
            max_pages=max_pages,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "announcement_sync_failed", "message": str(exc)[:500]},
        ) from exc


@router.post("/data-center/import/industry-metrics")
def data_center_import_industry_metrics(path: str = Query(..., description="Local JSON/CSV path.")) -> dict[str, Any]:
    """Import industry metric definitions and values from local JSON/CSV."""

    try:
        return import_industry_metrics(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "industry_metric_import_failed", "message": str(exc)[:500]},
        ) from exc


@router.get("/data-center/estimate-runtime")
def data_center_estimate_runtime(years: int = Query(default=10, ge=1, le=30)) -> dict[str, Any]:
    """Estimate first full-load and daily incremental runtime."""

    return estimate_data_center_runtime(years=years)

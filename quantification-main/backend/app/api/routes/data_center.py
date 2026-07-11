from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.services.data_center import (
    build_stock_daily_snapshot,
    ensure_data_center_seeded,
    estimate_data_center_runtime,
    get_data_center_status,
    import_industry_metrics,
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_expectation_bundle,
    sync_financial_batch,
    sync_financial_bundle,
    sync_financial_period_bundle,
    sync_governance_bundle,
    sync_index_bundle,
    sync_market_bundle,
    sync_stock_basic,
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_catalog_from_probe,
)

router = APIRouter(tags=["data-center"])


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
        backend_root = Path(__file__).resolve().parents[3]
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


@router.post("/data-center/sync/market")
def data_center_sync_market(
    trade_date: str = Query(..., description="YYYYMMDD"),
    include_suspend: bool = Query(default=False),
) -> dict[str, Any]:
    """Sync daily market bundle and build snapshot for one trade date."""

    return sync_market_bundle(trade_date, include_suspend=include_suspend)


@router.post("/data-center/sync/financial")
def data_center_sync_financial(
    ts_code: str = Query(...),
    period: str = Query(..., description="YYYYMMDD report period"),
    include_yearly: bool = Query(default=True),
) -> dict[str, Any]:
    """Sync financial statement/indicator bundle for one stock and report period."""

    return sync_financial_bundle(ts_code, period, include_yearly=include_yearly)


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

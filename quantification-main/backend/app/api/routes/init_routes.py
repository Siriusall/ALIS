from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.services.init_service import get_data_init_status, run_data_initialization

router = APIRouter(tags=["init"])


@router.post("/init")
def post_init(
    all_in_one_only: bool = Query(default=True, description="Keep only all-in-one cache after initialization."),
    strict: bool = Query(default=True, description="Fail when first-load data quality checks do not pass."),
    allow_remote: bool = Query(default=True, description="Allow TuShare/remote market-data calls."),
) -> dict:
    """Run explicit market-data initialization.

    This endpoint replaces heavy startup initialization. The service should boot
    first; data refresh is an operator action or scheduled task.
    """

    try:
        result = run_data_initialization(
            all_in_one_only=all_in_one_only,
            strict=strict,
            allow_remote=allow_remote,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_508_LOOP_DETECTED,
            detail={
                "code": "init_failed_508",
                "message": "Data initialization failed. Check /api/init/status and backend logs.",
                "error": str(exc)[:500],
            },
        ) from exc

    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_508_LOOP_DETECTED,
            detail={
                "code": "init_quality_check_failed_508",
                "message": "Data initialization quality check failed.",
                "result": result,
            },
        )
    return result


@router.get("/init/status")
def get_init_status() -> dict:
    """Return the latest explicit data-initialization status."""

    return get_data_init_status()


@router.get("/cache/all")
def get_cache_all(
    date: str | None = Query(default=..., description="Required YYYYMMDD date."),
    symbols: str | None = Query(default=None, description="Optional comma-separated stock symbols."),
) -> dict:
    """Read the all-in-one cache for one trade date."""

    from app.services.cache_consolidation import (
        MAX_SYMBOL_ROWS_PER_REQUEST,
        compute_factor_coverage,
        read_consolidated_cache,
    )

    if not date or not str(date).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "date_required",
                "message": "Pass date=YYYYMMDD. Only one trade date is supported per request.",
            },
        )

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    payload = read_consolidated_cache(date=date, symbols=symbol_list)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "postgres_snapshot_not_found",
                "message": "No PostgreSQL market snapshot exists for this date. Run data-center sync/backfill first.",
                "date": date,
            },
        )

    counts = payload.get("counts") or {}
    rows = int(counts.get("symbol_rows") or 0)
    if not symbol_list and rows > MAX_SYMBOL_ROWS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "too_many_rows",
                "message": (
                    f"Date {date} contains {rows} rows, above the single-request limit "
                    f"{MAX_SYMBOL_ROWS_PER_REQUEST}. Pass symbols or split the request."
                ),
                "date": date,
                "symbol_rows": rows,
                "max_rows": MAX_SYMBOL_ROWS_PER_REQUEST,
            },
        )

    date_ymd = str(date).strip().replace("-", "") if date else ""
    if date_ymd and isinstance(payload.get("data"), dict):
        payload = dict(payload)
        if not payload.get("factor_coverage") or date_ymd not in (payload.get("factor_coverage") or {}):
            coverage = compute_factor_coverage(payload["data"])
            payload["factor_coverage"] = dict(payload.get("factor_coverage") or {})
            payload["factor_coverage"].update(coverage)
        payload["factor_coverage_summary"] = (payload.get("factor_coverage") or {}).get(date_ymd) or {}
    return payload


@router.post("/init/full-sync")
def post_init_full_sync(
    all_in_one_only: bool = Query(default=True, description="Clean source caches after full sync."),
) -> dict:
    """PostgreSQL is already the historical source of truth; no file merge runs."""
    return {
        "ok": True,
        "started": False,
        "storage": "postgresql",
        "message": "No all-in-one file merge is required. Use data-center backfill to extend PostgreSQL history.",
    }


@router.get("/init/full-sync/status")
def get_init_full_sync_status() -> dict:
    """Return PostgreSQL canonical-storage status for legacy callers."""
    return {"status": "not_required", "storage": "postgresql", "message": "Historical data is read directly from PostgreSQL."}


@router.post("/init/backfill-merge-meta")
def post_init_backfill_merge_meta(
    force: bool = Query(default=False, description="Force a full rescan even when marker exists."),
    async_run: bool = Query(default=True, description="Run in background by default."),
) -> dict:
    """Manually backfill all-in-one merge metadata."""

    return {
        "ok": True,
        "started": False,
        "running": False,
        "storage": "postgresql",
        "message": "Merge metadata belongs to retired file caches and is not required.",
    }

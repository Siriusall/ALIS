"""One read request per shared-console page."""

from fastapi import APIRouter, HTTPException, Query, Response

from app.services.console_snapshots import PAGE_KEYS, read, request_refresh, status


router = APIRouter(tags=["console-snapshots"])


@router.get("/console/snapshots/{page_key}")
def console_page_snapshot(
    page_key: str,
    response: Response,
    refresh: bool = Query(default=False, description="Queue a background rebuild; never blocks this read."),
) -> dict:
    try:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        if refresh:
            # The browser can request freshness but never executes a build.
            # Coalesce repeated clicks while a control-worker build is already
            # in flight, so its compare-and-swap can still commit the result.
            request_refresh(page_key)
        return read(page_key, force=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "unknown_snapshot_page", "pages": PAGE_KEYS}) from exc


@router.get("/console/snapshots")
def console_snapshot_status() -> dict:
    return status()

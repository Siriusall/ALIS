"""One read request per shared-console page."""

from fastapi import APIRouter, HTTPException, Query

from app.services.console_snapshots import PAGE_KEYS, read, status


router = APIRouter(tags=["console-snapshots"])


@router.get("/console/snapshots/{page_key}")
def console_page_snapshot(
    page_key: str,
    refresh: bool = Query(default=False, description="Queue a background rebuild; never blocks this read."),
) -> dict:
    try:
        return read(page_key, force=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "unknown_snapshot_page", "pages": PAGE_KEYS}) from exc


@router.get("/console/snapshots")
def console_snapshot_status() -> dict:
    return status()

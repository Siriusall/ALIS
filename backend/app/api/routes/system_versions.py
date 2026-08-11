from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.system_release_center import (
    CONTRACT_CATALOG,
    activate_release,
    create_release,
    list_releases,
    release_diff,
    release_events,
    release_status,
    rollback_release,
)

router = APIRouter(tags=["system-versions"])


@router.get("/system-versions/status")
def system_version_status() -> dict:
    return release_status()


@router.get("/system-versions/releases")
def system_version_releases(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    return {"ok": True, "items": list_releases(limit)}


@router.get("/system-versions/contracts")
def system_version_contracts() -> dict:
    return {"ok": True, "schema_version": "alis_contract_catalog_v1", "items": CONTRACT_CATALOG}


@router.get("/system-versions/releases/{release_id}/events")
def system_version_events(release_id: int) -> dict:
    return {"ok": True, "items": release_events(release_id)}


@router.get("/system-versions/diff")
def system_version_diff(left_id: int, right_id: int) -> dict:
    try:
        return release_diff(left_id, right_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/system-versions/releases")
def system_version_create() -> dict:
    return create_release(actor="owner_api", reason="explicit_owner_release_build")


@router.post("/system-versions/releases/{release_id}/activate")
def system_version_activate(release_id: int) -> dict:
    try:
        return activate_release(release_id, actor="owner_api", reason="explicit_owner_activation")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/system-versions/releases/{release_id}/rollback")
def system_version_rollback(release_id: int, reason: str = "owner_requested") -> dict:
    try:
        return rollback_release(release_id, actor="owner_api", reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

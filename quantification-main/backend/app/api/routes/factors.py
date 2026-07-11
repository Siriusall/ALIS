from __future__ import annotations

from fastapi import APIRouter

from app.services.factor_registry import factor_library_summary

router = APIRouter()


@router.get("/factors")
def list_factors() -> dict:
    return factor_library_summary()

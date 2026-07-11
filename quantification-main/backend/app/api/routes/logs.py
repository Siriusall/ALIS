from fastapi import APIRouter

from app.api.state import get_logs
from app.schemas.log import LogEntryResponse


router = APIRouter()


@router.get("/logs", response_model=list[LogEntryResponse])
def list_logs() -> list[LogEntryResponse]:
    return get_logs()

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query

from app.api.state import get_logs
from app.schemas.log import LogEntryResponse


router = APIRouter()


@router.get("/logs", response_model=list[LogEntryResponse])
def list_logs() -> list[LogEntryResponse]:
    return get_logs()


def _backend_log_path(source: str) -> Path:
    project_root = Path(__file__).resolve().parents[4]
    log_dir = Path(os.getenv("ALIS_BACKEND_LOG_DIR", str(project_root / "outputs"))).resolve()
    filename = "backend.err.log" if source == "stderr" else "backend.out.log"
    return log_dir / filename


@router.get("/logs/backend")
def read_backend_log(
    source: Literal["stdout", "stderr"] = "stdout",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=1024 * 1024, ge=1, le=4 * 1024 * 1024),
) -> dict:
    """Return raw backend process output without parsing or rewriting it."""
    path = _backend_log_path(source)
    if not path.exists() or not path.is_file():
        return {"source": source, "text": "", "offset": 0, "next_offset": 0, "size": 0, "eof": True,
                "current_pid": os.getpid(), "file_updated_at": None, "provenance": "missing"}
    stat = path.stat()
    size = stat.st_size
    start = offset if offset <= size else 0
    with path.open("rb") as stream:
        stream.seek(start)
        payload = stream.read(limit)
        next_offset = stream.tell()
    return {
        "source": source,
        "text": payload.decode("utf-8", errors="replace"),
        "offset": start,
        "next_offset": next_offset,
        "size": size,
        "eof": next_offset >= size,
        "current_pid": os.getpid(),
        "file_updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "provenance": "unverified_process_file",
        "provenance_note": "该文件未绑定当前服务 PID，仅作为历史进程输出查看；实时运行证据请以结构化系统日志为准",
    }

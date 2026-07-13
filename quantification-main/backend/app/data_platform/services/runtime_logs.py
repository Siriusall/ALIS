from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn


def write_runtime_log(level: str, logger: str, message: str, context: dict[str, Any] | None = None) -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO dc_runtime_logs(level,logger,message,context_json,created_at) VALUES(?,?,?,?,?)",
                (level.upper(), logger, message[:2000], json.dumps(context or {}, ensure_ascii=False, default=str)[:10000], datetime.now().isoformat(timespec="seconds")),
            )
    except Exception:
        # Logging must never break the data path.
        return


def list_runtime_logs(limit: int = 200) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id,level,logger,message,context_json,created_at FROM dc_runtime_logs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["context"] = json.loads(item.pop("context_json") or "{}")
        except Exception:
            item["context"] = {}
        items.append(item)
    return items

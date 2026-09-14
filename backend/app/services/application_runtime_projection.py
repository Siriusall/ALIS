"""Persist the application node's compact runtime truth for DB-local snapshots."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.store import _conn, _read_conn


KIND = "health"
MAX_AGE_SECONDS = 180


def publish_health(payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    compact = {
        "status": payload.get("status"),
        "ready": bool(payload.get("ready")),
        "degraded": bool(payload.get("degraded")),
        "issues": list(payload.get("issues") or []),
        "advisories": list(payload.get("advisories") or []),
        "dependencies": {
            key: (payload.get("dependencies") or {}).get(key)
            for key in ("primary_database", "data_platform_database", "database_schema_revisions", "release_runtime", "redis")
        },
    }
    with _conn() as conn:
        conn.execute(
            """INSERT INTO application_runtime_projections
                 (projection_kind,source_role,source_instance,observed_at,expires_at,payload_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (projection_kind) DO UPDATE SET
                 source_role=EXCLUDED.source_role,
                 source_instance=EXCLUDED.source_instance,
                 observed_at=EXCLUDED.observed_at,
                 expires_at=EXCLUDED.expires_at,
                 payload_json=EXCLUDED.payload_json""",
            (
                KIND,
                "application",
                f"{socket.gethostname()}:{os.getpid()}",
                now.isoformat(),
                (now + timedelta(seconds=MAX_AGE_SECONDS)).isoformat(),
                json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
            ),
        )


def read_health() -> dict[str, Any]:
    with _read_conn() as conn:
        row = conn.execute(
            """SELECT source_role,source_instance,observed_at,expires_at,payload_json
                 FROM application_runtime_projections WHERE projection_kind=?""",
            (KIND,),
        ).fetchone()
    if not row:
        return _unavailable("application_runtime_projection_missing")
    record = dict(row)
    try:
        expires = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        payload = json.loads(record.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _unavailable("application_runtime_projection_invalid")
    if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc):
        return _unavailable(
            "application_runtime_projection_stale",
            observed_at=record.get("observed_at"),
            source_instance=record.get("source_instance"),
        )
    return {
        **payload,
        "source_role": record.get("source_role"),
        "source_instance": record.get("source_instance"),
        "observed_at": record.get("observed_at"),
        "expires_at": record.get("expires_at"),
        "projection_status": "current",
    }


def _unavailable(reason: str, **evidence: Any) -> dict[str, Any]:
    return {
        "status": "unknown",
        "ready": False,
        "degraded": True,
        "issues": [reason],
        "source_role": "application",
        "projection_status": "stale" if reason.endswith("stale") else "unavailable",
        **evidence,
    }

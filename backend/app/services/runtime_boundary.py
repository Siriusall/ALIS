"""Cross-process ownership boundary for ALIS background work."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger("quant.runtime_boundary")

# Stable signed bigint reserved for the ALIS background-runtime namespace.
BACKGROUND_RUNTIME_LOCK_ID = 4_701_529_026_072_026


def _postgres_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    return url


@dataclass
class BackgroundRuntimeLease:
    mode: str
    owner: str
    acquired: bool = False
    reason: str = "not_started"
    connection: Any | None = None

    @property
    def should_start(self) -> bool:
        return self.mode == "enabled" and self.acquired

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "owner": self.owner,
            "acquired": self.acquired,
            "should_start": self.should_start,
            "reason": self.reason,
            "lock_contract": "postgres_session_advisory_lock_v1",
            "lock_id": BACKGROUND_RUNTIME_LOCK_ID,
        }

    def release(self) -> None:
        connection = self.connection
        self.connection = None
        if connection is None:
            return
        try:
            if self.acquired:
                connection.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (BACKGROUND_RUNTIME_LOCK_ID,),
                )
        except Exception:  # noqa: BLE001 - closing the session still releases the lock
            logger.exception("Failed to explicitly release background runtime lease")
        finally:
            connection.close()
            self.acquired = False
            self.reason = "released"


def acquire_background_runtime_lease(
    *,
    mode: str | None = None,
    database_url: str | None = None,
    connect: Callable[..., Any] | None = None,
) -> BackgroundRuntimeLease:
    selected_mode = (mode or settings.background_runtime_mode).strip().lower()
    lease = BackgroundRuntimeLease(
        mode=selected_mode,
        owner=f"{socket.gethostname()}:{os.getpid()}",
    )
    if selected_mode == "disabled":
        lease.reason = "explicitly_disabled"
        return lease
    if selected_mode != "enabled":
        raise ValueError("background runtime mode must be enabled or disabled")

    if connect is None:
        import psycopg

        connect = psycopg.connect
    connection = connect(
        _postgres_url(database_url or settings.database_url),
        connect_timeout=settings.database_connect_timeout_seconds,
        autocommit=True,
    )
    row = connection.execute(
        "SELECT pg_try_advisory_lock(%s) AS acquired",
        (BACKGROUND_RUNTIME_LOCK_ID,),
    ).fetchone()
    acquired = bool(row["acquired"] if isinstance(row, dict) else row[0])
    if not acquired:
        connection.close()
        lease.reason = "held_by_another_process"
        return lease
    lease.connection = connection
    lease.acquired = True
    lease.reason = "acquired"
    return lease

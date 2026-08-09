"""Small process-local guard for PostgreSQL connection creation.

The runtime intentionally uses short-lived psycopg connections rather than a
third-party pool.  A bounded gate still prevents a burst of synchronous API
requests from exhausting PostgreSQL's small production connection budget.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator


class DatabaseConnectionCapacity(RuntimeError):
    """Raised when this process cannot safely open another DB connection."""


class DatabaseConnectionGate:
    def __init__(self, env_name: str, *, default: int = 10) -> None:
        try:
            configured = int(os.getenv(env_name, str(default)))
        except (TypeError, ValueError):
            configured = default
        self.limit = max(1, configured)
        self._slots = threading.BoundedSemaphore(self.limit)

    @contextmanager
    def slot(self, timeout: float) -> Iterator[None]:
        acquired = self._slots.acquire(timeout=max(0.1, float(timeout)))
        if not acquired:
            raise DatabaseConnectionCapacity(
                f"database connection capacity exhausted (limit={self.limit})"
            )
        try:
            yield
        finally:
            self._slots.release()

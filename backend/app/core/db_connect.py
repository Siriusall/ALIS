"""Bounded retries for transient PostgreSQL connection establishment."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger("quant.db_connect")


def connect_postgres(
    connect: Callable[..., Any], url: str, *, timeout: int, role: str, **kwargs: Any
) -> Any:
    """Retry only before a PostgreSQL connection exists.

    No query or transaction is retried here. Once ``connect`` succeeds, the
    caller owns the connection and normal transaction boundaries apply.
    """
    attempts = max(1, min(int(os.getenv("DATABASE_CONNECT_RETRY_ATTEMPTS", "2")), 3))
    backoff = max(0.05, min(float(os.getenv("DATABASE_CONNECT_RETRY_BACKOFF_SECONDS", "0.35")), 2.0))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            connection = connect(url, connect_timeout=timeout, **kwargs)
            logger.debug(
                "database connection established role=%s attempt=%s elapsed_ms=%.1f",
                role, attempt,
                (time.perf_counter() - started) * 1000,
            )
            return connection
        except Exception as exc:  # psycopg exposes several version-specific connection errors
            last_error = exc
            if attempt >= attempts:
                raise
            logger.warning(
                "database connection attempt failed role=%s attempt=%s/%s elapsed_ms=%.1f error=%s",
                role, attempt, attempts,
                (time.perf_counter() - started) * 1000, type(exc).__name__,
            )
            time.sleep(backoff * attempt)
    raise last_error or RuntimeError("database connection failed")

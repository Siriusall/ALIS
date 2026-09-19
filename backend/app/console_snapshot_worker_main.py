"""Standalone control-worker entry point for shared console snapshots."""

from __future__ import annotations

import logging
import signal
import threading

from app.data_platform.db import _conn as data_platform_conn
from app.db.store import _conn as business_conn
from app.services.console_snapshots import record_worker_heartbeat, run_snapshot_worker_forever


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    record_worker_heartbeat(status="starting", force=True)
    try:
        # Verify both authorities before the worker begins its page aggregations.
        with business_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        with data_platform_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        record_worker_heartbeat(
            status="failed",
            last_error_code="snapshot_worker_startup_dependency_unavailable",
            details={"error_type": type(exc).__name__},
            force=True,
        )
        raise
    run_snapshot_worker_forever(stop_event)


if __name__ == "__main__":
    main()

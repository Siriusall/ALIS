"""Standalone process entry point for the PostgreSQL-leased evolution worker."""

from __future__ import annotations

import logging
import signal
import threading

from app.data_platform.db import _conn as data_platform_conn
from app.db.store import _conn as business_conn
from app.services.evolution_orchestrator import (
    recover_evolution_tasks,
    start_evolution_worker,
)


logger = logging.getLogger("quant.evolution_worker")


def _verify_database_connections() -> None:
    """Verify the API-owned schemas are reachable without performing migrations."""

    with business_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    with data_platform_conn() as conn:
        conn.execute("SELECT 1").fetchone()


def run_forever(stop_event: threading.Event | None = None) -> None:
    """Verify shared schemas, recover expired leases, and serve until stopped."""

    stopper = stop_event or threading.Event()
    _verify_database_connections()
    recovered = recover_evolution_tasks()
    started = start_evolution_worker()
    logger.info(
        "Standalone evolution worker ready: started=%s recovered=%s",
        started,
        recovered,
    )
    while not stopper.wait(5):
        continue


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_forever(stop_event)


if __name__ == "__main__":
    main()

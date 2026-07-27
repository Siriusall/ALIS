"""Standalone process entry point for the PostgreSQL-leased evolution worker."""

from __future__ import annotations

import logging
import signal
import threading

from app.data_platform.db import init_db as init_data_platform_db
from app.db.store import init_db
from app.services.evolution_orchestrator import (
    recover_evolution_tasks,
    start_evolution_worker,
)


logger = logging.getLogger("quant.evolution_worker")


def run_forever(stop_event: threading.Event | None = None) -> None:
    """Initialize shared schemas, recover expired leases, and serve until stopped."""

    stopper = stop_event or threading.Event()
    init_db()
    init_data_platform_db()
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

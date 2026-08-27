"""Standalone ALIS system-impact email alert worker."""

from __future__ import annotations

import logging
import os
import signal
import threading

from app.services.system_alerts import ALERT_RECIPIENT, run_alert_cycle

logger = logging.getLogger("quant.alert_worker")


def run_forever(stop_event: threading.Event | None = None) -> None:
    stopper = stop_event or threading.Event()
    interval = max(30, min(int(os.getenv("ALIS_ALERT_INTERVAL_SECONDS", "60")), 3600))
    logger.info("ALIS alert worker started recipient=%s interval=%ss", ALERT_RECIPIENT, interval)
    while not stopper.is_set():
        result = run_alert_cycle()
        logger.info(
            "Alert cycle issues=%s new=%s recovered=%s delivered=%s",
            result["issue_count"], result["new_count"], result["recovered_count"], result["ok"],
        )
        stopper.wait(interval)


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

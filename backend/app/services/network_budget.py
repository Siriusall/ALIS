"""Host-local network budget for optional cross-node workloads."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _enabled() -> bool:
    return str(os.getenv("ALIS_NETWORK_BUDGET_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _daily_budget_bytes() -> int:
    raw = str(os.getenv("ALIS_NETWORK_DAILY_BUDGET_MB", "1024")).strip()
    try:
        megabytes = max(128, int(raw))
    except ValueError:
        megabytes = 1024
    return megabytes * 1024 * 1024


def _interface() -> str:
    return str(os.getenv("ALIS_NETWORK_BUDGET_INTERFACE", "eth0")).strip() or "eth0"


def _direction() -> str:
    value = str(os.getenv("ALIS_NETWORK_BUDGET_DIRECTION", "total")).strip().lower()
    return value if value in {"rx", "tx", "total"} else "total"


def _state_path() -> Path:
    return Path(
        str(
            os.getenv(
                "ALIS_NETWORK_BUDGET_STATE_PATH",
                "/var/lib/alis-network-budget/state.json",
            )
        ).strip()
    )


def _read_counter(interface: str, direction: str) -> int:
    root = Path("/sys/class/net") / interface / "statistics"
    rx = int((root / "rx_bytes").read_text(encoding="ascii").strip())
    tx = int((root / "tx_bytes").read_text(encoding="ascii").strip())
    if direction == "rx":
        return rx
    if direction == "tx":
        return tx
    return rx + tx


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def network_budget_status(
    *,
    now: datetime | None = None,
    counter_reader: Callable[[str, str], int] | None = None,
) -> dict[str, Any]:
    """Return and persist today's interface usage.

    The first observation establishes a baseline. Persisted accumulated usage
    survives process restarts; a host reboot resets the kernel counter but not
    the amount already accumulated for the day.
    """

    budget_bytes = _daily_budget_bytes()
    if not _enabled():
        return {
            "enabled": False,
            "available": True,
            "exhausted": False,
            "budget_bytes": budget_bytes,
            "used_bytes": 0,
            "remaining_bytes": budget_bytes,
        }
    current_time = now or datetime.now().astimezone()
    day = current_time.date().isoformat()
    interface = _interface()
    direction = _direction()
    reader = counter_reader or _read_counter
    try:
        counter = max(0, int(reader(interface, direction)))
        path = _state_path()
        with _lock, _file_lock(path):
            state = _load_state(path)
            if (
                str(state.get("day") or "") != day
                or str(state.get("interface") or "") != interface
                or str(state.get("direction") or "") != direction
            ):
                used_bytes = 0
                previous_counter = counter
            else:
                try:
                    used_bytes = max(0, int(state.get("used_bytes") or 0))
                    previous_counter = max(0, int(state.get("last_counter") or 0))
                except (TypeError, ValueError):
                    used_bytes = 0
                    previous_counter = counter
                if counter >= previous_counter:
                    used_bytes += counter - previous_counter
            state = {
                "day": day,
                "interface": interface,
                "direction": direction,
                "last_counter": counter,
                "used_bytes": used_bytes,
                "updated_at": current_time.isoformat(),
            }
            _save_state(path, state)
    except (OSError, TypeError, ValueError) as exc:
        return {
            "enabled": True,
            "available": False,
            "exhausted": True,
            "budget_bytes": budget_bytes,
            "used_bytes": None,
            "remaining_bytes": 0,
            "interface": interface,
            "direction": direction,
            "error_code": "network_counter_unavailable",
            "error_type": type(exc).__name__,
        }
    remaining = max(0, budget_bytes - used_bytes)
    return {
        "enabled": True,
        "available": True,
        "exhausted": used_bytes >= budget_bytes,
        "budget_bytes": budget_bytes,
        "used_bytes": used_bytes,
        "remaining_bytes": remaining,
        "usage_ratio": round(used_bytes / budget_bytes, 6),
        "interface": interface,
        "direction": direction,
        "day": day,
    }


def optional_network_work_allowed() -> bool:
    status = network_budget_status()
    return bool(status.get("available")) and not bool(status.get("exhausted"))

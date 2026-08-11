"""Cooperative cancellation for bounded autonomy steps.

Database connections opened by an autonomy worker register with its scope.
When the bus deadline expires, PostgreSQL ``cancel()`` is sent to every live
connection owned by that step.  This prevents a timed-out SQL statement from
continuing to consume a pool slot after the bus has moved on.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import uuid
from typing import Any, Iterator


_active_scope: ContextVar[str] = ContextVar("alis_autonomy_step_scope", default="")
_lock = threading.Lock()
_connections: dict[str, dict[int, Any]] = {}
_cancelled: set[str] = set()


@contextmanager
def autonomy_step_scope() -> Iterator[str]:
    scope_id = uuid.uuid4().hex
    token = _active_scope.set(scope_id)
    with _lock:
        _connections[scope_id] = {}
    try:
        yield scope_id
    finally:
        with _lock:
            _connections.pop(scope_id, None)
            _cancelled.discard(scope_id)
        _active_scope.reset(token)


def register_postgres_connection(raw: Any) -> tuple[str, int] | None:
    scope_id = _active_scope.get()
    if not scope_id:
        return None
    registration = (scope_id, id(raw))
    with _lock:
        bucket = _connections.get(scope_id)
        if bucket is not None:
            bucket[id(raw)] = raw
    return registration


def unregister_postgres_connection(registration: tuple[str, int] | None) -> None:
    if registration is None:
        return
    scope_id, connection_id = registration
    with _lock:
        bucket = _connections.get(scope_id)
        if bucket is not None:
            bucket.pop(connection_id, None)


def cancel_autonomy_scope(scope_id: str) -> int:
    with _lock:
        _cancelled.add(scope_id)
        connections = list((_connections.get(scope_id) or {}).values())
    cancelled = 0
    for raw in connections:
        try:
            raw.cancel()
            cancelled += 1
        except Exception:
            # Closing/failed connections are already no longer consuming a
            # live query; cancellation remains best-effort and auditable.
            continue
    return cancelled


def cancellation_requested() -> bool:
    scope_id = _active_scope.get()
    if not scope_id:
        return False
    with _lock:
        return scope_id in _cancelled


def raise_if_cancelled() -> None:
    if cancellation_requested():
        raise RuntimeError("autonomy_step_cancelled")

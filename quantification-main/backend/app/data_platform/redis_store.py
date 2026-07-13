from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover
    redis = None

_client = None
_last_connection_failure_at = 0.0
_CLIENT_RETRY_SECONDS = 5.0
_client_lock = threading.Lock()
_worker_started = False


def client():
    global _client, _last_connection_failure_at
    if _client is not None or redis is None:
        return _client
    if time.monotonic() - _last_connection_failure_at < _CLIENT_RETRY_SECONDS:
        return None
    with _client_lock:
        if _client is not None:
            return _client
        if time.monotonic() - _last_connection_failure_at < _CLIENT_RETRY_SECONDS:
            return None
        try:
            value = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                socket_connect_timeout=0.5,
                socket_timeout=1.0,
                decode_responses=True,
            )
            value.ping()
            _client = value
            _last_connection_failure_at = 0.0
        except Exception:
            _last_connection_failure_at = time.monotonic()
    return _client


def get_json(key: str) -> Any | None:
    value = client()
    if value is None:
        return None
    try:
        raw = value.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def set_json(key: str, payload: Any, ttl: int = 300) -> bool:
    value = client()
    if value is None:
        return False
    try:
        value.set(key, json.dumps(payload, ensure_ascii=False, default=str), ex=ttl)
        return True
    except Exception:
        return False


def delete(key: str) -> None:
    value = client()
    if value is not None:
        try:
            value.delete(key)
        except Exception:
            pass


def set_add(key: str, member: str) -> None:
    value = client()
    if value is not None:
        try:
            value.sadd(key, member)
        except Exception:
            pass


def set_remove(key: str, member: str) -> None:
    value = client()
    if value is not None:
        try:
            value.srem(key, member)
        except Exception:
            pass


def health() -> dict[str, Any]:
    value = client()
    if value is None:
        return {"available": False, "url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")}
    try:
        return {"available": bool(value.ping()), "url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")}
    except Exception:
        return {"available": False, "url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")}


STREAM_KEY = os.getenv("ALIS_TASK_STREAM", "alis:data-platform:tasks")
STREAM_GROUP = os.getenv("ALIS_TASK_GROUP", "data-platform-workers")
STREAM_CONSUMER = os.getenv("ALIS_TASK_CONSUMER", f"{socket.gethostname()}-{os.getpid()}")
STREAM_MAX_ATTEMPTS = max(1, int(os.getenv("ALIS_TASK_MAX_ATTEMPTS", "5")))
STREAM_DEAD_LETTER = os.getenv("ALIS_TASK_DEAD_LETTER", f"{STREAM_KEY}:dead-letter")


def ensure_stream_group(stream: str = STREAM_KEY, group: str = STREAM_GROUP) -> bool:
    """Create the durable consumer group once; return False when Redis is unavailable."""
    value = client()
    if value is None:
        return False
    try:
        value.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            return False
    return True


def enqueue_task(task_type: str, payload: dict[str, Any], *, stream: str = STREAM_KEY) -> str | None:
    """Append a replayable task. The idempotency key is carried in the message, not inferred."""
    value = client()
    if value is None:
        return None
    ensure_stream_group(stream)
    fields = {
        "task_type": task_type,
        "payload_json": json.dumps(payload, ensure_ascii=False, default=str),
        "enqueued_at": str(time.time()),
        "attempt": "0",
    }
    try:
        return str(value.xadd(stream, fields, maxlen=100000, approximate=True))
    except Exception:
        return None


def read_tasks(*, count: int = 1, block_ms: int = 1000, stream: str = STREAM_KEY,
               group: str = STREAM_GROUP, consumer: str = STREAM_CONSUMER) -> list[tuple[str, dict[str, str]]]:
    """Read new tasks as a consumer group; messages remain pending until ack_task."""
    value = client()
    if value is None or not ensure_stream_group(stream, group):
        return []
    try:
        rows = value.xreadgroup(group, consumer, {stream: ">"}, count=max(1, count), block=max(0, block_ms))
        return [(str(message_id), {str(k): str(v) for k, v in fields.items()})
                for _, messages in rows for message_id, fields in messages]
    except Exception:
        return []


def ack_task(message_id: str, *, stream: str = STREAM_KEY, group: str = STREAM_GROUP) -> bool:
    value = client()
    if value is None:
        return False


def record_task_failure(message_id: str, fields: dict[str, str], *, stream: str = STREAM_KEY,
                        group: str = STREAM_GROUP) -> bool:
    """Count failures and dead-letter after the configured retry budget."""
    value = client()
    if value is None:
        return False
    key = f"{stream}:attempts:{message_id}"
    try:
        attempts = int(value.incr(key))
        value.expire(key, 7 * 24 * 3600)
        if attempts >= STREAM_MAX_ATTEMPTS:
            dead = dict(fields)
            dead["attempt"] = str(attempts)
            dead["failed_at"] = str(time.time())
            dead["source_message_id"] = str(message_id)
            value.xadd(STREAM_DEAD_LETTER, dead, maxlen=100000, approximate=True)
            value.xack(stream, group, message_id)
        return True
    except Exception:
        return False
    try:
        return bool(value.xack(stream, group, message_id))
    except Exception:
        return False


def reclaim_pending(*, min_idle_ms: int = 60000, count: int = 10, stream: str = STREAM_KEY,
                    group: str = STREAM_GROUP, consumer: str = STREAM_CONSUMER) -> list[tuple[str, dict[str, str]]]:
    """Reclaim tasks left pending after a worker crash."""
    value = client()
    if value is None or not ensure_stream_group(stream, group):
        return []
    try:
        rows = value.xautoclaim(stream, group, consumer, min_idle_time=max(0, min_idle_ms), start_id="0-0", count=count)
        messages = rows[1] if isinstance(rows, tuple) else []
        return [(str(message_id), {str(k): str(v) for k, v in fields.items()})
                for message_id, fields in messages]
    except Exception:
        return []


def stream_health(stream: str = STREAM_KEY, group: str = STREAM_GROUP) -> dict[str, Any]:
    value = client()
    if value is None:
        return {"available": False, "stream": stream, "group": group}
    try:
        info = value.xinfo_stream(stream)
        groups = value.xinfo_groups(stream)
        current = next((g for g in groups if g.get("name") == group), None)
        dead_length = 0
        try:
            dead_length = int(value.xinfo_stream(STREAM_DEAD_LETTER).get("length", 0))
        except Exception:
            pass
        return {
            "available": True,
            "stream": stream,
            "group": group,
            "length": int(info.get("length", 0)),
            "pending": int((current or {}).get("pending", 0)),
            "consumers": int((current or {}).get("consumers", 0)),
            "dead_letter": dead_length,
        }
    except Exception:
        return {"available": True, "stream": stream, "group": group, "length": 0, "pending": 0, "consumers": 0, "dead_letter": 0}


def start_task_worker(handler) -> bool:
    """Start one durable stream consumer; pending messages are reclaimed on restart."""
    global _worker_started
    if _worker_started or client() is None:
        return False
    _worker_started = True

    def loop() -> None:
        while True:
            messages = reclaim_pending(min_idle_ms=1000, count=1)
            messages += read_tasks(count=1, block_ms=1000)
            for message_id, fields in messages:
                try:
                    handler(fields)
                except Exception:
                    # Keep it pending until retry budget is exhausted, then
                    # move it to a durable dead-letter stream.
                    record_task_failure(message_id, fields)
                    continue
                ack_task(message_id)

    threading.Thread(target=loop, name="redis-stream-worker", daemon=True).start()
    return True

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.services import application_runtime_projection as projection


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((str(sql), tuple(params)))
        return _Result(self.row)


def test_publish_health_records_application_source(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(projection, "_conn", lambda: conn)
    projection.publish_health({"status": "ok", "ready": True, "dependencies": {"redis": {"available": True}}})
    params = conn.calls[0][1]
    assert params[1] == "application"
    assert json.loads(params[-1])["dependencies"]["redis"]["available"] is True


def test_read_health_rejects_expired_projection(monkeypatch):
    now = datetime.now(timezone.utc)
    conn = _Conn({
        "source_role": "application",
        "source_instance": "app:1",
        "observed_at": (now - timedelta(minutes=5)).isoformat(),
        "expires_at": (now - timedelta(minutes=2)).isoformat(),
        "payload_json": json.dumps({"status": "ok", "ready": True}),
    })
    monkeypatch.setattr(projection, "_read_conn", lambda: conn)
    result = projection.read_health()
    assert result["ready"] is False
    assert result["projection_status"] == "stale"

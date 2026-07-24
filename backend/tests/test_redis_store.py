from __future__ import annotations

from app.data_platform import redis_store


def test_ack_task_uses_xack(monkeypatch):
    calls = []

    class DummyRedis:
        def xack(self, stream, group, message_id):
            calls.append((stream, group, message_id))
            return 1

    monkeypatch.setattr(redis_store, "client", lambda: DummyRedis())

    assert redis_store.ack_task("msg-1", stream="stream-a", group="group-a") is True
    assert calls == [("stream-a", "group-a", "msg-1")]

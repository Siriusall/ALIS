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


def test_reclaim_pending_accepts_real_redis_py_list_shape(monkeypatch):
    class DummyRedis:
        def xautoclaim(self, stream, group, consumer, **kwargs):
            assert (stream, group, consumer) == ("stream-a", "group-a", "consumer-a")
            return [
                "0-0",
                [("msg-1", {"task_type": "data_platform.plan_model_data_backfill"})],
                [],
            ]

    monkeypatch.setattr(redis_store, "client", lambda: DummyRedis())
    monkeypatch.setattr(redis_store, "ensure_stream_group", lambda *_args: True)

    result = redis_store.reclaim_pending(
        stream="stream-a",
        group="group-a",
        consumer="consumer-a",
    )

    assert result == [
        (
            "msg-1",
            {"task_type": "data_platform.plan_model_data_backfill"},
        )
    ]

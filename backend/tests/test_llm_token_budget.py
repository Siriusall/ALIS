from __future__ import annotations

import pytest

from app.services import llm_review_assistant as assistant


def test_default_daily_token_budget_is_ten_million(monkeypatch):
    monkeypatch.setattr(assistant, "get_env_value", lambda _key, default="": default)

    per_request, daily = assistant._token_limits()

    assert per_request == 2400
    assert daily == 10_000_000


def test_deepseek_call_is_blocked_when_daily_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(assistant, "get_env_value", lambda key, default="": "test-key" if key == "DEEPSEEK_API_KEY" else default)
    monkeypatch.setattr(
        assistant,
        "llm_token_budget_status",
        lambda: {"per_request_limit": 1200, "remaining_tokens": 0},
    )

    with pytest.raises(RuntimeError, match="llm_token_budget_exhausted"):
        assistant._call_deepseek_json(system="system", user="user", purpose="test")


def test_request_output_is_reduced_to_remaining_budget(monkeypatch):
    captured: dict = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def post(_url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(*_args, **_kwargs):
            return None

    def env(key, default=""):
        return {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MAX_INPUT_CHARS": "16000"}.get(key, default)

    monkeypatch.setattr(assistant, "get_env_value", env)
    monkeypatch.setattr(assistant, "llm_token_budget_status", lambda: {"per_request_limit": 1200, "remaining_tokens": 500})
    monkeypatch.setattr(assistant.httpx, "Client", Client)
    monkeypatch.setattr(assistant, "_conn", lambda: Conn())

    payload, _ = assistant._call_deepseek_json(system="s", user="u", purpose="test")

    assert payload == {"ok": True}
    assert 200 <= captured["max_tokens"] <= 500

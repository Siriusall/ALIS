from starlette.requests import Request

from app.api.routes import auth


def _request(host: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": (host, 1234)})


def test_local_debug_auth_bypass_is_limited_to_loopback(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "env", "dev")
    monkeypatch.setattr(auth.settings, "local_debug_auth_bypass", True)

    assert auth._can_bypass_auth(_request("127.0.0.1")) is True
    assert auth._can_bypass_auth(_request("::1")) is True
    assert auth._can_bypass_auth(_request("192.168.1.8")) is False


def test_local_debug_auth_bypass_is_disabled_in_production(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "env", "production")
    monkeypatch.setattr(auth.settings, "local_debug_auth_bypass", True)

    assert auth._can_bypass_auth(_request("127.0.0.1")) is False

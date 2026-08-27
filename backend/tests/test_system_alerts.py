from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.services import system_alerts
from app.services.system_alerts import AlertIssue


def test_alert_recipient_is_fixed() -> None:
    assert system_alerts.ALERT_RECIPIENT == "siriusal@icloud.com"


def test_alert_cycle_deduplicates_and_sends_recovery(monkeypatch, tmp_path: Path) -> None:
    current = [AlertIssue("api_not_ready", "主服务未就绪", "数据库不可用")]
    sent: list[tuple[list[AlertIssue], bool]] = []
    monkeypatch.setattr(system_alerts, "collect_impacting_issues", lambda: list(current))
    monkeypatch.setattr(
        system_alerts,
        "send_issue_email",
        lambda issues, recovered=False: sent.append((list(issues), recovered)),
    )
    state = tmp_path / "alerts.json"

    first = system_alerts.run_alert_cycle(state)
    second = system_alerts.run_alert_cycle(state)
    current.clear()
    third = system_alerts.run_alert_cycle(state)

    assert first["sent_new"] is True
    assert second["sent_new"] is False
    assert third["sent_recovered"] is True
    assert [(items[0].code, recovered) for items, recovered in sent] == [
        ("api_not_ready", False),
        ("api_not_ready", True),
    ]


def test_delivery_failure_keeps_issue_pending_for_retry(monkeypatch, tmp_path: Path) -> None:
    issue = AlertIssue("api_unreachable", "主服务无法访问", "连接超时")
    monkeypatch.setattr(system_alerts, "collect_impacting_issues", lambda: [issue])
    attempts = []

    def fail(_issues, recovered=False):
        attempts.append(recovered)
        raise RuntimeError("smtp unavailable")

    monkeypatch.setattr(system_alerts, "send_issue_email", fail)
    state = tmp_path / "alerts.json"

    assert system_alerts.run_alert_cycle(state)["ok"] is False
    assert system_alerts.run_alert_cycle(state)["new_count"] == 1
    assert attempts == [False, False]


def test_resend_delivery_reuses_reference_idempotency_contract(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"id":"email-test-1"}'
    captured = {}

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setenv("ALIS_ALERT_RESEND_API_KEY", "test-key")
    monkeypatch.setattr(system_alerts.urllib.request, "urlopen", open_request)
    issue = AlertIssue("api_not_ready", "主服务未就绪", "数据库不可用")

    system_alerts.send_issue_email([issue])

    assert captured["request"].full_url == "https://api.resend.com/emails"
    assert captured["request"].headers["Idempotency-key"]
    assert captured["timeout"] == 20

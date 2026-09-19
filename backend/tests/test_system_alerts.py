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
    response.json.return_value = {"id": "email-test-1"}
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return response

    monkeypatch.setenv("ALIS_ALERT_RESEND_API_KEY", "test-key")
    monkeypatch.setattr(system_alerts.httpx, "post", post)
    issue = AlertIssue("api_not_ready", "主服务未就绪", "数据库不可用")

    system_alerts.send_issue_email([issue])

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Idempotency-Key"]
    assert captured["timeout"] == 20


def test_snapshot_alerts_use_stable_worker_and_page_identities(monkeypatch) -> None:
    health = {
        "status": "worker_unavailable",
        "worker": {
            "healthy": False,
            "status": "worker_unavailable",
            "reason": "snapshot_worker_heartbeat_expired",
            "heartbeat_age_seconds": 181,
        },
        "required_pages": ["overview", "research"],
        "pages": {
            "overview": {
                "status": "stale",
                "reason": "snapshot_age_exceeded",
                "age_seconds": 3601,
            },
            "research": {"status": "ready"},
        },
    }
    monkeypatch.setattr(system_alerts, "console_snapshot_health", lambda: health)

    first = system_alerts._snapshot_issues()
    health["worker"]["heartbeat_age_seconds"] = 999
    health["pages"]["overview"]["age_seconds"] = 7200
    second = system_alerts._snapshot_issues()

    assert {issue.code for issue in first} == {
        "console_snapshot_worker_unhealthy",
        "console_snapshot_page_unhealthy",
    }
    assert {issue.fingerprint for issue in first} == {issue.fingerprint for issue in second}


def test_snapshot_probe_failure_becomes_a_deduplicated_alert(monkeypatch) -> None:
    monkeypatch.setattr(
        system_alerts,
        "console_snapshot_health",
        lambda: (_ for _ in ()).throw(RuntimeError("database timeout")),
    )

    issue = system_alerts._snapshot_issues()[0]

    assert issue.code == "console_snapshot_health_probe_failed"
    assert issue.dedupe_key == "console_snapshot_health_probe"


def test_snapshot_alert_cycle_deduplicates_and_sends_page_recovery(monkeypatch, tmp_path: Path) -> None:
    health = {
        "status": "stale",
        "worker": {"healthy": True, "status": "ready"},
        "required_pages": ["overview"],
        "pages": {"overview": {"status": "stale", "reason": "snapshot_age_exceeded"}},
    }
    sent: list[tuple[list[AlertIssue], bool]] = []
    monkeypatch.setattr(system_alerts, "console_snapshot_health", lambda: health)
    monkeypatch.setattr(system_alerts, "_health_issue", lambda: [])
    monkeypatch.setattr(system_alerts, "_chain_monitor_issues", lambda: [])
    monkeypatch.setattr(system_alerts, "_blocked_plan_issues", lambda _now: [])
    monkeypatch.setattr(system_alerts, "_missing_daily_output_issues", lambda _now: [])
    monkeypatch.setattr(system_alerts, "_critical_task_issues", lambda _now: [])
    monkeypatch.setattr(
        system_alerts,
        "send_issue_email",
        lambda issues, recovered=False: sent.append((list(issues), recovered)),
    )
    state = tmp_path / "snapshot-alerts.json"

    assert system_alerts.run_alert_cycle(state)["sent_new"] is True
    health["pages"]["overview"] = {"status": "ready"}
    assert system_alerts.run_alert_cycle(state)["sent_recovered"] is True

    assert [(issues[0].code, recovered) for issues, recovered in sent] == [
        ("console_snapshot_page_unhealthy", False),
        ("console_snapshot_page_unhealthy", True),
    ]


def test_chain_monitor_state_reports_only_failed_target(monkeypatch, tmp_path: Path) -> None:
    state = tmp_path / "chain-state.json"
    system_alerts._save_state(state, {
        "observed_at": system_alerts._now().isoformat(),
        "results": {
            "app": {"ok": True, "code": 0},
            "research": {"ok": False, "code": 1, "detail": "research worker inactive"},
        },
    })
    monkeypatch.setenv("ALIS_CHAIN_MONITOR_STATE_FILE", str(state))

    issues = system_alerts._chain_monitor_issues()

    assert [(issue.code, issue.dedupe_key) for issue in issues] == [
        ("chain_monitor_target_unhealthy", "chain_monitor:target:research"),
    ]


def test_resend_409_idempotency_conflict_is_a_success(monkeypatch) -> None:
    response = MagicMock()
    response.status_code = 409
    response.json.return_value = {"id": "email-existing"}
    monkeypatch.setenv("ALIS_ALERT_RESEND_API_KEY", "test-key")
    monkeypatch.setattr(system_alerts.httpx, "post", lambda *_args, **_kwargs: response)

    message_id = system_alerts._send_with_resend(
        subject="subject",
        text_body="text",
        html_body="<p>text</p>",
        idempotency_key="stable-key",
    )

    assert message_id == "email-existing"
    response.raise_for_status.assert_not_called()


def test_successful_new_batch_is_not_resent_when_recovery_delivery_fails(monkeypatch, tmp_path: Path) -> None:
    old = AlertIssue("old", "旧问题", "旧详情")
    new = AlertIssue("new", "新问题", "新详情")
    state = tmp_path / "partial-delivery.json"
    system_alerts._save_state(state, {"active": {old.fingerprint: system_alerts.asdict(old)}})
    monkeypatch.setattr(system_alerts, "collect_impacting_issues", lambda: [new])
    sent: list[tuple[list[AlertIssue], bool]] = []

    def deliver(issues, recovered=False):
        sent.append((list(issues), recovered))
        if recovered:
            raise RuntimeError("recovery smtp unavailable")

    monkeypatch.setattr(system_alerts, "send_issue_email", deliver)

    first = system_alerts.run_alert_cycle(state)
    second = system_alerts.run_alert_cycle(state)

    assert first["sent_new"] is True
    assert first["ok"] is False
    assert second["new_count"] == 0
    assert [([issue.code for issue in issues], recovered) for issues, recovered in sent] == [
        (["new"], False),
        (["old"], True),
        (["old"], True),
    ]

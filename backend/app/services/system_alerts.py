"""Out-of-band email alerts for failures that can affect the ALIS main line."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import smtplib
import ssl
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.data_platform.db import _read_conn as data_read_conn
from app.db.store import _read_conn as business_read_conn

logger = logging.getLogger("quant.system_alerts")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
ALERT_RECIPIENT = "siriusal@icloud.com"
_BLOCKING_PLAN_STATUSES = {
    "blocked_by_contract_validation",
    "blocked_by_contract_revision",
    "blocked_by_execution_recovery",
    "missed_execution_window",
}
_CRITICAL_TASK_TYPES = {
    "system_autonomy_cycle",
    "system_release_governance",
    "sealed_holdout_adj_factor_backfill",
}


@dataclass(frozen=True)
class AlertIssue:
    code: str
    title: str
    detail: str
    severity: str = "critical"

    @property
    def fingerprint(self) -> str:
        source = f"{self.code}\n{self.title}\n{self.detail}".encode()
        return hashlib.sha256(source).hexdigest()


def _now() -> datetime:
    return datetime.now(_BUSINESS_TZ)


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, os.getenv(name.removeprefix("ALIS_ALERT_"), default)) or "").strip()


def _health_issue() -> list[AlertIssue]:
    url = _env("ALIS_ALERT_HEALTH_URL", "https://www.xlytism.com/api/health")
    timeout = max(3, min(int(_env("ALIS_ALERT_HTTP_TIMEOUT_SECONDS", "10")), 30))
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ALIS system alert worker/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return [AlertIssue("api_unreachable", "ALIS 主服务无法访问", str(exc)[:500])]
    if bool(payload.get("ready")):
        return []
    details = payload.get("issues") or [payload.get("status") or "健康检查未通过"]
    return [AlertIssue("api_not_ready", "ALIS 主服务未就绪", "；".join(map(str, details))[:1000])]


def _blocked_plan_issues(now: datetime) -> list[AlertIssue]:
    today = now.strftime("%Y%m%d")
    with business_read_conn() as conn:
        rows = conn.execute(
            """SELECT trade_date,planned_execute_date,status,validation_error_code
                 FROM daily_rebalance_plans
                WHERE (planned_execute_date=? OR trade_date=?)
                  AND status IN (?,?,?,?)
                ORDER BY planned_execute_date DESC,version DESC LIMIT 5""",
            (today, today, *_BLOCKING_PLAN_STATUSES),
        ).fetchall()
    issues = []
    for row in rows:
        item = dict(row)
        issues.append(AlertIssue(
            "rebalance_plan_blocked",
            f"调仓计划 {item.get('trade_date')}→{item.get('planned_execute_date')} 被阻断",
            f"状态：{item.get('status')}；错误：{item.get('validation_error_code') or '未记录'}",
        ))
    return issues


def _missing_daily_output_issues(now: datetime) -> list[AlertIssue]:
    cutoff_hour = max(15, min(int(_env("ALIS_ALERT_PLAN_DEADLINE_HOUR", "18")), 23))
    cutoff_minute = max(0, min(int(_env("ALIS_ALERT_PLAN_DEADLINE_MINUTE", "30")), 59))
    if (now.hour, now.minute) < (cutoff_hour, cutoff_minute):
        return []
    today = now.strftime("%Y%m%d")
    with data_read_conn() as conn:
        row = conn.execute(
            "SELECT is_open FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date=? LIMIT 1",
            (today,),
        ).fetchone()
    if not row or not bool(dict(row).get("is_open")):
        return []
    with business_read_conn() as conn:
        row = conn.execute(
            "SELECT status FROM daily_rebalance_plans WHERE trade_date=? ORDER BY version DESC LIMIT 1",
            (today,),
        ).fetchone()
    if row:
        return []
    return [AlertIssue(
        "daily_plan_missing",
        f"{today} 调仓计划未按时产出",
        f"截至 {cutoff_hour:02d}:{cutoff_minute:02d} 仍没有当日冻结计划，可能影响下一交易日执行。",
    )]


def _critical_task_issues(now: datetime) -> list[AlertIssue]:
    since = (now - timedelta(days=1)).isoformat()
    placeholders = ",".join("?" for _ in _CRITICAL_TASK_TYPES)
    with business_read_conn() as conn:
        rows = conn.execute(
            f"""SELECT id,task_type,status,phase,message,error,updated_at
                  FROM evolution_tasks
                 WHERE task_type IN ({placeholders})
                   AND status IN ('failed','blocked','timed_out')
                   AND updated_at>=?
                 ORDER BY updated_at DESC LIMIT 10""",
            (*sorted(_CRITICAL_TASK_TYPES), since),
        ).fetchall()
    return [AlertIssue(
        "critical_task_failed",
        f"关键任务失败：{dict(row).get('task_type')} #{dict(row).get('id')}",
        str(dict(row).get("error") or dict(row).get("message") or dict(row).get("phase") or "未记录原因")[:1000],
    ) for row in rows]


def collect_impacting_issues(now: datetime | None = None) -> list[AlertIssue]:
    current = now or _now()
    readers: tuple[Callable[[], list[AlertIssue]], ...] = (
        _health_issue,
        lambda: _blocked_plan_issues(current),
        lambda: _missing_daily_output_issues(current),
        lambda: _critical_task_issues(current),
    )
    issues: list[AlertIssue] = []
    for reader in readers:
        try:
            issues.extend(reader())
        except Exception as exc:  # noqa: BLE001 - diagnostics must remain independent
            logger.warning("Alert diagnostic failed reader=%s error=%s", reader, type(exc).__name__)
    unique: dict[str, AlertIssue] = {}
    for issue in issues:
        unique[issue.fingerprint] = issue
    return list(unique.values())


def _smtp_settings() -> dict[str, Any]:
    secure = _env("ALIS_ALERT_SMTP_SECURE", "true").lower() in {"1", "true", "yes", "on"}
    return {
        "host": _env("ALIS_ALERT_SMTP_HOST", "smtpdm.aliyun.com"),
        "port": int(_env("ALIS_ALERT_SMTP_PORT", "465" if secure else "587")),
        "secure": secure,
        "starttls": _env("ALIS_ALERT_SMTP_STARTTLS", "false").lower() in {"1", "true", "yes", "on"},
        "user": _env("ALIS_ALERT_SMTP_USER", "noreply@mail.xlytism.com"),
        "password": _env("ALIS_ALERT_SMTP_PASS"),
        "sender": _env("ALIS_ALERT_SMTP_FROM") or _env("ALIS_ALERT_SMTP_USER", "noreply@mail.xlytism.com"),
        "sender_name": _env("ALIS_ALERT_SMTP_FROM_NAME", "ALIS 系统告警"),
    }


def _resend_settings() -> dict[str, str]:
    return {
        "api_key": _env("ALIS_ALERT_RESEND_API_KEY"),
        "sender": _env("ALIS_ALERT_RESEND_FROM", "ALIS 系统告警 <onboarding@resend.dev>"),
    }


def _email_content(issues: list[AlertIssue], *, recovered: bool) -> tuple[str, str, str]:
    prefix = "[恢复]" if recovered else "[紧急]"
    subject = f"{prefix} ALIS 系统告警（{len(issues)}项）"
    heading = "ALIS 监控检测到以下已恢复问题：" if recovered else "ALIS 监控检测到以下可能影响主线运行的问题："
    text_lines = [heading, ""]
    html_rows = []
    for index, issue in enumerate(issues, 1):
        text_lines.extend([f"{index}. {issue.title}", f"   {issue.detail}", f"   代码：{issue.code}", ""])
        html_rows.append(
            "<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'>{index}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'><strong>{html.escape(issue.title)}</strong><br>"
            f"{html.escape(issue.detail)}<br><small>{html.escape(issue.code)}</small></td>"
            "</tr>"
        )
    checked_at = _now().isoformat(timespec="seconds")
    text_lines.extend([f"检测时间：{checked_at}", f"收件人固定为 {ALERT_RECIPIENT}"])
    html_body = (
        "<html><body>"
        f"<h2>{html.escape(heading)}</h2>"
        "<table style='border-collapse:collapse'>"
        f"{''.join(html_rows)}</table>"
        f"<p>检测时间：{html.escape(checked_at)}<br>收件人：{ALERT_RECIPIENT}</p>"
        "</body></html>"
    )
    return subject, "\n".join(text_lines), html_body


def _send_with_resend(*, subject: str, text_body: str, html_body: str, idempotency_key: str) -> str:
    settings = _resend_settings()
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        json={
            "from": settings["sender"],
            "to": [ALERT_RECIPIENT],
            "subject": subject,
            "text": text_body,
            "html": html_body,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    message_id = str(payload.get("id") or "")
    if not message_id:
        raise RuntimeError("Resend response did not contain an email id")
    return message_id


def _send_webhook_alert(*, subject: str, text_body: str, issues: list[AlertIssue]) -> None:
    webhook_url = _env("ALIS_ALERT_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        payload = {
            "msg_type": "text",
            "title": subject,
            "text": text_body,
            "issues_count": len(issues),
            "created_at": _now().isoformat(),
        }
        httpx.post(webhook_url, json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001 - non-blocking backup channel
        logger.warning("Webhook alert broadcast failed: %s", exc)


def send_issue_email(issues: list[AlertIssue], *, recovered: bool = False) -> None:
    if not issues:
        return
    subject, text_body, html_body = _email_content(issues, recovered=recovered)
    _send_webhook_alert(subject=subject, text_body=text_body, issues=issues)
    resend = _resend_settings()
    idempotency_source = "|".join(sorted(issue.fingerprint for issue in issues))
    idempotency_key = hashlib.sha256(f"{recovered}|{idempotency_source}".encode()).hexdigest()
    if resend["api_key"]:
        _send_with_resend(
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            idempotency_key=idempotency_key,
        )
        return
    settings = _smtp_settings()
    if not settings["password"]:
        raise RuntimeError("ALIS_ALERT_SMTP_PASS is not configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings['sender_name']} <{settings['sender']}>"
    message["To"] = ALERT_RECIPIENT
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if settings["secure"]:
        client_context = smtplib.SMTP_SSL(
            settings["host"], settings["port"], timeout=15, context=context,
        )
    else:
        client_context = smtplib.SMTP(settings["host"], settings["port"], timeout=15)
    with client_context as client:
        if settings["starttls"] and not settings["secure"]:
            client.starttls(context=context)
        client.login(settings["user"], settings["password"])
        client.send_message(message)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


def run_alert_cycle(state_path: Path | None = None) -> dict[str, Any]:
    path = state_path or Path(_env("ALIS_ALERT_STATE_FILE", "/var/lib/alis-alert-worker/state.json"))
    state = _load_state(path)
    previous = state.get("active") if isinstance(state.get("active"), dict) else {}
    issues = collect_impacting_issues()
    current = {issue.fingerprint: asdict(issue) for issue in issues}
    new_issues = [issue for issue in issues if issue.fingerprint not in previous]
    recovered = [AlertIssue(**item) for fingerprint, item in previous.items() if fingerprint not in current]
    sent_new = False
    sent_recovered = False
    delivery_error = ""
    try:
        if new_issues:
            send_issue_email(new_issues)
            sent_new = True
        if recovered:
            send_issue_email(recovered, recovered=True)
            sent_recovered = True
    except Exception as exc:  # noqa: BLE001 - delivery cannot block monitored workloads
        delivery_error = str(exc)[:500]
        logger.error("System alert email delivery failed: %s", type(exc).__name__)
    if not delivery_error:
        state["active"] = current
    state.update({
        "checked_at": _now().isoformat(timespec="seconds"),
        "delivery_error": delivery_error,
        "recipient": ALERT_RECIPIENT,
    })
    _save_state(path, state)
    return {
        "ok": not bool(delivery_error),
        "issue_count": len(issues),
        "new_count": len(new_issues),
        "recovered_count": len(recovered),
        "sent_new": sent_new,
        "sent_recovered": sent_recovered,
        "delivery_error": delivery_error,
    }

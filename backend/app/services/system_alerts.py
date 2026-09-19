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
from app.services.console_snapshots import snapshot_health as console_snapshot_health

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
    # Diagnostics with a changing observation age (for example a worker
    # heartbeat) need one stable identity.  Otherwise every polling cycle
    # produces a new alert plus a false recovery for the previous one.
    dedupe_key: str = ""

    @property
    def fingerprint(self) -> str:
        source = self.dedupe_key or f"{self.code}\n{self.title}\n{self.detail}"
        source = source.encode()
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


def _snapshot_issues() -> list[AlertIssue]:
    """Turn the durable snapshot-worker health contract into stable alerts.

    The snapshot service already distinguishes an absent/expired heartbeat
    from stale page documents.  Keep those signals separate here: a stopped
    worker and a page that cannot catch up need different operational action.
    Page identity, rather than its age, is deliberately used for deduplication
    so an unchanged outage does not generate one notification per poll.
    """

    try:
        health = console_snapshot_health()
    except Exception as exc:  # noqa: BLE001 - monitoring must report probe failures
        return [AlertIssue(
            "console_snapshot_health_probe_failed",
            "控制台快照健康探针不可用",
            f"无法读取快照 Worker 或页面健康状态：{type(exc).__name__}",
            dedupe_key="console_snapshot_health_probe",
        )]
    if not isinstance(health, dict):
        return [AlertIssue(
            "console_snapshot_health_probe_invalid",
            "控制台快照健康探针返回无效数据",
            "快照健康接口未返回结构化状态。",
            dedupe_key="console_snapshot_health_probe",
        )]

    issues: list[AlertIssue] = []
    worker = health.get("worker")
    worker = worker if isinstance(worker, dict) else {}
    # A current heartbeat is only liveness evidence.  A worker can be alive
    # while every build is failing; alert on that operational degradation
    # separately so it is not silently treated as a healthy refresh service.
    worker_live = bool(worker.get("heartbeat_healthy", worker.get("live", worker.get("healthy"))))
    worker_degraded = bool(
        worker.get("degraded")
        or str(worker.get("operational_status") or "").lower() == "degraded"
        or str(worker.get("status") or "").lower() == "degraded"
    )
    if not worker_live:
        status = str(worker.get("status") or health.get("status") or "worker_unavailable")
        reason = str(worker.get("reason") or "snapshot_worker_unavailable")
        issues.append(AlertIssue(
            "console_snapshot_worker_unhealthy",
            "控制台快照 Worker 心跳异常",
            f"状态：{status}；原因：{reason}。",
            dedupe_key="console_snapshot_worker",
        ))
    elif worker_degraded:
        status = str(worker.get("status") or "degraded")
        reason = str(worker.get("reason") or "snapshot_worker_degraded")
        issues.append(AlertIssue(
            "console_snapshot_worker_degraded",
            "控制台快照 Worker 持续降级",
            f"状态：{status}；原因：{reason}。进程仍存活，但页面刷新可能持续失败。",
            dedupe_key="console_snapshot_worker",
        ))

    pages = health.get("pages")
    pages = pages if isinstance(pages, dict) else {}
    required = health.get("required_pages")
    required_pages = [str(page) for page in required] if isinstance(required, (list, tuple)) else sorted(pages)
    if not required_pages and str(health.get("status") or "") not in {"ready", "degraded"}:
        issues.append(AlertIssue(
            "console_snapshot_page_health_unavailable",
            "控制台快照页面健康状态不可用",
            f"整体状态：{str(health.get('status') or 'unavailable')}。",
            dedupe_key="console_snapshot_pages",
        ))
        return issues

    for page_key in sorted(set(required_pages)):
        page = pages.get(page_key)
        page = page if isinstance(page, dict) else {}
        state = str(page.get("status") or "unavailable").strip().lower()
        if state in {"ready", "degraded"}:
            continue
        reason = str(page.get("reason") or "snapshot_page_unavailable")
        error_code = str(page.get("last_error_code") or "")
        detail = f"页面：{page_key}；状态：{state}；原因：{reason}"
        if error_code:
            detail += f"；最近错误：{error_code}"
        issues.append(AlertIssue(
            "console_snapshot_page_unhealthy",
            f"控制台快照页面异常：{page_key}",
            detail + "。",
            dedupe_key=f"console_snapshot_page:{page_key}",
        ))
    return issues


def _chain_monitor_issues() -> list[AlertIssue]:
    """Consume the app-local cross-node monitor state for remote failures.

    The alert worker has direct database visibility for snapshot health, but
    it cannot reliably infer SSH reachability or the two research processes.
    The chain monitor runs on this same app node and writes an atomic state
    file, which makes those remote checks visible to the normal alert/recovery
    and deduplication path.
    """

    path = Path(_env(
        "ALIS_CHAIN_MONITOR_STATE_FILE",
        "/var/lib/alis-chain-monitor/state.json",
    ))
    state = _load_state(path)
    if not state:
        return [AlertIssue(
            "chain_monitor_state_unavailable",
            "跨节点链路监控状态不可用",
            f"未能读取 {path}；无法确认应用、数据库快照和研究 Worker 链路。",
            dedupe_key="chain_monitor:state",
        )]
    observed = _parse_alert_timestamp(state.get("observed_at"))
    stale_after = max(120, min(int(_env("ALIS_CHAIN_MONITOR_STALE_SECONDS", "360")), 3600))
    age = None if observed is None else max(0.0, (_now() - observed).total_seconds())
    if age is None or age > stale_after:
        return [AlertIssue(
            "chain_monitor_state_stale",
            "跨节点链路监控未按时运行",
            f"最后状态时间：{state.get('observed_at') or '未记录'}；允许间隔：{stale_after} 秒。",
            dedupe_key="chain_monitor:state",
        )]
    results = state.get("results")
    if not isinstance(results, dict):
        return [AlertIssue(
            "chain_monitor_state_invalid",
            "跨节点链路监控状态无效",
            "状态文件没有 results 字段，无法确认远端节点。",
            dedupe_key="chain_monitor:state",
        )]
    issues: list[AlertIssue] = []
    for role, result in sorted(results.items()):
        if not isinstance(result, dict) or bool(result.get("ok")):
            continue
        code = str(result.get("code") or "unknown")
        detail = str(result.get("detail") or "未记录失败详情").replace("\n", " ")[:600]
        issues.append(AlertIssue(
            "chain_monitor_target_unhealthy",
            f"跨节点链路异常：{role}",
            f"节点：{role}；返回码：{code}；详情：{detail}",
            dedupe_key=f"chain_monitor:target:{role}",
        ))
    return issues


def _parse_alert_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BUSINESS_TZ)
    return parsed.astimezone(_BUSINESS_TZ)


def collect_impacting_issues(now: datetime | None = None) -> list[AlertIssue]:
    current = now or _now()
    readers: tuple[Callable[[], list[AlertIssue]], ...] = (
        _health_issue,
        _snapshot_issues,
        _chain_monitor_issues,
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
    # Resend returns 409 when this exact idempotency key has already been
    # accepted. Treat it as a successful prior delivery; retrying with a fresh
    # key would create duplicate emergency notifications.
    if response.status_code == 409:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        message_id = str(payload.get("id") or f"idempotent:{idempotency_key}")
        logger.info("Resend idempotency conflict accepted message_id=%s", message_id)
        return message_id
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
    recovered = [
        AlertIssue(**item)
        for fingerprint, item in previous.items()
        if fingerprint not in current and isinstance(item, dict)
    ]
    sent_new = False
    sent_recovered = False
    delivery_errors: list[str] = []
    acknowledged = dict(previous)
    if new_issues:
        try:
            send_issue_email(new_issues)
            sent_new = True
            for issue in new_issues:
                acknowledged[issue.fingerprint] = asdict(issue)
        except Exception as exc:  # noqa: BLE001 - delivery cannot block monitored workloads
            delivery_errors.append(str(exc)[:500])
            logger.error("System alert email delivery failed kind=new error=%s", type(exc).__name__)
    if recovered:
        try:
            send_issue_email(recovered, recovered=True)
            sent_recovered = True
            for issue in recovered:
                acknowledged.pop(issue.fingerprint, None)
        except Exception as exc:  # noqa: BLE001 - delivery cannot block monitored workloads
            delivery_errors.append(str(exc)[:500])
            logger.error("System alert email delivery failed kind=recovery error=%s", type(exc).__name__)
    # Preserve only currently observed issues plus old issues whose recovery
    # notification failed. This prevents a successfully delivered batch from
    # being resent merely because another batch failed in the same cycle.
    state["active"] = {
        fingerprint: item
        for fingerprint, item in acknowledged.items()
        if fingerprint in current or fingerprint in previous
    }
    delivery_error = "; ".join(delivery_errors)[:500]
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

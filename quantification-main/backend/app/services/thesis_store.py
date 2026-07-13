from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

from app.db.store import _conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def create_thesis(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    next_review_at = payload.get("next_review_at") or (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute(
            """INSERT INTO investment_theses(ts_code,title,thesis_type,core_thesis,status,confidence,next_review_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) RETURNING id""",
            (payload["ts_code"], payload["title"], payload.get("thesis_type") or "quality_growth", payload.get("core_thesis") or "",
             "watch", 0.0, next_review_at, now, now),
        ).fetchone()
        thesis_id = int(dict(row)["id"])
        for item in payload.get("hypotheses") or []:
            c.execute(
                """INSERT INTO thesis_hypotheses(thesis_id,statement,metric,necessary,expected_json,status,evidence_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,'unverified','[]',?,?)""",
                (thesis_id, item["statement"], item.get("metric") or "", bool(item.get("necessary")), json.dumps(item.get("expected") or {}, ensure_ascii=False), now, now),
            )
    return get_thesis(thesis_id) or {"id": thesis_id}


def _derive_thesis_state(hypotheses: list[dict[str, Any]]) -> tuple[str, float]:
    if not hypotheses:
        return "draft", 0.0
    necessary_failed = any(bool(x.get("necessary")) and x.get("status") in {"failed", "invalidated"} for x in hypotheses)
    core_failed = any(x.get("status") == "invalidated" for x in hypotheses)
    verified = sum(1 for x in hypotheses if x.get("status") == "verified")
    confidence = verified / len(hypotheses) * 100.0
    if necessary_failed:
        confidence = min(confidence, 40.0)
    status = "invalidated" if core_failed else "warning" if necessary_failed else "valid" if verified == len(hypotheses) else "watch"
    return status, round(confidence, 2)


def get_thesis(thesis_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM investment_theses WHERE id=?", (thesis_id,)).fetchone()
        if not row:
            return None
        hypotheses = [dict(x) for x in c.execute("SELECT * FROM thesis_hypotheses WHERE thesis_id=? ORDER BY id", (thesis_id,)).fetchall()]
    for item in hypotheses:
        item["expected"] = _loads(item.pop("expected_json", "{}"), {})
        item["evidence"] = _loads(item.pop("evidence_json", "[]"), [])
    value = dict(row)
    value["hypotheses"] = hypotheses
    value["derived_status"], value["derived_confidence"] = _derive_thesis_state(hypotheses)
    return value


def list_theses(ts_code: str = "", limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as c:
        if ts_code:
            rows = c.execute("SELECT id FROM investment_theses WHERE ts_code=? ORDER BY updated_at DESC LIMIT ?", (ts_code, limit)).fetchall()
        else:
            rows = c.execute("SELECT id FROM investment_theses ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [value for row in rows if (value := get_thesis(int(dict(row)["id"]))) is not None]


def update_hypothesis(hypothesis_id: int, *, status: str, evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = _now()
    with _conn() as c:
        row = c.execute("SELECT thesis_id FROM thesis_hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
        if not row:
            return None
        thesis_id = int(dict(row)["thesis_id"])
        c.execute("UPDATE thesis_hypotheses SET status=?,evidence_json=?,last_checked_at=?,updated_at=? WHERE id=?", (status, json.dumps(evidence, ensure_ascii=False), now, now, hypothesis_id))
        c.execute("UPDATE investment_theses SET updated_at=? WHERE id=?", (now, thesis_id))
    result = get_thesis(thesis_id)
    if result:
        with _conn() as c:
            c.execute("UPDATE investment_theses SET status=?,confidence=?,updated_at=? WHERE id=?", (result["derived_status"], result["derived_confidence"], now, thesis_id))
        result = get_thesis(thesis_id)
    return result


def review_thesis(thesis_id: int) -> dict[str, Any] | None:
    """Re-evaluate machine-checkable hypotheses from point-in-time research facts."""
    thesis = get_thesis(thesis_id)
    if not thesis:
        return None
    from app.services.research_engine import build_stock_research

    research = build_stock_research(str(thesis["ts_code"]), persist=True)
    metrics = research.get("raw_metrics") or {}
    evidence_base = research.get("evidence") or []
    for hypothesis in thesis.get("hypotheses") or []:
        metric = str(hypothesis.get("metric") or "")
        expected = hypothesis.get("expected") or {}
        value = metrics.get(metric)
        if not metric or value is None:
            status = "unverified"
            reason = "指标尚无合格点时数据"
        else:
            minimum = expected.get("min")
            maximum = expected.get("max")
            failed = (minimum is not None and float(value) < float(minimum)) or (maximum is not None and float(value) > float(maximum))
            warning_min = expected.get("warning_min")
            warning_max = expected.get("warning_max")
            warning = (warning_min is not None and float(value) < float(warning_min)) or (warning_max is not None and float(value) > float(warning_max))
            status = "failed" if failed else "warning" if warning else "verified"
            reason = "超出失效范围" if failed else "进入预警范围" if warning else "满足预期范围"
        update_hypothesis(
            int(hypothesis["id"]),
            status=status,
            evidence=[{"metric": metric, "value": value, "expected": expected, "reason": reason, "sources": evidence_base, "checked_at": _now()}],
        )
    return get_thesis(thesis_id)


def review_due_theses(*, limit: int = 100, review_interval_days: int = 30) -> dict[str, Any]:
    """Review due thesis cards and schedule their next evidence check.

    Null review dates are treated as due once so existing manually-created cards
    enter the managed review cycle instead of staying permanently unreviewed.
    """
    now = _now()
    with _conn() as c:
        rows = c.execute(
            """SELECT id FROM investment_theses
               WHERE next_review_at IS NULL OR next_review_at <= ?
               ORDER BY COALESCE(next_review_at, created_at), id LIMIT ?""",
            (now, max(1, min(int(limit), 500))),
        ).fetchall()
    reviewed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    next_review_at = (datetime.now() + timedelta(days=max(1, int(review_interval_days)))).isoformat(timespec="seconds")
    for row in rows:
        thesis_id = int(dict(row)["id"])
        try:
            thesis = review_thesis(thesis_id)
            if not thesis:
                continue
            with _conn() as c:
                c.execute("UPDATE investment_theses SET next_review_at=?, updated_at=? WHERE id=?", (next_review_at, _now(), thesis_id))
            reviewed.append({"thesis_id": thesis_id, "status": thesis.get("derived_status"), "confidence": thesis.get("derived_confidence"), "next_review_at": next_review_at})
        except Exception as exc:  # noqa: BLE001
            failed.append({"thesis_id": thesis_id, "error": str(exc)[:300]})
    return {"ok": not failed, "due": len(rows), "reviewed": reviewed, "failed": failed, "next_review_interval_days": max(1, int(review_interval_days))}

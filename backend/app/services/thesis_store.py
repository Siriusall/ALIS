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
    has_warning = any(x.get("status") in {"warning", "failed"} for x in hypotheses)
    verified = sum(1 for x in hypotheses if x.get("status") == "verified")
    confidence = verified / len(hypotheses) * 100.0
    if necessary_failed:
        confidence = min(confidence, 40.0)
    # A failed necessary condition is the formal falsification of the thesis,
    # not merely a confidence warning.  Once invalidated it is intentionally
    # kept out of automated re-entry until an operator explicitly updates the
    # affected hypothesis through the audited API.
    status = "invalidated" if necessary_failed else "warning" if has_warning else "valid" if verified == len(hypotheses) else "watch"
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
            rows = c.execute("SELECT * FROM investment_theses WHERE ts_code=? ORDER BY updated_at DESC LIMIT ?", (ts_code, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM investment_theses ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        values = [dict(row) for row in rows]
        ids = [int(row["id"]) for row in values]
        hypotheses = [] if not ids else [dict(row) for row in c.execute(
            f"SELECT * FROM thesis_hypotheses WHERE thesis_id IN ({','.join('?' for _ in ids)}) ORDER BY thesis_id,id",
            tuple(ids),
        ).fetchall()]
    by_thesis: dict[int, list[dict[str, Any]]] = {thesis_id: [] for thesis_id in ids}
    for item in hypotheses:
        item["expected"] = _loads(item.pop("expected_json", "{}"), {})
        item["evidence"] = _loads(item.pop("evidence_json", "[]"), [])
        by_thesis.setdefault(int(item["thesis_id"]), []).append(item)
    for value in values:
        value["hypotheses"] = by_thesis.get(int(value["id"]), [])
        value["derived_status"], value["derived_confidence"] = _derive_thesis_state(value["hypotheses"])
    return values


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


def review_thesis(thesis_id: int, *, as_of_time: str | None = None) -> dict[str, Any] | None:
    """Re-evaluate machine-checkable hypotheses from point-in-time research facts."""
    thesis = get_thesis(thesis_id)
    if not thesis:
        return None
    from app.services.research_engine import build_stock_research

    research_kwargs: dict[str, Any] = {"persist": True}
    if as_of_time is not None:
        research_kwargs["as_of_time"] = as_of_time
    research = build_stock_research(str(thesis["ts_code"]), **research_kwargs)
    metrics = research.get("raw_metrics") or {}
    evidence_base = research.get("evidence") or []
    for hypothesis in thesis.get("hypotheses") or []:
        metric = str(hypothesis.get("metric") or "")
        expected = hypothesis.get("expected") or {}
        value = metrics.get(metric)
        prior_status = str(hypothesis.get("status") or "")
        if prior_status == "invalidated":
            status = "invalidated"
            reason = "此前已触发失效；需要人工显式恢复后才可重新评价"
        elif not metric or value is None:
            status = "unverified"
            reason = "指标尚无合格点时数据"
        else:
            minimum = expected.get("min")
            maximum = expected.get("max")
            failed = (minimum is not None and float(value) < float(minimum)) or (maximum is not None and float(value) > float(maximum))
            warning_min = expected.get("warning_min")
            warning_max = expected.get("warning_max")
            warning = (warning_min is not None and float(value) < float(warning_min)) or (warning_max is not None and float(value) > float(warning_max))
            status = "invalidated" if failed and bool(hypothesis.get("necessary")) else "failed" if failed else "warning" if warning else "verified"
            reason = "触发必要条件失效" if status == "invalidated" else "超出失效范围" if failed else "进入预警范围" if warning else "满足预期范围"
        prior_evidence = list(hypothesis.get("evidence") or [])
        update_hypothesis(
            int(hypothesis["id"]),
            status=status,
            evidence=(prior_evidence[-19:] + [{
                "metric": metric,
                "value": value,
                "expected": expected,
                "reason": reason,
                "sources": evidence_base,
                "checked_at": _now(),
                "evidence_as_of_time": as_of_time,
            }]),
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


def generate_thesis_for_stock(
    ts_code: str, *, force: bool = False, as_of_time: str | None = None,
) -> dict[str, Any]:
    """Create or refresh a falsifiable system thesis from authoritative research facts."""
    from app.services.research_engine import build_stock_research

    code = str(ts_code).strip().upper()
    research_kwargs: dict[str, Any] = {"persist": True}
    if as_of_time is not None:
        research_kwargs["as_of_time"] = as_of_time
    research = build_stock_research(code, **research_kwargs)
    if not research.get("ok"):
        return {"ok": False, "ts_code": code, "status": "blocked", "reason": research.get("reason") or "research_unavailable"}
    if not research.get("risk", {}).get("eligible"):
        return {"ok": False, "ts_code": code, "status": "blocked", "reason": "risk_hard_veto", "research": research.get("risk")}
    ready = [key for key, value in (research.get("dimensions") or {}).items() if value.get("status") == "ready"]
    if len(ready) < 3:
        return {"ok": False, "ts_code": code, "status": "insufficient_evidence", "reason": "fewer_than_three_ready_dimensions", "ready_dimensions": ready}

    all_existing = [item for item in list_theses(ts_code=code, limit=50) if item.get("thesis_type") == "system_research_v1"]
    invalidated = [item for item in all_existing if item.get("derived_status") == "invalidated"]
    if invalidated and not force:
        return {
            "ok": False, "ts_code": code, "status": "blocked", "reason": "invalidated_thesis_requires_manual_reset",
            "item": invalidated[0],
        }
    existing = [item for item in all_existing if item.get("derived_status") != "invalidated"]
    if existing and not force:
        reviewed = (
            review_thesis(int(existing[0]["id"]), as_of_time=as_of_time)
            if as_of_time is not None else review_thesis(int(existing[0]["id"]))
        )
        return {"ok": True, "ts_code": code, "status": "reviewed_existing", "created": False, "item": reviewed}

    metrics = research.get("raw_metrics") or {}
    classification = research.get("research_state", {}).get("classification") or "研究观察"
    profile = research.get("profile") or {}
    hypotheses: list[dict[str, Any]] = []

    def add(statement: str, metric: str, *, minimum: float | None = None, maximum: float | None = None, warning_min: float | None = None, warning_max: float | None = None, necessary: bool = True) -> None:
        if metrics.get(metric) is None:
            return
        expected = {key: value for key, value in {"min": minimum, "max": maximum, "warning_min": warning_min, "warning_max": warning_max}.items() if value is not None}
        hypotheses.append({"statement": statement, "metric": metric, "necessary": necessary, "expected": expected})

    add("收入增长不能持续恶化", "revenue_growth", minimum=-10, warning_min=0)
    add("核心利润不能进入显著衰退", "core_profit_growth", minimum=-15, warning_min=0)
    add("经营现金流应能支持账面利润", "cash_conversion", minimum=0, warning_min=.5)
    add("资产负债水平不得突破风险边界", "debt_to_assets", maximum=90, warning_max=80)
    add("基准估值情景应保留正安全边际", "valuation_base_upside", minimum=0, warning_min=.1)
    add("增长加速度不应显著转负", "growth_acceleration", minimum=-20, warning_min=-5, necessary=False)
    if not hypotheses:
        return {"ok": False, "ts_code": code, "status": "insufficient_evidence", "reason": "no_machine_checkable_hypotheses"}

    dimension_summary = "、".join(f"{key}:{research['dimensions'][key]['score']:.1f}" for key in ready)
    item = create_thesis({
        "ts_code": code,
        "title": f"{profile.get('name') or code} · {classification}",
        "thesis_type": "system_research_v1",
        "core_thesis": f"系统基于{dimension_summary}形成候选逻辑；只有必要条件持续成立且组合风险允许时才可建仓。",
        "next_review_at": datetime.now().isoformat(timespec="seconds"),
        "hypotheses": hypotheses,
    })
    reviewed = (
        review_thesis(int(item["id"]), as_of_time=as_of_time)
        if as_of_time is not None else review_thesis(int(item["id"]))
    )
    return {
        "ok": True,
        "ts_code": code,
        "status": "created_and_reviewed",
        "created": True,
        "item": reviewed,
        "ready_dimensions": ready,
        "evidence_as_of_time": as_of_time,
    }


def generate_watch_pool_theses(*, limit: int = 100) -> dict[str, Any]:
    """Generate managed theses for staged candidates without promoting their pool tier."""
    from app.services.decision_store import list_pool_members

    members = list_pool_members("candidate")[:max(1, min(int(limit), 500))]
    generated: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for member in members:
        result = generate_thesis_for_stock(str(member.get("ts_code") or ""))
        (generated if result.get("ok") else blocked).append(result)
    return {"ok": True, "candidates": len(members), "generated_or_reviewed": generated, "blocked": blocked}

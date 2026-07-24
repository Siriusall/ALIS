"""Deterministic quality gate for LLM research proposals."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.db.store import _conn
from app.services.experiment_memory import find_rejected_hypothesis


MIN_CONFIDENCE = 0.5


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_proposal_assessments (
            id BIGSERIAL PRIMARY KEY,
            proposal_id BIGINT NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL,
            assessment_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )""")


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _fingerprint(item: dict[str, Any]) -> str:
    value = "|".join(str(item.get(key) or "").strip().lower() for key in ("target", "title", "proposed_experiment"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def assess_llm_research_proposals(*, limit: int = 20) -> dict[str, Any]:
    """Assess proposal evidence before any deterministic experiment is triggered."""
    _ensure_tables()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM llm_research_proposals WHERE status IN ('proposed','routed') ORDER BY id ASC LIMIT ?", (max(1, min(int(limit), 100)),)
        ).fetchall()]
    seen: set[str] = set()
    accepted_by_proposal: dict[int, set[int]] = {}
    assessments: list[dict[str, Any]] = []
    for row in rows:
        accepted: list[int] = []
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate((_payload(row).get("hypotheses") or [])[:3]):
            if not isinstance(item, dict):
                rejected.append({"index": index, "reason": "invalid_hypothesis"})
                continue
            missing = [key for key in ("title", "rationale", "proposed_experiment") if not str(item.get(key) or "").strip()]
            if not list(item.get("evidence_refs") or []):
                missing.append("evidence_refs")
            if float(item.get("confidence") or 0) < MIN_CONFIDENCE:
                missing.append("minimum_confidence")
            fingerprint = _fingerprint(item)
            if fingerprint in seen:
                missing.append("duplicate_hypothesis")
            if find_rejected_hypothesis("llm_research", {
                "target": str(item.get("target") or "").strip().lower(),
                "title": str(item.get("title") or "").strip(),
                "proposed_experiment": str(item.get("proposed_experiment") or "").strip(),
            }):
                missing.append("formally_rejected_hypothesis")
            seen.add(fingerprint)
            if missing:
                rejected.append({"index": index, "reason": ",".join(missing)})
            else:
                accepted.append(index)
        status = "accepted" if accepted else "rejected"
        assessment = {"accepted_indices": accepted, "rejected": rejected, "policy": "requires_evidence_and_non_duplicate_hypothesis"}
        now = datetime.now().isoformat(timespec="seconds")
        with _conn() as conn:
            conn.execute(
                """INSERT INTO llm_research_proposal_assessments(proposal_id,status,assessment_json,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET
                   status=excluded.status,assessment_json=excluded.assessment_json,updated_at=excluded.updated_at""",
                (int(row["id"]), status, json.dumps(assessment, ensure_ascii=False), now, now),
            )
        accepted_by_proposal[int(row["id"])] = set(accepted)
        assessments.append({"proposal_id": row["id"], "status": status, **assessment})
    return {"ok": True, "schema_version": "alis_llm_research_governance_v1", "assessed_count": len(assessments), "accepted_by_proposal": accepted_by_proposal, "assessments": assessments}

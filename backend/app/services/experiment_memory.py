"""Persistent, evidence-backed memory for bounded research hypotheses.

Only formal validation outcomes may reject a hypothesis.  A completed task,
an unavailable data window, or an LLM opinion is intentionally not enough to
block future research.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.db.store import _conn


REJECTING_STAGES = {"formal_oos", "shadow", "cooling"}


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS experiment_memories (
            id BIGSERIAL PRIMARY KEY,
            hypothesis_fingerprint VARCHAR(64) NOT NULL UNIQUE,
            target VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            source_model_version_id BIGINT NULL,
            outcome_stage VARCHAR(64) NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experiment_memories_status ON experiment_memories(status, target)"
        )


def hypothesis_fingerprint(target: str, hypothesis: dict[str, Any]) -> str:
    """Return a stable identity for one falsifiable, bounded experiment."""
    payload = {
        "target": str(target or "").strip().lower(),
        "hypothesis": hypothesis if isinstance(hypothesis, dict) else {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find_rejected_hypothesis(target: str, hypothesis: dict[str, Any]) -> dict[str, Any] | None:
    """Return formal rejection evidence for exactly the same hypothesis."""
    _ensure_tables()
    fingerprint = hypothesis_fingerprint(target, hypothesis)
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM experiment_memories
               WHERE hypothesis_fingerprint=? AND target=? AND status='rejected'""",
            (fingerprint, str(target or "").strip().lower()),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["evidence"] = json.loads(item.pop("evidence_json", "") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["evidence"] = {}
    return item


def upsert_experiment_memory(
    *,
    target: str,
    hypothesis: dict[str, Any],
    status: str,
    source_model_version_id: int | None = None,
    outcome_stage: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one hypothesis lifecycle state without weakening rejection evidence."""
    _ensure_tables()
    now = datetime.now().astimezone().isoformat()
    normalized_target = str(target or "").strip().lower()
    fingerprint = hypothesis_fingerprint(normalized_target, hypothesis)
    payload = {
        "schema_version": "alis_experiment_memory_v1",
        "hypothesis": hypothesis,
        "outcome": evidence or {},
    }
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO experiment_memories(
                   hypothesis_fingerprint,target,status,source_model_version_id,
                   outcome_stage,evidence_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(hypothesis_fingerprint) DO UPDATE SET
                   status=CASE WHEN experiment_memories.status='rejected' THEN 'rejected' ELSE excluded.status END,
                   source_model_version_id=COALESCE(excluded.source_model_version_id, experiment_memories.source_model_version_id),
                   outcome_stage=CASE WHEN experiment_memories.status='rejected' THEN experiment_memories.outcome_stage ELSE excluded.outcome_stage END,
                   evidence_json=CASE WHEN experiment_memories.status='rejected' THEN experiment_memories.evidence_json ELSE excluded.evidence_json END,
                   updated_at=excluded.updated_at
               RETURNING *""",
            (
                fingerprint, normalized_target, str(status),
                int(source_model_version_id) if source_model_version_id else None,
                str(outcome_stage or ""), json.dumps(payload, ensure_ascii=False, default=str), now, now,
            ),
        ).fetchone()
    item = dict(row)
    item["hypothesis"] = hypothesis
    return item


def record_model_experiment_outcome(
    version: dict[str, Any],
    *,
    stage: str,
    rejected: bool,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    """Store a candidate outcome only when its immutable memory contract exists."""
    config = version.get("config") if isinstance(version.get("config"), dict) else {}
    contract = config.get("experiment_memory") if isinstance(config.get("experiment_memory"), dict) else {}
    hypothesis = contract.get("hypothesis") if isinstance(contract.get("hypothesis"), dict) else {}
    target = str(contract.get("target") or "")
    if not target or not hypothesis:
        return None
    if rejected and stage not in REJECTING_STAGES:
        raise ValueError("only_formal_oos_shadow_or_cooling_may_reject_experiment")
    return upsert_experiment_memory(
        target=target,
        hypothesis=hypothesis,
        status="rejected" if rejected else "observing",
        source_model_version_id=int(version.get("id") or 0) or None,
        outcome_stage=stage,
        evidence=evidence,
    )

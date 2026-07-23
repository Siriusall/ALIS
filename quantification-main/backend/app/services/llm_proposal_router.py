"""Route LLM research proposals into deterministic, bounded experiments.

The LLM supplies an auditable research *signal*, never parameters, code, or a
production action.  Each target maps to one existing deterministic service.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from app.db.store import _conn
from app.services.llm_research_governance import assess_llm_research_proposals
from app.services.experiment_memory import find_rejected_hypothesis, upsert_experiment_memory


MAX_PROPOSALS_PER_CYCLE = 3
MIN_CONFIDENCE = 0.5


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_routes (
            id BIGSERIAL PRIMARY KEY,
            proposal_id BIGINT NOT NULL,
            hypothesis_index INTEGER NOT NULL,
            route_key VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL,
            UNIQUE(proposal_id,hypothesis_index,route_key)
        )""")


def _routes() -> dict[str, tuple[str, Callable[[], dict[str, Any]]]]:
    # Imports stay lazy so the router remains cheap and testable in isolation.
    from app.services.factor_platform import run_factor_daily_cycle
    from app.services.plan_model_auto_optimizer import run_auto_optimization_once
    from app.services.review_data_quality_governance import run_review_data_quality_governance
    from app.services.strategy_controller import run_strategy_policy_auto_optimization
    return {
        "factor": ("factor_daily_evaluation", lambda: run_factor_daily_cycle()),
        "model": ("model_challenger_cycle", lambda: run_auto_optimization_once(force=False)),
        "strategy": ("strategy_policy_whitelist", lambda: run_strategy_policy_auto_optimization(force=False)),
        "data": ("oos_data_quality_governance", lambda: run_review_data_quality_governance()),
    }


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        decoded = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    return decoded if isinstance(decoded, dict) else {}


def route_llm_research_proposals(*, max_proposals: int = MAX_PROPOSALS_PER_CYCLE) -> dict[str, Any]:
    """Consume at most three proposal hypotheses through fixed experiment routes.

    A route is idempotent for one proposal/hypothesis/template.  Existing
    candidate, OOS, shadow, promotion and rollback gates remain authoritative.
    """
    _ensure_tables()
    governance = assess_llm_research_proposals()
    accepted_by_proposal = governance.get("accepted_by_proposal") or {}
    limit = max(1, min(int(max_proposals or MAX_PROPOSALS_PER_CYCLE), MAX_PROPOSALS_PER_CYCLE))
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM llm_research_proposals WHERE status IN ('proposed','routed') ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()]
    registered = _routes()
    routed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for proposal in rows:
        hypotheses = _payload(proposal).get("hypotheses") or []
        for index, hypothesis in enumerate(hypotheses[:3]):
            if not isinstance(hypothesis, dict):
                continue
            if index not in set(accepted_by_proposal.get(int(proposal["id"]) or [], [])):
                skipped.append({"proposal_id": proposal["id"], "hypothesis_index": index, "reason": "proposal_quality_gate_rejected"})
                continue
            target = str(hypothesis.get("target") or "")
            memory_hypothesis = {
                "target": target.strip().lower(),
                "title": str(hypothesis.get("title") or "").strip(),
                "proposed_experiment": str(hypothesis.get("proposed_experiment") or "").strip(),
            }
            if find_rejected_hypothesis("llm_research", memory_hypothesis):
                skipped.append({"proposal_id": proposal["id"], "hypothesis_index": index, "reason": "formally_rejected_hypothesis"})
                continue
            route = registered.get(target)
            confidence = float(hypothesis.get("confidence") or 0.0)
            if not route or confidence < MIN_CONFIDENCE:
                skipped.append({"proposal_id": proposal["id"], "hypothesis_index": index, "reason": "target_not_whitelisted_or_low_confidence"})
                continue
            route_key, action = route
            now = datetime.now().isoformat(timespec="seconds")
            evidence = {
                "proposal_key": proposal.get("proposal_key"), "source_period": proposal.get("source_period"),
                "target": target, "title": str(hypothesis.get("title") or "")[:160],
                "confidence": confidence, "evidence_refs": hypothesis.get("evidence_refs") or [],
                "policy": "llm_signal_only; fixed_template_only; no_production_write",
            }
            upsert_experiment_memory(
                target="llm_research",
                hypothesis=memory_hypothesis,
                status="routed",
                outcome_stage="deterministic_route_started",
                evidence={"proposal_id": proposal.get("id"), "hypothesis_index": index, "route_key": route_key},
            )
            with _conn() as conn:
                exists = conn.execute(
                    "SELECT id,status FROM llm_research_routes WHERE proposal_id=? AND hypothesis_index=? AND route_key=?",
                    (int(proposal["id"]), index, route_key),
                ).fetchone()
                if exists:
                    skipped.append({"proposal_id": proposal["id"], "hypothesis_index": index, "reason": "already_routed"})
                    continue
                conn.execute(
                    """INSERT INTO llm_research_routes(proposal_id,hypothesis_index,route_key,status,evidence_json,result_json,created_at,updated_at)
                       VALUES(?,?,?,'running',?,?,?,?)""",
                    (int(proposal["id"]), index, route_key, json.dumps(evidence, ensure_ascii=False), "{}", now, now),
                )
            try:
                result = action()
                status = "completed" if bool(result.get("ok", True)) else "failed"
            except Exception as exc:  # retain the failure in the audit trail, continue other routes
                result, status = {"ok": False, "error": str(exc)[:1000]}, "failed"
            with _conn() as conn:
                conn.execute(
                    """UPDATE llm_research_routes SET status=?,result_json=?,updated_at=?
                       WHERE proposal_id=? AND hypothesis_index=? AND route_key=?""",
                    (status, json.dumps(result, ensure_ascii=False, default=str), datetime.now().isoformat(timespec="seconds"), int(proposal["id"]), index, route_key),
                )
                conn.execute("UPDATE llm_research_proposals SET status='routed',updated_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), int(proposal["id"])))
            routed.append({"proposal_id": proposal["id"], "hypothesis_index": index, "route_key": route_key, "status": status})
    return {
        "ok": True, "schema_version": "alis_llm_proposal_router_v1", "proposal_limit": limit,
        "routed_count": len(routed), "routed": routed, "skipped": skipped,
        "governance": {"assessed_count": governance.get("assessed_count", 0)},
        "policy": "fixed_whitelist_templates_only; candidate_OOS_shadow_promotion_rollback_gates_unchanged",
    }

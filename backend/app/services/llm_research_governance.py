"""Deterministic quality gate for LLM research proposals."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.db.store import _conn, _read_conn
from app.services.experiment_memory import find_rejected_hypothesis

MIN_CONFIDENCE = 0.5
MIN_QUALITY_PROPOSALS = 20
MIN_QUALITY_SOURCE_PERIODS = 3
QUALITY_REPORT_BATCH_SIZE = 200

ISSUE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "data_quality": ("数据质量", "数据异常", "数据缺失", "data_quality", "missing data"),
    "high_turnover": ("高换手", "换手率", "交易成本", "high_turnover", "turnover"),
    "model_failure": ("模型失效", "模型信号衰减", "模型预测偏差", "model_failure"),
    "factor_drift": ("因子漂移", "特征漂移", "结构性反转", "因子衰减", "factor", "feature drift"),
    "research_omission": ("研究遗漏", "未监控", "research_omission"),
    "negative_excess_return": ("负超额", "超额收益为负", "negative_excess_return"),
    "liquidity": ("流动性", "liquidity"),
}
TARGET_ISSUE_PRIORITY: dict[str, tuple[str, ...]] = {
    "data": ("data_quality", "research_omission", "factor_drift", "negative_excess_return"),
    "strategy": ("high_turnover", "negative_excess_return", "model_failure", "liquidity"),
    "model": ("model_failure", "factor_drift", "liquidity", "negative_excess_return", "data_quality"),
    "factor": ("factor_drift", "research_omission", "model_failure", "negative_excess_return"),
}


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_research_proposals (
            id BIGSERIAL PRIMARY KEY,
            proposal_key VARCHAR(64) NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL DEFAULT 'proposed',
            source_period VARCHAR(32) NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            model VARCHAR(128) NOT NULL DEFAULT '',
            created_at VARCHAR(64) NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )""")
        conn.execute(
            "ALTER TABLE llm_research_proposals ADD COLUMN IF NOT EXISTS evidence_json TEXT NOT NULL DEFAULT '{}'"
        )
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


def _semantic_signature(item: dict[str, Any]) -> str:
    target = str(item.get("target") or "").strip().lower()
    text = "|".join(
        [
            str(item.get("title") or ""),
            str(item.get("rationale") or ""),
            str(item.get("proposed_experiment") or ""),
            "|".join(str(ref) for ref in (item.get("evidence_refs") or [])),
        ]
    ).lower()
    matched = {
        issue
        for issue, keywords in ISSUE_KEYWORDS.items()
        if any(keyword.lower() in text for keyword in keywords)
    }
    primary = next(
        (issue for issue in TARGET_ISSUE_PRIORITY.get(target, ()) if issue in matched),
        "",
    )
    if primary:
        return f"{target}|{primary}"
    return f"{target}|exact:{_fingerprint(item)}"


def _json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    return decoded if isinstance(decoded, dict) else {}


def assess_llm_research_proposals(*, limit: int = 20) -> dict[str, Any]:
    """Assess proposal evidence before any deterministic experiment is triggered."""
    _ensure_tables()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM llm_research_proposals WHERE status IN ('proposed','routed') ORDER BY id ASC LIMIT ?", (max(1, min(int(limit), 100)),)
        ).fetchall()]
    seen: set[str] = set()
    semantic_seen: set[str] = set()
    accepted_by_proposal: dict[int, set[int]] = {}
    assessments: list[dict[str, Any]] = []
    for row in rows:
        evidence_snapshot = _json_object(row.get("evidence_json"))
        evidence_catalog = set(str(item) for item in (evidence_snapshot.get("evidence_catalog") or []))
        accepted: list[int] = []
        rejected: list[dict[str, Any]] = []
        hypothesis_evidence: list[dict[str, Any]] = []
        for index, item in enumerate((_payload(row).get("hypotheses") or [])[:3]):
            if not isinstance(item, dict):
                rejected.append({"index": index, "reason": "invalid_hypothesis"})
                continue
            missing = [key for key in ("title", "rationale", "proposed_experiment") if not str(item.get(key) or "").strip()]
            if not list(item.get("evidence_refs") or []):
                missing.append("evidence_refs")
            unsupported_refs = list(item.get("unsupported_evidence_refs") or [])
            if unsupported_refs:
                missing.append("unsupported_evidence_reference")
            refs = [str(ref) for ref in (item.get("evidence_refs") or [])]
            if evidence_catalog and any(ref not in evidence_catalog for ref in refs):
                missing.append("unfrozen_evidence_reference")
            if float(item.get("confidence") or 0) < MIN_CONFIDENCE:
                missing.append("minimum_confidence")
            fingerprint = _fingerprint(item)
            if fingerprint in seen:
                missing.append("duplicate_hypothesis")
            semantic_signature = _semantic_signature(item)
            semantic_key = f"{row.get('source_period') or ''}|{semantic_signature}"
            if semantic_key in semantic_seen:
                missing.append("semantic_duplicate_hypothesis")
            if find_rejected_hypothesis("llm_research", {
                "target": str(item.get("target") or "").strip().lower(),
                "title": str(item.get("title") or "").strip(),
                "proposed_experiment": str(item.get("proposed_experiment") or "").strip(),
            }):
                missing.append("formally_rejected_hypothesis")
            seen.add(fingerprint)
            semantic_seen.add(semantic_key)
            hypothesis_evidence.append(
                {
                    "index": index,
                    "semantic_signature": semantic_signature,
                    "evidence_snapshot_available": bool(evidence_catalog),
                    "evidence_reference_count": len(refs),
                    "evidence_refs_exactly_verified": bool(evidence_catalog) and bool(refs) and all(ref in evidence_catalog for ref in refs),
                    "unsupported_evidence_refs": unsupported_refs,
                }
            )
            if missing:
                rejected.append({"index": index, "reason": ",".join(missing)})
            else:
                accepted.append(index)
        status = "accepted" if accepted else "rejected"
        assessment = {
            "accepted_indices": accepted,
            "rejected": rejected,
            "hypothesis_evidence": hypothesis_evidence,
            "policy": "requires_frozen_evidence_and_cross_cycle_semantic_non_duplicate_hypothesis",
        }
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
    return {
        "ok": True,
        "schema_version": "alis_llm_research_governance_v2",
        "assessed_count": len(assessments),
        "accepted_by_proposal": accepted_by_proposal,
        "assessments": assessments,
        "quality_report": llm_research_proposal_quality_report(),
    }


def _route_effectful(route_key: str, result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    numeric_keys = ("created_count", "rows_written", "repaired", "updated", "materialized")
    if any(float(result.get(key) or 0) > 0 for key in numeric_keys):
        return True
    if int(result.get("task_id") or 0) > 0:
        return True
    if route_key == "factor_daily_evaluation":
        materialization = result.get("materialization") if isinstance(result.get("materialization"), dict) else {}
        evaluation = result.get("evaluation") if isinstance(result.get("evaluation"), dict) else {}
        if int(materialization.get("value_count") or 0) > 0:
            return True
        if list(evaluation.get("results") or evaluation.get("evaluations") or []):
            return True
    repair = result.get("repair") if isinstance(result.get("repair"), dict) else {}
    return bool(repair.get("attempted"))


def llm_research_proposal_quality_report(*, limit: int | None = None) -> dict[str, Any]:
    """Aggregate cross-cycle quality without granting production authority."""

    _ensure_tables()
    requested = None if limit is None else max(1, int(limit))
    hypothesis_count = 0
    accepted_count = 0
    rejected_count = 0
    semantic_duplicate_count = 0
    evidence_present_count = 0
    evidence_verified_count = 0
    evidence_snapshot_hypothesis_count = 0
    unsupported_evidence_count = 0
    target_counts: dict[str, int] = {}
    source_periods: set[str] = set()
    proposal_count = route_count = completed_route_count = failed_route_count = 0
    effectful_routes = 0
    last_id: int | None = None
    remaining = requested
    with _read_conn() as conn:
        while remaining is None or remaining > 0:
            batch_size = QUALITY_REPORT_BATCH_SIZE if remaining is None else min(QUALITY_REPORT_BATCH_SIZE, remaining)
            if requested is None:
                where = "WHERE id>?" if last_id is not None else ""
                params = (last_id, batch_size) if last_id is not None else (batch_size,)
                order = "ASC"
            else:
                where = "WHERE id<?" if last_id is not None else ""
                params = (last_id, batch_size) if last_id is not None else (batch_size,)
                order = "DESC"
            proposals = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT id,source_period,payload_json
                        FROM llm_research_proposals {where}
                        ORDER BY id {order} LIMIT ?""",
                    params,
                ).fetchall()
            ]
            if not proposals:
                break
            last_id = int(proposals[-1]["id"])
            if remaining is not None:
                remaining -= len(proposals)
            proposal_ids = [int(row["id"]) for row in proposals]
            placeholders = ",".join("?" for _ in proposal_ids)
            assessments = {
                int(row["proposal_id"]): dict(row)
                for row in conn.execute(
                    f"""SELECT proposal_id,assessment_json
                        FROM llm_research_proposal_assessments
                        WHERE proposal_id IN ({placeholders})""",
                    tuple(proposal_ids),
                ).fetchall()
            }
            routes = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT proposal_id,status,route_key,result_json
                        FROM llm_research_routes
                        WHERE proposal_id IN ({placeholders})""",
                    tuple(proposal_ids),
                ).fetchall()
            ]
            proposal_count += len(proposals)
            for proposal in proposals:
                period = str(proposal.get("source_period") or "")
                if period:
                    source_periods.add(period)
                items = (_payload(proposal).get("hypotheses") or [])[:3]
                assessment = _json_object(
                    (assessments.get(int(proposal["id"])) or {}).get("assessment_json")
                )
                accepted_indices = {
                    int(index) for index in (assessment.get("accepted_indices") or [])
                }
                rejected_by_index = {
                    int(item.get("index")): str(item.get("reason") or "")
                    for item in (assessment.get("rejected") or [])
                    if isinstance(item, dict) and item.get("index") is not None
                }
                evidence_rows = {
                    int(item.get("index")): item
                    for item in (assessment.get("hypothesis_evidence") or [])
                    if isinstance(item, dict) and item.get("index") is not None
                }
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    hypothesis_count += 1
                    target = str(item.get("target") or "")
                    target_counts[target] = target_counts.get(target, 0) + 1
                    evidence_present_count += int(bool(list(item.get("evidence_refs") or [])))
                    if index in accepted_indices:
                        accepted_count += 1
                    else:
                        rejected_count += 1
                    semantic_duplicate_count += int(
                        "semantic_duplicate_hypothesis" in rejected_by_index.get(index, "")
                    )
                    evidence_row = evidence_rows.get(index) or {}
                    evidence_snapshot_hypothesis_count += int(
                        bool(evidence_row.get("evidence_snapshot_available"))
                    )
                    evidence_verified_count += int(
                        bool(evidence_row.get("evidence_refs_exactly_verified"))
                    )
                    unsupported_evidence_count += len(
                        evidence_row.get("unsupported_evidence_refs") or []
                    )
            route_count += len(routes)
            for row in routes:
                status = str(row.get("status") or "")
                if status == "completed":
                    completed_route_count += 1
                    effectful_routes += int(
                        _route_effectful(
                            str(row.get("route_key") or ""),
                            _json_object(row.get("result_json")),
                        )
                    )
                elif status == "failed":
                    failed_route_count += 1
            if len(proposals) < batch_size:
                break

    sufficient_history = proposal_count >= MIN_QUALITY_PROPOSALS and len(source_periods) >= MIN_QUALITY_SOURCE_PERIODS
    metrics = {
        "proposal_count": proposal_count,
        "source_period_count": len(source_periods),
        "hypothesis_count": hypothesis_count,
        "accepted_hypothesis_count": accepted_count,
        "rejected_hypothesis_count": rejected_count,
        "semantic_duplicate_count": semantic_duplicate_count,
        "semantic_duplicate_rate": round(semantic_duplicate_count / hypothesis_count, 6) if hypothesis_count else None,
        "evidence_reference_presence_rate": round(evidence_present_count / hypothesis_count, 6) if hypothesis_count else None,
        "frozen_evidence_snapshot_coverage": round(evidence_snapshot_hypothesis_count / hypothesis_count, 6) if hypothesis_count else None,
        "exact_evidence_verification_rate": round(evidence_verified_count / hypothesis_count, 6) if hypothesis_count else None,
        "unsupported_evidence_reference_count": unsupported_evidence_count,
        "target_distribution": dict(sorted(target_counts.items())),
        "route_count": route_count,
        "completed_route_count": completed_route_count,
        "failed_route_count": failed_route_count,
        "effectful_route_count": effectful_routes,
        "completed_route_noop_count": completed_route_count - effectful_routes,
    }
    return {
        "ok": True,
        "schema_version": "alis_llm_research_quality_report_v1",
        "status": "measured" if sufficient_history else "observing",
        "metrics": metrics,
        "evidence_sufficiency": {
            "minimum_proposals": MIN_QUALITY_PROPOSALS,
            "minimum_source_periods": MIN_QUALITY_SOURCE_PERIODS,
            "sufficient": sufficient_history,
        },
        "limitations": [
            "历史提案未保存冻结证据目录时只能统计引用存在，不能回溯证明引用值真实。",
            "重复率衡量同周期、同目标、同问题族的路由冗余，不评价自然语言写作质量。",
            "下游无动作不必然表示提案错误，也可能表示确定性治理已无缺口。",
        ],
        "production_change_forbidden": True,
    }

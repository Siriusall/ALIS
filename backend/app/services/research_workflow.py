from __future__ import annotations

from typing import Any

from app.data_platform.services.data_center import financial_research_completeness
from app.services.decision_store import list_pool_members, transition_pool_member
from app.services.research_engine import build_stock_research, build_valuation_state
from app.services.thesis_store import generate_thesis_for_stock, list_theses


def evaluate_core_pool_eligibility(
    ts_code: str,
    *,
    as_of_time: str | None = None,
    prepared_thesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all plan-defined evidence gates against one authoritative snapshot."""
    normalized = str(ts_code).strip().upper()
    research = build_stock_research(normalized, as_of_time=as_of_time)
    valuation = build_valuation_state(normalized, as_of_time=as_of_time)
    theses = (
        [prepared_thesis]
        if isinstance(prepared_thesis, dict) and prepared_thesis
        else list_theses(ts_code=normalized, limit=20)
    )
    active_theses = [item for item in theses if item.get("derived_status") not in {"invalidated", "failed", "draft"}]
    financial_fields = financial_research_completeness(normalized, period_limit=5, as_of_time=as_of_time)
    dimensions = research.get("dimensions") or {}
    ready_dimensions = [key for key, value in dimensions.items() if value.get("status") == "ready"]
    gates = [
        {"key": "stock_master", "label": "股票与研究事实", "passed": bool(research.get("ok")), "detail": research.get("reason") or "股票主数据与研究事实可读取"},
        {"key": "risk", "label": "风险硬门控", "passed": bool(research.get("risk", {}).get("eligible")), "detail": "；".join(research.get("risk", {}).get("hard_reasons") or []) or "未触发硬否决"},
        {"key": "financial_fields", "label": "研究财务字段完整性", "passed": financial_fields.get("status") == "complete", "detail": f"字段检查覆盖 {float(financial_fields.get('coverage') or 0):.1%}"},
        {"key": "research", "label": "多维研究覆盖", "passed": len(ready_dimensions) >= 3, "detail": f"{len(ready_dimensions)}/{len(dimensions)} 个维度达到可评价状态"},
        {"key": "valuation", "label": "估值与安全边际", "passed": bool(valuation.get("ok") and valuation.get("scenarios")), "detail": valuation.get("reason") or valuation.get("method") or "已形成估值情景"},
        {"key": "thesis", "label": "可证伪投资逻辑", "passed": bool(active_theses), "detail": f"{len(active_theses)} 条有效逻辑" if active_theses else "尚无有效投资逻辑"},
    ]
    return {
        "ok": True,
        "ts_code": normalized,
        "as_of_time": as_of_time,
        "eligible_for_core": all(item["passed"] for item in gates),
        "gates": gates,
        "research_state": research.get("research_state"),
        "research_dimensions": {
            key: {
                "status": value.get("status"),
                "score": value.get("score"),
            }
            for key, value in dimensions.items()
            if isinstance(value, dict)
        },
        "raw_metrics": {
            key: (research.get("raw_metrics") or {}).get(key)
            for key in (
                "revenue_growth", "core_profit_growth", "cash_conversion",
                "debt_to_assets", "valuation_base_upside", "growth_acceleration",
            )
            if (research.get("raw_metrics") or {}).get(key) is not None
        },
        "financial_field_completeness": financial_fields,
        "valuation": {
            "status": valuation.get("status"),
            "method": valuation.get("method"),
            "scenarios": valuation.get("scenarios"),
            "dimension": valuation.get("dimension") or {},
        },
        "thesis_count": len(active_theses),
        "thesis_source": "prepared_point_in_time" if prepared_thesis else "persisted_compatibility",
    }


def process_watch_pool(*, limit: int = 100) -> dict[str, Any]:
    """Evaluate staged candidates for core admission; retained name kept for API compatibility."""
    members = list_pool_members("candidate")[:max(1, min(int(limit), 500))]
    promoted: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for member in members:
        code = str(member.get("ts_code") or "").strip().upper()
        try:
            thesis = generate_thesis_for_stock(code)
            eligibility = evaluate_core_pool_eligibility(code)
            evidence_reason = "; ".join(f"{gate['key']}={'pass' if gate['passed'] else 'blocked'}" for gate in eligibility["gates"])
            if eligibility["eligible_for_core"]:
                pool_item = transition_pool_member(ts_code=code, from_pool="candidate", to_pool="core", reason=f"automatic_research_workflow_v2; {evidence_reason}")
                promoted.append({"ts_code": code, "pool_item": pool_item, "thesis": thesis, "eligibility": eligibility})
            else:
                blocked.append({"ts_code": code, "thesis": thesis, "eligibility": eligibility})
        except Exception as exc:  # noqa: BLE001 - each candidate must be isolated
            failed.append({"ts_code": code, "error": str(exc)[:300]})
    return {"ok": not failed, "schema_version": "candidate_to_core_v2", "candidates": len(members), "promoted": promoted, "blocked": blocked, "failed": failed}


def stage_watch_pool_candidates(*, limit: int = 50) -> dict[str, Any]:
    """Repair/stage the highest research-scored watch names into the candidate tier."""
    members = list_pool_members("watch")
    def score(item: dict[str, Any]) -> float:
        reason = str(item.get("reason") or "")
        try:
            return float(reason.split("score=", 1)[1].split()[0])
        except (IndexError, ValueError):
            return 0.0
    staged = []
    failed = []
    for member in sorted(members, key=score, reverse=True)[:max(1, min(int(limit), 100))]:
        code = str(member.get("ts_code") or "")
        try:
            item = transition_pool_member(
                ts_code=code, from_pool="watch", to_pool="candidate",
                reason=f"candidate_stage_v2; research_score={score(member):.2f}; source={member.get('reason') or ''}",
            )
            staged.append(item)
        except Exception as exc:
            failed.append({"ts_code": code, "error": str(exc)[:200]})
    return {"ok": not failed, "staged": len(staged), "items": staged, "failed": failed}

"""Turn periodic-review data-quality findings into bounded data governance."""

from __future__ import annotations

from typing import Any

from app.data_platform.services.data_center import run_data_quality_checks
from app.data_platform.services.oos_hard_data import audit_oos_hard_data, repair_oos_hard_data_batch
from app.services.performance_reports import latest_performance_learning_context


MAX_REPAIR_DATES_PER_CYCLE = 5


def _data_quality_error_count(context: dict[str, Any]) -> int:
    errors = context.get("error_breakdown") if isinstance(context.get("error_breakdown"), dict) else {}
    types = errors.get("review_error_types") if isinstance(errors.get("review_error_types"), dict) else {}
    return int(types.get("数据质量") or 0)


def run_review_data_quality_governance(*, max_repair_dates: int = MAX_REPAIR_DATES_PER_CYCLE) -> dict[str, Any]:
    """Audit, selectively repair, then re-audit review-driven quality issues.

    A review label alone never triggers a broad historical pull.  Automatic
    repair is limited to concrete missing sessions in the permanent OOS data
    contract, and is capped per autonomy cycle.
    """
    context = latest_performance_learning_context()
    error_count = _data_quality_error_count(context)
    before = audit_oos_hard_data(include_dates=False)
    trade_date = str(before.get("window_end") or "")
    quality = run_data_quality_checks(trade_date=trade_date or None)
    result: dict[str, Any] = {
        "ok": True,
        "schema_version": "alis_review_data_quality_governance_v1",
        "status": "not_triggered" if error_count <= 0 else "audited",
        "review_period": context.get("period"),
        "review_data_quality_error_count": error_count,
        "hard_data_before": before,
        "quality_checks": quality.get("checks") or [],
        "repair_limit": max(1, min(int(max_repair_dates or MAX_REPAIR_DATES_PER_CYCLE), MAX_REPAIR_DATES_PER_CYCLE)),
        "repair": {"attempted": False, "dates": [], "result": None},
        "policy": "only_repair_explicit_oos_hard_data_gaps; never_overwrite_history_or_relax_quality_gate",
    }
    if error_count <= 0:
        return result
    if str(before.get("status") or "") == "ready":
        result["status"] = "audited_no_actionable_hard_gap"
        return result

    detailed = audit_oos_hard_data(include_dates=True)
    gaps = {
        str(item.get("trade_date") or ""): item
        for item in detailed.get("dates") or []
        if isinstance(item, dict) and not bool(item.get("eligible")) and str(item.get("trade_date") or "")
    }
    limit = result["repair_limit"]
    dates = sorted(gaps)[:limit]
    if not dates:
        result["status"] = "audited_no_repairable_gap"
        return result
    repair = repair_oos_hard_data_batch(dates, date_gaps=gaps)
    after = audit_oos_hard_data(include_dates=False)
    result.update({
        "ok": bool(repair.get("ok")),
        "status": "repaired" if repair.get("ok") else "repair_partial",
        "hard_data_after": after,
        "repair": {"attempted": True, "dates": dates, "result": repair},
    })
    return result

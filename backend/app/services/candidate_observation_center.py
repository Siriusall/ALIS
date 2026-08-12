"""Owner-facing projection of all candidate runtimes and isolated accounts."""

from __future__ import annotations

import json
from typing import Any

from app.db.store import _conn


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def build_candidate_observation_center(*, limit: int = 200) -> dict[str, Any]:
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT c.*,m.model_key,m.version,m.status AS version_status,
                      m.validation_json,m.created_at AS version_created_at,
                      a.id AS account_id,a.status AS account_status,
                      a.latest_trade_date,a.initial_cash,a.latest_cash,a.latest_equity,
                      a.updated_at AS account_updated_at,
                      e.stock_value,e.positions_json,
                      (SELECT COUNT(*) FROM candidate_simulation_operations o WHERE o.account_id=a.id) AS operation_count,
                      (SELECT COALESCE(SUM(o.fees),0) FROM candidate_simulation_operations o WHERE o.account_id=a.id) AS total_fees,
                      (SELECT COUNT(*) FROM candidate_simulation_blocked_events b WHERE b.account_id=a.id) AS blocked_event_count,
                      (SELECT COUNT(*) FROM candidate_simulation_decisions d WHERE d.account_id=a.id) AS decision_count
                 FROM candidate_lifecycle_records c
                 JOIN model_versions m ON m.id=c.model_version_id
                 LEFT JOIN candidate_simulation_accounts a ON a.id=(
                     SELECT a2.id FROM candidate_simulation_accounts a2
                      WHERE a2.model_version_id=c.model_version_id
                      ORDER BY a2.updated_at DESC,a2.id DESC LIMIT 1
                 )
                 LEFT JOIN candidate_simulation_daily_equity e
                   ON e.account_id=a.id
                  AND e.trade_date=(
                     SELECT MAX(e2.trade_date) FROM candidate_simulation_daily_equity e2
                      WHERE e2.account_id=a.id
                  )
                ORDER BY c.updated_at DESC,c.model_version_id DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()]
    items = []
    for row in rows:
        validation = _loads(row.pop("validation_json", None), {})
        review = validation.get("review") if isinstance(validation.get("review"), dict) else {}
        blocked = (
            (validation.get("production_eligibility") or {}).get("blocked_reasons")
            if isinstance(validation.get("production_eligibility"), dict)
            else validation.get("blocked_reasons")
        ) or []
        initial = float(row.get("initial_cash") or 0.0)
        equity = float(row.get("latest_equity") or 0.0)
        positions = _loads(row.get("positions_json"), {})
        items.append({
            "model_version_id": int(row["model_version_id"]),
            "candidate_type": row.get("candidate_type"),
            "name": row.get("version"),
            "component": row.get("model_key"),
            "change_type": row.get("change_type"),
            "runtime_status": row.get("runtime_status"),
            "eligibility_status": row.get("eligibility_status"),
            "version_status": row.get("version_status"),
            "stop_reason": row.get("stop_reason") or "",
            "reviewer": review.get("reviewer") or "",
            "review_comment": review.get("comment") or "",
            "started_at": row.get("version_created_at") or row.get("created_at"),
            "problem_family": row.get("problem_family"),
            "production_change_forbidden": bool(row.get("production_change_forbidden")),
            "blocked_reasons": list(blocked),
            "account": {
                "status": row.get("account_status") or "awaiting_first_replay",
                "latest_trade_date": row.get("latest_trade_date"),
                "initial_cash": initial or None,
                "latest_cash": float(row.get("latest_cash") or 0.0) if row.get("account_id") else None,
                "stock_value": float(row.get("stock_value") or 0.0) if row.get("account_id") else None,
                "latest_equity": equity if row.get("account_id") else None,
                "return": round(equity / initial - 1.0, 10) if initial > 0 else None,
                "position_count": len(positions) if isinstance(positions, dict) else 0,
                "decision_count": int(row.get("decision_count") or 0),
                "operation_count": int(row.get("operation_count") or 0),
                "total_fees": float(row.get("total_fees") or 0.0),
                "blocked_event_count": int(row.get("blocked_event_count") or 0),
                "updated_at": row.get("account_updated_at"),
            },
            "next_action": (
                "continue_isolated_observation"
                if str(row.get("runtime_status")) == "observing"
                else "none"
            ),
        })
    return {
        "ok": True,
        "schema_version": "alis_candidate_observation_center_v1",
        "summary": {
            "total": len(items),
            "observing": sum(item["runtime_status"] == "observing" for item in items),
            "with_isolated_account": sum(item["account"]["initial_cash"] is not None for item in items),
            "with_replay_evidence": sum(bool(item["account"]["latest_trade_date"]) for item in items),
            "production_eligible": sum(item["eligibility_status"] == "eligible" for item in items),
            "production_authority": 0,
        },
        "items": items,
        "policy": {
            "observation": "candidate_starts_immediately_in_isolated_simulation",
            "production": "only_explicit_eligibility_and_promotion_can_change_champion",
            "evidence": "new_versions_never_inherit_parent_performance",
        },
    }

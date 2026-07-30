from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.store import list_model_train_runs, list_model_versions
from app.services.backend_acceptance import backend_acceptance_matrix
from app.services.market_context import market_state_model_governance
from app.services.model_registry import get_model_status, MODEL_CONSTITUTION
from app.services.operations_overview import build_governance_overview


def _status_from_counts(counts: dict[str, Any]) -> str:
    if int(counts.get("failed") or 0) > 0:
        return "blocked"
    if int(counts.get("partial") or 0) > 0:
        return "partial"
    return "ready"


def _version_card(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    validation = item.get("validation") or {}
    return {
        "id": item.get("id"),
        "version": item.get("version"),
        "status": item.get("status"),
        "is_active": bool(item.get("is_active")),
        "metrics": item.get("metrics") or {},
        "validation": validation,
        "production_eligible": bool(validation.get("production_eligible")),
        "blocked_reasons": (validation.get("production_eligibility") or {}).get("blocked_reasons") or [],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def build_strategy_workbench(limit: int = 10, *, include_details: bool = False) -> dict[str, Any]:
    """Read-only strategy workbench contract for the frontend.

    The workbench intentionally aggregates evidence and links only; it does not
    trigger training, API refreshes, backfills, promotion, or simulated trades.
    """
    n = max(1, min(int(limit or 10), 50))
    model_status = get_model_status()
    main_versions = list_model_versions("main", limit=n)
    train_runs = list_model_train_runs(limit=n)
    market_governance = market_state_model_governance()
    acceptance = backend_acceptance_matrix() if include_details else {
        "status": "linked",
        "counts": {},
        "checks": [],
    }
    operations = build_governance_overview() if include_details else {
        "ok": None,
        "status": "linked",
        "links": {"detail": "/api/research/governance/overview"},
    }

    active = _version_card(model_status.get("active_version"))
    challengers = [_version_card(item) for item in main_versions if not item.get("is_active")]
    failed_or_watch = [
        run for run in train_runs
        if str(run.get("status") or "") in {"failed"}
        or str(run.get("phase") or "") == "failed"
        or (run.get("error") or "")
    ]
    experiments = [
        {
            "id": run.get("id"),
            "status": run.get("status"),
            "phase": run.get("phase"),
            "progress": run.get("progress"),
            "message": run.get("message"),
            "model_version_id": run.get("model_version_id"),
            "metrics": run.get("metrics") or {},
            "error": run.get("error"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "updated_at": run.get("updated_at"),
        }
        for run in train_runs
    ]

    acceptance_counts = acceptance.get("counts") or {}
    status = {
        "overall": "blocked" if not model_status.get("has_active_model") else _status_from_counts(acceptance_counts),
        "model": "ready" if model_status.get("has_active_model") else "transparent_rule_fallback",
        "backend_acceptance": acceptance.get("status"),
        "market_regime": (market_governance.get("hsmm") or {}).get("status"),
        "operations": "ready" if operations.get("ok") else "linked" if operations.get("ok") is None else "partial",
    }
    decisions = [
        {
            "key": "main_model_fallback",
            "status": status["model"],
            "message": "主横截面模型未处于生产 active；当前分析使用透明规则评分。",
            "evidence": {
                "invalid_active_version": model_status.get("invalid_active_version"),
                "active_artifact": model_status.get("active_artifact"),
            },
        },
        {
            "key": "hsmm_shadow",
            "status": (market_governance.get("hsmm") or {}).get("status"),
            "message": (market_governance.get("hsmm") or {}).get("blocking_reason") or "HSMM已完成生产上线门控。",
            "evidence": market_governance.get("hsmm") or {},
        },
    ]
    return {
        "ok": True,
        "schema_version": "strategy_workbench_v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": status,
        "current_analysis": {
            "mode": "post_close_t_plus_1_simulation",
            "scoring_source": "transparent_rule_fallback" if not model_status.get("has_active_model") else "active_model",
            "execution_rule": "T日收盘生成信号，T+1开盘价加滑点和费用模拟成交",
            "links": {
                "analyze": "/api/quant-runs/analyze",
                "preview": "/api/quant-runs/execute-preview",
                "ledger_summary": "/api/quant-runs/ledger-summary",
            },
        },
        "model": {
            "constitution": MODEL_CONSTITUTION,
            "active": active,
            "active_artifact": model_status.get("active_artifact"),
            "invalid_active_version": model_status.get("invalid_active_version"),
            "challengers": [item for item in challengers if item],
            "latest_train_run": model_status.get("latest_train_run"),
            "experiments": experiments,
            "failed_experiments": failed_or_watch,
            "links": {
                "status": "/api/model-strategy/plan-models",
                "auto_tune": "/api/model-strategy/auto-tune",
                "oos_report": "/api/model-strategy/oos/report",
                "drift": "/api/model-strategy/drift",
            },
        },
        "market": {
            "governance": market_governance,
            "links": {
                "live": "/api/model-strategy/market-context/live",
                "history": "/api/model-strategy/market-context/history",
                "hsmm_train_shadow": "/api/model-strategy/market-context/hsmm/train-shadow",
            },
        },
        "backend_acceptance": {
            "status": acceptance.get("status"),
            "counts": acceptance_counts,
            "checks": acceptance.get("checks") or [],
            "include_details": include_details,
            "links": {"detail": "/api/model-strategy/backend-acceptance"},
        },
        "operations": operations,
        "pending_user_decisions": decisions,
    }

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db.store import get_active_model_version
from app.services.execution_cost_calibration import active_execution_cost_contract
from app.services.factor_platform import FACTOR_SCHEMA_VERSION, active_factor_bindings
from app.services.plan_model_control import PLAN_MODELS
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_controller import active_strategy_policy_context


def build_decision_version_manifest(
    *,
    trade_date: str,
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze every production component that can change a simulated decision."""
    champion = resolve_production_strategy_champion()
    strategy_policy = active_strategy_policy_context()
    model_versions = {
        key: {
            "id": int(active["id"]),
            "version": str(active.get("version") or ""),
            "status": str(active.get("status") or ""),
        }
        for key in PLAN_MODELS
        for active in [get_active_model_version(key)]
        if active
    }
    optimizer = (portfolio or {}).get("optimizer") if isinstance((portfolio or {}).get("optimizer"), dict) else {}
    manifest = {
        "schema_version": "alis_decision_version_manifest_v1",
        "trade_date": str(trade_date).replace("-", "")[:8],
        "data_contract": {
            "schema_version": "alis_oos_hard_data_v1",
            "point_in_time_cutoff": f"{str(trade_date).replace('-', '')[:4]}-{str(trade_date).replace('-', '')[4:6]}-{str(trade_date).replace('-', '')[6:8]}T15:00:00",
        },
        "factor_contract": {
            "schema_version": FACTOR_SCHEMA_VERSION,
            "bindings": active_factor_bindings(),
        },
        "model_versions": model_versions,
        "strategy": {
            "model_version_id": int(champion.get("champion_id") or 0),
            "version": str(champion.get("champion_version") or ""),
            "strategy_id": int(champion.get("strategy_id") or 0),
            "strategy_controller_version_id": strategy_policy.get("strategy_policy_version_id"),
            "strategy_controller_version": strategy_policy.get("strategy_policy_version"),
        },
        "portfolio": {
            "selected_method": optimizer.get("selected_method") or optimizer.get("optimizer"),
            "competition_schema": ((optimizer.get("method_competition") or {}).get("schema_version")),
            "risk_budget": (portfolio or {}).get("risk_budget") or {},
        },
        "execution_cost": active_execution_cost_contract(),
        "simulation_only": True,
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return manifest

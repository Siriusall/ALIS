#!/usr/bin/env python3
"""Register the first ALIS Challenger without changing the active Champion."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json

from app.db.store import _conn, create_model_version, create_strategy, list_model_versions, list_strategies, update_strategy
from app.services.production_strategy import resolve_production_strategy_champion


NAME = "ALIS Challenger 换手追高控制 V1"
MODEL_KEY = "alis_strategy_challenger"


def main() -> int:
    champion = resolve_production_strategy_champion()
    content = deepcopy(champion["strategy"]["content"])
    content["schema_version"] = "alis_challenger_turnover_control_v1"
    content.setdefault("rebalance", {})["freq"] = "weekly"
    content["challenger_controls"] = {
        "max_single_weight": 0.08,
        "max_rebalance_turnover": 0.20,
        "min_weight_change": 0.02,
        "max_entry_1d_return": 0.07,
        "max_entry_5d_return": 0.20,
        "rationale": ["限制单票集中", "周频再平衡", "单次双边换手不超过20%", "禁止单日或五日异常追高"],
    }
    existing = next((item for item in list_strategies() if item.get("name") == NAME), None)
    if not existing:
        with _conn() as conn:
            if not conn._is_mysql:
                conn.execute(
                    """SELECT setval(pg_get_serial_sequence('strategies','id'),
                                     GREATEST(COALESCE((SELECT MAX(id) FROM strategies),1),1),true)"""
                )
    strategy = update_strategy(int(existing["id"]), NAME, "formula", content) if existing else create_strategy(NAME, "formula", content)
    versions = list_model_versions(MODEL_KEY, limit=100)
    version = next((item for item in versions if int((item.get("config") or {}).get("strategy_id") or 0) == int(strategy["id"])), None)
    if not version:
        version_id = create_model_version(
            model_key=MODEL_KEY,
            version=f"alis-challenger-turnover-v1-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            status="candidate",
            artifact_path="builtin://alis/challenger/turnover-control-v1",
            config={
                "role": "challenger",
                "strategy_id": int(strategy["id"]),
                "champion_reference": champion["champion_version"],
                "selection_strategy": content,
            },
            metrics={},
            validation={"status": "pending_oos", "production_eligible": False},
            is_active=False,
        )
    else:
        version_id = int(version["id"])
    print(json.dumps({
        "status": "registered",
        "strategy_id": int(strategy["id"]),
        "model_version_id": version_id,
        "champion_unchanged": champion["champion_version"],
        "controls": content["challenger_controls"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

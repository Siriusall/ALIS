#!/usr/bin/env python3
"""Register and optionally run the two formal strategy comparison baselines."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from typing import Any

from app.db.store import (
    _conn,
    create_model_version,
    create_strategy,
    list_model_versions,
    list_strategies,
    update_model_version_metrics,
    update_model_version_validation,
    update_strategy,
)
from app.data_platform.services.oos_hard_data import audit_oos_hard_data
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.walk_forward_validation import run_purged_walk_forward_validation


MODEL_KEY = "alis_strategy_baseline"
BASELINES = (
    {
        "key": "multifactor_equal_weight",
        "name": "ALIS Baseline 多因子等权 V1",
        "schema_version": "alis_baseline_multifactor_equal_weight_v1",
        "score": None,
    },
    {
        "key": "simple_momentum",
        "name": "ALIS Baseline 简单动量 V1",
        "schema_version": "alis_baseline_simple_momentum_v1",
        "score": {"momentum_20": 1.0},
    },
)


def _baseline_content(champion_content: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
    content = deepcopy(champion_content)
    content["schema_version"] = definition["schema_version"]
    content["portfolio_method"] = "equal_weight"
    content.setdefault("rebalance", {})["freq"] = "weekly"
    if definition["score"] is not None:
        content["score"] = {"factors": definition["score"]}
    content["challenger_controls"] = {
        "max_single_weight": 0.08,
        "max_rebalance_turnover": 0.20,
        "min_weight_change": 0.02,
        "max_entry_1d_return": 0.07,
        "max_entry_5d_return": 0.20,
        "rationale": ["与 Challenger V1 使用相同执行纪律，隔离排序与组合方法差异"],
    }
    return content


def _ensure_strategy_sequence() -> None:
    with _conn() as conn:
        if not conn._is_mysql:
            conn.execute(
                """SELECT setval(pg_get_serial_sequence('strategies','id'),
                                 GREATEST(COALESCE((SELECT MAX(id) FROM strategies),1),1),true)"""
            )


def register_baselines() -> list[dict[str, Any]]:
    champion = resolve_production_strategy_champion()
    champion_content = (champion.get("strategy") or {}).get("content") or {}
    strategies = list_strategies()
    versions = list_model_versions(MODEL_KEY, limit=100)
    registered = []
    for definition in BASELINES:
        content = _baseline_content(champion_content, definition)
        existing = next((item for item in strategies if item.get("name") == definition["name"]), None)
        if not existing:
            _ensure_strategy_sequence()
        strategy = (
            update_strategy(int(existing["id"]), definition["name"], "formula", content)
            if existing else create_strategy(definition["name"], "formula", content)
        )
        version = next(
            (item for item in versions if int((item.get("config") or {}).get("strategy_id") or 0) == int(strategy["id"])),
            None,
        )
        if not version:
            version_id = create_model_version(
                model_key=MODEL_KEY,
                version=f"alis-baseline-{definition['key']}-v1-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                status="baseline",
                artifact_path=f"builtin://alis/baseline/{definition['key']}/v1",
                config={
                    "role": "baseline",
                    "baseline_key": definition["key"],
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
        registered.append({
            "key": definition["key"],
            "name": definition["name"],
            "strategy_id": int(strategy["id"]),
            "model_version_id": version_id,
        })
    return registered


def _persist_result(item: dict[str, Any], result: dict[str, Any]) -> None:
    summary = result.get("summary") or {}
    windows = result.get("windows") or []
    formal = {
        "status": result.get("status"),
        "valid_windows": int(summary.get("valid_window_count") or 0),
        "outperform_windows": int(summary.get("outperform_window_count") or 0),
        "outperform_ratio": summary.get("outperform_ratio"),
        "aggregate_cost_after_return": summary.get("aggregate_cost_after_return"),
        "aggregate_benchmark_return": summary.get("aggregate_benchmark_return"),
        "aggregate_excess_return": summary.get("aggregate_excess_return"),
        "worst_window_drawdown": summary.get("worst_window_drawdown"),
        "total_fees": summary.get("total_fees"),
        "window_returns": [float((window.get("metrics") or {}).get("total_return") or 0.0) for window in windows],
    }
    update_model_version_metrics(item["model_version_id"], metrics={"formal_oos": formal})
    update_model_version_validation(item["model_version_id"], validation={
        "status": f"formal_oos_{result.get('status')}",
        "production_eligible": False,
        "passed_gates": [key for key, value in (result.get("promotion_gates") or {}).items() if value],
        "blocking_reasons": result.get("blocking_reasons") or [],
        "comparison_role": "diagnostic_baseline_not_promotable",
    })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Run the formal five-window OOS after registration")
    parser.add_argument("--only", choices=[item["key"] for item in BASELINES])
    args = parser.parse_args()
    registered = register_baselines()
    if args.only:
        registered = [item for item in registered if item["key"] == args.only]
    output: dict[str, Any] = {"registered": registered, "executed": []}
    if args.execute:
        hard_data = audit_oos_hard_data(include_dates=False)
        if hard_data.get("status") != "ready":
            raise RuntimeError("OOS 永久硬数据未就绪，禁止正式基线验证")
        start_date = str(hard_data["window_start"])
        end_date = str(hard_data["window_end"])
        for item in registered:
            result = run_purged_walk_forward_validation(
                strategy_id=item["strategy_id"],
                start_date=start_date,
                end_date=end_date,
                max_windows=5,
                execute=True,
            )
            _persist_result(item, result)
            output["executed"].append({
                **item,
                "status": result.get("status"),
                "summary": result.get("summary"),
                "blocking_reasons": result.get("blocking_reasons"),
            })
            print(json.dumps(output["executed"][-1], ensure_ascii=False), flush=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

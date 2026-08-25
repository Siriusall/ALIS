from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from statistics import mean
from typing import Any

from app.db.store import (
    _conn,
    create_model_version,
    create_strategy,
    list_model_versions,
    list_strategies,
)
from app.services.factor_platform import ensure_factor_registry
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_config import get_strategy_factor_names


FACTOR_COMBINATION_SCHEMA = "alis_factor_combination_v1"
FACTOR_STRATEGY_LINK_SCHEMA = "alis_factor_strategy_distillation_v1"
CONTINUOUS_RESEARCH_CONFIRMATION_SOURCE = (
    "continuous_factor_model_strategy_research_v9_confirmation"
)
MIN_FACTOR_EVALUATIONS = 20
MAX_COMBINATION_FACTORS = 8
CONTINUOUS_RESEARCH_COMBINATION_STATUS = "research_confirmed"


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat()


def _ensure_tables() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_combinations (
               combination_key TEXT PRIMARY KEY,
               version TEXT NOT NULL,
               name TEXT NOT NULL,
               status TEXT NOT NULL,
               formula_hash TEXT NOT NULL,
               metrics_json TEXT NOT NULL,
               lineage_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_combination_members (
               combination_key TEXT NOT NULL,
               factor_definition_id BIGINT NOT NULL,
               factor_key TEXT NOT NULL,
               weight REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               PRIMARY KEY(combination_key,factor_definition_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_strategy_links (
               combination_key TEXT NOT NULL,
               model_version_id BIGINT NOT NULL,
               strategy_id BIGINT NOT NULL,
               relation TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               PRIMARY KEY(combination_key,model_version_id)
            )"""
        )


def _factor_evidence() -> list[dict[str, Any]]:
    with _conn() as conn:
        definitions = [
            dict(row) for row in conn.execute(
                """SELECT * FROM factor_definitions
                   WHERE status='active' AND category<>'交易约束'
                   ORDER BY category,factor_key"""
            ).fetchall()
        ]
        output: list[dict[str, Any]] = []
        for definition in definitions:
            rows = [
                dict(row) for row in conn.execute(
                    """SELECT rank_ic,cost_after_incremental_oos,coverage,end_date
                       FROM factor_evaluations WHERE factor_definition_id=?
                       ORDER BY end_date DESC,start_date ASC LIMIT 240""",
                    (int(definition["id"]),),
                ).fetchall()
            ]
            if not rows:
                continue
            unique_future_dates: dict[str, dict[str, Any]] = {}
            for row in rows:
                unique_future_dates.setdefault(str(row.get("end_date") or ""), row)
            rows = list(unique_future_dates.values())
            recent = rows[:MIN_FACTOR_EVALUATIONS]
            output.append({
                "definition_id": int(definition["id"]),
                "factor_key": str(definition["factor_key"]),
                "category": str(definition.get("category") or "其他"),
                "direction": int(definition.get("direction") or 1),
                "evaluation_count": len(rows),
                "average_rank_ic": mean(float(row.get("rank_ic") or 0.0) for row in recent),
                "average_cost_after": mean(float(row.get("cost_after_incremental_oos") or 0.0) for row in recent),
                "average_coverage": mean(float(row.get("coverage") or 0.0) for row in recent),
                "latest_end_date": str(rows[0].get("end_date") or ""),
            })
    return output


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _frozen_factor_bindings(keys: Any) -> dict[str, dict[str, Any]]:
    """Bind exact research factor definitions without activating them globally."""
    wanted = {str(key) for key in keys}
    return {
        str(item["factor_key"]): {
            "definition_id": int(item["id"]),
            "version": str(item["version"]),
            "status": str(item.get("status") or ""),
            "formula": item["formula"],
            "available_time_rule": item["available_time_rule"],
            "leakage_status": item["leakage_status"],
        }
        for item in ensure_factor_registry()
        if str(item.get("factor_key") or "") in wanted
        and str(item.get("status") or "") in {"active", "candidate"}
    }


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) < 20 or len(left) != len(right):
        return 0.0
    lx, rx = mean(left), mean(right)
    denominator = (
        sum((value - lx) ** 2 for value in left)
        * sum((value - rx) ** 2 for value in right)
    ) ** 0.5
    return (
        sum((left[index] - lx) * (right[index] - rx) for index in range(len(left))) / denominator
        if denominator > 0 else 0.0
    )


def _deduplicate_factor_evidence(
    evidence: list[dict[str, Any]],
    *,
    correlation_threshold: float = 0.90,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(evidence) < 2:
        return evidence, []
    ids = [int(item["definition_id"]) for item in evidence]
    placeholders = ",".join("?" for _ in ids)
    with _conn() as conn:
        latest_row = conn.execute(
            "SELECT MAX(trade_date) AS trade_date FROM factor_values"
        ).fetchone()
        latest = str(dict(latest_row).get("trade_date") or "") if latest_row else ""
        rows = [
            dict(row) for row in conn.execute(
                f"""SELECT factor_definition_id,ts_code,standardized_value
                    FROM factor_values
                    WHERE trade_date=? AND factor_definition_id IN ({placeholders})""",
                (latest, *ids),
            ).fetchall()
        ] if latest else []
    values: dict[int, dict[str, float]] = {}
    for row in rows:
        values.setdefault(int(row["factor_definition_id"]), {})[str(row["ts_code"])] = float(row.get("standardized_value") or 0.0)
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for candidate in evidence:
        candidate_values = values.get(int(candidate["definition_id"]), {})
        duplicate_of = ""
        duplicate_corr = 0.0
        for existing in kept:
            existing_values = values.get(int(existing["definition_id"]), {})
            common = sorted(set(candidate_values) & set(existing_values))
            correlation = _pearson(
                [candidate_values[symbol] for symbol in common],
                [existing_values[symbol] for symbol in common],
            )
            if abs(correlation) >= float(correlation_threshold):
                duplicate_of = str(existing["factor_key"])
                duplicate_corr = correlation
                break
        if duplicate_of:
            duplicates.append({
                "factor_key": candidate["factor_key"],
                "duplicate_of": duplicate_of,
                "correlation": round(duplicate_corr, 8),
            })
        else:
            kept.append(candidate)
    return kept, duplicates


def build_factor_combination_candidates() -> dict[str, Any]:
    """Build bounded, evidence-weighted factor mixes from mature active factors."""
    _ensure_tables()
    evidence = [
        item for item in _factor_evidence()
        if item["average_rank_ic"] > 0
        and item["average_cost_after"] > 0
        and item["average_coverage"] >= 0.80
    ]
    evidence.sort(
        key=lambda item: (
            -(item["average_rank_ic"] * max(item["average_cost_after"], 0.0)),
            item["factor_key"],
        )
    )
    evidence, duplicates = _deduplicate_factor_evidence(evidence)
    selected: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for item in evidence:
        if item["category"] not in seen_categories:
            selected.append(item)
            seen_categories.add(item["category"])
        if len(selected) >= MAX_COMBINATION_FACTORS:
            break
    if len(selected) < 3:
        for item in evidence:
            if item not in selected:
                selected.append(item)
            if len(selected) >= min(3, MAX_COMBINATION_FACTORS):
                break
    if not selected:
        return {
            "ok": True,
            "schema_version": FACTOR_COMBINATION_SCHEMA,
            "status": "waiting_for_mature_factors",
            "eligible_factor_count": 0,
            "duplicate_factors": duplicates,
            "required_evaluations": MIN_FACTOR_EVALUATIONS,
            "combinations": [],
        }
    strengths = [
        max(1e-9, item["average_rank_ic"] * max(item["average_cost_after"], 1e-6))
        for item in selected
    ]
    total = sum(strengths)
    members = [
        {
            **item,
            "weight": round((strengths[index] / total) * int(item["direction"] or 1), 10),
        }
        for index, item in enumerate(selected)
    ]
    identity = {
        "schema": FACTOR_COMBINATION_SCHEMA,
        "members": [
            {"definition_id": item["definition_id"], "factor_key": item["factor_key"], "weight": item["weight"]}
            for item in members
        ],
    }
    digest = _stable_hash(identity)
    combination_key = f"factor-combination-{digest[:16]}"
    status = (
        "production_eligible"
        if len(members) >= 3 and all(item["evaluation_count"] >= MIN_FACTOR_EVALUATIONS for item in members)
        else "observing"
    )
    metrics = {
        "member_count": len(members),
        "minimum_evaluation_count": min(item["evaluation_count"] for item in members),
        "average_rank_ic": round(mean(item["average_rank_ic"] for item in members), 8),
        "average_cost_after": round(mean(item["average_cost_after"] for item in members), 8),
        "average_coverage": round(mean(item["average_coverage"] for item in members), 8),
        "latest_end_date": max(item["latest_end_date"] for item in members),
    }
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT combination_key FROM factor_combinations WHERE combination_key=?",
            (combination_key,),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO factor_combinations(
                   combination_key,version,name,status,formula_hash,metrics_json,
                   lineage_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    combination_key, f"v-{digest[:12]}", "自动蒸馏多因子组合",
                    status, digest, json.dumps(metrics, ensure_ascii=False),
                    json.dumps({"source": "mature_active_factor_evidence", "identity": identity}, ensure_ascii=False),
                    now, now,
                ),
            )
            for member in members:
                conn.execute(
                    """INSERT INTO factor_combination_members(
                       combination_key,factor_definition_id,factor_key,weight,
                       evidence_json,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        combination_key, member["definition_id"], member["factor_key"],
                        member["weight"], json.dumps(member, ensure_ascii=False), now,
                    ),
                )
    return {
        "ok": True,
        "schema_version": FACTOR_COMBINATION_SCHEMA,
        "status": status,
        "eligible_factor_count": len(evidence),
        "duplicate_factors": duplicates,
        "required_evaluations": MIN_FACTOR_EVALUATIONS,
        "combinations": [{
            "combination_key": combination_key,
            "version": f"v-{digest[:12]}",
            "status": status,
            "metrics": metrics,
            "members": members,
        }],
    }


def distill_factor_combinations_to_strategies(
    combinations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create research-only Challenger recipes from eligible factor mixes."""
    _ensure_tables()
    payload = combinations or build_factor_combination_candidates()
    generated: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    champion = resolve_production_strategy_champion()
    champion_strategy = champion.get("strategy") if isinstance(champion.get("strategy"), dict) else {}
    base_content = champion_strategy.get("content") if isinstance(champion_strategy.get("content"), dict) else {}
    existing_versions = list_model_versions("alis_strategy_challenger", limit=200)
    existing_strategies = list_strategies()
    for combination in payload.get("combinations") or []:
        if str(combination.get("status") or "") not in {
            "production_eligible",
            CONTINUOUS_RESEARCH_COMBINATION_STATUS,
        }:
            continue
        combination_key = str(combination["combination_key"])
        previous = next(
            (
                version for version in existing_versions
                if str(((version.get("config") or {}).get("factor_combination") or {}).get("combination_key") or "")
                == combination_key
            ),
            None,
        )
        if previous:
            reused.append({
                "combination_key": combination_key,
                "model_version_id": int(previous["id"]),
                "version": str(previous.get("version") or ""),
            })
            continue
        members = combination.get("members") or []
        score = {
            str(item["factor_key"]): float(item["weight"])
            for item in members if abs(float(item.get("weight") or 0.0)) > 1e-12
        }
        if len(score) < 3:
            continue
        research_spec = combination.get("research_spec") if isinstance(combination.get("research_spec"), dict) else {}
        frozen_selection = (
            research_spec.get("selection_strategy")
            if isinstance(research_spec.get("selection_strategy"), dict) else {}
        )
        content = deepcopy(
            frozen_selection.get("content")
            if isinstance(frozen_selection.get("content"), dict) else base_content
        )
        content["schema_version"] = "alis_factor_distilled_strategy_v1"
        content["score"] = {"normalization": "rank_percentile", "factors": score}
        content["rebalance"] = {**(content.get("rebalance") or {}), "freq": "weekly", "top_n": 30}
        content["lookback_days"] = max(120, int(content.get("lookback_days") or 0))
        content["portfolio_method"] = str(
            research_spec.get("portfolio_method") or content.get("portfolio_method") or "cvar"
        )
        consumed_factor_keys = (
            get_strategy_factor_names(content, "formula")
            | {"momentum_20", "momentum_60", "volatility_20"}
        )
        universe = content.get("universe") if isinstance(content.get("universe"), dict) else {}
        if float(universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
            consumed_factor_keys.add("float_mv")
        research_factor_bindings = (
            research_spec.get("factor_bindings")
            if isinstance(research_spec.get("factor_bindings"), dict) else {}
        )
        if research_factor_bindings:
            missing_factor_bindings = sorted(
                key for key in consumed_factor_keys
                if not isinstance(research_factor_bindings.get(key), dict)
            )
            if missing_factor_bindings:
                raise ValueError(
                    "连续研究包缺少冻结因子绑定："
                    + ",".join(missing_factor_bindings)
                )
            factor_bindings = {
                key: deepcopy(research_factor_bindings[key])
                for key in sorted(consumed_factor_keys)
            }
        else:
            factor_bindings = _frozen_factor_bindings(consumed_factor_keys)
        content["factor_combination"] = {
            "combination_key": combination_key,
            "version": combination.get("version"),
            "schema_version": FACTOR_COMBINATION_SCHEMA,
        }
        name = f"ALIS Challenger 因子蒸馏 {combination_key[-8:]}"
        strategy = next((item for item in existing_strategies if str(item.get("name") or "") == name), None)
        if not strategy:
            strategy = create_strategy(name, "formula", content)
            existing_strategies.append(strategy)
        evidence = {
            "schema_version": FACTOR_STRATEGY_LINK_SCHEMA,
            "combination": combination,
            "champion_reference": (
                research_spec.get("champion_reference")
                or champion.get("champion_version")
            ),
            "changed_fields": [
                "score", "model_bindings", "market_state_binding",
                "strategy_controller_version_id", "portfolio_method",
            ],
            "research_package": {
                "research_package_hash": research_spec.get("research_package_hash"),
                "model_bindings": research_spec.get("model_bindings") or {},
                "market_state_binding": research_spec.get("market_state_binding") or {},
                "strategy_controller_version_id": research_spec.get("strategy_controller_version_id"),
                "portfolio_method": research_spec.get("portfolio_method") or "cvar",
            },
            "safety": {
                "production_eligible": False,
                "next_gate": "quick_diagnostic",
                "manual_promotion_forbidden": True,
            },
        }
        version_name = f"alis-factor-distilled-{_stable_hash(evidence)[:16]}"
        model_version_id = create_model_version(
            model_key="alis_strategy_challenger",
            version=version_name,
            status="candidate",
            artifact_path=f"builtin://alis/challenger/factor-distilled/{combination_key}",
            config={
                "role": "challenger",
                "generation": "automatic_factor_combination_distillation",
                "strategy_id": int(strategy["id"]),
                "champion_reference": (
                    research_spec.get("champion_reference")
                    or champion.get("champion_version")
                ),
                "selection_strategy": content,
                "factor_combination": {
                    "combination_key": combination_key,
                    "version": combination.get("version"),
                },
                "factor_bindings": factor_bindings,
                "model_bindings": research_spec.get("model_bindings") or {},
                "market_state_binding": research_spec.get("market_state_binding") or {},
                "strategy_controller_version_id": research_spec.get("strategy_controller_version_id"),
                "research_package_hash": research_spec.get("research_package_hash"),
                "research_spec": research_spec,
                "portfolio_optimizer": research_spec.get("portfolio_optimizer") or {},
                "execution_contract": research_spec.get("execution_contract") or {},
                "lineage": {
                    "root_model_version_id": int(
                        research_spec.get("champion_version_id")
                        or champion.get("champion_id") or 0
                    ),
                    "parent_model_version_id": int(
                        research_spec.get("champion_version_id")
                        or champion.get("champion_id") or 0
                    ),
                    "generation_index": 1,
                    "hypothesis_family": "factor_model_strategy_package_distillation",
                    "applied_mutations": [
                        str(research_spec.get("optimization_lane") or "factor")
                    ],
                },
                "evidence_contract": evidence,
            },
            metrics={},
            validation={
                "status": "pending_oos",
                "production_eligible": False,
                "next_gate": "quick_diagnostic",
                "parallel_shadow_enabled": (
                    str(combination.get("status") or "")
                    == CONTINUOUS_RESEARCH_COMBINATION_STATUS
                ),
                "parallel_shadow_started_after": str(
                    combination.get("frozen_after") or ""
                ),
            },
            is_active=False,
        )
        with _conn() as conn:
            conn.execute(
                """INSERT INTO factor_strategy_links(
                   combination_key,model_version_id,strategy_id,relation,evidence_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    combination_key, model_version_id, int(strategy["id"]),
                    "distilled_candidate", json.dumps(evidence, ensure_ascii=False), _now(),
                ),
            )
        generated.append({
            "combination_key": combination_key,
            "strategy_id": int(strategy["id"]),
            "model_version_id": int(model_version_id),
            "version": version_name,
            "status": "pending_oos",
        })
    return {
        "ok": True,
        "schema_version": FACTOR_STRATEGY_LINK_SCHEMA,
        "generated": generated,
        "reused": reused,
    }


def distill_confirmed_research_experiments(
    *,
    snapshot_id: str,
    limit: int = 1,
) -> dict[str, Any]:
    """Freeze validated continuous-research elites and create formal Challengers.

    Historical quick screens are never sufficient. Only completed confirmation
    replays with positive multi-window excess, acceptable drawdown and complete
    point-in-time evidence may cross this research-to-validation boundary.
    """
    _ensure_tables()
    with _conn() as conn:
        snapshot_row = conn.execute(
            "SELECT data_as_of FROM factor_research_snapshots WHERE snapshot_id=?",
            (str(snapshot_id),),
        ).fetchone()
        frozen_after = str(dict(snapshot_row).get("data_as_of") or "") if snapshot_row else ""
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT experiment_id,strategy_hash,factor_weights_json,metrics_json,
                          fitness,generation,parent_experiment_id,finished_at
                   FROM factor_combination_experiments
                   WHERE snapshot_id=? AND research_stage='confirmation'
                     AND status='completed'
                   ORDER BY fitness DESC,finished_at ASC LIMIT ?""",
                (str(snapshot_id), max(1, int(limit))),
            ).fetchall()
        ]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        metrics = json.loads(row.get("metrics_json") or "{}")
        research_spec = metrics.get("research_spec") if isinstance(metrics.get("research_spec"), dict) else {}
        aggregate = metrics.get("aggregate") if isinstance(metrics.get("aggregate"), dict) else {}
        folds = metrics.get("folds") if isinstance(metrics.get("folds"), list) else []
        gates = {
            "confirmation_stage_complete": len(folds) >= 3,
            "point_in_time_all_passed": bool(folds) and all(
                bool(item.get("point_in_time_passed")) for item in folds
            ),
            "mean_excess_positive": float(aggregate.get("mean_excess_return") or 0.0) > 0.0,
            "majority_windows_positive": float(
                aggregate.get("positive_excess_window_ratio") or 0.0
            ) >= (2.0 / 3.0),
            "drawdown_bounded": float(aggregate.get("worst_drawdown") or -1.0) >= -0.20,
        }
        if not all(gates.values()):
            rejected.append({
                "experiment_id": str(row["experiment_id"]),
                "gates": gates,
            })
            continue
        weights = json.loads(row.get("factor_weights_json") or "{}")
        frozen_bindings = (
            research_spec.get("factor_bindings")
            if isinstance(research_spec.get("factor_bindings"), dict) else {}
        )
        missing_frozen_bindings = sorted(
            key for key, weight in weights.items()
            if abs(float(weight)) > 1e-12
            and not int((frozen_bindings.get(key) or {}).get("definition_id") or 0)
        )
        if missing_frozen_bindings:
            rejected.append({
                "experiment_id": str(row["experiment_id"]),
                "gates": {
                    **gates,
                    "frozen_factor_bindings_complete": False,
                },
                "missing_factor_bindings": missing_frozen_bindings,
            })
            continue
        members = [
            {
                "definition_id": int(frozen_bindings[key]["definition_id"]),
                "factor_key": key,
                "weight": float(weight),
                "research_evidence": {
                    "snapshot_id": str(snapshot_id),
                    "experiment_id": str(row["experiment_id"]),
                    "fitness": float(row.get("fitness") or 0.0),
                    "aggregate": aggregate,
                },
            }
            for key, weight in sorted(weights.items())
            if abs(float(weight)) > 1e-12
        ]
        if len(members) < 3:
            rejected.append({
                "experiment_id": str(row["experiment_id"]),
                "gates": {**gates, "minimum_bound_factors": False},
            })
            continue
        identity = {
            "schema": FACTOR_COMBINATION_SCHEMA,
            "source": CONTINUOUS_RESEARCH_CONFIRMATION_SOURCE,
            "snapshot_id": str(snapshot_id),
            "strategy_hash": str(row["strategy_hash"]),
            "research_package_hash": str(research_spec.get("research_package_hash") or row["strategy_hash"]),
            "members": [
                {"definition_id": item["definition_id"], "factor_key": item["factor_key"], "weight": item["weight"]}
                for item in members
            ],
        }
        digest = _stable_hash(identity)
        combination_key = f"factor-combination-{digest[:16]}"
        combination = {
            "combination_key": combination_key,
            "version": f"v-{digest[:12]}",
            "status": CONTINUOUS_RESEARCH_COMBINATION_STATUS,
            "frozen_after": frozen_after,
            "metrics": {
                **aggregate,
                "fitness": float(row.get("fitness") or 0.0),
                "confirmation_fold_count": len(folds),
            },
            "members": members,
            "research_spec": research_spec,
        }
        now = _now()
        with _conn() as conn:
            existing = conn.execute(
                "SELECT combination_key FROM factor_combinations WHERE combination_key=?",
                (combination_key,),
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO factor_combinations(
                       combination_key,version,name,status,formula_hash,metrics_json,
                       lineage_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        combination_key,
                        combination["version"],
                        "连续研究复筛蒸馏组合",
                        CONTINUOUS_RESEARCH_COMBINATION_STATUS,
                        digest,
                        json.dumps(combination["metrics"], ensure_ascii=False),
                        json.dumps({
                            "source": CONTINUOUS_RESEARCH_CONFIRMATION_SOURCE,
                            "snapshot_id": str(snapshot_id),
                            "experiment_id": str(row["experiment_id"]),
                            "parent_experiment_id": row.get("parent_experiment_id"),
                            "generation": int(row.get("generation") or 0),
                            "gates": gates,
                            "identity": identity,
                            "research_spec": research_spec,
                        }, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                for member in members:
                    conn.execute(
                        """INSERT INTO factor_combination_members(
                           combination_key,factor_definition_id,factor_key,weight,
                           evidence_json,created_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            combination_key,
                            member["definition_id"],
                            member["factor_key"],
                            member["weight"],
                            json.dumps(member, ensure_ascii=False),
                            now,
                        ),
                    )
        accepted.append(combination)
    distillation = distill_factor_combinations_to_strategies({
        "combinations": accepted,
    })
    return {
        "ok": True,
        "schema_version": "alis_continuous_research_distillation_v1",
        "snapshot_id": str(snapshot_id),
        "accepted": accepted,
        "rejected": rejected,
        "distillation": distillation,
    }


def run_factor_strategy_evolution_cycle() -> dict[str, Any]:
    combinations = build_factor_combination_candidates()
    distillation = distill_factor_combinations_to_strategies(combinations)
    return {
        "ok": True,
        "schema_version": "alis_factor_strategy_joint_cycle_v1",
        "status": "completed",
        "combinations": combinations,
        "distillation": distillation,
    }


def factor_strategy_lineage_status() -> dict[str, Any]:
    _ensure_tables()
    with _conn() as conn:
        combinations = [dict(row) for row in conn.execute(
            "SELECT * FROM factor_combinations ORDER BY updated_at DESC"
        ).fetchall()]
        members = [dict(row) for row in conn.execute(
            "SELECT * FROM factor_combination_members ORDER BY combination_key,factor_key"
        ).fetchall()]
        links = [dict(row) for row in conn.execute(
            "SELECT * FROM factor_strategy_links ORDER BY created_at DESC"
        ).fetchall()]
    by_combination: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        try:
            member["evidence"] = json.loads(member.pop("evidence_json") or "{}")
        except Exception:
            member["evidence"] = {}
        by_combination.setdefault(str(member["combination_key"]), []).append(member)
    for combination in combinations:
        try:
            combination["metrics"] = json.loads(combination.pop("metrics_json") or "{}")
            combination["lineage"] = json.loads(combination.pop("lineage_json") or "{}")
        except Exception:
            combination["metrics"], combination["lineage"] = {}, {}
        combination["members"] = by_combination.get(str(combination["combination_key"]), [])
    for link in links:
        try:
            link["evidence"] = json.loads(link.pop("evidence_json") or "{}")
        except Exception:
            link["evidence"] = {}
    return {
        "ok": True,
        "schema_version": "alis_factor_strategy_lineage_v1",
        "combinations": combinations,
        "strategy_links": links,
    }

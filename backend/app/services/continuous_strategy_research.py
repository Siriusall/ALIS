from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import random
import threading
import time
from typing import Any, Callable

from app.db.store import _conn
from app.services.model_signal_fusion import MODEL_KEYS


RESEARCH_SCHEMA_VERSION = "alis_continuous_strategy_research_v10"
GENERATION_MODES = (
    "random_exploration",
    "prototype_mutation",
    "cross_cluster_crossover",
    "targeted_gap_fill",
)
MAX_PENDING_CANDIDATES = max(
    1, int(os.getenv("ALIS_V10_MAX_PENDING_CANDIDATES", "4"))
)
WAVE_MAX_RUNTIME_SECONDS = max(
    300, int(os.getenv("ALIS_V10_WAVE_MAX_RUNTIME_SECONDS", "14400"))
)
WAVE_MAX_RETRIES = max(0, int(os.getenv("ALIS_V10_WAVE_MAX_RETRIES", "2")))
EXPERT_MAX_AGE_DAYS = max(1, int(os.getenv("ALIS_V10_EXPERT_MAX_AGE_DAYS", "30")))
FACTOR_ANALYSIS_QUERY_BATCH_SIZE = max(
    1, min(int(os.getenv("ALIS_V10_FACTOR_ANALYSIS_QUERY_BATCH_SIZE", "10")), 20)
)
RESEARCH_LINES = (
    "candidate_generation",
    "cluster_distillation",
    "strategy_portfolio",
)

_RESEARCH_TABLES_READY = False
_RESEARCH_TABLES_INIT_LOCK = threading.Lock()
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def strategy_research_enabled() -> bool:
    """V10 is the default research engine; operators can explicitly disable it."""
    return str(os.getenv("ALIS_V10_RESEARCH_ENABLED", "1")).lower() in {
        "1", "true", "yes", "on",
    }


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat()


def _age_days_from_timestamp(value: Any, *, now: datetime | None = None) -> float:
    """Compute expert age without mixing legacy naive and aware timestamps."""
    current = now or datetime.now(_BUSINESS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BUSINESS_TZ)
    else:
        current = current.astimezone(_BUSINESS_TZ)
    try:
        updated = datetime.fromisoformat(str(value or ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=_BUSINESS_TZ)
        else:
            updated = updated.astimezone(_BUSINESS_TZ)
        return max(0.0, (current - updated).total_seconds() / 86400)
    except (TypeError, ValueError):
        return float(EXPERT_MAX_AGE_DAYS + 1)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def ensure_strategy_research_tables() -> None:
    global _RESEARCH_TABLES_READY
    if _RESEARCH_TABLES_READY:
        return
    with _RESEARCH_TABLES_INIT_LOCK:
        if _RESEARCH_TABLES_READY:
            return
        _ensure_strategy_research_tables_once()
        _RESEARCH_TABLES_READY = True


def _ensure_strategy_research_tables_once() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_research_events (
               event_id TEXT PRIMARY KEY,
               research_line TEXT NOT NULL,
               event_type TEXT NOT NULL,
               source_id TEXT,
               evidence_hash TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               consumed_at TEXT
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_research_events_line_status
               ON strategy_research_events(research_line,status,created_at)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_research_events_generation_source
               ON strategy_research_events(research_line,event_type,source_id,created_at)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_research_waves (
               wave_id TEXT PRIMARY KEY,
               research_snapshot_id TEXT NOT NULL,
               candidate_ids_json TEXT NOT NULL,
               expected_count INTEGER NOT NULL,
               completed_count INTEGER NOT NULL,
               failed_count INTEGER NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               distilled_at TEXT
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_research_waves_status
               ON strategy_research_waves(status,updated_at)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dynamic_market_environments (
               environment_id TEXT PRIMARY KEY,
               evidence_hash TEXT NOT NULL,
               environment_type TEXT NOT NULL,
               name TEXT NOT NULL,
               specification_json TEXT NOT NULL,
               source_contract_json TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_failure_memories (
               memory_id TEXT PRIMARY KEY,
               strategy_hash TEXT NOT NULL,
               environment_id TEXT,
               failure_type TEXT NOT NULL,
               attribution_json TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               model_training_request_json TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_prototypes (
               prototype_id TEXT PRIMARY KEY,
               cluster_id TEXT NOT NULL,
               research_snapshot_id TEXT NOT NULL,
               package_json TEXT NOT NULL,
               applicability_json TEXT NOT NULL,
               failure_boundaries_json TEXT NOT NULL,
               capacity_risk_json TEXT NOT NULL,
               maturity_status TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_portfolio_candidates (
               portfolio_id TEXT PRIMARY KEY,
               member_prototype_ids_json TEXT NOT NULL,
               routing_json TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_environment_backtests (
               candidate_id TEXT NOT NULL,
               environment_id TEXT NOT NULL,
               result_json TEXT NOT NULL,
               status TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(candidate_id,environment_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_environment_experts (
               environment_id TEXT NOT NULL,
               environment_version TEXT NOT NULL,
               prototype_id TEXT NOT NULL,
               rank_no INTEGER NOT NULL,
               role TEXT NOT NULL,
               fitness REAL NOT NULL,
               fitness_json TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               status TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(environment_id,environment_version,prototype_id)
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_environment_experts_rank
               ON strategy_environment_experts(environment_version,environment_id,rank_no)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_environment_expert_history (
               history_id TEXT PRIMARY KEY,
               environment_id TEXT NOT NULL,
               environment_version TEXT NOT NULL,
               prototype_id TEXT NOT NULL,
               rank_no INTEGER NOT NULL,
               role TEXT NOT NULL,
               fitness REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               observed_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_expert_history_environment
               ON strategy_environment_expert_history(
                 environment_id,observed_at,rank_no
               )"""
        )
        current_experts = conn.execute(
            """SELECT * FROM strategy_environment_experts e
               WHERE NOT EXISTS (
                 SELECT 1 FROM strategy_environment_expert_history h
                 WHERE h.environment_id=e.environment_id
                   AND h.environment_version=e.environment_version
                   AND h.prototype_id=e.prototype_id
                   AND h.rank_no=e.rank_no
               )"""
        ).fetchall()
        for raw_expert in current_experts:
            expert = dict(raw_expert)
            observed_at = str(expert.get("updated_at") or _now())
            identity = {
                "environment_id": expert["environment_id"],
                "environment_version": expert["environment_version"],
                "prototype_id": expert["prototype_id"],
                "rank": int(expert["rank_no"]),
                "observed_at": observed_at,
            }
            conn.execute(
                """INSERT INTO strategy_environment_expert_history(
                   history_id,environment_id,environment_version,prototype_id,
                   rank_no,role,fitness,evidence_json,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(history_id) DO NOTHING""",
                (
                    f"expert-history-{_hash(identity)[:24]}",
                    expert["environment_id"], expert["environment_version"],
                    expert["prototype_id"], expert["rank_no"], expert["role"],
                    expert["fitness"], expert["evidence_json"], observed_at,
                ),
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_research_wave_contracts (
               wave_id TEXT PRIMARY KEY,
               freeze_hash TEXT NOT NULL,
               freeze_json TEXT NOT NULL,
               quota_json TEXT NOT NULL,
               checkpoint_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_environment_replay_cache (
               cache_key TEXT PRIMARY KEY,
               candidate_hash TEXT NOT NULL,
               environment_id TEXT NOT NULL,
               environment_version TEXT NOT NULL,
               snapshot_id TEXT NOT NULL,
               contract_hash TEXT NOT NULL,
               result_json TEXT NOT NULL,
               hit_count INTEGER NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_replay_cache_contract
               ON strategy_environment_replay_cache(
                 candidate_hash,environment_version,snapshot_id,contract_hash
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_factor_contributions (
               candidate_id TEXT NOT NULL,
               environment_id TEXT NOT NULL,
               factor_key TEXT NOT NULL,
               signed_contribution REAL NOT NULL,
               absolute_contribution REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(candidate_id,environment_id,factor_key)
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_factor_contribution_factor
               ON strategy_factor_contributions(factor_key,updated_at)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_strategy_factor_contribution_recent
               ON strategy_factor_contributions(updated_at,absolute_contribution)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_factor_interactions (
               factor_left TEXT NOT NULL,
               factor_right TEXT NOT NULL,
               observation_count INTEGER NOT NULL,
               mean_joint_fitness REAL NOT NULL,
               complementarity REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(factor_left,factor_right)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_factor_learning_runs (
               candidate_id TEXT PRIMARY KEY,
               candidate_hash TEXT NOT NULL,
               learned_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_research_runtime_metrics (
               metric_id TEXT PRIMARY KEY,
               wave_id TEXT,
               candidate_id TEXT,
               metric_type TEXT NOT NULL,
               value REAL NOT NULL,
               dimensions_json TEXT NOT NULL,
               observed_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS strategy_research_metric_rollups (
               metric_date TEXT NOT NULL,
               metric_type TEXT NOT NULL,
               observation_count INTEGER NOT NULL,
               average_value REAL NOT NULL,
               minimum_value REAL NOT NULL,
               maximum_value REAL NOT NULL,
               archived_at TEXT NOT NULL,
               PRIMARY KEY(metric_date,metric_type)
            )"""
        )
        missing_waves = conn.execute(
            """SELECT w.* FROM strategy_research_waves w
               LEFT JOIN strategy_research_wave_contracts c ON c.wave_id=w.wave_id
               WHERE c.wave_id IS NULL"""
        ).fetchall()
        for raw_wave in missing_waves:
            wave = dict(raw_wave)
            candidate_ids = list(json.loads(wave.get("candidate_ids_json") or "[]"))
            environment_ids: set[str] = set()
            candidate_hashes: list[str] = []
            for candidate_id in candidate_ids:
                event = conn.execute(
                    """SELECT payload_json FROM strategy_research_events
                       WHERE research_line='candidate_generation'
                         AND event_type='candidate_strategy_generated'
                         AND source_id=? ORDER BY created_at DESC LIMIT 1""",
                    (str(candidate_id),),
                ).fetchone()
                payload = json.loads(dict(event).get("payload_json") or "{}") if event else {}
                candidate_hashes.append(str(payload.get("candidate_hash") or ""))
                environment_ids.update(str(item) for item in (payload.get("environment_ids") or []))
            freeze = {
                "schema_version": "alis_strategy_research_freeze_v1",
                "research_schema_version": RESEARCH_SCHEMA_VERSION,
                "research_snapshot_id": str(wave.get("research_snapshot_id") or ""),
                "candidate_hashes": sorted(candidate_hashes),
                "environment_ids": sorted(environment_ids),
                "cost_contract": "strategy_replay_frozen_cost_contract",
                "execution_contract": "a_share_t_plus_1_replay",
                "backfilled_from_persistent_wave": True,
            }
            quota = {
                "schema_version": "alis_strategy_research_wave_quota_v1",
                "maximum_candidates": int(wave.get("expected_count") or len(candidate_ids)),
                "maximum_runtime_seconds": WAVE_MAX_RUNTIME_SECONDS,
                "maximum_retries_per_candidate": WAVE_MAX_RETRIES,
                "tiered_budget": {
                    "screening": "all_candidates_all_registered_environments",
                    "full_replay": "historical_windows_first",
                    "promotion": "formal_chain_only",
                },
            }
            now = _now()
            conn.execute(
                """INSERT INTO strategy_research_wave_contracts(
                   wave_id,freeze_hash,freeze_json,quota_json,checkpoint_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(wave_id) DO NOTHING""",
                (
                    str(wave["wave_id"]), _hash(freeze), _json(freeze), _json(quota),
                    _json({"completed_environment_ids": {}, "retry_counts": {}}),
                    str(wave.get("created_at") or now), now,
                ),
            )


def dynamic_factor_complexity(
    *,
    universe_size: int,
    seed_material: dict[str, Any],
    parent_factor_count: int | None = None,
    marginal_contribution: float | None = None,
    redundancy_ratio: float = 0.0,
    point_in_time_coverage: float = 1.0,
) -> dict[str, Any]:
    """Choose candidate size from evidence, with only a resource safety ceiling.

    The ceiling prevents pathological packages; it is not a preferred factor
    count.  Mutation expands or contracts around the parent according to the
    latest marginal contribution and redundancy evidence.
    """
    available = max(1, int(universe_size))
    safety_ceiling = max(1, min(available, int(math.ceil(math.sqrt(available) * 3))))
    if parent_factor_count is not None:
        target = max(1, min(int(parent_factor_count), safety_ceiling))
        if marginal_contribution is not None and float(marginal_contribution) > 0.001:
            target += max(1, int(math.ceil(math.sqrt(target))))
        elif marginal_contribution is not None and float(marginal_contribution) <= 0:
            target -= max(1, int(math.ceil(math.sqrt(target) / 2)))
        target -= int(round(max(0.0, min(float(redundancy_ratio), 1.0)) * target * 0.5))
    else:
        seed = int(_hash({"schema": RESEARCH_SCHEMA_VERSION, **seed_material})[:16], 16)
        rng = random.Random(seed)
        target = 1 + int((rng.random() ** 1.6) * safety_ceiling)
    coverage_cap = max(1, int(math.floor(safety_ceiling * max(0.1, min(float(point_in_time_coverage), 1.0)))))
    target = max(1, min(target, coverage_cap, safety_ceiling))
    return {
        "schema_version": "alis_dynamic_factor_complexity_v1",
        "target_factor_count": target,
        "resource_safety_ceiling": safety_ceiling,
        "decision_inputs": {
            "universe_size": available,
            "parent_factor_count": parent_factor_count,
            "marginal_contribution": marginal_contribution,
            "redundancy_ratio": round(float(redundancy_ratio), 8),
            "point_in_time_coverage": round(float(point_in_time_coverage), 8),
        },
        "fixed_factor_count": False,
    }


def _normalized_weights(raw: dict[str, float]) -> dict[str, float]:
    cleaned = {str(key): float(value) for key, value in raw.items() if abs(float(value)) > 1e-12}
    total = sum(abs(value) for value in cleaned.values())
    return {
        key: round(value / total, 10)
        for key, value in sorted(cleaned.items())
    } if total else {}


def _factor_lookup(factors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["factor_key"]): item for item in factors}


def analyze_factor_pool(factors: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply point-in-time quality and structural redundancy checks before generation."""
    if not factors:
        return {"eligible_factors": [], "quality": [], "redundancy_groups": []}
    ids = [int(item.get("id") or 0) for item in factors if int(item.get("id") or 0) > 0]
    values_by_id: dict[int, dict[str, float]] = {}
    latest_date = ""
    if ids:
        placeholders = ",".join("?" for _ in ids)
        with _conn() as conn:
            latest = conn.execute(
                f"SELECT MAX(trade_date) AS trade_date FROM factor_values WHERE factor_definition_id IN ({placeholders})",
                tuple(ids),
            ).fetchone()
            latest_date = str(dict(latest).get("trade_date") or "") if latest else ""
            rows = []
            if latest_date:
                # Keep each latest-day read below the statement/transfer
                # budget when the factor pool spans the full registry.
                for start in range(0, len(ids), FACTOR_ANALYSIS_QUERY_BATCH_SIZE):
                    chunk = ids[start:start + FACTOR_ANALYSIS_QUERY_BATCH_SIZE]
                    chunk_placeholders = ",".join("?" for _ in chunk)
                    rows.extend(conn.execute(
                        f"""SELECT factor_definition_id,ts_code,standardized_value
                            FROM factor_values WHERE trade_date=?
                              AND factor_definition_id IN ({chunk_placeholders})""",
                        (latest_date, *chunk),
                    ).fetchall())
        for raw in rows:
            row = dict(raw)
            if row.get("standardized_value") is None:
                continue
            values_by_id.setdefault(int(row["factor_definition_id"]), {})[
                str(row["ts_code"])
            ] = float(row["standardized_value"])
    maximum_rows = max((len(values) for values in values_by_id.values()), default=0)
    quality: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for factor in factors:
        definition_id = int(factor.get("id") or 0)
        coverage = len(values_by_id.get(definition_id, {})) / maximum_rows if maximum_rows else 0.0
        gates = {
            "point_in_time_rule": bool(str(factor.get("available_time_rule") or "")),
            "missing_policy": bool(str(factor.get("missing_policy") or "")),
            "leakage_passed": str(factor.get("leakage_status") or "") == "passed",
            "materialized_coverage": coverage >= 0.50,
            "tradable_alpha": str(factor.get("category") or "") != "交易约束",
        }
        item = {
            "factor_key": str(factor.get("factor_key") or ""),
            "definition_id": definition_id,
            "coverage": round(coverage, 8),
            "gates": gates,
            "passed": all(gates.values()),
        }
        quality.append(item)
        if item["passed"]:
            passed.append(factor)
    redundancy_groups: list[dict[str, Any]] = []
    redundant: set[str] = set()
    for left_index, left in enumerate(passed):
        left_key = str(left["factor_key"])
        if left_key in redundant:
            continue
        group = [left_key]
        left_values = values_by_id.get(int(left.get("id") or 0), {})
        for right in passed[left_index + 1:]:
            right_key = str(right["factor_key"])
            if right_key in redundant:
                continue
            right_values = values_by_id.get(int(right.get("id") or 0), {})
            shared = sorted(set(left_values) & set(right_values))
            if len(shared) < 30:
                continue
            left_series = {key: left_values[key] for key in shared}
            right_series = {key: right_values[key] for key in shared}
            correlation = _correlation(left_series, right_series)
            if correlation is not None and abs(correlation) >= 0.90:
                group.append(right_key)
                redundant.add(right_key)
        if len(group) > 1:
            redundancy_groups.append({
                "representative": left_key,
                "members": group,
                "rule": "absolute_cross_section_correlation_gte_0_90",
            })
    eligible = [item for item in passed if str(item["factor_key"]) not in redundant]
    return {
        "schema_version": "alis_factor_quality_structure_analysis_v1",
        "latest_materialized_date": latest_date,
        "quality": quality,
        "redundancy_groups": redundancy_groups,
        "eligible_factors": eligible,
        "input_count": len(factors),
        "eligible_count": len(eligible),
    }


def factor_combination_guidance(limit: int = 200) -> dict[str, Any]:
    """Build a bounded sampling guide from persisted marginal and pair evidence."""
    ensure_strategy_research_tables()
    with _conn() as conn:
        contribution_rows = [dict(row) for row in conn.execute(
            """SELECT factor_key,COUNT(*) AS observations,
                      AVG(signed_contribution) AS mean_contribution,
                      AVG(absolute_contribution) AS mean_absolute_contribution
               FROM strategy_factor_contributions
               GROUP BY factor_key
               ORDER BY AVG(signed_contribution) DESC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()]
        interaction_rows = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_factor_interactions
               ORDER BY complementarity DESC,observation_count DESC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()]
    scores = {
        str(row["factor_key"]): float(row.get("mean_contribution") or 0.0)
        for row in contribution_rows
    }
    if scores:
        low = min(scores.values())
        shifted = {key: max(0.01, value - low + 0.01) for key, value in scores.items()}
        total = sum(shifted.values())
        probabilities = {key: round(value / total, 8) for key, value in shifted.items()}
    else:
        probabilities = {}
    return {
        "schema_version": "alis_factor_combination_guide_v1",
        "policy": {
            "guided_share": 0.70,
            "pure_random_share": 0.30,
            "direct_production_write": False,
            "minimum_evidence_for_exclusion": 20,
        },
        "factor_scores": contribution_rows,
        "sampling_probabilities": probabilities,
        "interactions": interaction_rows,
        "positive_pairs": [
            row for row in interaction_rows if float(row.get("complementarity") or 0.0) > 0
        ][:50],
        "conflicting_pairs": [
            row for row in reversed(interaction_rows)
            if float(row.get("complementarity") or 0.0) < 0
        ][:50],
    }


def _category_balanced_selection(
    factors: list[dict[str, Any]],
    *,
    target: int,
    rng: random.Random,
    preferred_categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for item in factors:
        categories.setdefault(str(item.get("category") or "unknown"), []).append(item)
    ordered = sorted(categories, key=lambda key: (key not in (preferred_categories or set()), key))
    rng.shuffle(ordered)
    if preferred_categories:
        ordered.sort(key=lambda key: key not in preferred_categories)
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    while ordered and len(selected) < target:
        progressed = False
        for category in ordered:
            options = [
                item for item in sorted(categories[category], key=lambda value: str(value["factor_key"]))
                if str(item["factor_key"]) not in used
            ]
            if not options:
                continue
            item = options[rng.randrange(len(options))]
            selected.append(item)
            used.add(str(item["factor_key"]))
            progressed = True
            if len(selected) >= target:
                break
        if not progressed:
            break
    return selected


def _guided_factor_selection(
    factors: list[dict[str, Any]],
    *,
    target: int,
    rng: random.Random,
    probabilities: dict[str, float],
) -> list[dict[str, Any]]:
    remaining = list(factors)
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < target:
        weights = [
            max(0.001, float(probabilities.get(str(item.get("factor_key") or ""), 0.001)))
            for item in remaining
        ]
        total = sum(weights)
        cursor = rng.random() * total
        chosen_index = len(remaining) - 1
        for index, weight in enumerate(weights):
            cursor -= weight
            if cursor <= 0:
                chosen_index = index
                break
        selected.append(remaining.pop(chosen_index))
    return selected


def generate_candidate_batch(
    *,
    snapshot_id: str,
    factors: list[dict[str, Any]],
    count: int,
    prototypes: list[dict[str, Any]] | None = None,
    failure_memories: list[dict[str, Any]] | None = None,
    generation_nonce: int = 0,
) -> list[dict[str, Any]]:
    """Generate all four candidate modes without a fixed per-mode quota."""
    if not factors:
        return []
    prototype_rows = prototypes or []
    memories = failure_memories or []
    lookup = _factor_lookup(factors)
    guidance = factor_combination_guidance()
    sampling_probabilities = dict(guidance.get("sampling_probabilities") or {})
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(max(1, int(count))):
        mode = GENERATION_MODES[index % len(GENERATION_MODES)]
        generation_index = max(0, int(generation_nonce)) + index
        seed_hex = _hash({
            "schema": RESEARCH_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "candidate_index": generation_index,
            "mode": mode,
        })[:16]
        rng = random.Random(int(seed_hex, 16))
        parent = prototype_rows[index % len(prototype_rows)] if prototype_rows else None
        parent_weights = dict((parent or {}).get("factor_weights") or {})
        marginal = (parent or {}).get("marginal_contribution")
        redundancy = float((parent or {}).get("redundancy_ratio") or 0.0)
        complexity = dynamic_factor_complexity(
            universe_size=len(factors),
            seed_material={
                "snapshot_id": snapshot_id,
                "index": generation_index,
                "mode": mode,
            },
            parent_factor_count=len(parent_weights) if parent_weights else None,
            marginal_contribution=float(marginal) if marginal is not None else None,
            redundancy_ratio=redundancy,
        )
        target = int(complexity["target_factor_count"])
        source_prototypes: list[str] = []
        preferred_categories: set[str] = set()
        if mode == "targeted_gap_fill" and memories:
            memory = memories[index % len(memories)]
            attribution = memory.get("attribution") if isinstance(memory.get("attribution"), dict) else memory
            preferred_categories = {
                str(item) for item in (attribution.get("preferred_factor_categories") or []) if str(item)
            }
        if mode == "prototype_mutation" and parent_weights:
            weights = dict(parent_weights)
            source_prototypes.append(str((parent or {}).get("prototype_id") or ""))
            available = [key for key in sorted(lookup) if key not in weights]
            if marginal is not None and float(marginal) <= 0 and len(weights) > 1:
                for key in sorted(weights, key=lambda item: (abs(float(weights[item])), item))[:max(1, len(weights) - target)]:
                    weights.pop(key, None)
            while available and len(weights) < target:
                key = available.pop(rng.randrange(len(available)))
                weights[key] = float(lookup[key].get("direction") or 1) * rng.uniform(0.5, 1.5)
        elif mode == "cross_cluster_crossover" and len(prototype_rows) >= 2:
            left = prototype_rows[index % len(prototype_rows)]
            right = prototype_rows[(index + 1) % len(prototype_rows)]
            source_prototypes.extend([
                str(left.get("prototype_id") or ""), str(right.get("prototype_id") or ""),
            ])
            merged = dict(left.get("factor_weights") or {})
            for key, value in (right.get("factor_weights") or {}).items():
                merged[str(key)] = (float(merged.get(str(key), 0.0)) + float(value)) / 2.0
            ranked = sorted(merged, key=lambda key: (-abs(float(merged[key])), key))[:target]
            weights = {key: merged[key] for key in ranked}
        else:
            selected = (
                _category_balanced_selection(
                    factors,
                    target=target,
                    rng=rng,
                    preferred_categories=preferred_categories,
                )
                if mode == "random_exploration" or not sampling_probabilities
                else _guided_factor_selection(
                    factors,
                    target=target,
                    rng=rng,
                    probabilities=sampling_probabilities,
                )
            )
            weights = {
                str(item["factor_key"]): float(item.get("direction") or 1) * rng.uniform(0.5, 1.5)
                for item in selected
            }
        weights = _normalized_weights(weights)
        if not weights:
            continue
        holding_period = (3, 5, 10, 20, 40, 60)[rng.randrange(6)]
        identity = {
            "snapshot_id": snapshot_id,
            "mode": mode,
            "factor_weights": weights,
            "holding_period_sessions": holding_period,
        }
        candidate_hash = _hash(identity)
        if candidate_hash in seen:
            continue
        seen.add(candidate_hash)
        candidates.append({
            "candidate_id": f"strategy-research-candidate-{candidate_hash[:20]}",
            "candidate_hash": candidate_hash,
            "snapshot_id": snapshot_id,
            "generation_mode": mode,
            "factor_weights": weights,
            "factor_set_hash": _hash(sorted(weights)),
            "factor_count": len(weights),
            "factor_complexity": complexity,
            "holding_period_sessions": holding_period,
            "source_prototype_ids": [item for item in source_prototypes if item],
            "exploration_seed": seed_hex,
            "guidance_contract": {
                "schema_version": "alis_factor_combination_guide_v1",
                "applied": mode != "random_exploration" and bool(sampling_probabilities),
                "pure_random": mode == "random_exploration",
            },
        })
    return candidates


def enqueue_research_event(
    *,
    research_line: str,
    event_type: str,
    payload: dict[str, Any],
    source_id: str | None = None,
) -> dict[str, Any]:
    if research_line not in RESEARCH_LINES:
        raise ValueError(f"unsupported research line: {research_line}")
    ensure_strategy_research_tables()
    evidence_hash = _hash(payload)
    event_id = f"strategy-research-event-{_hash({'line': research_line, 'type': event_type, 'payload': payload})[:20]}"
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_research_events(
               event_id,research_line,event_type,source_id,evidence_hash,payload_json,
               status,created_at,consumed_at
               ) VALUES(?,?,?,?,?,?,?, ?,NULL)
               ON CONFLICT(event_id) DO NOTHING""",
            (
                event_id, research_line, event_type, source_id, evidence_hash,
                _json(payload), "queued", now,
            ),
        )
    return {
        "event_id": event_id,
        "research_line": research_line,
        "event_type": event_type,
        "evidence_hash": evidence_hash,
        "status": "queued",
    }


def _candidate_id_from_event(row: dict[str, Any]) -> str:
    payload = json.loads(row.get("payload_json") or "{}")
    return str(payload.get("candidate_id") or "")


def _candidate_event(candidate_id: str) -> dict[str, Any] | None:
    ensure_strategy_research_tables()
    with _conn() as conn:
        exact = conn.execute(
            """SELECT * FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'
                 AND source_id=?
               ORDER BY created_at DESC,event_id DESC LIMIT 1""",
            (str(candidate_id),),
        ).fetchone()
        if exact:
            return dict(exact)
        # Legacy rows may predate source_id.  Search only that legacy subset;
        # unrelated NULL history must never push the target beyond a cap.
        rows = conn.execute(
            """SELECT * FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'
                 AND source_id IS NULL
               ORDER BY created_at DESC,event_id DESC"""
        ).fetchall()
    for raw in rows:
        row = dict(raw)
        if str(row.get("source_id") or "") == str(candidate_id) or _candidate_id_from_event(row) == str(candidate_id):
            return row
    return None


def _persist_research_wave(
    *,
    snapshot_id: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    candidate_ids = sorted(str(item["candidate_id"]) for item in candidates)
    wave_id = f"strategy-research-wave-{_hash({'snapshot_id': snapshot_id, 'candidate_ids': candidate_ids})[:20]}"
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_research_waves(
               wave_id,research_snapshot_id,candidate_ids_json,expected_count,
               completed_count,failed_count,status,created_at,updated_at,distilled_at
               ) VALUES(?,?,?,?,0,0,'queued',?,?,NULL)
               ON CONFLICT(wave_id) DO NOTHING""",
            (
                wave_id, str(snapshot_id), _json(candidate_ids), len(candidate_ids),
                now, now,
            ),
        )
        freeze = {
            "schema_version": "alis_strategy_research_freeze_v1",
            "research_schema_version": RESEARCH_SCHEMA_VERSION,
            "research_snapshot_id": str(snapshot_id),
            "candidate_hashes": sorted(str(item.get("candidate_hash") or "") for item in candidates),
            "environment_ids": sorted({
                str(environment_id)
                for item in candidates
                for environment_id in (item.get("environment_ids") or [])
            }),
            "cost_contract": "strategy_replay_frozen_cost_contract",
            "execution_contract": "a_share_t_plus_1_replay",
        }
        quota = {
            "schema_version": "alis_strategy_research_wave_quota_v1",
            "maximum_candidates": len(candidate_ids),
            "maximum_runtime_seconds": WAVE_MAX_RUNTIME_SECONDS,
            "maximum_retries_per_candidate": WAVE_MAX_RETRIES,
            "tiered_budget": {
                "screening": "all_candidates_all_registered_environments",
                "full_replay": "historical_windows_first",
                "promotion": "formal_chain_only",
            },
        }
        conn.execute(
            """INSERT INTO strategy_research_wave_contracts(
               wave_id,freeze_hash,freeze_json,quota_json,checkpoint_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(wave_id) DO NOTHING""",
            (
                wave_id, _hash(freeze), _json(freeze), _json(quota),
                _json({"completed_environment_ids": {}, "retry_counts": {}}),
                now, now,
            ),
        )
    for item in candidates:
        item["research_wave_id"] = wave_id
    return refresh_research_wave(wave_id)


def _wave_contract(wave_id: str) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_research_wave_contracts WHERE wave_id=?",
            (str(wave_id),),
        ).fetchone()
    if not row:
        return {}
    item = dict(row)
    item["freeze"] = json.loads(item.pop("freeze_json") or "{}")
    item["quota"] = json.loads(item.pop("quota_json") or "{}")
    item["checkpoint"] = json.loads(item.pop("checkpoint_json") or "{}")
    return item


def _update_wave_checkpoint(
    wave_id: str,
    candidate_id: str,
    environment_ids: list[str],
) -> None:
    if not wave_id:
        return
    contract = _wave_contract(wave_id)
    if not contract:
        return
    checkpoint = dict(contract.get("checkpoint") or {})
    completed = dict(checkpoint.get("completed_environment_ids") or {})
    completed[str(candidate_id)] = sorted({str(item) for item in environment_ids})
    checkpoint["completed_environment_ids"] = completed
    checkpoint["last_candidate_id"] = str(candidate_id)
    checkpoint["updated_at"] = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_wave_contracts
               SET checkpoint_json=?,updated_at=? WHERE wave_id=?""",
            (_json(checkpoint), _now(), str(wave_id)),
        )


def _record_runtime_metric(
    *,
    metric_type: str,
    value: float,
    wave_id: str = "",
    candidate_id: str = "",
    dimensions: dict[str, Any] | None = None,
) -> None:
    observed_at = _now()
    payload = {
        "metric_type": str(metric_type),
        "wave_id": str(wave_id),
        "candidate_id": str(candidate_id),
        "dimensions": dimensions or {},
        "observed_at": observed_at,
    }
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_research_runtime_metrics(
               metric_id,wave_id,candidate_id,metric_type,value,dimensions_json,observed_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                f"research-metric-{_hash(payload)[:24]}", wave_id or None,
                candidate_id or None, str(metric_type), float(value),
                _json(dimensions or {}), observed_at,
            ),
        )


def compact_research_runtime_metrics(retention_days: int = 30) -> dict[str, Any]:
    """Roll old high-cardinality runtime samples into daily recoverable summaries."""
    ensure_strategy_research_tables()
    cutoff = (datetime.now().astimezone() - timedelta(
        days=max(7, int(retention_days))
    )).isoformat()
    archived_at = _now()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT SUBSTR(observed_at,1,10) AS metric_date,metric_type,
                      COUNT(*) AS observation_count,AVG(value) AS average_value,
                      MIN(value) AS minimum_value,MAX(value) AS maximum_value
               FROM strategy_research_runtime_metrics
               WHERE observed_at<?
               GROUP BY SUBSTR(observed_at,1,10),metric_type""",
            (cutoff,),
        ).fetchall()]
        for row in rows:
            conn.execute(
                """INSERT INTO strategy_research_metric_rollups(
                   metric_date,metric_type,observation_count,average_value,
                   minimum_value,maximum_value,archived_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(metric_date,metric_type) DO UPDATE SET
                   observation_count=excluded.observation_count,
                   average_value=excluded.average_value,
                   minimum_value=excluded.minimum_value,
                   maximum_value=excluded.maximum_value,
                   archived_at=excluded.archived_at""",
                (
                    row["metric_date"], row["metric_type"], row["observation_count"],
                    row["average_value"], row["minimum_value"], row["maximum_value"],
                    archived_at,
                ),
            )
        deleted = conn.execute(
            "DELETE FROM strategy_research_runtime_metrics WHERE observed_at<?",
            (cutoff,),
        )
    return {
        "ok": True,
        "schema_version": "alis_research_metric_compaction_v1",
        "rollup_count": len(rows),
        "deleted_detail_count": int(deleted.rowcount or 0),
        "retention_days": max(7, int(retention_days)),
        "recoverable_from_daily_rollup": True,
    }


def refresh_research_wave(wave_id: str) -> dict[str, Any] | None:
    ensure_strategy_research_tables()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_research_waves WHERE wave_id=?",
            (str(wave_id),),
        ).fetchone()
    if not row:
        return None
    wave = dict(row)
    candidate_ids = list(json.loads(wave.get("candidate_ids_json") or "[]"))
    statuses: dict[str, str] = {}
    for candidate_id in candidate_ids:
        event = _candidate_event(str(candidate_id))
        statuses[str(candidate_id)] = str((event or {}).get("status") or "missing")
    completed_count = sum(value == "completed" for value in statuses.values())
    failed_count = sum(value in {"failed", "cancelled"} for value in statuses.values())
    expected_count = int(wave.get("expected_count") or len(candidate_ids))
    terminal_count = completed_count + failed_count
    status = (
        "completed" if expected_count > 0 and terminal_count >= expected_count
        else "running" if any(value == "running" for value in statuses.values()) or completed_count > 0
        else "queued"
    )
    if str(wave.get("status") or "") == "distilled":
        status = "distilled"
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_waves
               SET completed_count=?,failed_count=?,status=?,updated_at=?
               WHERE wave_id=?""",
            (completed_count, failed_count, status, now, str(wave_id)),
        )
        updated = conn.execute(
            "SELECT * FROM strategy_research_waves WHERE wave_id=?",
            (str(wave_id),),
        ).fetchone()
    result = dict(updated) if updated else wave
    result["candidate_ids"] = candidate_ids
    result["candidate_statuses"] = statuses
    result["terminal_count"] = terminal_count
    result["ready_for_distillation"] = status == "completed"
    result.pop("candidate_ids_json", None)
    return result


def mark_research_wave_distilled(wave_id: str) -> dict[str, Any] | None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_waves
               SET status='distilled',distilled_at=?,updated_at=?
               WHERE wave_id=? AND status IN ('completed','distilled')""",
            (now, now, str(wave_id)),
        )
    return refresh_research_wave(str(wave_id))


def cancel_candidate_research_event(candidate_id: str) -> dict[str, Any]:
    event = _candidate_event(str(candidate_id))
    if not event:
        return {
            "ok": True,
            "status": "candidate_event_not_found",
            "candidate_id": str(candidate_id),
        }
    payload = json.loads(event.get("payload_json") or "{}")
    wave_id = str(payload.get("research_wave_id") or "")
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_events
               SET status='cancelled',consumed_at=?
               WHERE event_id=? AND status IN ('queued','running')""",
            (_now(), event["event_id"]),
        )
    wave = refresh_research_wave(wave_id) if wave_id else None
    return {
        "ok": True,
        "status": "cancelled",
        "candidate_id": str(candidate_id),
        "research_wave_id": wave_id or None,
        "research_wave": wave,
        "wave_ready_for_distillation": bool(
            (wave or {}).get("ready_for_distillation")
        ),
    }


def _load_environment(environment_id: str) -> dict[str, Any] | None:
    ensure_strategy_research_tables()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM dynamic_market_environments WHERE environment_id=?",
            (str(environment_id),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["specification"] = json.loads(item.pop("specification_json") or "{}")
    item["source_contract"] = json.loads(item.pop("source_contract_json") or "{}")
    return item


def _stress_result(replay: dict[str, Any], environment: dict[str, Any]) -> dict[str, Any]:
    name = str(environment.get("name") or "")
    spec = environment.get("specification") or {}
    metrics = replay.get("metrics") if isinstance(replay.get("metrics"), dict) else {}
    summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
    initial_cash = float(replay.get("initial_cash") or 0.0)
    if name == "transaction_cost_jump" and initial_cash > 0:
        fee_multiplier = max(1.0, float(spec.get("fee_multiplier") or 1.0))
        slippage_multiplier = max(1.0, float(spec.get("slippage_multiplier") or 1.0))
        observed_fees = float(summary.get("total_fees") or 0.0)
        observed_slippage_proxy = sum(
            float(item.get("gross_amount") or 0.0) * 0.0005
            for item in (replay.get("trades") or [])
        )
        extra_cost = observed_fees * (fee_multiplier - 1.0) + observed_slippage_proxy * (slippage_multiplier - 1.0)
        stressed_return = float(metrics.get("total_return") or 0.0) - extra_cost / initial_cash
        return {
            "status": "completed",
            "method": "observed_trade_cost_repricing_v1",
            "total_return": round(stressed_return, 8),
            "excess_return": round(stressed_return - float(metrics.get("benchmark_return") or 0.0), 8),
            "extra_cost": round(extra_cost, 2),
        }
    if name == "rapid_regime_switch":
        transitions = ((metrics.get("market_state_matrix") or {}).get("transitions") or {})
        return {
            "status": "completed" if int(transitions.get("count") or 0) > 0 else "insufficient_transition_evidence",
            "method": "point_in_time_transition_replay_v1",
            "transition_count": int(transitions.get("count") or 0),
            "transition_excess_return": float(transitions.get("excess_return") or 0.0),
            "post_transition": transitions.get("post_3_sessions") or {},
        }
    if name == "liquidity_contraction":
        blocked = replay.get("blocked_events") or []
        traded_amount = sum(float(item.get("gross_amount") or 0.0) for item in (replay.get("trades") or []))
        return {
            "status": "completed",
            "method": "observed_capacity_contraction_v1",
            "volume_capacity_multiplier": float(spec.get("volume_capacity_multiplier") or 1.0),
            "observed_traded_amount": round(traded_amount, 2),
            "blocked_trade_count": len(blocked),
            "capacity_recheck_required": True,
        }
    return {
        "status": "blocked_by_missing_calibration",
        "method": "no_synthetic_return_fabrication",
        "blocking_reason": f"{name}_requires_dedicated_counterfactual_adapter",
    }


def _compact_replay_evidence(replay: dict[str, Any]) -> dict[str, Any]:
    equity = replay.get("equity_series") if isinstance(replay.get("equity_series"), list) else []
    daily_returns: dict[str, float] = {}
    for index in range(1, len(equity)):
        previous = float(equity[index - 1].get("total_value") or 0.0)
        current = float(equity[index].get("total_value") or 0.0)
        if previous > 0:
            daily_returns[str(equity[index].get("date") or "")] = round(current / previous - 1.0, 10)
    holding_sets = {
        str(item.get("signal_date") or ""): sorted(
            str(symbol) for symbol, weight in (item.get("target_weights") or {}).items()
            if float(weight or 0.0) > 0
        )
        for item in (replay.get("decisions") or [])
    }
    return {
        "daily_returns": daily_returns,
        "holding_sets": holding_sets,
        "turnover_rate": float((replay.get("metrics") or {}).get("turnover_rate") or 0.0),
        "blocked_trade_count": len(replay.get("blocked_events") or []),
    }


def _observed_slice_result(replay: dict[str, Any], environment: dict[str, Any]) -> dict[str, Any]:
    spec = environment.get("specification") or {}
    dimension = str(spec.get("dimension") or "")
    target = str(spec.get("value") or "").lower()
    equity_rows = replay.get("equity_series") or []
    benchmark_rows = replay.get("benchmark_series") or []
    equity = {str(item.get("date") or ""): float(item.get("total_value") or 0.0) for item in equity_rows}
    benchmark = {str(item.get("date") or ""): float(item.get("total_value") or 0.0) for item in benchmark_rows}
    dates = sorted(set(equity) & set(benchmark))
    benchmark_daily = {
        dates[index]: benchmark[dates[index]] / benchmark[dates[index - 1]] - 1.0
        for index in range(1, len(dates)) if benchmark[dates[index - 1]] > 0
    }
    volatility_values = sorted(abs(value) for value in benchmark_daily.values())
    volatility_median = volatility_values[len(volatility_values) // 2] if volatility_values else 0.0
    decision_profiles = {
        str(item.get("signal_date") or ""): item.get("market_profile") or {}
        for item in (replay.get("decisions") or [])
    }
    intervals: list[tuple[float, float]] = []
    for index in range(1, len(dates)):
        prior_date, current_date = dates[index - 1], dates[index]
        if equity[prior_date] <= 0 or benchmark[prior_date] <= 0:
            continue
        label = ""
        if dimension == "trend":
            start_index = max(0, index - 5)
            start_value = benchmark[dates[start_index]]
            trailing = benchmark[prior_date] / start_value - 1.0 if start_value > 0 else 0.0
            label = "up" if trailing > 0.01 else "down" if trailing < -0.01 else "sideways"
        elif dimension == "volatility":
            label = "high" if abs(float(benchmark_daily.get(prior_date) or 0.0)) >= volatility_median else "low"
        else:
            profile = decision_profiles.get(prior_date) or {}
            raw = profile.get(dimension)
            if raw is None:
                raw = profile.get(f"{dimension}_state")
            label = str(raw or "").lower()
        if label != target:
            continue
        intervals.append((
            equity[current_date] / equity[prior_date] - 1.0,
            benchmark[current_date] / benchmark[prior_date] - 1.0,
        ))
    if not intervals:
        return {
            "status": "insufficient_observed_slice_evidence",
            "dimension": dimension,
            "value": target,
            "observations": 0,
        }
    strategy_value = 1.0
    benchmark_value = 1.0
    peak = 1.0
    drawdown = 0.0
    for strategy_return, benchmark_return in intervals:
        strategy_value *= 1.0 + strategy_return
        benchmark_value *= 1.0 + benchmark_return
        peak = max(peak, strategy_value)
        drawdown = min(drawdown, strategy_value / peak - 1.0)
    return {
        "status": "completed",
        "method": "point_in_time_dynamic_slice_attribution_v1",
        "dimension": dimension,
        "value": target,
        "observations": len(intervals),
        "total_return": round(strategy_value - 1.0, 8),
        "benchmark_return": round(benchmark_value - 1.0, 8),
        "excess_return": round(strategy_value - benchmark_value, 8),
        "max_drawdown": round(drawdown, 8),
    }


def environment_fitness(result: dict[str, Any]) -> dict[str, Any]:
    """Score one comparable environment without an arbitrary pass threshold."""
    if str(result.get("status") or "") != "completed":
        return {
            "schema_version": "alis_environment_fitness_v1",
            "rankable": False,
            "fitness": None,
            "reason": str(result.get("status") or "not_completed"),
        }
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else result
    has_return = "excess_return" in metrics or "total_return" in metrics
    if not has_return:
        return {
            "schema_version": "alis_environment_fitness_v1",
            "rankable": False,
            "fitness": None,
            "reason": "risk_only_environment",
            "risk_evidence": {
                key: result.get(key)
                for key in (
                    "blocked_trade_count", "transition_count",
                    "capacity_recheck_required", "method",
                )
                if key in result
            },
        }
    excess = float(metrics.get("excess_return") or 0.0)
    drawdown = float(metrics.get("max_drawdown") or 0.0)
    compact = result.get("compact_evidence") if isinstance(result.get("compact_evidence"), dict) else {}
    turnover = float(metrics.get("turnover_rate") or compact.get("turnover_rate") or 0.0)
    observations = int(
        result.get("observations")
        or metrics.get("observations")
        or len(compact.get("daily_returns") or {})
    )
    fitness = excess + 0.15 * drawdown - 0.0025 * turnover
    return {
        "schema_version": "alis_environment_fitness_v1",
        "rankable": True,
        "fitness": round(fitness, 8),
        "components": {
            "cost_after_excess_return": round(excess, 8),
            "max_drawdown_penalty": round(0.15 * drawdown, 8),
            "turnover_penalty": round(-0.0025 * turnover, 8),
            "observations": observations,
        },
        "comparison_rule": "relative_rank_within_same_environment_version",
        "absolute_pass_threshold": None,
    }


def _replay_cache_key(
    *,
    candidate: dict[str, Any],
    environment: dict[str, Any],
    snapshot_id: str,
    content: dict[str, Any],
    spec: dict[str, Any],
) -> tuple[str, str]:
    contract = {
        "candidate_hash": str(candidate.get("candidate_hash") or ""),
        "environment_version": str(environment.get("evidence_hash") or ""),
        "snapshot_id": str(snapshot_id),
        "strategy_content": content,
        "factor_bindings": spec.get("factor_bindings"),
        "model_bindings": spec.get("model_bindings"),
        "market_state_binding": spec.get("market_state_binding"),
        "strategy_controller_version_id": spec.get("strategy_controller_version_id"),
        "cost_contract": "strategy_replay_frozen_cost_contract",
    }
    contract_hash = _hash(contract)
    return _hash({"environment_id": environment["environment_id"], **contract}), contract_hash


def _load_replay_cache(cache_key: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT result_json FROM strategy_environment_replay_cache WHERE cache_key=?",
            (str(cache_key),),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE strategy_environment_replay_cache
                   SET hit_count=hit_count+1,updated_at=? WHERE cache_key=?""",
                (_now(), str(cache_key)),
            )
    return json.loads(dict(row).get("result_json") or "{}") if row else None


def _store_replay_cache(
    *,
    cache_key: str,
    contract_hash: str,
    candidate: dict[str, Any],
    environment: dict[str, Any],
    snapshot_id: str,
    result: dict[str, Any],
) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_environment_replay_cache(
               cache_key,candidate_hash,environment_id,environment_version,
               snapshot_id,contract_hash,result_json,hit_count,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,0,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET
               result_json=excluded.result_json,updated_at=excluded.updated_at""",
            (
                cache_key, str(candidate.get("candidate_hash") or ""),
                str(environment["environment_id"]),
                str(environment.get("evidence_hash") or ""),
                str(snapshot_id), contract_hash, _json(result), now, now,
            ),
        )


def _persist_factor_learning(
    candidate: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    weights = {
        str(key): float(value)
        for key, value in (candidate.get("factor_weights") or {}).items()
    }
    if not weights:
        return
    now = _now()
    with _conn() as conn:
        inserted = conn.execute(
            """INSERT INTO strategy_factor_learning_runs(
               candidate_id,candidate_hash,learned_at
               ) VALUES(?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
            (
                str(candidate["candidate_id"]),
                str(candidate.get("candidate_hash") or ""),
                now,
            ),
        )
        if int(inserted.rowcount or 0) != 1:
            return
    pair_samples: dict[tuple[str, str], list[float]] = {}
    with _conn() as conn:
        for result in results:
            fitness = result.get("environment_fitness") or environment_fitness(result)
            if not bool(fitness.get("rankable")):
                continue
            score = float(fitness.get("fitness") or 0.0)
            environment_id = str(result.get("environment_id") or "")
            for factor_key, weight in weights.items():
                signed = score * weight
                conn.execute(
                    """INSERT INTO strategy_factor_contributions(
                       candidate_id,environment_id,factor_key,signed_contribution,
                       absolute_contribution,evidence_json,updated_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(candidate_id,environment_id,factor_key) DO UPDATE SET
                       signed_contribution=excluded.signed_contribution,
                       absolute_contribution=excluded.absolute_contribution,
                       evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (
                        str(candidate["candidate_id"]), environment_id, factor_key,
                        signed, abs(score * weight),
                        _json({
                            "method": "fitness_weighted_leave_direction_proxy_v1",
                            "fitness": score, "weight": weight,
                        }),
                        now,
                    ),
                )
            factor_keys = sorted(weights)
            for left_index, left in enumerate(factor_keys):
                for right in factor_keys[left_index + 1:]:
                    pair_samples.setdefault((left, right), []).append(
                        score * (1.0 - abs(weights[left] - weights[right]))
                    )
        for (left, right), values in pair_samples.items():
            observed = len(values)
            mean_joint = sum(values) / observed
            conn.execute(
                """INSERT INTO strategy_factor_interactions(
                   factor_left,factor_right,observation_count,mean_joint_fitness,
                   complementarity,evidence_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(factor_left,factor_right) DO UPDATE SET
                   mean_joint_fitness=(
                     strategy_factor_interactions.mean_joint_fitness *
                     strategy_factor_interactions.observation_count +
                     excluded.mean_joint_fitness * excluded.observation_count
                   ) / (
                     strategy_factor_interactions.observation_count +
                     excluded.observation_count
                   ),
                   complementarity=(
                     strategy_factor_interactions.complementarity *
                     strategy_factor_interactions.observation_count +
                     excluded.complementarity * excluded.observation_count
                   ) / (
                     strategy_factor_interactions.observation_count +
                     excluded.observation_count
                   ),
                   observation_count=strategy_factor_interactions.observation_count+
                     excluded.observation_count,
                   evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (
                    left, right, observed, mean_joint, mean_joint,
                    _json({"method": "joint_fitness_balance_v1"}), now,
                ),
            )


def backfill_factor_learning(limit: int = 500) -> dict[str, Any]:
    """Idempotently learn from completed historical candidate/environment evidence."""
    ensure_strategy_research_tables()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT e.payload_json
               FROM strategy_research_events e
               LEFT JOIN strategy_factor_learning_runs l ON l.candidate_id=e.source_id
               WHERE e.research_line='candidate_generation'
                 AND e.event_type='candidate_strategy_generated'
                 AND e.status='completed' AND l.candidate_id IS NULL
               ORDER BY e.created_at ASC LIMIT ?""",
            (max(1, min(int(limit), 2000)),),
        ).fetchall()]
    learned = 0
    for row in rows:
        candidate = json.loads(row.get("payload_json") or "{}")
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            continue
        with _conn() as conn:
            result_rows = conn.execute(
                """SELECT result_json FROM strategy_environment_backtests
                   WHERE candidate_id=? ORDER BY environment_id""",
                (candidate_id,),
            ).fetchall()
        results = [json.loads(dict(item).get("result_json") or "{}") for item in result_rows]
        if not results:
            continue
        _persist_factor_learning(candidate, results)
        learned += 1
    return {
        "ok": True,
        "schema_version": "alis_factor_learning_backfill_v1",
        "learned_candidates": learned,
    }


def run_candidate_environment_matrix(
    *,
    candidate: dict[str, Any],
    replay_runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    from app.services.continuous_factor_research import (
        _active_research_snapshot,
        _baseline_research_spec,
        _strategy_content,
    )

    snapshot = _active_research_snapshot()
    spec = _baseline_research_spec(snapshot)
    if isinstance(candidate.get("model_bindings"), dict):
        spec["model_bindings"] = dict(candidate["model_bindings"])
    spec["holding_period_sessions"] = int(candidate.get("holding_period_sessions") or 20)
    content = _strategy_content(candidate.get("factor_weights") or {}, spec)
    environments = [
        item for item in (
            _load_environment(str(environment_id))
            for environment_id in (candidate.get("environment_ids") or [])
        ) if item
    ]
    historical = [
        item for item in environments
        if str(item.get("environment_type") or "") in {"historical_rolling", "extreme_historical_replay"}
    ]
    results: list[dict[str, Any]] = []
    base_replays: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    started_at = time.monotonic()
    def execute_historical(environment: dict[str, Any]) -> dict[str, Any]:
        window = environment.get("specification") or {}
        if not window.get("start") or not window.get("end"):
            return {
                "environment": environment,
                "result": {"environment_id": environment["environment_id"], "status": "invalid_window"},
                "replay": None,
                "cache_hit": False,
            }
        cache_key, contract_hash = _replay_cache_key(
            candidate=candidate,
            environment=environment,
            snapshot_id=str(snapshot.get("snapshot_id") or candidate.get("snapshot_id") or ""),
            content=content,
            spec=spec,
        )
        replay = _load_replay_cache(cache_key)
        cache_hit = replay is not None
        if replay is None:
            replay = replay_runner(
                strategy_id=None,
                start_date=str(window["start"]),
                end_date=str(window["end"]),
                strategy_content=content,
                strategy_name=f"V10 environment {candidate['candidate_id'][-8:]}",
                factor_bindings=spec.get("factor_bindings"),
                model_bindings=spec.get("model_bindings"),
                market_state_binding=spec.get("market_state_binding"),
                strategy_controller_version_id=spec.get("strategy_controller_version_id"),
                research_package_hash=str(candidate.get("candidate_hash") or ""),
                evaluate_market_states=True,
            )
            _store_replay_cache(
                cache_key=cache_key,
                contract_hash=contract_hash,
                candidate=candidate,
                environment=environment,
                snapshot_id=str(snapshot.get("snapshot_id") or candidate.get("snapshot_id") or ""),
                result=replay,
            )
        return {
            "environment": environment,
            "replay": replay,
            "cache_hit": cache_hit,
            "result": {
                "environment_id": environment["environment_id"],
                "environment_version": environment["evidence_hash"],
                "environment_name": environment["name"],
                "environment_type": environment["environment_type"],
                "status": "completed",
                "metrics": replay.get("metrics") or {},
                "data_quality": replay.get("data_quality") or {},
                "compact_evidence": _compact_replay_evidence(replay),
                "cache": {"hit": cache_hit, "cache_key": cache_key},
            },
        }

    low_memory_worker = str(os.getenv("ALIS_WORKER_PROFILE", "")).lower() in {"2gb", "low_memory", "low-memory"}
    parallelism = max(1, min(
        int(os.getenv("ALIS_V10_ENV_PARALLELISM", "1" if low_memory_worker else "2")),
        max(1, len(historical)),
        4,
    ))
    if parallelism > 1 and len(historical) > 1:
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="alis-v10-environment",
        ) as executor:
            historical_outcomes = list(executor.map(execute_historical, historical))
    else:
        historical_outcomes = []
        for environment_index, environment in enumerate(historical):
            outcome = execute_historical(environment)
            historical_outcomes.append(outcome)
            completed_results = [
                item["result"] for item in historical_outcomes
                if str((item.get("result") or {}).get("status") or "") == "completed"
            ]
            completed_fitness = [
                float(environment_fitness(item).get("fitness") or 0.0)
                for item in completed_results if environment_fitness(item).get("rankable")
            ]
            if len(completed_fitness) >= 3 and max(completed_fitness) < -0.50:
                for remaining in historical[environment_index + 1:]:
                    historical_outcomes.append({
                        "environment": remaining,
                        "replay": None,
                        "cache_hit": False,
                        "result": {
                            "environment_id": remaining["environment_id"],
                            "environment_version": remaining["evidence_hash"],
                            "environment_name": remaining["name"],
                            "environment_type": remaining["environment_type"],
                            "status": "early_stopped_by_catastrophic_bound",
                            "early_stop_contract": "alis_research_catastrophic_bound_v1",
                        },
                    })
                break
    for outcome in historical_outcomes:
        results.append(outcome["result"])
        if outcome.get("replay") is not None:
            base_replays.append(outcome["replay"])
        if outcome.get("cache_hit"):
            cache_hits += 1
        elif str((outcome.get("result") or {}).get("status") or "") == "completed":
            cache_misses += 1
    baseline = max(
        base_replays,
        key=lambda item: len(item.get("equity_series") or []),
        default=None,
    )
    for environment in environments:
        if environment in historical:
            continue
        if str(environment.get("environment_type") or "") == "calibrated_stress":
            stress = _stress_result(baseline, environment) if baseline else {
                "status": "blocked_by_missing_historical_baseline"
            }
            results.append({
                "environment_id": environment["environment_id"],
                "environment_version": environment["evidence_hash"],
                "environment_name": environment["name"],
                "environment_type": environment["environment_type"],
                **stress,
            })
        elif str(environment.get("environment_type") or "") == "observed_state_slice":
            observed = _observed_slice_result(baseline, environment) if baseline else {
                "status": "blocked_by_missing_historical_baseline"
            }
            results.append({
                "environment_id": environment["environment_id"],
                "environment_version": environment["evidence_hash"],
                "environment_name": environment["name"],
                "environment_type": environment["environment_type"],
                **observed,
            })
        else:
            results.append({
                "environment_id": environment["environment_id"],
                "environment_version": environment["evidence_hash"],
                "environment_name": environment["name"],
                "environment_type": environment["environment_type"],
                "status": "blocked_by_missing_environment_adapter",
                "specification": environment.get("specification") or {},
            })
    for result in results:
        result["environment_fitness"] = environment_fitness(result)
    now = _now()
    ensure_strategy_research_tables()
    with _conn() as conn:
        for result in results:
            conn.execute(
                """INSERT INTO strategy_environment_backtests(
                   candidate_id,environment_id,result_json,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(candidate_id,environment_id) DO UPDATE SET
                   result_json=excluded.result_json,status=excluded.status,updated_at=excluded.updated_at""",
                (
                    candidate["candidate_id"], result["environment_id"], _json(result),
                    result["status"], now, now,
                ),
            )
    _persist_factor_learning(candidate, results)
    wave_id = str(candidate.get("research_wave_id") or "")
    _update_wave_checkpoint(
        wave_id,
        str(candidate["candidate_id"]),
        [str(item.get("environment_id") or "") for item in results],
    )
    elapsed_seconds = time.monotonic() - started_at
    _record_runtime_metric(
        metric_type="candidate_runtime_seconds",
        value=elapsed_seconds,
        wave_id=wave_id,
        candidate_id=str(candidate["candidate_id"]),
        dimensions={
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "environment_count": len(results),
            "early_stopped": any(
                str(item.get("status") or "").startswith("early_stopped")
                for item in results
            ),
        },
    )
    _record_runtime_metric(
        metric_type="cache_hit_ratio",
        value=(cache_hits / max(1, cache_hits + cache_misses)),
        wave_id=wave_id,
        candidate_id=str(candidate["candidate_id"]),
        dimensions={"hits": cache_hits, "misses": cache_misses},
    )
    failure_memories: list[dict[str, Any]] = []
    environment_by_id = {str(item["environment_id"]): item for item in environments}
    for result in results:
        if str(result.get("status") or "") != "completed":
            continue
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else result
        excess = float(metrics.get("excess_return") or 0.0)
        if excess > 0:
            continue
        environment = environment_by_id.get(str(result.get("environment_id") or "")) or {}
        name = str(environment.get("name") or "").lower()
        primary_gap = (
            "liquidity_and_capital_persistence_failure"
            if "liquidity" in name or "cost" in name
            else "regime_transition_failure"
            if "regime" in name or "state" in name or "volatility" in name
            else "ranking_failure"
        )
        failure_memories.append(record_failure_memory(
            strategy_hash=str(candidate.get("candidate_hash") or ""),
            environment_id=str(result.get("environment_id") or ""),
            attribution={
                "primary_gap": primary_gap,
                "environment_name": name,
                "environment_type": environment.get("environment_type"),
                "excess_return": excess,
            },
            evidence={
                "candidate_id": candidate["candidate_id"],
                "research_snapshot_id": snapshot.get("snapshot_id"),
                "environment_result": result,
            },
            data_cutoff=str(snapshot.get("data_as_of") or ""),
        ))
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "results": results,
        "completed": sum(item.get("status") == "completed" for item in results),
        "blocked": sum(str(item.get("status") or "").startswith("blocked") for item in results),
        "failure_memories": failure_memories,
        "runtime": {
            "elapsed_seconds": round(elapsed_seconds, 6),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
        },
    }


def run_next_environment_backtest(
    *,
    candidate_id: str | None = None,
    replay_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.services.strategy_replay import run_strategy_replay

    ensure_strategy_research_tables()
    requested_candidate_id = str(candidate_id or "")
    if not requested_candidate_id:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "blocked_by_missing_candidate_id",
        }
    event = _candidate_event(requested_candidate_id)
    if not event:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "candidate_event_not_found",
            "candidate_id": requested_candidate_id,
        }
    candidate = json.loads(event.get("payload_json") or "{}")
    if str(candidate.get("candidate_id") or "") != requested_candidate_id:
        raise ValueError("environment backtest task candidate binding mismatch")
    wave_id = str(candidate.get("research_wave_id") or "")
    if str(event.get("status") or "") == "completed":
        with _conn() as conn:
            rows = conn.execute(
                """SELECT result_json FROM strategy_environment_backtests
                   WHERE candidate_id=? ORDER BY environment_id""",
                (requested_candidate_id,),
            ).fetchall()
        existing_results = [
            json.loads(dict(row).get("result_json") or "{}") for row in rows
        ]
        wave = refresh_research_wave(wave_id) if wave_id else None
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "already_completed",
            "candidate_id": requested_candidate_id,
            "results": existing_results,
            "research_wave_id": wave_id or None,
            "research_wave": wave,
            "wave_ready_for_distillation": bool(
                (wave or {}).get("ready_for_distillation")
            ),
        }
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_events SET status='queued'
               WHERE event_id=? AND status='running'""",
            (event["event_id"],),
        )
        updated = conn.execute(
            """UPDATE strategy_research_events SET status='running'
               WHERE event_id=? AND status='queued'""",
            (event["event_id"],),
        )
        if int(updated.rowcount or 0) != 1:
            return {
                "ok": True,
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "status": "claim_lost",
                "candidate_id": requested_candidate_id,
            }
    try:
        result = run_candidate_environment_matrix(
            candidate=candidate,
            replay_runner=replay_runner or run_strategy_replay,
        )
        status = "completed"
    except Exception:
        with _conn() as conn:
            conn.execute(
                "UPDATE strategy_research_events SET status='queued' WHERE event_id=? AND status='running'",
                (event["event_id"],),
            )
        raise
    with _conn() as conn:
        conn.execute(
            """UPDATE strategy_research_events SET status=?,consumed_at=?
               WHERE event_id=? AND status='running'""",
            (status, _now(), event["event_id"]),
        )
    wave = refresh_research_wave(wave_id) if wave_id else None
    return {
        **result,
        "event_id": event["event_id"],
        "status": status,
        "research_wave_id": wave_id or None,
        "research_wave": wave,
        "wave_ready_for_distillation": bool(
            (wave or {}).get("ready_for_distillation")
        ),
    }


def run_candidate_generation_line(
    *,
    snapshot_id: str,
    factors: list[dict[str, Any]],
    prototypes: list[dict[str, Any]] | None = None,
    failure_memories: list[dict[str, Any]] | None = None,
    environment_ids: list[str] | None = None,
    batch_size: int = 4,
    generation_nonce: int = 0,
    model_bindings_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = generate_candidate_batch(
        snapshot_id=snapshot_id,
        factors=factors,
        count=max(1, int(batch_size)),
        prototypes=prototypes,
        failure_memories=failure_memories,
        generation_nonce=generation_nonce,
    )
    for item in candidates:
        item["environment_ids"] = list(environment_ids or [])
        item["environment_matrix_required"] = True
        if model_bindings_override:
            item["model_bindings"] = dict(model_bindings_override)
            item["candidate_hash"] = _hash({
                "base_candidate_hash": item["candidate_hash"],
                "model_bindings": item["model_bindings"],
            })
            item["candidate_id"] = f"strategy-research-candidate-{item['candidate_hash'][:20]}"
    wave = _persist_research_wave(snapshot_id=snapshot_id, candidates=candidates)
    events = [
        enqueue_research_event(
            research_line="candidate_generation",
            event_type="candidate_strategy_generated",
            payload=item,
            source_id=str(item["candidate_id"]),
        )
        for item in candidates
    ]
    if wave:
        wave = refresh_research_wave(str(wave["wave_id"]))
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "research_line": "candidate_generation",
        "generated": candidates,
        "events": events,
        "research_wave": wave,
        "research_wave_id": (wave or {}).get("wave_id"),
    }


def _load_failure_memories(
    limit: int = 100,
    *,
    snapshot_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_strategy_research_tables()
    query_limit = max(500, min(max(1, int(limit)) * 20, 5000))
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_failure_memories
               ORDER BY updated_at DESC LIMIT ?""",
            (query_limit,),
        ).fetchall()]
    output: list[dict[str, Any]] = []
    for row in rows:
        row["attribution"] = json.loads(row.pop("attribution_json") or "{}")
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
        row["model_training_request"] = json.loads(row.pop("model_training_request_json") or "{}")
        memory_snapshot_id = str(
            row["evidence"].get("research_snapshot_id")
            or row["evidence"].get("snapshot_id")
            or ""
        )
        if snapshot_id and memory_snapshot_id != str(snapshot_id):
            continue
        output.append(row)
        if len(output) >= max(1, min(int(limit), 500)):
            break
    return output


def _load_prototypes(limit: int = 100) -> list[dict[str, Any]]:
    ensure_strategy_research_tables()
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_prototypes
               ORDER BY updated_at DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()]
    for row in rows:
        package = json.loads(row.pop("package_json") or "{}")
        row.update(package)
        row["applicability"] = json.loads(row.pop("applicability_json") or "{}")
        row["failure_boundaries"] = json.loads(row.pop("failure_boundaries_json") or "{}")
        row["capacity_risk"] = json.loads(row.pop("capacity_risk_json") or "{}")
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
    return rows


def _model_binding_identity(bindings: dict[str, Any] | None) -> dict[str, int | None]:
    payload = bindings if isinstance(bindings, dict) else {}
    return {
        key: (
            int((payload.get(key) or {}).get("id") or 0) or None
            if isinstance(payload.get(key), dict)
            else None
        )
        for key in MODEL_KEYS
    }


def _prototype_matches_snapshot(
    prototype: dict[str, Any],
    snapshot: dict[str, Any],
) -> bool:
    from app.services.continuous_factor_research import _baseline_research_spec

    if str(prototype.get("research_snapshot_id") or "") != str(
        snapshot.get("snapshot_id") or ""
    ):
        return False
    baseline = _baseline_research_spec(snapshot)
    return _model_binding_identity(
        prototype.get("model_bindings")
    ) == _model_binding_identity(baseline.get("model_bindings"))


def _current_snapshot_prototypes(
    snapshot: dict[str, Any],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return [
        item
        for item in _load_prototypes(limit=limit)
        if _prototype_matches_snapshot(item, snapshot)
    ]


def _pending_candidate_count(*, snapshot_id: str | None = None) -> int:
    ensure_strategy_research_tables()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT payload_json
               FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'
                 AND status IN ('queued','running')
               ORDER BY created_at ASC LIMIT 500"""
        ).fetchall()
    if not snapshot_id:
        return len(rows)
    return sum(
        str(
            json.loads(dict(row).get("payload_json") or "{}").get(
                "snapshot_id"
            )
            or ""
        )
        == str(snapshot_id)
        for row in rows
    )


def _candidate_event_count() -> int:
    ensure_strategy_research_tables()
    with _conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS event_count
               FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'"""
        ).fetchone()
    return int(row["event_count"] if row else 0)


def run_active_candidate_generation_line(
    *,
    batch_size: int = 4,
    model_version_id: int | None = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    from app.services.continuous_factor_research import _active_research_snapshot

    snapshot = _active_research_snapshot()
    pending_count = _pending_candidate_count(
        snapshot_id=str(snapshot.get("snapshot_id") or ""),
    )
    available_slots = max(0, MAX_PENDING_CANDIDATES - pending_count)
    if available_slots == 0:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "research_line": "candidate_generation",
            "status": "backpressured",
            "generated": [],
            "events": [],
            "pending_candidate_count": pending_count,
            "maximum_pending_candidates": MAX_PENDING_CANDIDATES,
        }

    backfill_factor_learning()
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    evidence_hash = str(contract.get("evidence_signature") or _hash({
        "snapshot_id": snapshot["snapshot_id"],
        "data_as_of": snapshot.get("data_as_of"),
    }))
    environments = build_dynamic_market_environments(
        evidence_hash=evidence_hash,
        historical_windows=list(snapshot.get("research_windows") or []),
        observed_dimensions=(contract.get("observed_market_dimensions") or {
            "trend": ["up", "down", "sideways"],
            "volatility": ["high", "low"],
            "liquidity": ["expanding", "contracting"],
            "style": ["large_cap", "small_cap", "value", "growth"],
            "crowding": ["rising", "unwinding"],
        }),
        extreme_windows=(
            list(contract.get("extreme_historical_windows") or [])
            or discover_extreme_historical_windows(str(snapshot.get("data_as_of") or ""))
        ),
    )
    persist_market_environments(environments)
    factor_analysis = analyze_factor_pool(list(snapshot.get("factors") or []))
    model_bindings_override = None
    if model_version_id and model_key:
        from app.db.store import get_model_version
        from app.services.continuous_factor_research import _baseline_research_spec

        version = get_model_version(int(model_version_id))
        if version and str(version.get("model_key") or "") == str(model_key):
            baseline_bindings = dict((_baseline_research_spec(snapshot).get("model_bindings") or {}))
            baseline_bindings[str(model_key)] = {
                "id": int(version["id"]),
                "model_key": str(model_key),
                "version": str(version.get("version") or ""),
                "status": str(version.get("status") or ""),
            }
            model_bindings_override = baseline_bindings
    result = run_candidate_generation_line(
        snapshot_id=str(snapshot["snapshot_id"]),
        factors=list(factor_analysis.get("eligible_factors") or []),
        prototypes=_current_snapshot_prototypes(snapshot),
        failure_memories=_load_failure_memories(
            snapshot_id=str(snapshot.get("snapshot_id") or ""),
        ),
        environment_ids=[item["environment_id"] for item in environments],
        batch_size=min(max(1, int(batch_size)), available_slots),
        generation_nonce=_candidate_event_count(),
        model_bindings_override=model_bindings_override,
    )
    result["status"] = "generated"
    result["pending_candidate_count_before_generation"] = pending_count
    result["maximum_pending_candidates"] = MAX_PENDING_CANDIDATES
    result["factor_analysis"] = {
        key: value for key, value in factor_analysis.items() if key != "eligible_factors"
    }
    result["model_candidate_binding"] = {
        "model_key": model_key,
        "model_version_id": model_version_id,
        "complete_strategy_package_required": bool(model_bindings_override),
    }
    return result


def _prototype_maturity(member_count: int, representative_fitness: float) -> str:
    if member_count >= 4 and representative_fitness > 0:
        return "cross_window_candidate"
    if member_count >= 2:
        return "local_market_value"
    return "weak_evidence"


def _load_completed_v10_candidates(snapshot_id: str) -> list[dict[str, Any]]:
    ensure_strategy_research_tables()
    with _conn() as conn:
        wave_rows = conn.execute(
            """SELECT candidate_ids_json FROM strategy_research_waves
               WHERE research_snapshot_id=? ORDER BY created_at""",
            (str(snapshot_id),),
        ).fetchall()
        candidate_ids = sorted({
            str(candidate_id)
            for raw in wave_rows
            for candidate_id in json.loads(dict(raw).get("candidate_ids_json") or "[]")
            if str(candidate_id)
        })
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        events = [dict(row) for row in conn.execute(
            f"""SELECT * FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'
                 AND status='completed'
                 AND source_id IN ({placeholders})
               ORDER BY created_at ASC,event_id ASC""",
            tuple(candidate_ids),
        ).fetchall()]
        backtests = [dict(row) for row in conn.execute(
            f"""SELECT * FROM strategy_environment_backtests
                WHERE candidate_id IN ({placeholders})
                ORDER BY candidate_id,environment_id""",
            tuple(candidate_ids),
        ).fetchall()]
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in backtests:
        by_candidate.setdefault(str(row["candidate_id"]), []).append(
            json.loads(row.get("result_json") or "{}")
        )
    candidates: list[dict[str, Any]] = []
    for event in events:
        payload = json.loads(event.get("payload_json") or "{}")
        candidate_id = str(payload.get("candidate_id") or event.get("source_id") or "")
        if candidate_id not in candidate_ids:
            continue
        if str(payload.get("snapshot_id") or "") != str(snapshot_id):
            continue
        payload["environment_results"] = by_candidate.get(candidate_id, [])
        if payload["environment_results"]:
            candidates.append(payload)
    return candidates


def _v10_candidate_fitness(candidate: dict[str, Any]) -> float:
    scores = [
        float(fitness["fitness"])
        for item in (candidate.get("environment_results") or [])
        for fitness in [(
            item.get("environment_fitness")
            if isinstance(item.get("environment_fitness"), dict)
            else environment_fitness(item)
        )]
        if bool(fitness.get("rankable")) and fitness.get("fitness") is not None
    ]
    return round(sum(scores) / len(scores), 8) if scores else -1.0


def _v10_candidate_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_factors = set((left.get("factor_weights") or {}).keys())
    right_factors = set((right.get("factor_weights") or {}).keys())
    union = left_factors | right_factors
    factor_similarity = len(left_factors & right_factors) / len(union) if union else 0.0
    left_results = {
        str(item.get("environment_id") or ""): float(
            (item.get("environment_fitness") or environment_fitness(item)).get("fitness") or 0.0
        )
        for item in (left.get("environment_results") or [])
        if bool((item.get("environment_fitness") or environment_fitness(item)).get("rankable"))
    }
    right_results = {
        str(item.get("environment_id") or ""): float(
            (item.get("environment_fitness") or environment_fitness(item)).get("fitness") or 0.0
        )
        for item in (right.get("environment_results") or [])
        if bool((item.get("environment_fitness") or environment_fitness(item)).get("rankable"))
    }
    shared = set(left_results) & set(right_results)
    behavior_similarity = (
        sum(max(0.0, 1.0 - min(1.0, abs(left_results[key] - right_results[key]) / 0.20)) for key in shared)
        / len(shared)
        if shared else 0.0
    )
    return round(0.45 * factor_similarity + 0.55 * behavior_similarity, 8)


def _distill_v10_candidates(
    *,
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=_v10_candidate_fitness, reverse=True):
        target = next(
            (
                cluster for cluster in clusters
                if _v10_candidate_similarity(candidate, cluster["representative"]) >= 0.62
            ),
            None,
        )
        if target is None:
            clusters.append({"representative": candidate, "members": [candidate]})
        else:
            target["members"].append(candidate)
    from app.services.continuous_factor_research import _baseline_research_spec

    baseline = _baseline_research_spec(snapshot)
    now = _now()
    generated: list[dict[str, Any]] = []
    for cluster in clusters:
        members = cluster["members"]
        representative = cluster["representative"]
        # Clusters guide the next generation, but an expert must be a package
        # that was actually replayed. It must not inherit a centroid's metrics.
        weights = _normalized_weights(representative.get("factor_weights") or {})
        cluster_id = f"v10-strategy-cluster-{_hash(sorted(item['candidate_id'] for item in members))[:20]}"
        package = {
            "schema_version": "alis_complete_strategy_prototype_v1",
            "factor_weights": weights,
            "factor_set_hash": _hash(sorted(weights)),
            "model_bindings": representative.get("model_bindings") or baseline.get("model_bindings") or {},
            "market_state_binding": baseline.get("market_state_binding") or {},
            "strategy_controller_version_id": baseline.get("strategy_controller_version_id"),
            "portfolio_method": baseline.get("portfolio_method") or "cvar",
            "holding_period_sessions": representative.get("holding_period_sessions"),
            "cost_policy": baseline.get("cost_policy"),
        }
        environment_scores = {
            str(result.get("environment_id") or ""): float(
                (result.get("environment_fitness") or environment_fitness(result)).get("fitness") or 0.0
            )
            for result in (representative.get("environment_results") or [])
            if bool((result.get("environment_fitness") or environment_fitness(result)).get("rankable"))
        }
        applicability = {
            "environment_excess": environment_scores,
            "positive_environment_ids": sorted(key for key, value in environment_scores.items() if value > 0),
            "environment_selection": "dynamic_environment_matrix_v1",
        }
        failure_boundaries = {
            "negative_environment_ids": sorted(key for key, value in environment_scores.items() if value <= 0),
            "exit_on_domain_drift": True,
        }
        capacity_risk = {
            "environment_rechecks": [
                result for result in (representative.get("environment_results") or [])
                if "capacity" in str(result.get("method") or "")
            ],
            "cost_and_capacity_recheck_required": True,
        }
        prototype_id = f"strategy-prototype-{_hash({'cluster': cluster_id, 'package': package})[:20]}"
        maturity = _prototype_maturity(len(members), _v10_candidate_fitness(representative))
        evidence = {
            "schema_version": "alis_v10_strategy_cluster_evidence_v1",
            "member_candidate_ids": [item["candidate_id"] for item in members],
            "representative_candidate_id": representative["candidate_id"],
            "representative_fitness": _v10_candidate_fitness(representative),
            "distillation_method": "replayed_representative_plus_cluster_gene_guidance_v1",
            "cluster_gene_centroid": _normalized_weights({
                factor_key: sum(
                    float((member.get("factor_weights") or {}).get(factor_key, 0.0))
                    for member in members
                ) / len(members)
                for factor_key in sorted(set().union(*(
                    set(member.get("factor_weights") or {}) for member in members
                )))
            }),
            "metrics_inheritance_forbidden": True,
            "representative_environment_results": representative.get("environment_results") or [],
        }
        with _conn() as conn:
            conn.execute(
                """INSERT INTO strategy_prototypes(
                   prototype_id,cluster_id,research_snapshot_id,package_json,
                   applicability_json,failure_boundaries_json,capacity_risk_json,
                   maturity_status,evidence_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(prototype_id) DO UPDATE SET
                   package_json=excluded.package_json,applicability_json=excluded.applicability_json,
                   failure_boundaries_json=excluded.failure_boundaries_json,
                   capacity_risk_json=excluded.capacity_risk_json,maturity_status=excluded.maturity_status,
                   evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (
                    prototype_id, cluster_id, snapshot["snapshot_id"], _json(package),
                    _json(applicability), _json(failure_boundaries), _json(capacity_risk),
                    maturity, _json(evidence), now, now,
                ),
            )
        generated.append({
            "prototype_id": prototype_id,
            "cluster_id": cluster_id,
            "maturity_status": maturity,
            **package,
            "applicability": applicability,
            "failure_boundaries": failure_boundaries,
            "capacity_risk": capacity_risk,
            "evidence": evidence,
        })
    return generated


def refresh_environment_expert_pool(
    prototypes: list[dict[str, Any]] | None = None,
    *,
    backups_per_environment: int = 2,
) -> list[dict[str, Any]]:
    """Rank replayed prototypes per environment; every environment gets one champion."""
    rows = prototypes if prototypes is not None else _load_prototypes(limit=500)
    ranked: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for prototype in rows:
        for result in _prototype_environment_results(prototype):
            fitness = (
                result.get("environment_fitness")
                if isinstance(result.get("environment_fitness"), dict)
                else environment_fitness(result)
            )
            if not fitness.get("rankable") or fitness.get("fitness") is None:
                continue
            environment_id = str(result.get("environment_id") or "")
            environment_version = str(result.get("environment_version") or "")
            if not environment_id or not environment_version:
                environment = _load_environment(environment_id) or {}
                environment_version = str(environment.get("evidence_hash") or "")
            if not environment_id or not environment_version:
                continue
            ranked.setdefault((environment_id, environment_version), []).append({
                "environment_id": environment_id,
                "environment_version": environment_version,
                "environment_name": str(result.get("environment_name") or ""),
                "environment_type": str(result.get("environment_type") or ""),
                "prototype_id": str(prototype["prototype_id"]),
                "cluster_id": str(prototype.get("cluster_id") or ""),
                "factor_weights": prototype.get("factor_weights") or {},
                "factor_count": len(prototype.get("factor_weights") or {}),
                "fitness": float(fitness["fitness"]),
                "fitness_detail": fitness,
                "backtest_metrics": result.get("metrics") or result,
                "maturity_status": str(prototype.get("maturity_status") or ""),
            })
    now = _now()
    output: list[dict[str, Any]] = []
    with _conn() as conn:
        conn.execute("DELETE FROM strategy_environment_experts")
        for key, candidates in sorted(ranked.items()):
            ordered = sorted(
                candidates,
                key=lambda item: (-float(item["fitness"]), item["prototype_id"]),
            )[:1 + max(0, int(backups_per_environment))]
            for index, item in enumerate(ordered, start=1):
                row = {
                    **item,
                    "rank": index,
                    "role": "champion" if index == 1 else "backup",
                    "status": "active_research_expert",
                }
                conn.execute(
                    """INSERT INTO strategy_environment_experts(
                       environment_id,environment_version,prototype_id,rank_no,role,
                       fitness,fitness_json,evidence_json,status,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key[0], key[1], item["prototype_id"], index, row["role"],
                        item["fitness"], _json(item["fitness_detail"]), _json({
                            "environment_name": item["environment_name"],
                            "environment_type": item["environment_type"],
                            "cluster_id": item["cluster_id"],
                            "factor_weights": item["factor_weights"],
                            "factor_count": item["factor_count"],
                            "backtest_metrics": item["backtest_metrics"],
                            "maturity_status": item["maturity_status"],
                        }), row["status"], now,
                    ),
                )
                history_evidence = {
                    "environment_name": item["environment_name"],
                    "environment_type": item["environment_type"],
                    "cluster_id": item["cluster_id"],
                    "factor_count": item["factor_count"],
                    "maturity_status": item["maturity_status"],
                }
                history_identity = {
                    'environment_id': key[0],
                    'environment_version': key[1],
                    'prototype_id': item['prototype_id'],
                    'rank': index,
                    'observed_at': now,
                }
                history_id = f"expert-history-{_hash(history_identity)[:24]}"
                conn.execute(
                    """INSERT INTO strategy_environment_expert_history(
                       history_id,environment_id,environment_version,prototype_id,
                       rank_no,role,fitness,evidence_json,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(history_id) DO NOTHING""",
                    (
                        history_id,
                        key[0], key[1], item["prototype_id"], index, row["role"],
                        item["fitness"], _json(history_evidence), now,
                    ),
                )
                output.append(row)
    return output


def run_cluster_distillation_line(
    *,
    snapshot_id: str | None = None,
    research_wave_id: str | None = None,
) -> dict[str, Any]:
    from app.services.continuous_factor_research import (
        _active_research_snapshot,
        _experiment_rows,
        refresh_research_clusters,
    )

    snapshot = _active_research_snapshot()
    current_snapshot_id = str(snapshot["snapshot_id"])
    active_snapshot_id = str(snapshot_id or current_snapshot_id)
    if active_snapshot_id != current_snapshot_id:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "research_line": "cluster_distillation",
            "status": "blocked_stale_research_snapshot",
            "snapshot_id": active_snapshot_id,
            "current_snapshot_id": current_snapshot_id,
            "generated": [],
            "production_change_forbidden": True,
        }
    v10_candidates = _load_completed_v10_candidates(active_snapshot_id)
    if v10_candidates:
        generated = _distill_v10_candidates(snapshot=snapshot, candidates=v10_candidates)
        expert_pool = refresh_environment_expert_pool(generated) if generated else []
        if generated:
            enqueue_research_event(
                research_line="cluster_distillation",
                event_type="strategy_prototypes_distilled",
                payload={"snapshot_id": active_snapshot_id, "prototype_ids": [item["prototype_id"] for item in generated]},
                source_id=active_snapshot_id,
            )
        wave = (
            mark_research_wave_distilled(str(research_wave_id))
            if research_wave_id else None
        )
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "research_line": "cluster_distillation",
            "snapshot_id": active_snapshot_id,
            "source": "v10_environment_matrix",
            "generated": generated,
            "expert_pool": expert_pool,
            "research_wave_id": research_wave_id,
            "research_wave": wave,
        }
    rows = _experiment_rows(active_snapshot_id)
    by_id = {str(item["experiment_id"]): item for item in rows}
    clusters = refresh_research_clusters(active_snapshot_id)
    ensure_strategy_research_tables()
    now = _now()
    generated: list[dict[str, Any]] = []
    for cluster in clusters:
        representative = by_id.get(str(cluster.get("representative_experiment_id") or ""))
        if not representative:
            continue
        spec = representative.get("research_spec") if isinstance(representative.get("research_spec"), dict) else {}
        factor_centroid = cluster.get("factor_centroid") if isinstance(cluster.get("factor_centroid"), dict) else {}
        behavior = cluster.get("behavior_centroid") if isinstance(cluster.get("behavior_centroid"), dict) else {}
        weights = factor_centroid.get("distilled_weights") or representative.get("factor_weights") or {}
        package = {
            "schema_version": "alis_complete_strategy_prototype_v1",
            "factor_weights": weights,
            "factor_set_hash": _hash(sorted(weights)),
            "model_bindings": spec.get("model_bindings") or {},
            "market_state_binding": spec.get("market_state_binding") or {},
            "strategy_controller_version_id": spec.get("strategy_controller_version_id"),
            "portfolio_method": spec.get("portfolio_method") or "cvar",
            "holding_period_sessions": spec.get("holding_period_sessions"),
            "cost_policy": spec.get("cost_policy"),
        }
        applicability = {
            "market_state_excess": behavior.get("market_state_excess") or {},
            "environment_selection": "dynamic_environment_matrix_v1",
        }
        failure_boundaries = {
            "worst_drawdown": behavior.get("worst_drawdown"),
            "negative_market_states": sorted(
                key for key, value in (behavior.get("market_state_excess") or {}).items()
                if float(value or 0.0) < 0
            ),
            "exit_on_domain_drift": True,
        }
        capacity_risk = {
            "mean_turnover_rate": behavior.get("mean_turnover_rate"),
            "cost_and_capacity_recheck_required": True,
        }
        identity = {
            "snapshot_id": active_snapshot_id,
            "cluster_id": cluster["cluster_id"],
            "package": package,
        }
        prototype_id = f"strategy-prototype-{_hash(identity)[:20]}"
        maturity = _prototype_maturity(
            int(cluster.get("member_count") or 0),
            float(cluster.get("representative_fitness") or 0.0),
        )
        evidence = {
            "cluster": cluster,
            "representative_experiment_id": representative["experiment_id"],
            "distillation_method": "complete_strategy_cluster_distillation_v1",
        }
        with _conn() as conn:
            conn.execute(
                """INSERT INTO strategy_prototypes(
                   prototype_id,cluster_id,research_snapshot_id,package_json,
                   applicability_json,failure_boundaries_json,capacity_risk_json,
                   maturity_status,evidence_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(prototype_id) DO UPDATE SET
                   package_json=excluded.package_json,
                   applicability_json=excluded.applicability_json,
                   failure_boundaries_json=excluded.failure_boundaries_json,
                   capacity_risk_json=excluded.capacity_risk_json,
                   maturity_status=excluded.maturity_status,
                   evidence_json=excluded.evidence_json,
                   updated_at=excluded.updated_at""",
                (
                    prototype_id, cluster["cluster_id"], active_snapshot_id, _json(package),
                    _json(applicability), _json(failure_boundaries), _json(capacity_risk),
                    maturity, _json(evidence), now, now,
                ),
            )
        generated.append({
            "prototype_id": prototype_id,
            "cluster_id": cluster["cluster_id"],
            "maturity_status": maturity,
            **package,
            "applicability": applicability,
            "failure_boundaries": failure_boundaries,
            "capacity_risk": capacity_risk,
        })
    if generated:
        enqueue_research_event(
            research_line="cluster_distillation",
            event_type="strategy_prototypes_distilled",
            payload={"snapshot_id": active_snapshot_id, "prototype_ids": [item["prototype_id"] for item in generated]},
            source_id=active_snapshot_id,
        )
    wave = (
        mark_research_wave_distilled(str(research_wave_id))
        if research_wave_id else None
    )
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "research_line": "cluster_distillation",
        "snapshot_id": active_snapshot_id,
        "generated": generated,
        "research_wave_id": research_wave_id,
        "research_wave": wave,
    }


def _correlation(left: dict[str, float], right: dict[str, float]) -> float | None:
    shared = sorted(set(left) & set(right))
    if len(shared) < 3:
        return None
    a = [float(left[key]) for key in shared]
    b = [float(right[key]) for key in shared]
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    denominator = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return round(numerator / denominator, 8) if denominator > 0 else 0.0


def _maximum_drawdown_from_returns(returns: list[float]) -> float:
    value = 1.0
    peak = 1.0
    drawdown = 0.0
    for item in returns:
        value *= 1.0 + float(item)
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1.0)
    return round(drawdown, 8)


def _prototype_environment_results(prototype: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = prototype.get("evidence") if isinstance(prototype.get("evidence"), dict) else {}
    return list(evidence.get("representative_environment_results") or [])


def build_environment_expert_router(
    prototypes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze one point-in-time expert per live S0-S3 state plus a fallback."""
    experts = {
        str(item["prototype_id"]): {
            "prototype_id": str(item["prototype_id"]),
            "cluster_id": str(item.get("cluster_id") or ""),
            "factor_weights": item.get("factor_weights") or {},
            "factor_set_hash": str(item.get("factor_set_hash") or ""),
        }
        for item in prototypes
    }
    global_scores: dict[str, list[float]] = {}
    state_scores: dict[str, dict[str, list[float]]] = {
        state: {} for state in ("S0", "S1", "S2", "S3")
    }
    state_environments: dict[str, dict[str, list[str]]] = {
        state: {} for state in ("S0", "S1", "S2", "S3")
    }
    environment_versions: set[str] = set()
    for prototype in prototypes:
        prototype_id = str(prototype["prototype_id"])
        for result in _prototype_environment_results(prototype):
            fitness = (
                result.get("environment_fitness")
                if isinstance(result.get("environment_fitness"), dict)
                else environment_fitness(result)
            )
            if not fitness.get("rankable") or fitness.get("fitness") is None:
                continue
            value = float(fitness["fitness"])
            global_scores.setdefault(prototype_id, []).append(value)
            environment_id = str(result.get("environment_id") or "")
            environment_versions.add(str(result.get("environment_version") or ""))
            for state in _environment_state_map([environment_id]):
                state_scores[state].setdefault(prototype_id, []).append(value)
                state_environments[state].setdefault(prototype_id, []).append(environment_id)
    fallback_id = max(
        global_scores,
        key=lambda key: (sum(global_scores[key]) / len(global_scores[key]), key),
        default=next(iter(sorted(experts)), ""),
    )
    state_experts: dict[str, dict[str, Any]] = {}
    for state in ("S0", "S1", "S2", "S3"):
        candidates = state_scores[state] or ({fallback_id: global_scores.get(fallback_id, [0.0])} if fallback_id else {})
        if not candidates:
            continue
        prototype_id = max(
            candidates,
            key=lambda key: (sum(candidates[key]) / len(candidates[key]), key),
        )
        state_experts[state] = {
            "prototype_id": prototype_id,
            "fitness": round(sum(candidates[prototype_id]) / len(candidates[prototype_id]), 8),
            "environment_ids": sorted(state_environments[state].get(prototype_id) or []),
        }
    return {
        "schema_version": "alis_environment_expert_router_v1",
        "market_state_contract": "point_in_time_s0_s3_v1",
        "environment_versions": sorted(item for item in environment_versions if item),
        "experts": experts,
        "state_experts": state_experts,
        "fallback_prototype_id": fallback_id,
        "routing_method": "highest_relative_environment_fitness_then_frozen_fallback",
        "research_threshold": None,
    }


def _composite_router_content(
    *,
    snapshot: dict[str, Any],
    prototypes: list[dict[str, Any]],
    router: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.services.continuous_factor_research import (
        _baseline_research_spec,
        _strategy_content,
    )

    spec = _baseline_research_spec(snapshot)
    fallback_id = str(router.get("fallback_prototype_id") or "")
    fallback = next(
        (item for item in prototypes if str(item["prototype_id"]) == fallback_id),
        prototypes[0],
    )
    content = _strategy_content(fallback.get("factor_weights") or {}, spec)
    regime_scores: dict[str, dict[str, Any]] = {}
    by_id = {str(item["prototype_id"]): item for item in prototypes}
    for state, route in (router.get("state_experts") or {}).items():
        prototype = by_id.get(str((route or {}).get("prototype_id") or ""))
        if prototype:
            regime_scores[str(state)] = {
                "normalization": "rank_percentile",
                "factors": prototype.get("factor_weights") or {},
                "prototype_id": prototype["prototype_id"],
            }
    content.update({
        "schema_version": "alis_composite_environment_champion_v1",
        "regime_scores": regime_scores,
        "environment_expert_router": router,
    })
    return content, spec


def _run_composite_router_backtest(
    *,
    snapshot: dict[str, Any],
    prototypes: list[dict[str, Any]],
    router: dict[str, Any],
    replay_runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    content, spec = _composite_router_content(
        snapshot=snapshot,
        prototypes=prototypes,
        router=router,
    )
    windows: list[dict[str, Any]] = []
    for window in snapshot.get("research_windows") or []:
        if not window.get("start") or not window.get("end"):
            continue
        replay = replay_runner(
            strategy_id=None,
            start_date=str(window["start"]),
            end_date=str(window["end"]),
            strategy_content=content,
            strategy_name="V10 composite environment champion",
            factor_bindings=spec.get("factor_bindings"),
            model_bindings=spec.get("model_bindings"),
            market_state_binding=spec.get("market_state_binding"),
            strategy_controller_version_id=spec.get("strategy_controller_version_id"),
            research_package_hash=_hash({
                "content": content,
                "snapshot_id": snapshot.get("snapshot_id"),
            }),
            evaluate_market_states=True,
        )
        windows.append({
            "start": window["start"],
            "end": window["end"],
            "metrics": replay.get("metrics") or {},
            "data_quality": replay.get("data_quality") or {},
            "compact_evidence": _compact_replay_evidence(replay),
        })
    return {
        "schema_version": "alis_composite_router_end_to_end_replay_v1",
        "status": "completed" if windows else "blocked_missing_replay_window",
        "windows": windows,
        "strategy_content": content,
        "research_package_hash": _hash({
            "content": content,
            "snapshot_id": snapshot.get("snapshot_id"),
        }),
        "evaluated_object_equals_materialized_object": True,
        "performance_gate_applied": False,
    }


def _secondary_portfolio_backtest(selected: list[dict[str, Any]]) -> dict[str, Any]:
    per_member = {
        str(item["prototype_id"]): {
            str(result.get("environment_id") or ""): result
            for result in _prototype_environment_results(item)
            if str(result.get("status") or "") == "completed"
        }
        for item in selected
    }
    shared_environments = set.intersection(
        *(set(results) for results in per_member.values())
    ) if per_member else set()
    environment_results: list[dict[str, Any]] = []
    all_correlations: list[float] = []
    all_overlaps: list[float] = []
    for environment_id in sorted(shared_environments):
        member_results = {
            member_id: results[environment_id]
            for member_id, results in per_member.items()
        }
        return_series = {
            member_id: ((result.get("compact_evidence") or {}).get("daily_returns") or {})
            for member_id, result in member_results.items()
        }
        member_ids = sorted(return_series)
        for left_index, left_id in enumerate(member_ids):
            for right_id in member_ids[left_index + 1:]:
                value = _correlation(return_series[left_id], return_series[right_id])
                if value is not None:
                    all_correlations.append(value)
        common_dates = set.intersection(
            *(set(values) for values in return_series.values())
        ) if return_series else set()
        combined_returns = [
            sum(float(return_series[member_id][date]) for member_id in member_ids) / len(member_ids)
            for date in sorted(common_dates)
        ]
        combined_value = 1.0
        for value in combined_returns:
            combined_value *= 1.0 + value
        excess_values = [
            float((result.get("metrics") or {}).get("excess_return") or result.get("excess_return") or 0.0)
            for result in member_results.values()
        ]
        holding_maps = {
            member_id: ((result.get("compact_evidence") or {}).get("holding_sets") or {})
            for member_id, result in member_results.items()
        }
        holding_dates = set().union(*(set(values) for values in holding_maps.values())) if holding_maps else set()
        for date in holding_dates:
            sets = [set(values.get(date) or []) for values in holding_maps.values()]
            for left_index, left in enumerate(sets):
                for right in sets[left_index + 1:]:
                    union = left | right
                    if union:
                        all_overlaps.append(len(left & right) / len(union))
        environment_results.append({
            "environment_id": environment_id,
            "member_count": len(member_results),
            "combined_total_return": round(combined_value - 1.0, 8),
            "mean_member_excess_return": round(sum(excess_values) / len(excess_values), 8),
            "combined_max_drawdown": _maximum_drawdown_from_returns(combined_returns),
            "combined_turnover_rate": round(sum(
                float((result.get("compact_evidence") or {}).get("turnover_rate") or 0.0)
                for result in member_results.values()
            ), 8),
            "blocked_trade_count": sum(
                int((result.get("compact_evidence") or {}).get("blocked_trade_count") or 0)
                for result in member_results.values()
            ),
        })
    mean_excess = (
        sum(float(item["mean_member_excess_return"]) for item in environment_results) / len(environment_results)
        if environment_results else 0.0
    )
    worst_drawdown = min(
        (float(item["combined_max_drawdown"]) for item in environment_results), default=-1.0
    )
    maximum_correlation = max(all_correlations, default=0.0)
    mean_overlap = sum(all_overlaps) / len(all_overlaps) if all_overlaps else 0.0
    diagnostics = {
        "shared_environment_evidence": bool(environment_results),
        "mean_environment_excess_positive": mean_excess > 0.0,
        "worst_drawdown": round(worst_drawdown, 8),
        "maximum_pairwise_correlation": round(maximum_correlation, 8),
        "mean_holding_overlap": round(mean_overlap, 8),
        "capacity_observed": all(int(item["blocked_trade_count"]) >= 0 for item in environment_results),
    }
    return {
        "schema_version": "alis_strategy_portfolio_diversity_diagnostics_v2",
        "status": "completed" if environment_results else "insufficient_shared_environment_evidence",
        "diagnostics": diagnostics,
        "absolute_pass_threshold": None,
        "formal_promotion_evidence": False,
        "environment_results": environment_results,
        "mean_environment_excess_return": round(mean_excess, 8),
        "worst_drawdown": round(worst_drawdown, 8),
        "maximum_pairwise_correlation": round(maximum_correlation, 8),
        "mean_holding_overlap": round(mean_overlap, 8),
    }


def run_strategy_portfolio_line(
    *,
    maximum_members: int = 4,
    replay_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.services.continuous_factor_research import _active_research_snapshot
    from app.services.strategy_replay import run_strategy_replay

    snapshot = _active_research_snapshot()
    prototypes = [
        item for item in _current_snapshot_prototypes(snapshot, limit=200)
        if _prototype_environment_results(item)
    ]
    champion_ids: list[str] = []
    with _conn() as conn:
        champion_ids = [
            str(dict(row).get("prototype_id") or "")
            for row in conn.execute(
                """SELECT prototype_id,MIN(rank_no) AS best_rank
                   FROM strategy_environment_experts
                   WHERE role='champion'
                   GROUP BY prototype_id
                   ORDER BY best_rank,prototype_id"""
            ).fetchall()
        ]
    if champion_ids:
        by_id = {str(item["prototype_id"]): item for item in prototypes}
        prototypes = [by_id[item] for item in champion_ids if item in by_id] + [
            item for item in prototypes if str(item["prototype_id"]) not in set(champion_ids)
        ]
    selected = prototypes[:max(2, int(maximum_members))]
    if len(selected) < 2:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "research_line": "strategy_portfolio",
            "status": "awaiting_complementary_prototypes",
            "selected": [item.get("prototype_id") for item in selected],
        }
    member_ids = sorted(str(item["prototype_id"]) for item in selected)
    routing = build_environment_expert_router(selected)
    evidence = {
        "required_secondary_backtest": [
            "correlation", "holding_overlap", "combined_turnover", "capacity", "tail_risk"
        ],
        "formal_evidence": False,
        "production_eligible": False,
    }
    diversity_diagnostics = _secondary_portfolio_backtest(selected)
    end_to_end_replay = _run_composite_router_backtest(
        snapshot=snapshot,
        prototypes=selected,
        router=routing,
        replay_runner=replay_runner or run_strategy_replay,
    )
    secondary_backtest = {
        "schema_version": "alis_strategy_portfolio_secondary_backtest_v2",
        "status": (
            "evidence_ready"
            if end_to_end_replay["status"] == "completed"
            else "blocked_missing_replay_evidence"
        ),
        "end_to_end_router_replay": end_to_end_replay,
        "diversity_diagnostics": diversity_diagnostics,
        "research_performance_threshold": None,
        "next_gate": "relative_formal_oos_against_incumbent_benchmark_and_cash",
    }
    evidence["secondary_backtest"] = secondary_backtest
    portfolio_id = f"strategy-portfolio-{_hash({'members': member_ids, 'routing': routing})[:20]}"
    now = _now()
    ensure_strategy_research_tables()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_portfolio_candidates(
               portfolio_id,member_prototype_ids_json,routing_json,evidence_json,
               status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(portfolio_id) DO UPDATE SET
               routing_json=excluded.routing_json,evidence_json=excluded.evidence_json,
               status=excluded.status,updated_at=excluded.updated_at""",
            (
                portfolio_id, _json(member_ids), _json(routing), _json(evidence),
                "formal_validation_queued" if secondary_backtest["status"] == "evidence_ready" else "needs_improvement",
                now, now,
            ),
        )
    event_type = (
        "strategy_portfolio_ready_for_formal_validation"
        if secondary_backtest["status"] == "evidence_ready"
        else "strategy_portfolio_needs_improvement"
    )
    event = enqueue_research_event(
        research_line="strategy_portfolio",
        event_type=event_type,
        payload={
            "portfolio_id": portfolio_id,
            "member_prototype_ids": member_ids,
            "routing": routing,
            "strategy_content": end_to_end_replay.get("strategy_content") or {},
            "research_package_hash": end_to_end_replay.get("research_package_hash"),
            "secondary_backtest": secondary_backtest,
        },
        source_id=portfolio_id,
    )
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "research_line": "strategy_portfolio",
        "status": "formal_validation_queued" if secondary_backtest["status"] == "evidence_ready" else "needs_improvement",
        "portfolio_id": portfolio_id,
        "member_prototype_ids": member_ids,
        "routing": routing,
        "secondary_backtest": secondary_backtest,
        "event": event,
    }


def _environment_state_map(environment_ids: list[str]) -> set[str]:
    states: set[str] = set()
    for environment_id in environment_ids:
        environment = _load_environment(environment_id)
        if not environment:
            continue
        name = str(environment.get("name") or "").lower()
        if any(token in name for token in ("trend:up", "expanding", "growth", "crowding:rising")):
            states.add("S0")
        if any(token in name for token in ("sideways", "volatility:low", "value")):
            states.add("S1")
        if any(token in name for token in ("trend:down", "contracting", "unwinding")):
            states.add("S2")
        if any(token in name for token in ("volatility:high", "extreme", "rapid_regime_switch")):
            states.add("S3")
    return states


def materialize_next_portfolio_challenger() -> dict[str, Any]:
    from copy import deepcopy

    from app.db.store import create_model_version, create_strategy, list_model_versions, list_strategies
    from app.services.continuous_factor_research import _active_research_snapshot, _baseline_research_spec
    from app.services.production_strategy import resolve_production_strategy_champion
    from app.services.strategy_config import get_replay_required_factor_names

    ensure_strategy_research_tables()
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM strategy_research_events
               WHERE research_line='strategy_portfolio'
                 AND event_type='strategy_portfolio_ready_for_formal_validation'
                 AND status='queued' ORDER BY created_at ASC LIMIT 1"""
        ).fetchone()
        if not row:
            return {"ok": True, "schema_version": RESEARCH_SCHEMA_VERSION, "status": "awaiting_portfolio"}
        event = dict(row)
        claimed = conn.execute(
            "UPDATE strategy_research_events SET status='running' WHERE event_id=? AND status='queued'",
            (event["event_id"],),
        )
        if int(claimed.rowcount or 0) != 1:
            return {"ok": True, "schema_version": RESEARCH_SCHEMA_VERSION, "status": "claim_lost"}
    payload = json.loads(event.get("payload_json") or "{}")
    prototype_ids = set(str(item) for item in (payload.get("member_prototype_ids") or []))
    prototypes = [item for item in _load_prototypes(limit=500) if str(item.get("prototype_id")) in prototype_ids]
    if len(prototypes) < 2:
        with _conn() as conn:
            conn.execute(
                "UPDATE strategy_research_events SET status='blocked',consumed_at=? WHERE event_id=?",
                (_now(), event["event_id"]),
            )
        return {"ok": False, "status": "blocked_missing_prototypes", "event_id": event["event_id"]}
    structural_keys = ("model_bindings", "strategy_controller_version_id", "portfolio_method")
    if any(
        any(item.get(key) != prototypes[0].get(key) for key in structural_keys)
        for item in prototypes[1:]
    ):
        with _conn() as conn:
            conn.execute(
                "UPDATE strategy_research_events SET status='blocked',consumed_at=? WHERE event_id=?",
                (_now(), event["event_id"]),
            )
        return {
            "ok": False,
            "status": "blocked_incompatible_strategy_packages",
            "reason": "state_specific_model_controller_routing_not_frozen_as_one_replayable_package",
        }
    snapshot = _active_research_snapshot()
    baseline = _baseline_research_spec(snapshot)
    if any(
        not _prototype_matches_snapshot(item, snapshot)
        for item in prototypes
    ):
        with _conn() as conn:
            conn.execute(
                "UPDATE strategy_research_events SET status='blocked',consumed_at=? WHERE event_id=?",
                (_now(), event["event_id"]),
            )
        return {
            "ok": False,
            "status": "blocked_stale_research_model_contract",
            "event_id": event["event_id"],
            "current_snapshot_id": snapshot.get("snapshot_id"),
            "production_change_forbidden": True,
        }
    champion = resolve_production_strategy_champion()
    content = deepcopy(
        payload.get("strategy_content")
        if isinstance(payload.get("strategy_content"), dict) else {}
    )
    if not content:
        content, _ = _composite_router_content(
            snapshot=snapshot,
            prototypes=prototypes,
            router=payload.get("routing") or build_environment_expert_router(prototypes),
        )
    model_bindings = (
        prototypes[0].get("model_bindings")
        or baseline.get("model_bindings")
        or {}
    )
    factor_bindings = baseline.get("factor_bindings") or {}
    required_factors = get_replay_required_factor_names(
        content,
        "formula",
        model_bindings=model_bindings,
    )
    missing_factor_bindings = sorted(
        factor for factor in required_factors
        if not isinstance(factor_bindings.get(factor), dict)
    )
    if missing_factor_bindings:
        blocked_payload = {
            **payload,
            "governance_block": {
                "schema_version": "alis_replay_factor_contract_gate_v1",
                "status": "blocked_incomplete_factor_bindings",
                "required_factors": sorted(required_factors),
                "missing_factor_bindings": missing_factor_bindings,
                "production_change_forbidden": True,
            },
        }
        with _conn() as conn:
            conn.execute(
                """UPDATE strategy_research_events
                   SET status='blocked',payload_json=?,consumed_at=?
                   WHERE event_id=? AND status='running'""",
                (_json(blocked_payload), _now(), event["event_id"]),
            )
            conn.execute(
                """UPDATE strategy_portfolio_candidates
                   SET status='blocked_incomplete_factor_bindings',updated_at=?
                   WHERE portfolio_id=?""",
                (_now(), payload.get("portfolio_id")),
            )
        return {
            "ok": False,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "blocked_incomplete_factor_bindings",
            "portfolio_id": payload.get("portfolio_id"),
            "required_factors": sorted(required_factors),
            "missing_factor_bindings": missing_factor_bindings,
            "production_change_forbidden": True,
        }
    name = f"ALIS V10 策略组合 {str(payload.get('portfolio_id') or '')[-8:]}"
    strategy = next((item for item in list_strategies() if str(item.get("name") or "") == name), None)
    if not strategy:
        strategy = create_strategy(name, "formula", content)
    version_name = f"alis-v10-portfolio-{_hash(payload)[:16]}"
    existing = next(
        (item for item in list_model_versions("alis_strategy_challenger", limit=500) if item.get("version") == version_name),
        None,
    )
    if existing:
        model_version_id = int(existing["id"])
    else:
        model_version_id = create_model_version(
            model_key="alis_strategy_challenger",
            version=version_name,
            status="candidate",
            artifact_path=f"builtin://alis/challenger/v10-portfolio/{payload.get('portfolio_id')}",
            config={
                "role": "challenger",
                "generation": "v10_dynamic_strategy_portfolio",
                "strategy_id": int(strategy["id"]),
                "champion_reference": champion.get("champion_version"),
                "selection_strategy": content,
                "factor_bindings": factor_bindings,
                "model_bindings": model_bindings,
                "market_state_binding": prototypes[0].get("market_state_binding") or baseline.get("market_state_binding") or {},
                "strategy_controller_version_id": prototypes[0].get("strategy_controller_version_id"),
                "research_package_hash": str(
                    payload.get("research_package_hash") or _hash({
                        "content": content,
                        "snapshot_id": snapshot.get("snapshot_id"),
                    })
                ),
                "research_spec": {
                    **baseline,
                    "strategy_portfolio_id": payload.get("portfolio_id"),
                    "member_prototype_ids": sorted(prototype_ids),
                },
                "portfolio_optimizer": baseline.get("portfolio_optimizer") or {},
                "execution_contract": baseline.get("execution_contract") or {},
            },
            metrics={"secondary_backtest": payload.get("secondary_backtest") or {}},
            validation={
                "status": "pending_oos",
                "production_eligible": False,
                "next_gate": "quick_diagnostic",
                "manual_promotion_forbidden": True,
            },
            is_active=False,
        )
    with _conn() as conn:
        conn.execute(
            "UPDATE strategy_research_events SET status='completed',consumed_at=? WHERE event_id=? AND status='running'",
            (_now(), event["event_id"]),
        )
        conn.execute(
            "UPDATE strategy_portfolio_candidates SET status='formal_validation_queued',updated_at=? WHERE portfolio_id=?",
            (_now(), payload.get("portfolio_id")),
        )
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "status": "formal_validation_queued",
        "portfolio_id": payload.get("portfolio_id"),
        "strategy_id": int(strategy["id"]),
        "model_version_id": model_version_id,
        "version": version_name,
    }


def build_dynamic_market_environments(
    *,
    evidence_hash: str,
    historical_windows: list[dict[str, Any]],
    observed_dimensions: dict[str, list[str]] | None = None,
    extreme_windows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build an evidence-bound environment library instead of fixed S0-S3 buckets."""
    dimensions = observed_dimensions or {}
    environments: list[dict[str, Any]] = []

    def add(kind: str, name: str, specification: dict[str, Any], source: dict[str, Any]) -> None:
        identity = {
            "schema": RESEARCH_SCHEMA_VERSION,
            "evidence_hash": evidence_hash,
            "kind": kind,
            "name": name,
            "specification": specification,
        }
        environments.append({
            "environment_id": f"market-environment-{_hash(identity)[:20]}",
            "evidence_hash": evidence_hash,
            "environment_type": kind,
            "name": name,
            "specification": specification,
            "source_contract": source,
        })

    for window in historical_windows:
        add("historical_rolling", f"historical:{window.get('start')}:{window.get('end')}", dict(window), {
            "point_in_time": True,
            "synthetic": False,
        })
    for dimension, values in sorted(dimensions.items()):
        for value in sorted({str(item) for item in values if str(item)}):
            add("observed_state_slice", f"{dimension}:{value}", {"dimension": dimension, "value": value}, {
                "point_in_time": True,
                "synthetic": False,
                "discovery": "observed_dimension_registry_v1",
            })
    for window in extreme_windows or []:
        add("extreme_historical_replay", f"extreme:{window.get('start')}:{window.get('end')}", dict(window), {
            "point_in_time": True,
            "synthetic": False,
            "selection_rule": str(window.get("selection_rule") or "historical_tail_event"),
        })
    stress_specs = (
        ("liquidity_contraction", {"volume_capacity_multiplier": 0.35, "slippage_multiplier": 2.5}),
        ("transaction_cost_jump", {"fee_multiplier": 2.0, "slippage_multiplier": 3.0}),
        ("rapid_regime_switch", {"state_transition_lag_sessions": 3}),
        ("correlation_spike", {"cross_asset_correlation_floor": 0.75}),
        ("state_misclassification", {"route_to_second_probability_state": True}),
    )
    for name, perturbation in stress_specs:
        add("calibrated_stress", name, perturbation, {
            "point_in_time": True,
            "synthetic": True,
            "calibration_required": True,
            "price_or_return_fabrication_forbidden": True,
        })
    return environments


def discover_extreme_historical_windows(data_as_of: str) -> list[dict[str, Any]]:
    from app.services.backtest_data_source import BacktestDataSource

    source = BacktestDataSource()
    dates = source.get_available_trade_dates("2000-01-01", str(data_as_of))[-251:]
    returns = source.get_index_returns("000300.SH", str(data_as_of), lookback=250)
    usable = min(len(returns), max(0, len(dates) - 1))
    if usable < 40:
        return []
    dates = dates[-(usable + 1):]
    returns = returns[-usable:]
    windows: list[dict[str, Any]] = []
    ranked_drawdowns: list[tuple[float, int, int]] = []
    ranked_volatility: list[tuple[float, int, int]] = []
    width = 20
    for start in range(0, usable - width + 1):
        values = returns[start:start + width]
        compound = 1.0
        for value in values:
            compound *= 1.0 + float(value)
        mean_value = sum(values) / len(values)
        variance = sum((float(value) - mean_value) ** 2 for value in values) / max(1, len(values) - 1)
        ranked_drawdowns.append((compound - 1.0, start, start + width))
        ranked_volatility.append((math.sqrt(variance), start, start + width))
    selected = [
        (*min(ranked_drawdowns, key=lambda item: item[0]), "rolling_20d_worst_return"),
        (*max(ranked_volatility, key=lambda item: item[0]), "rolling_20d_highest_volatility"),
    ]
    seen: set[tuple[str, str]] = set()
    for _score, start, end, rule in selected:
        window = (str(dates[start]), str(dates[min(end, len(dates) - 1)]))
        if window in seen:
            continue
        seen.add(window)
        windows.append({
            "start": window[0],
            "end": window[1],
            "sessions": width,
            "selection_rule": rule,
            "point_in_time_cutoff": str(data_as_of),
        })
    return windows


FAILURE_MODEL_ROUTING: tuple[tuple[str, str], ...] = (
    ("regime", "market_regime_hsmm_v1"),
    ("transition", "market_regime_hsmm_v1"),
    ("ranking", "medium_horizon_lambdarank_v1"),
    ("market_exposure", "medium_horizon_lambdarank_v1"),
    ("capital", "capital_persistence_multitask_v1"),
    ("liquidity", "capital_persistence_multitask_v1"),
    ("leader", "leader_lambdarank_v1"),
    ("event", "event_impact_assessor_v2"),
    ("narrative", "narrative_transition_multitask_v1"),
)


def _preferred_factor_categories(failure_type: str) -> list[str]:
    text = str(failure_type or "").lower()
    if "liquidity" in text or "capital" in text or "cost" in text:
        return ["量价", "资金", "流动性", "波动"]
    if "regime" in text or "transition" in text:
        return ["趋势", "波动", "反转", "交互"]
    if "event" in text or "narrative" in text:
        return ["事件", "叙事", "基本面", "交互"]
    if "ranking" in text or "exposure" in text:
        return ["价值", "质量", "趋势", "规模", "低波"]
    return []


def targeted_model_training_request(
    *,
    strategy_hash: str,
    environment_id: str | None,
    attribution: dict[str, Any],
    data_cutoff: str,
) -> dict[str, Any] | None:
    failure_type = str(attribution.get("primary_gap") or attribution.get("failure_type") or "unknown")
    searchable = " ".join((failure_type, _json(attribution))).lower()
    model_key = next((model for token, model in FAILURE_MODEL_ROUTING if token in searchable), None)
    if model_key is None or model_key not in set(MODEL_KEYS) | {"market_regime_hsmm_v1"}:
        return None
    identity = {
        "strategy_hash": strategy_hash,
        "environment_id": environment_id,
        "failure_type": failure_type,
        "model_key": model_key,
        "data_cutoff": data_cutoff,
    }
    return {
        "request_id": f"targeted-model-training-{_hash(identity)[:20]}",
        "schema_version": "alis_strategy_failure_targeted_model_training_v1",
        "model_key": model_key,
        "strategy_hash": strategy_hash,
        "environment_id": environment_id,
        "failure_type": failure_type,
        "training_objective": {
            "target_environment": environment_id,
            "target_market_states": sorted(_environment_state_map([environment_id])) if environment_id else [],
            "sample_reweighting": "increase_failed_environment_weight_with_point_in_time_cap_v1",
            "comparison": "candidate_vs_current_model_in_complete_strategy_package",
        },
        "data_cutoff": data_cutoff,
        "production_change_forbidden": True,
    }


def record_failure_memory(
    *,
    strategy_hash: str,
    environment_id: str | None,
    attribution: dict[str, Any],
    evidence: dict[str, Any],
    data_cutoff: str,
) -> dict[str, Any]:
    ensure_strategy_research_tables()
    attribution = dict(attribution)
    failure_type = str(attribution.get("primary_gap") or attribution.get("failure_type") or "unknown")
    attribution.setdefault("preferred_factor_categories", _preferred_factor_categories(failure_type))
    identity = {
        "strategy_hash": strategy_hash,
        "environment_id": environment_id,
        "failure_type": failure_type,
        "evidence_hash": _hash(evidence),
    }
    memory_id = f"strategy-failure-{_hash(identity)[:20]}"
    request = targeted_model_training_request(
        strategy_hash=strategy_hash,
        environment_id=environment_id,
        attribution=attribution,
        data_cutoff=data_cutoff,
    )
    now = _now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO strategy_failure_memories(
               memory_id,strategy_hash,environment_id,failure_type,attribution_json,
               evidence_json,model_training_request_json,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(memory_id) DO UPDATE SET
               attribution_json=excluded.attribution_json,
               evidence_json=excluded.evidence_json,
               model_training_request_json=excluded.model_training_request_json,
               updated_at=excluded.updated_at""",
            (
                memory_id, strategy_hash, environment_id, failure_type,
                _json(attribution), _json(evidence), _json(request or {}),
                "training_requested" if request else "research_memory", now, now,
            ),
        )
    return {
        "memory_id": memory_id,
        "failure_type": failure_type,
        "model_training_request": request,
        "status": "training_requested" if request else "research_memory",
    }


def dispatch_targeted_model_training(
    *,
    limit: int = 3,
    plan_trainer: Callable[[str], dict[str, Any]] | None = None,
    regime_trainer: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consume failure-attribution requests without granting production authority."""
    ensure_strategy_research_tables()
    from app.services.continuous_factor_research import _active_research_snapshot

    current_snapshot_id = str(
        _active_research_snapshot().get("snapshot_id") or ""
    )
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_failure_memories
               WHERE status='training_requested'
               ORDER BY created_at ASC LIMIT ?""",
            (5000,),
        ).fetchall()]
    matching_rows: list[dict[str, Any]] = []
    for row in rows:
        evidence = json.loads(row.get("evidence_json") or "{}")
        memory_snapshot_id = str(
            evidence.get("research_snapshot_id")
            or evidence.get("snapshot_id")
            or ""
        )
        if memory_snapshot_id != current_snapshot_id:
            continue
        matching_rows.append(row)
    eligible_rows = matching_rows[:max(1, min(int(limit), 20))]
    default_plan_trainer = plan_trainer is None
    if default_plan_trainer:
        from app.services.plan_model_control import request_plan_model_training
        plan_trainer = request_plan_model_training
    if regime_trainer is None:
        from app.services.hsmm_shadow import train_hsmm_shadow
        regime_trainer = train_hsmm_shadow
    results: list[dict[str, Any]] = []
    for row in eligible_rows:
        request = json.loads(row.get("model_training_request_json") or "{}")
        model_key = str(request.get("model_key") or "")
        try:
            result = (
                regime_trainer()
                if model_key == "market_regime_hsmm_v1"
                else plan_trainer(model_key, training_context=request)
                if default_plan_trainer
                else plan_trainer(model_key)
            )
            started = bool(result.get("ok", True)) and (
                bool(result.get("started", True))
                or str(result.get("reason") or "") == "training_in_progress"
            )
            status = "training_dispatched" if started else "training_blocked"
        except Exception as exc:  # noqa: BLE001 - persist and retry through the event line
            result = {"ok": False, "error": str(exc)}
            status = "training_requested"
        evidence = json.loads(row.get("evidence_json") or "{}")
        evidence["training_dispatch"] = {
            "request": request,
            "result": result,
            "dispatched_at": _now(),
        }
        with _conn() as conn:
            conn.execute(
                """UPDATE strategy_failure_memories
                   SET evidence_json=?,status=?,updated_at=? WHERE memory_id=?""",
                (_json(evidence), status, _now(), row["memory_id"]),
            )
        results.append({
            "memory_id": row["memory_id"],
            "model_key": model_key,
            "status": status,
            "result": result,
        })
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "dispatched": results,
        "research_snapshot_id": current_snapshot_id,
        "stale_request_count": len(rows) - len(matching_rows),
    }


def persist_market_environments(environments: list[dict[str, Any]]) -> int:
    ensure_strategy_research_tables()
    now = _now()
    with _conn() as conn:
        for item in environments:
            conn.execute(
                """INSERT INTO dynamic_market_environments(
                   environment_id,evidence_hash,environment_type,name,specification_json,
                   source_contract_json,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(environment_id) DO UPDATE SET
                   specification_json=excluded.specification_json,
                   source_contract_json=excluded.source_contract_json,
                   status=excluded.status,updated_at=excluded.updated_at""",
                (
                    item["environment_id"], item["evidence_hash"], item["environment_type"],
                    item["name"], _json(item["specification"]), _json(item["source_contract"]),
                    "active", now, now,
                ),
            )
    return len(environments)


def continuous_strategy_research_status(*, console: bool = False) -> dict[str, Any]:
    with _conn() as conn:
        event_rows = [dict(row) for row in conn.execute(
            """SELECT research_line,event_type,status,COUNT(*) AS count
               FROM strategy_research_events
               GROUP BY research_line,event_type,status
               ORDER BY research_line,event_type,status"""
        ).fetchall()]
        environment_rows = [dict(row) for row in conn.execute(
            """SELECT environment_type,status,COUNT(*) AS count
               FROM dynamic_market_environments
               GROUP BY environment_type,status ORDER BY environment_type,status"""
        ).fetchall()]
        failure_rows = [dict(row) for row in conn.execute(
            """SELECT failure_type,status,COUNT(*) AS count
               FROM strategy_failure_memories
               GROUP BY failure_type,status ORDER BY failure_type,status"""
        ).fetchall()]
        prototype_rows = [dict(row) for row in conn.execute(
            """SELECT maturity_status,COUNT(*) AS count
               FROM strategy_prototypes GROUP BY maturity_status ORDER BY maturity_status"""
        ).fetchall()]
        portfolio_rows = [dict(row) for row in conn.execute(
            """SELECT status,COUNT(*) AS count
               FROM strategy_portfolio_candidates GROUP BY status ORDER BY status"""
        ).fetchall()]
        raw_waves = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_research_waves
               ORDER BY created_at DESC LIMIT ?""",
            (40 if console else 100,),
        ).fetchall()]
        raw_experts = [dict(row) for row in conn.execute(
            """SELECT e.*,m.name AS environment_name,m.environment_type
               FROM strategy_environment_experts e
               LEFT JOIN dynamic_market_environments m
                 ON m.environment_id=e.environment_id
               ORDER BY e.environment_version DESC,e.environment_id,e.rank_no
               LIMIT ?""",
            (120 if console else 200,),
        ).fetchall()]
        raw_events = [dict(row) for row in conn.execute(
            """SELECT event_id,payload_json,status,created_at
               FROM strategy_research_events
               WHERE research_line='candidate_generation'
                 AND event_type='candidate_strategy_generated'
               ORDER BY created_at DESC LIMIT ?""",
            (40 if console else 200,),
        ).fetchall()]
        raw_backtests = [dict(row) for row in conn.execute(
            """SELECT b.candidate_id,b.environment_id,b.result_json,b.status,b.updated_at,
                      m.name AS environment_name,m.environment_type
                 FROM strategy_environment_backtests b
                 LEFT JOIN dynamic_market_environments m ON m.environment_id=b.environment_id
                ORDER BY b.updated_at DESC LIMIT ?""",
            (800 if console else 2000,),
        ).fetchall()]
    experts: list[dict[str, Any]] = []
    for row in raw_experts:
        evidence = json.loads(row.pop("evidence_json") or "{}")
        fitness_detail = json.loads(row.pop("fitness_json") or "{}")
        if console:
            # The research console renders factor weights and identity only;
            # nested backtest evidence remains available from Wave details.
            row["factor_count"] = int(evidence.get("factor_count") or 0)
            row["factor_weights"] = evidence.get("factor_weights") or {}
        else:
            row["fitness_detail"] = fitness_detail
            row.update(evidence)
        row["rank"] = int(row.pop("rank_no") or 0)
        experts.append(row)
    matrix: dict[str, list[dict[str, Any]]] = {}
    for row in raw_backtests:
        result = json.loads(row.get("result_json") or "{}")
        if console:
            metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
            result = {
                "environment_name": result.get("environment_name") or row.get("environment_name"),
                "environment_type": result.get("environment_type") or row.get("environment_type"),
                "environment_version": result.get("environment_version"),
                "environment_fitness": {
                    "fitness": (result.get("environment_fitness") or {}).get("fitness"),
                    "rankable": bool((result.get("environment_fitness") or {}).get("rankable")),
                },
                "metrics": {
                    key: metrics.get(key)
                    for key in ("total_return", "benchmark_return", "excess_return", "max_drawdown", "turnover_rate")
                    if metrics.get(key) is not None
                },
            }
        result.setdefault("status", row.get("status"))
        result.setdefault("environment_id", row.get("environment_id"))
        result.setdefault("updated_at", row.get("updated_at"))
        matrix.setdefault(str(row.get("candidate_id") or ""), []).append(result)
    combinations: list[dict[str, Any]] = []
    for event in raw_events[:24] if console else raw_events:
        payload = json.loads(event.get("payload_json") or "{}")
        environment_results = matrix.get(str(payload.get("candidate_id") or ""), [])
        if console:
            environment_results = environment_results[:8]
        rankable = [
            item for item in environment_results
            if bool((item.get("environment_fitness") or environment_fitness(item)).get("rankable"))
        ]
        combinations.append({
            "candidate_id": payload.get("candidate_id"),
            "snapshot_id": payload.get("snapshot_id"),
            "generation_mode": payload.get("generation_mode"),
            "factor_weights": payload.get("factor_weights") or {},
            "factor_count": int(payload.get("factor_count") or len(payload.get("factor_weights") or {})),
            "holding_period_sessions": payload.get("holding_period_sessions"),
            "environment_versions": sorted({
                str(item.get("environment_version") or "") for item in environment_results
                if str(item.get("environment_version") or "")
            }),
            "environment_results": environment_results,
            "fitness": (
                round(sum(
                    float((item.get("environment_fitness") or environment_fitness(item))["fitness"])
                    for item in rankable
                ) / len(rankable), 8)
                if rankable else None
            ),
            "status": event.get("status"),
            "created_at": event.get("created_at"),
        })
    waves: list[dict[str, Any]] = []
    for row in raw_waves:
        row["candidate_ids"] = json.loads(row.pop("candidate_ids_json") or "[]")
        row["terminal_count"] = int(row.get("completed_count") or 0) + int(
            row.get("failed_count") or 0
        )
        waves.append(row)
    enabled = strategy_research_enabled()
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "console": bool(console),
        "activation_status": "enabled" if enabled else "explicitly_disabled",
        "research_lines": list(RESEARCH_LINES),
        "generation_modes": list(GENERATION_MODES),
        "factor_count_policy": "dynamic_marginal_contribution_with_resource_ceiling",
        "market_environment_policy": "dynamic_evidence_bound_environment_registry",
        "events": event_rows,
        "environments": environment_rows,
        "failure_memories": failure_rows,
        "prototypes": prototype_rows,
        "portfolios": portfolio_rows,
        "waves": waves,
        "expert_pool": experts,
        "factor_combinations": combinations,
        "expert_pool_policy": {
            "one_champion_per_environment": True,
            "backup_count": 2,
            "ranking": "relative_environment_fitness",
            "absolute_research_threshold": None,
        },
        "safety": {
            "research_only": True,
            "direct_champion_write_forbidden": True,
            "formal_chain": ["purged_oos", "dsr", "sealed_holdout", "future_shadow"],
            "replace_only_after_confirmed_improvement": True,
        },
        "projection_limits": {
            "max_candidates": 24,
            "max_environments_per_candidate": 8,
            "detail_route": "/api/factors/research/analytics/waves/{wave_id}",
        } if console else None,
    }


def strategy_research_analytics_console() -> dict[str, Any]:
    """Return only the bounded evidence needed by the Wave console.

    The full analytics projection also materializes factor guidance and
    learning backfill. Those sections belong to dedicated research views and
    are not needed to render Wave status, expert governance, or similarity.
    """
    now = datetime.now().astimezone()
    with _conn() as conn:
        contracts = [dict(row) for row in conn.execute(
            """SELECT c.*,w.status AS wave_status,w.expected_count,w.completed_count,
                      w.failed_count,w.created_at AS wave_created_at,w.distilled_at
                 FROM strategy_research_wave_contracts c
                 LEFT JOIN strategy_research_waves w ON w.wave_id=c.wave_id
                ORDER BY c.created_at DESC LIMIT 40"""
        ).fetchall()]
        experts = [dict(row) for row in conn.execute(
            """SELECT e.*,m.name AS environment_name,m.environment_type
                 FROM strategy_environment_experts e
                 LEFT JOIN dynamic_market_environments m ON m.environment_id=e.environment_id
                ORDER BY e.environment_id,e.environment_version DESC,e.rank_no
                LIMIT 120"""
        ).fetchall()]

    waves: list[dict[str, Any]] = []
    for row in contracts:
        row["freeze"] = json.loads(row.pop("freeze_json") or "{}")
        row["quota"] = json.loads(row.pop("quota_json") or "{}")
        row["checkpoint"] = json.loads(row.pop("checkpoint_json") or "{}")
        waves.append(row)

    expert_governance: list[dict[str, Any]] = []
    by_environment: dict[str, list[dict[str, Any]]] = {}
    for row in experts:
        row["fitness_detail"] = json.loads(row.pop("fitness_json") or "{}")
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
        by_environment.setdefault(str(row["environment_id"]), []).append(row)
    for environment_id, rows in by_environment.items():
        champions = [row for row in rows if str(row.get("role") or "") == "champion"]
        fitnesses = [float(row.get("fitness") or 0.0) for row in champions]
        latest = champions[0] if champions else rows[0]
        age_days = _age_days_from_timestamp(latest.get("updated_at"), now=now)
        mean_fitness = sum(fitnesses) / len(fitnesses) if fitnesses else 0.0
        variance = (
            sum((value - mean_fitness) ** 2 for value in fitnesses) / len(fitnesses)
            if fitnesses else 0.0
        )
        expert_governance.append({
            "environment_id": environment_id,
            "prototype_id": latest.get("prototype_id"),
            "environment_version": latest.get("environment_version"),
            "fitness": float(latest.get("fitness") or 0.0),
            "version_observations": len(fitnesses),
            "ranking_stability": round(1.0 / (1.0 + math.sqrt(variance)), 8),
            "age_days": round(age_days, 4),
            "expiry_status": "expired" if age_days > EXPERT_MAX_AGE_DAYS else "fresh",
            "maximum_age_days": EXPERT_MAX_AGE_DAYS,
        })

    environment_similarity: list[dict[str, Any]] = []
    environment_ids = sorted(by_environment)
    for left_index, left in enumerate(environment_ids):
        left_champions = {
            str(row.get("prototype_id") or "")
            for row in by_environment[left]
            if str(row.get("role") or "") == "champion"
        }
        for right in environment_ids[left_index + 1:]:
            right_champions = {
                str(row.get("prototype_id") or "")
                for row in by_environment[right]
                if str(row.get("role") or "") == "champion"
            }
            union = left_champions | right_champions
            similarity = len(left_champions & right_champions) / len(union) if union else 0.0
            if similarity <= 0:
                continue
            environment_similarity.append({
                "environment_left": left,
                "environment_right": right,
                "champion_jaccard": round(similarity, 8),
                "research_weight_multiplier": 0.5 if similarity >= 0.80 else 0.75,
                "recommendation": "merge_review" if similarity >= 0.80 else "downweight_overlap",
            })
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_analytics_console_v1",
        "console": True,
        "waves": waves,
        "expert_governance": expert_governance,
        "environment_similarity": sorted(environment_similarity, key=lambda row: -float(row["champion_jaccard"])),
        "comparability_gate": {
            "required_dimensions": [
                "research_snapshot_id", "environment_version",
                "cost_contract", "execution_contract",
            ],
            "cross_contract_ranking_forbidden": True,
        },
    }


def strategy_research_analytics() -> dict[str, Any]:
    """Read-only research efficiency, factor graph and batch audit projection.

    Historical learning backfill and runtime-metric compaction are owned by
    the autonomous research worker.  A console GET must not scan historical
    candidate evidence or mutate the research database.
    """
    compaction = {
        "ok": True,
        "schema_version": "alis_research_metric_compaction_v1",
        "deferred": True,
        "reason": "由自治研究任务后台维护，读取页面不执行压缩或删除",
    }
    learning_backfill = {
        "ok": True,
        "schema_version": "alis_factor_learning_backfill_v1",
        "deferred": True,
        "reason": "由候选生成自治任务后台维护，读取页面不执行历史回填",
    }
    guidance = factor_combination_guidance(limit=500)
    now = datetime.now().astimezone()
    with _conn() as conn:
        contracts = [dict(row) for row in conn.execute(
            """SELECT c.*,w.status AS wave_status,w.expected_count,w.completed_count,
                      w.failed_count,w.created_at AS wave_created_at,w.distilled_at
               FROM strategy_research_wave_contracts c
               LEFT JOIN strategy_research_waves w ON w.wave_id=c.wave_id
               ORDER BY c.created_at DESC LIMIT 100"""
        ).fetchall()]
        metrics = [dict(row) for row in conn.execute(
            """SELECT metric_type,COUNT(*) AS observations,AVG(value) AS average,
                      MIN(value) AS minimum,MAX(value) AS maximum
               FROM strategy_research_runtime_metrics
               GROUP BY metric_type ORDER BY metric_type"""
        ).fetchall()]
        cache = dict(conn.execute(
            """SELECT COUNT(*) AS entries,COALESCE(SUM(hit_count),0) AS hits,
                      COUNT(DISTINCT candidate_hash) AS candidates,
                      COUNT(DISTINCT environment_version) AS environment_versions
               FROM strategy_environment_replay_cache"""
        ).fetchone())
        experts = [dict(row) for row in conn.execute(
            """SELECT environment_id,environment_version,prototype_id,rank_no,role,
                      fitness,fitness_json,updated_at
               FROM strategy_environment_experts
               ORDER BY environment_id,environment_version DESC,rank_no"""
        ).fetchall()]
    wave_rows: list[dict[str, Any]] = []
    for row in contracts:
        row["freeze"] = json.loads(row.pop("freeze_json") or "{}")
        row["quota"] = json.loads(row.pop("quota_json") or "{}")
        row["checkpoint"] = json.loads(row.pop("checkpoint_json") or "{}")
        wave_rows.append(row)
    expert_governance: list[dict[str, Any]] = []
    by_environment: dict[str, list[dict[str, Any]]] = {}
    for row in experts:
        by_environment.setdefault(str(row["environment_id"]), []).append(row)
    for environment_id, rows in by_environment.items():
        champions = [row for row in rows if str(row.get("role") or "") == "champion"]
        fitnesses = [float(row.get("fitness") or 0.0) for row in champions]
        latest = champions[0] if champions else rows[0]
        age_days = _age_days_from_timestamp(latest.get("updated_at"), now=now)
        mean = sum(fitnesses) / len(fitnesses) if fitnesses else 0.0
        variance = (
            sum((value - mean) ** 2 for value in fitnesses) / len(fitnesses)
            if fitnesses else 0.0
        )
        stability = 1.0 / (1.0 + math.sqrt(variance))
        expert_governance.append({
            "environment_id": environment_id,
            "prototype_id": latest.get("prototype_id"),
            "environment_version": latest.get("environment_version"),
            "fitness": float(latest.get("fitness") or 0.0),
            "version_observations": len(fitnesses),
            "ranking_stability": round(stability, 8),
            "age_days": round(age_days, 4),
            "expiry_status": "expired" if age_days > EXPERT_MAX_AGE_DAYS else "fresh",
            "maximum_age_days": EXPERT_MAX_AGE_DAYS,
        })
    environment_similarity: list[dict[str, Any]] = []
    environment_ids = sorted(by_environment)
    for left_index, left in enumerate(environment_ids):
        left_champions = {
            str(row.get("prototype_id") or "")
            for row in by_environment[left]
            if str(row.get("role") or "") == "champion"
        }
        for right in environment_ids[left_index + 1:]:
            right_champions = {
                str(row.get("prototype_id") or "")
                for row in by_environment[right]
                if str(row.get("role") or "") == "champion"
            }
            union = left_champions | right_champions
            similarity = len(left_champions & right_champions) / len(union) if union else 0.0
            if similarity <= 0:
                continue
            environment_similarity.append({
                "environment_left": left,
                "environment_right": right,
                "champion_jaccard": round(similarity, 8),
                "research_weight_multiplier": 0.5 if similarity >= 0.80 else 0.75,
                "recommendation": "merge_review" if similarity >= 0.80 else "downweight_overlap",
            })
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_analytics_v1",
        "guidance": guidance,
        "learning_backfill": learning_backfill,
        "factor_graph": {
            "nodes": guidance.get("factor_scores") or [],
            "edges": guidance.get("interactions") or [],
        },
        "runtime_metrics": metrics,
        "cache": {
            **cache,
            "contract": "candidate_hash_environment_version_snapshot_cost_v1",
        },
        "waves": wave_rows,
        "expert_governance": expert_governance,
        "environment_similarity": sorted(
            environment_similarity,
            key=lambda row: -float(row["champion_jaccard"]),
        ),
        "comparability_gate": {
            "required_dimensions": [
                "research_snapshot_id", "environment_version",
                "cost_contract", "execution_contract",
            ],
            "cross_contract_ranking_forbidden": True,
        },
        "execution": {
            "tiered_budget": True,
            "catastrophic_early_stop": True,
            "parallel_environment_capability": True,
            "parallelism": max(1, min(4, int(os.getenv("ALIS_V10_ENV_PARALLELISM", "1" if str(os.getenv("ALIS_WORKER_PROFILE", "")).lower() in {"2gb", "low_memory", "low-memory"} else "2")))),
            "archive_policy": "daily_rollup_after_30_days_with_bounded_read_projection",
            "metric_compaction": compaction,
        },
    }


def strategy_research_factor_analytics(
    *,
    factor_key: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    ensure_strategy_research_tables()
    bounded = max(1, min(int(limit), 1000))
    params: list[Any] = []
    where = ""
    if factor_key:
        where = "WHERE c.factor_key=?"
        params.append(str(factor_key))
    params.append(bounded)
    with _conn() as conn:
        heatmap_rows = [dict(row) for row in conn.execute(
            """SELECT c.factor_key,c.environment_id,
                      AVG(c.signed_contribution) AS mean_contribution,
                      COUNT(*) AS observations,
                      m.name AS environment_name
                 FROM strategy_factor_contributions c
                 LEFT JOIN dynamic_market_environments m ON m.environment_id=c.environment_id
                GROUP BY c.factor_key,c.environment_id,m.name"""
        ).fetchall()]
        rows = [dict(row) for row in conn.execute(
            f"""SELECT c.*,m.name AS environment_name,m.environment_type
                FROM strategy_factor_contributions c
                LEFT JOIN dynamic_market_environments m
                  ON m.environment_id=c.environment_id
                {where}
                ORDER BY c.updated_at DESC,c.absolute_contribution DESC LIMIT ?""",
            tuple(params),
        ).fetchall()]
        candidate_ids = sorted({
            str(row.get("candidate_id") or "")
            for row in rows
            if str(row.get("candidate_id") or "")
        })
        event_rows = []
        if candidate_ids:
            event_placeholders = ",".join("?" for _ in candidate_ids)
            event_rows = [dict(row) for row in conn.execute(
                f"""SELECT source_id,payload_json,created_at
                    FROM strategy_research_events
                   WHERE research_line='candidate_generation'
                     AND event_type='candidate_strategy_generated'
                     AND source_id IN ({event_placeholders})
                   ORDER BY created_at ASC""",
                tuple(candidate_ids),
            ).fetchall()]
    event_by_candidate: dict[str, dict[str, Any]] = {}
    for event in event_rows:
        payload = json.loads(event.get("payload_json") or "{}")
        event_by_candidate[str(payload.get("candidate_id") or event.get("source_id") or "")] = {
            "wave_id": payload.get("research_wave_id"),
            "created_at": event.get("created_at"),
        }
    by_factor_environment: dict[tuple[str, str], list[float]] = {}
    by_factor_wave: dict[tuple[str, str], list[float]] = {}
    enriched: list[dict[str, Any]] = []
    for row in rows:
        event = event_by_candidate.get(str(row.get("candidate_id") or ""), {})
        row["wave_id"] = event.get("wave_id")
        row["candidate_created_at"] = event.get("created_at")
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
        enriched.append(row)
        factor = str(row.get("factor_key") or "")
        environment = str(row.get("environment_id") or "")
        wave = str(row.get("wave_id") or "legacy")
        # Historical rows may use ``contribution``/``weighted_contribution``;
        # preserve those values instead of silently turning the heatmap cell
        # into zero when the newer column is absent.
        value = float(
            row.get("signed_contribution")
            or row.get("weighted_contribution")
            or row.get("contribution")
            or 0.0
        )
        by_factor_environment.setdefault((factor, environment), []).append(value)
        by_factor_wave.setdefault((factor, wave), []).append(value)
    heatmap = [{
        "factor_key": str(row.get("factor_key") or ""),
        "environment_id": str(row.get("environment_id") or ""),
        "environment_key": str(row.get("environment_id") or ""),
        "environment_name": str(row.get("environment_name") or row.get("environment_id") or ""),
        "mean_contribution": round(float(row.get("mean_contribution") or 0.0), 8),
        "observations": int(row.get("observations") or 0),
    } for row in sorted(heatmap_rows, key=lambda item: (str(item.get("factor_key")), str(item.get("environment_id"))))]
    probability_trend = [{
        "factor_key": key[0],
        "wave_id": key[1],
        "mean_contribution": round(sum(values) / len(values), 8),
        "observations": len(values),
    } for key, values in sorted(by_factor_wave.items())]
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_factor_analytics_v1",
        "factor_key": factor_key,
        "items": enriched,
        "heatmap": heatmap,
        "trend": probability_trend,
        "returned": len(enriched),
    }


def strategy_research_timeseries(*, limit: int = 500) -> dict[str, Any]:
    ensure_strategy_research_tables()
    bounded = max(1, min(int(limit), 2000))
    with _conn() as conn:
        raw = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_research_runtime_metrics
               ORDER BY observed_at DESC LIMIT ?""",
            (bounded,),
        ).fetchall()]
        rollups = [dict(row) for row in conn.execute(
            """SELECT * FROM strategy_research_metric_rollups
               ORDER BY metric_date DESC,metric_type LIMIT ?""",
            (bounded,),
        ).fetchall()]
        early_stops = [dict(row) for row in conn.execute(
            """SELECT candidate_id,environment_id,result_json,updated_at
               FROM strategy_environment_backtests
               WHERE status LIKE ?
               ORDER BY updated_at DESC LIMIT ?""",
            ("early_stopped%", bounded),
        ).fetchall()]
        backtest_rows = [dict(row) for row in conn.execute(
            """SELECT candidate_id,result_json,updated_at
               FROM strategy_environment_backtests
               ORDER BY updated_at DESC LIMIT ?""",
            (bounded,),
        ).fetchall()]
    for row in raw:
        row["dimensions"] = json.loads(row.pop("dimensions_json") or "{}")
    for row in early_stops:
        result = json.loads(row.pop("result_json") or "{}")
        row.update(result)
    slow = sorted(
        [row for row in raw if row.get("metric_type") == "candidate_runtime_seconds"],
        key=lambda row: -float(row.get("value") or 0.0),
    )[:50]
    runtime_by_candidate = {
        str(row.get("candidate_id") or ""): float(row.get("value") or 0.0)
        for row in raw if row.get("metric_type") == "candidate_runtime_seconds"
    }
    fitness_by_candidate: dict[str, list[float]] = {}
    updated_by_candidate: dict[str, str] = {}
    for row in backtest_rows:
        result = json.loads(row.get("result_json") or "{}")
        fitness = result.get("environment_fitness") or {}
        if not fitness.get("rankable") or fitness.get("fitness") is None:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        fitness_by_candidate.setdefault(candidate_id, []).append(float(fitness["fitness"]))
        updated_by_candidate[candidate_id] = str(row.get("updated_at") or "")
    efficiency = []
    for candidate_id, values in fitness_by_candidate.items():
        runtime = runtime_by_candidate.get(candidate_id)
        mean_fitness = sum(values) / len(values)
        efficiency.append({
            "candidate_id": candidate_id,
            "observed_at": updated_by_candidate.get(candidate_id),
            "mean_fitness": round(mean_fitness, 8),
            "runtime_seconds": runtime,
            "fitness_per_compute_second": (
                round(max(0.0, mean_fitness) / runtime, 10)
                if runtime and runtime > 0 else None
            ),
            "environment_count": len(values),
        })
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_timeseries_v1",
        "items": raw,
        "rollups": rollups,
        "early_stops": early_stops,
        "slow_candidates": slow,
        "efficiency": sorted(
            efficiency,
            key=lambda row: str(row.get("observed_at") or ""),
        ),
    }


def strategy_research_wave_detail(wave_id: str) -> dict[str, Any]:
    ensure_strategy_research_tables()
    wave = refresh_research_wave(str(wave_id))
    if not wave:
        return {"ok": False, "status": "not_found", "wave_id": str(wave_id)}
    contract = _wave_contract(str(wave_id))
    candidate_ids = list(wave.get("candidate_ids") or [])
    candidates: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        event = _candidate_event(str(candidate_id)) or {}
        payload = json.loads(event.get("payload_json") or "{}")
        with _conn() as conn:
            backtests = [dict(row) for row in conn.execute(
                """SELECT environment_id,result_json,status,updated_at
                   FROM strategy_environment_backtests
                   WHERE candidate_id=? ORDER BY environment_id""",
                (str(candidate_id),),
            ).fetchall()]
            cache_rows = [dict(row) for row in conn.execute(
                """SELECT environment_id,hit_count,updated_at
                   FROM strategy_environment_replay_cache
                   WHERE candidate_hash=?""",
                (str(payload.get("candidate_hash") or ""),),
            ).fetchall()]
        cache_by_environment = {
            str(row["environment_id"]): row for row in cache_rows
        }
        environment_ids = list(payload.get("environment_ids") or [])
        result_by_environment = {
            str(row["environment_id"]): row for row in backtests
        }
        environments = []
        for environment_id in environment_ids:
            result_row = result_by_environment.get(str(environment_id))
            result = json.loads(result_row.get("result_json") or "{}") if result_row else {}
            cache = cache_by_environment.get(str(environment_id))
            environments.append({
                "environment_id": str(environment_id),
                "environment_name": result.get("environment_name"),
                "environment_type": result.get("environment_type"),
                "status": (
                    result_row.get("status") if result_row
                    else "running" if str(event.get("status") or "") == "running"
                    else "queued"
                ),
                "fitness": (result.get("environment_fitness") or {}).get("fitness"),
                "cache_hit_count": int((cache or {}).get("hit_count") or 0),
                "updated_at": (
                    (result_row or {}).get("updated_at")
                    or (cache or {}).get("updated_at")
                ),
            })
        candidates.append({
            "candidate_id": str(candidate_id),
            "candidate_hash": payload.get("candidate_hash"),
            "generation_mode": payload.get("generation_mode"),
            "factor_count": payload.get("factor_count"),
            "status": event.get("status") or "missing",
            "environment_completed": len(backtests),
            "environment_expected": len(environment_ids),
            "environments": environments,
        })
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_wave_detail_v1",
        "wave": wave,
        "contract": contract,
        "candidates": candidates,
    }


def strategy_research_expert_history(*, limit: int = 500) -> dict[str, Any]:
    ensure_strategy_research_tables()
    bounded = max(1, min(int(limit), 2000))
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT h.*,m.name AS environment_name,m.environment_type
               FROM strategy_environment_expert_history h
               LEFT JOIN dynamic_market_environments m
                 ON m.environment_id=h.environment_id
               ORDER BY h.observed_at DESC,h.environment_id,h.rank_no LIMIT ?""",
            (bounded,),
        ).fetchall()]
    for row in rows:
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_expert_history_v1",
        "items": rows,
    }


def strategy_research_archive_status() -> dict[str, Any]:
    ensure_strategy_research_tables()
    with _conn() as conn:
        detail = dict(conn.execute(
            """SELECT COUNT(*) AS count,MIN(observed_at) AS earliest,
                      MAX(observed_at) AS latest
               FROM strategy_research_runtime_metrics"""
        ).fetchone())
        rollup = dict(conn.execute(
            """SELECT COUNT(*) AS count,MIN(metric_date) AS earliest,
                      MAX(metric_date) AS latest
               FROM strategy_research_metric_rollups"""
        ).fetchone())
        backtests = dict(conn.execute(
            """SELECT COUNT(*) AS count,MIN(updated_at) AS earliest,
                      MAX(updated_at) AS latest
               FROM strategy_environment_backtests"""
        ).fetchone())
    return {
        "ok": True,
        "schema_version": "alis_strategy_research_archive_status_v1",
        "runtime_detail": detail,
        "daily_rollups": rollup,
        "environment_backtests": backtests,
        "retention_days": 30,
    }

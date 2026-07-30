from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
import hashlib
import json
import math
import multiprocessing
import os
import random
from statistics import mean
from typing import Any, Callable

from app.db.store import _conn, get_model_version, list_model_versions
from app.services.backtest_data_source import BacktestDataSource
from app.services.factor_platform import registered_factor_bindings
from app.services.model_evaluation_contracts import (
    SCHEMA_VERSION as MODEL_EVALUATION_CONTRACT,
)
from app.services.model_signal_fusion import MODEL_KEYS, freeze_model_bindings
from app.services.model_artifacts import model_artifact_exists
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.strategy_config import get_strategy_factor_names
from app.services.strategy_replay import run_strategy_replay


RESEARCH_SCHEMA_VERSION = "alis_continuous_factor_model_strategy_research_v9"
MAX_EXPERIMENTS_PER_SNAPSHOT = 32
MAX_GENERATIONS_PER_SNAPSHOT = MAX_EXPERIMENTS_PER_SNAPSHOT - 1
MAX_EPOCHS_PER_EVIDENCE = 3
DEFAULT_CANDIDATES_PER_TASK = 1
RESEARCH_FOLD_SESSIONS = 15
RESEARCH_FOLD_COUNT = 2
QUICK_SCREEN_TARGET = 24
CONFIRMATION_ELITE_COUNT = 3
CONFIRMATION_FOLD_SESSIONS = 30
CONFIRMATION_FOLD_COUNT = 3
MAX_REPLAY_SECONDS = 900
MIN_FACTOR_COMBINATION_SIZE = 3
MAX_FACTOR_COMBINATION_SIZE = 8
EXPLORATION_FACTOR_SET_SIMILARITY_LIMIT = 0.72
RESEARCH_CLUSTER_SIMILARITY_THRESHOLD = 0.62


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _research_evidence_signature(identity: dict[str, Any]) -> str:
    return _hash({
        "research_schema_version": RESEARCH_SCHEMA_VERSION,
        **identity,
    })


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def ensure_continuous_research_tables() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_research_snapshots (
               snapshot_id TEXT PRIMARY KEY,
               data_as_of TEXT NOT NULL,
               factor_universe_hash TEXT NOT NULL,
               factor_count INTEGER NOT NULL,
               research_windows_json TEXT NOT NULL,
               contract_json TEXT NOT NULL,
               created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_combination_experiments (
               experiment_id TEXT PRIMARY KEY,
               snapshot_id TEXT NOT NULL,
               generation INTEGER NOT NULL,
               parent_experiment_id TEXT,
               mutation_type TEXT NOT NULL,
               research_stage TEXT NOT NULL DEFAULT 'quick_screen',
               strategy_hash TEXT NOT NULL,
               factor_weights_json TEXT NOT NULL,
               windows_json TEXT NOT NULL,
               metrics_json TEXT NOT NULL,
               fitness REAL NOT NULL,
               status TEXT NOT NULL,
               error TEXT,
               created_at TEXT NOT NULL,
               finished_at TEXT
            )"""
        )
        if conn._is_postgres:
            column_exists = conn.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name='factor_combination_experiments'
                     AND column_name='research_stage'"""
            ).fetchone()
        else:
            column_exists = next(
                (
                    row for row in conn.execute(
                        "PRAGMA table_info(factor_combination_experiments)"
                    ).fetchall()
                    if str(row["name"]) == "research_stage"
                ),
                None,
            )
        if not column_exists:
            conn.execute(
                "ALTER TABLE factor_combination_experiments ADD COLUMN research_stage TEXT NOT NULL DEFAULT 'quick_screen'"
            )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_factor_combination_experiments_snapshot
               ON factor_combination_experiments(snapshot_id,fitness,generation)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_research_population (
               snapshot_id TEXT NOT NULL,
               experiment_id TEXT NOT NULL,
               rank_no INTEGER NOT NULL,
               role TEXT NOT NULL,
               fitness REAL NOT NULL,
               evidence_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(snapshot_id,experiment_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_research_clusters (
               snapshot_id TEXT NOT NULL,
               cluster_id TEXT NOT NULL,
               representative_experiment_id TEXT NOT NULL,
               member_count INTEGER NOT NULL,
               factor_centroid_json TEXT NOT NULL,
               behavior_centroid_json TEXT NOT NULL,
               evidence_json TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               PRIMARY KEY(snapshot_id,cluster_id)
            )"""
        )


def _supersede_incompatible_running_experiments() -> list[str]:
    superseded: list[str] = []
    now = _now()
    status = f"superseded_by_research_contract_{RESEARCH_SCHEMA_VERSION.rsplit('_', 1)[-1]}"
    with _conn() as conn:
        rows = conn.execute(
            """SELECT e.experiment_id,s.contract_json
               FROM factor_combination_experiments e
               JOIN factor_research_snapshots s ON s.snapshot_id=e.snapshot_id
               WHERE e.status='running'"""
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            contract = _loads(row.get("contract_json"), {})
            if str(contract.get("schema_version") or "") == RESEARCH_SCHEMA_VERSION:
                continue
            experiment_id = str(row.get("experiment_id") or "")
            if not experiment_id:
                continue
            conn.execute(
                """UPDATE factor_combination_experiments
                   SET status=?,error=?,finished_at=?
                   WHERE experiment_id=? AND status='running'""",
                (
                    status,
                    f"superseded by {RESEARCH_SCHEMA_VERSION}",
                    now,
                    experiment_id,
                ),
            )
            superseded.append(experiment_id)
    return superseded


def _factor_universe() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT id,factor_key,version,status,name_cn,category,formula_json,
                          inputs_json,direction,window_sessions,available_time_rule,
                          missing_policy,leakage_status
                   FROM factor_definitions
                   WHERE status IN ('active','candidate')
                     AND leakage_status='passed'
                     AND category<>'交易约束'
                   ORDER BY category,factor_key,version"""
            ).fetchall()
        ]
    return [{
        **row,
        "id": int(row.get("id") or 0),
        "direction": int(row.get("direction") or 1),
        "window_sessions": int(row.get("window_sessions") or 1),
        "formula": _loads(row.pop("formula_json", "{}"), {}),
        "inputs": _loads(row.pop("inputs_json", "[]"), []),
    } for row in rows]


def _frozen_factor_bindings(
    factors: list[dict[str, Any]],
    selection_strategy: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    bindings = {
        str(item["factor_key"]): {
            "definition_id": int(item["id"]),
            "factor_key": str(item["factor_key"]),
            "version": str(item["version"]),
            "status": str(item["status"]),
            "formula": item.get("formula") or {},
            "inputs": item.get("inputs") or [],
            "available_time_rule": str(item.get("available_time_rule") or ""),
            "missing_policy": str(item.get("missing_policy") or ""),
            "leakage_status": str(item.get("leakage_status") or ""),
        }
        for item in factors
    }
    content = (
        selection_strategy.get("content")
        if isinstance(selection_strategy.get("content"), dict) else {}
    )
    strategy_type = str(selection_strategy.get("type") or "formula")
    required = get_strategy_factor_names(content, strategy_type)
    required.update({"momentum_20", "momentum_60", "volatility_20"})
    universe = content.get("universe") if isinstance(content.get("universe"), dict) else {}
    if float(universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
        required.add("float_mv")
    for factor_key, binding in registered_factor_bindings(required).items():
        bindings.setdefault(str(factor_key), deepcopy(binding))
    missing = sorted(required - set(bindings))
    if missing:
        raise RuntimeError(f"研究快照缺少策略必需因子绑定：{','.join(missing)}")
    return dict(sorted(bindings.items()))


def _version_ref(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        "id": int(version["id"]),
        "model_key": str(version.get("model_key") or ""),
        "version": str(version.get("version") or ""),
        "status": str(version.get("status") or ""),
        "artifact_path": str(version.get("artifact_path") or ""),
        "created_at": str(version.get("created_at") or ""),
    }


def _research_model_bundle(
    _champion: dict[str, Any],
) -> dict[str, dict[str, Any] | None]:
    """Freeze only models with current production authority.

    The Champion package keeps historical model IDs for deterministic replay.
    Passing those IDs as an explicit bundle intentionally bypasses the live
    production audit, so they must never seed a new research snapshot.
    """

    return freeze_model_bindings()


def _model_bundle_identity(
    bindings: dict[str, Any] | None,
) -> dict[str, int | None]:
    payload = bindings if isinstance(bindings, dict) else {}
    return {
        key: (
            int((payload.get(key) or {}).get("id") or 0) or None
            if isinstance(payload.get(key), dict)
            else None
        )
        for key in MODEL_KEYS
    }


def _snapshot_model_contract_current(contract: dict[str, Any]) -> bool:
    package = (
        contract.get("research_package")
        if isinstance(contract.get("research_package"), dict)
        else {}
    )
    frozen = package.get("model_bindings")
    current = _research_model_bundle(resolve_production_strategy_champion())
    return _model_bundle_identity(frozen) == _model_bundle_identity(current)


def _model_candidate_pool(
    baseline: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in MODEL_KEYS:
        baseline_id = int((baseline.get(key) or {}).get("id") or 0)
        added = 0
        for version in list_model_versions(key, limit=10):
            version_id = int(version.get("id") or 0)
            artifact_path = str(version.get("artifact_path") or "")
            metrics = (
                version.get("metrics")
                if isinstance(version.get("metrics"), dict)
                else {}
            )
            validation = (
                version.get("validation")
                if isinstance(version.get("validation"), dict)
                else {}
            )
            task_native_oos = (
                metrics.get("task_native_oos")
                if isinstance(metrics.get("task_native_oos"), dict)
                else {}
            )
            review = (
                validation.get("review")
                if isinstance(validation.get("review"), dict)
                else {}
            )
            if version_id <= 0 or version_id == baseline_id:
                continue
            if str(version.get("status") or "") != "candidate":
                continue
            if str(metrics.get("model_evaluation_contract") or "") != MODEL_EVALUATION_CONTRACT:
                continue
            if not bool(task_native_oos.get("passed")) or not bool(
                task_native_oos.get("baseline_outperformed")
            ):
                continue
            if str(validation.get("status") or "") in {"failed", "rejected"}:
                continue
            if str(review.get("status") or "") == "rejected":
                continue
            if not artifact_path or not model_artifact_exists(artifact_path):
                continue
            candidates.append({
                **(_version_ref(version) or {}),
                "validation_status": str(validation.get("status") or ""),
                "review_status": str(review.get("status") or ""),
            })
            added += 1
            if added >= 2:
                break
    return candidates


def _controller_candidate_pool(baseline_id: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for version in list_model_versions("strategy_controller", limit=20):
        if int(version.get("id") or 0) == int(baseline_id):
            continue
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        if not isinstance(config.get("policy"), dict):
            continue
        candidates.append(_version_ref(version) or {})
        if len(candidates) >= 3:
            break
    return candidates


def _market_state_candidate_pool() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for version in list_model_versions("market_regime", limit=5):
        artifact_path = str(version.get("artifact_path") or "")
        validation = version.get("validation") if isinstance(version.get("validation"), dict) else {}
        if str(validation.get("status") or "") != "passed":
            continue
        if not artifact_path or not model_artifact_exists(artifact_path):
            continue
        candidates.append({
            "production_model": "market_regime_hsmm_v1",
            "change_model": "bocpd_gaussian_v1",
            "hsmm_shadow": {
                **(_version_ref(version) or {}),
                "validation_status": "passed",
            },
        })
        if len(candidates) >= 2:
            break
    return candidates


def _prior_population_seed(snapshot_id: str | None) -> list[dict[str, Any]]:
    if not snapshot_id:
        return []
    with _conn() as conn:
        cluster_rows = [dict(row) for row in conn.execute(
            """SELECT 'cluster_representative' AS role,e.fitness,e.factor_weights_json,
                      e.metrics_json,e.experiment_id,e.strategy_hash
               FROM factor_research_clusters c
               JOIN factor_combination_experiments e
                 ON e.experiment_id=c.representative_experiment_id
               WHERE c.snapshot_id=?
               ORDER BY e.fitness DESC LIMIT 12""",
            (snapshot_id,),
        ).fetchall()]
        population_rows = [dict(row) for row in conn.execute(
            """SELECT p.role,p.fitness,e.factor_weights_json,e.metrics_json,
                      e.experiment_id,e.strategy_hash
               FROM factor_research_population p
               JOIN factor_combination_experiments e ON e.experiment_id=p.experiment_id
               WHERE p.snapshot_id=? ORDER BY p.rank_no ASC LIMIT 12""",
            (snapshot_id,),
        ).fetchall()]
    rows = []
    used: set[str] = set()
    for row in cluster_rows + population_rows:
        experiment_id = str(row.get("experiment_id") or "")
        if not experiment_id or experiment_id in used:
            continue
        used.add(experiment_id)
        rows.append(row)
        if len(rows) >= 12:
            break
    return [{
        "source_experiment_id": str(row.get("experiment_id") or ""),
        "strategy_hash": str(row.get("strategy_hash") or ""),
        "role": str(row.get("role") or ""),
        "fitness": float(row.get("fitness") or 0.0),
        "factor_weights": _loads(row.get("factor_weights_json"), {}),
        "research_spec": (_loads(row.get("metrics_json"), {}).get("research_spec") or {}),
    } for row in rows]


def freeze_research_snapshot(
    data_source: BacktestDataSource | None = None,
    *,
    require_new_evidence: bool = False,
) -> dict[str, Any]:
    """Freeze factors, model bundle, regime, controller and replay contracts."""
    ensure_continuous_research_tables()
    _supersede_incompatible_running_experiments()
    ds = data_source or BacktestDataSource()
    dates = ds.get_available_trade_dates("2000-01-01", datetime.now().date().isoformat())
    minimum = RESEARCH_FOLD_SESSIONS * RESEARCH_FOLD_COUNT + 1
    if len(dates) < minimum:
        raise RuntimeError("连续因子研究可用交易日不足")
    factors = _factor_universe()
    if len(factors) < 3:
        raise RuntimeError("连续因子研究至少需要三个可用因子")
    windows: list[dict[str, Any]] = []
    for offset in range(RESEARCH_FOLD_COUNT, 0, -1):
        end_index = len(dates) - 1 - (offset - 1) * RESEARCH_FOLD_SESSIONS
        start_index = end_index - RESEARCH_FOLD_SESSIONS + 1
        windows.append({
            "index": len(windows) + 1,
            "start": str(dates[start_index]),
            "end": str(dates[end_index]),
            "sessions": RESEARCH_FOLD_SESSIONS,
        })
    factor_identity = [{
        "id": item["id"], "factor_key": item["factor_key"], "version": item["version"],
        "status": item["status"], "formula": item["formula"],
        "inputs": item.get("inputs") or [],
        "direction": int(item.get("direction") or 1),
        "window_sessions": int(item.get("window_sessions") or 1),
        "available_time_rule": str(item.get("available_time_rule") or ""),
        "missing_policy": str(item.get("missing_policy") or ""),
        "leakage_status": str(item.get("leakage_status") or ""),
    } for item in factors]
    factor_hash = _hash(factor_identity)
    champion = resolve_production_strategy_champion()
    champion_strategy = (
        champion.get("strategy")
        if isinstance(champion.get("strategy"), dict) else {}
    )
    selection_strategy = {
        "id": int(champion_strategy.get("id") or champion.get("strategy_id") or 0),
        "name": str(champion_strategy.get("name") or ""),
        "type": str(champion_strategy.get("type") or "formula"),
        "content": deepcopy(
            champion_strategy.get("content")
            if isinstance(champion_strategy.get("content"), dict) else {}
        ),
    }
    factor_bindings = _frozen_factor_bindings(factors, selection_strategy)
    model_bundle = _research_model_bundle(champion)
    model_candidates = _model_candidate_pool(model_bundle)
    controller_id = int(champion.get("strategy_controller_version_id") or 0)
    controller_candidates = _controller_candidate_pool(controller_id)
    market_state_candidates = _market_state_candidate_pool()
    market_state_binding = {
        "production_model": "transparent_four_state_v1",
        "change_model": "bocpd_gaussian_v1",
        "hsmm_shadow": (
            deepcopy(market_state_candidates[0]["hsmm_shadow"])
            if market_state_candidates else None
        ),
    }
    evidence_identity = {
        "data_as_of": str(dates[-1]),
        "champion_version_id": int(champion.get("champion_id") or 0),
        "champion_version": str(champion.get("champion_version") or ""),
        "selection_strategy": selection_strategy,
        "factor_universe_hash": factor_hash,
        "factor_binding_hash": _hash(factor_bindings),
        "model_bundle": model_bundle,
        "model_candidate_ids": [int(item["id"]) for item in model_candidates],
        "controller_version_id": controller_id,
        "controller_candidate_ids": [int(item["id"]) for item in controller_candidates],
        "market_state_binding": market_state_binding,
        "market_state_candidate_ids": [
            int((item.get("hsmm_shadow") or {}).get("id") or 0)
            for item in market_state_candidates
        ],
        "cost_policy": "strategy_replay_a_share_cost_v1",
        "portfolio_optimizer": champion.get("portfolio_optimizer") or {},
        "execution_contract": champion.get("execution_contract") or {},
    }
    evidence_signature = _research_evidence_signature(evidence_identity)
    with _conn() as conn:
        previous_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM factor_research_snapshots ORDER BY created_at DESC"
        ).fetchall()]
    same_evidence = [
        row for row in previous_rows
        if (_loads(row.get("contract_json"), {}) or {}).get("evidence_signature") == evidence_signature
    ]
    if require_new_evidence and same_evidence:
        latest = same_evidence[0]
        previous_contract = _loads(latest.get("contract_json"), {})
        return {
            "snapshot_id": str(latest["snapshot_id"]),
            "data_as_of": str(latest["data_as_of"]),
            "factor_universe_hash": str(latest["factor_universe_hash"]),
            "factor_count": int(latest["factor_count"]),
            "research_windows": _loads(latest.get("research_windows_json"), []),
            "contract": previous_contract,
            "factors": previous_contract.get("factor_universe") or factors,
        }
    if len(same_evidence) >= MAX_EPOCHS_PER_EVIDENCE:
        latest = same_evidence[0]
        previous_contract = _loads(latest.get("contract_json"), {})
        return {
            "snapshot_id": str(latest["snapshot_id"]),
            "data_as_of": str(latest["data_as_of"]),
            "factor_universe_hash": str(latest["factor_universe_hash"]),
            "factor_count": int(latest["factor_count"]),
            "research_windows": _loads(latest.get("research_windows_json"), []),
            "contract": previous_contract,
            "factors": previous_contract.get("factor_universe") or factors,
        }
    parent_snapshot_id = (
        str(same_evidence[0]["snapshot_id"])
        if same_evidence
        else str(previous_rows[0]["snapshot_id"]) if previous_rows else None
    )
    epoch_no = len(same_evidence) + 1
    research_package = {
        "schema_version": "alis_factor_model_strategy_research_package_v1",
        "champion_reference": str(champion.get("champion_version") or ""),
        "champion_version_id": int(champion.get("champion_id") or 0),
        "selection_strategy": selection_strategy,
        "factor_bindings": factor_bindings,
        "model_bindings": model_bundle,
        "market_state_binding": market_state_binding,
        "strategy_controller": {
            "id": controller_id,
            "version": str(champion.get("strategy_controller_version") or ""),
        },
        "portfolio_optimizer": champion.get("portfolio_optimizer") or {},
        "execution_contract": champion.get("execution_contract") or {},
        "cost_policy": "strategy_replay_a_share_cost_v1",
        "benchmark": "000300.SH",
    }
    contract = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "data_as_of": str(dates[-1]),
        "factor_universe_hash": factor_hash,
        "evidence_signature": evidence_signature,
        "epoch_no": epoch_no,
        "maximum_epochs_per_evidence": MAX_EPOCHS_PER_EVIDENCE,
        "parent_snapshot_id": parent_snapshot_id,
        "research_windows": windows,
        "cost_policy": "strategy_replay_a_share_cost_v1",
        "benchmark": "000300.SH",
        "search_use_only": True,
        "formal_oos_evidence": False,
        "trial_budget": MAX_EXPERIMENTS_PER_SNAPSHOT,
        "quick_screen": {
            "target": QUICK_SCREEN_TARGET,
            "fold_count": RESEARCH_FOLD_COUNT,
            "sessions_per_fold": RESEARCH_FOLD_SESSIONS,
        },
        "confirmation": {
            "elite_count": CONFIRMATION_ELITE_COUNT,
            "fold_count": CONFIRMATION_FOLD_COUNT,
            "sessions_per_fold": CONFIRMATION_FOLD_SESSIONS,
        },
        "factor_universe": factors,
        "research_package": research_package,
        "model_candidate_pool": model_candidates,
        "controller_candidate_pool": controller_candidates,
        "market_state_candidate_pool": market_state_candidates,
        "portfolio_candidate_pool": ["equal_weight", "hrp"],
        "seed_population": _prior_population_seed(parent_snapshot_id),
        "optimization_lanes": ["factor", "model", "strategy_control"],
        "factor_search_contract": {
            "exploration_ratio": 0.5,
            "exploitation_ratio": 0.5,
            "exploration_mode": "deterministic_category_balanced_random_v1",
            "exploitation_modes": ["cluster_lineage_mutation", "cross_cluster_recombination"],
            "minimum_factor_count": MIN_FACTOR_COMBINATION_SIZE,
            "maximum_factor_count": MAX_FACTOR_COMBINATION_SIZE,
            "factor_set_similarity_limit": EXPLORATION_FACTOR_SET_SIMILARITY_LIMIT,
            "cluster_schema": "alis_explainable_factor_research_cluster_v1",
            "cluster_similarity_threshold": RESEARCH_CLUSTER_SIMILARITY_THRESHOLD,
        },
    }
    snapshot_id = (
        f"factor-model-strategy-{str(dates[-1]).replace('-', '')}-"
        f"e{epoch_no}-{_hash(contract)[:12]}"
    )
    now = _now()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT snapshot_id FROM factor_research_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO factor_research_snapshots(
                   snapshot_id,data_as_of,factor_universe_hash,factor_count,
                   research_windows_json,contract_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id, str(dates[-1]), factor_hash, len(factors),
                    _json(windows), _json(contract), now,
                ),
            )
    return {
        "snapshot_id": snapshot_id,
        "data_as_of": str(dates[-1]),
        "factor_universe_hash": factor_hash,
        "factor_count": len(factors),
        "research_windows": windows,
        "contract": contract,
        "factors": factors,
    }


def _active_research_snapshot(
    data_source: BacktestDataSource | None = None,
    *,
    refresh_evidence: bool = False,
) -> dict[str, Any]:
    """Reuse the current frozen epoch until its search budget is consumed."""
    ensure_continuous_research_tables()
    if refresh_evidence:
        return freeze_research_snapshot(
            data_source=data_source,
            require_new_evidence=True,
        )
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM factor_research_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row:
        item = dict(row)
        contract = _loads(item.get("contract_json"), {})
        factors = contract.get("factor_universe") if isinstance(contract, dict) else None
        experiments = _experiment_rows(str(item["snapshot_id"]))
        max_generation = max(
            (int(experiment.get("generation") or 0) for experiment in experiments),
            default=-1,
        )
        if (
            isinstance(factors, list)
            and len(factors) >= 3
            and contract.get("schema_version") == RESEARCH_SCHEMA_VERSION
            and all(
                int(window.get("sessions") or 0) == RESEARCH_FOLD_SESSIONS
                for window in _loads(item.get("research_windows_json"), [])
            )
            and _snapshot_model_contract_current(contract)
            and not _research_epoch_terminal(experiments)
            and len(experiments) < MAX_EXPERIMENTS_PER_SNAPSHOT
            and max_generation < MAX_GENERATIONS_PER_SNAPSHOT
        ):
            return {
                "snapshot_id": str(item["snapshot_id"]),
                "data_as_of": str(item["data_as_of"]),
                "factor_universe_hash": str(item["factor_universe_hash"]),
                "factor_count": int(item["factor_count"]),
                "research_windows": _loads(item.get("research_windows_json"), []),
                "contract": contract,
                "factors": factors,
            }
    # The first closed loop deliberately reuses the existing factor universe.
    # New evidence, not unconditional discovery, opens the next cohort.
    return freeze_research_snapshot(data_source=data_source)


def _experiment_rows(snapshot_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT * FROM factor_combination_experiments
                   WHERE snapshot_id=? ORDER BY fitness DESC,created_at ASC""",
                (snapshot_id,),
            ).fetchall()
        ]
    for row in rows:
        row["factor_weights"] = _loads(row.pop("factor_weights_json", "{}"), {})
        row["windows"] = _loads(row.pop("windows_json", "[]"), [])
        row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
        row["research_spec"] = row["metrics"].get("research_spec") or {}
        row["generation"] = int(row.get("generation") or 0)
        row["fitness"] = float(row.get("fitness") or 0.0)
        row["research_stage"] = str(row.get("research_stage") or "quick_screen")
    return rows


def _proposal_from_running_experiment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": int(row.get("generation") or 0),
        "parent_experiment_id": row.get("parent_experiment_id"),
        "mutation_type": str(row.get("mutation_type") or "resume_interrupted"),
        "research_stage": str(row.get("research_stage") or "quick_screen"),
        "strategy_hash": str(row.get("strategy_hash") or ""),
        "factor_weights": deepcopy(row.get("factor_weights") or {}),
        "research_windows": deepcopy(row.get("windows") or []),
        "research_spec": deepcopy(row.get("research_spec") or {}),
    }


def _research_trial_count(snapshot_id: str) -> int:
    return max(1, len(_experiment_rows(str(snapshot_id))))


def _research_epoch_complete(experiments: list[dict[str, Any]]) -> bool:
    quick = [
        item for item in experiments
        if item.get("research_stage") == "quick_screen"
        and item.get("status") == "completed"
    ]
    confirmation = [
        item for item in experiments
        if item.get("research_stage") == "confirmation"
        and item.get("status") in {"completed", "blocked_point_in_time", "failed"}
    ]
    return len(quick) >= QUICK_SCREEN_TARGET and len(confirmation) >= CONFIRMATION_ELITE_COUNT


def _research_epoch_terminal(experiments: list[dict[str, Any]]) -> bool:
    if any(str(item.get("status") or "") == "running" for item in experiments):
        return False
    if _research_epoch_complete(experiments):
        return True
    quick = [
        item for item in experiments
        if item.get("research_stage") == "quick_screen"
        and item.get("status") == "completed"
    ]
    if len(quick) < QUICK_SCREEN_TARGET:
        return False
    confirmed_parent_ids = {
        str(item.get("parent_experiment_id") or "")
        for item in experiments if item.get("research_stage") == "confirmation"
    }
    retained_count = sum(
        bool(
            ((((item.get("metrics") or {}).get("aggregate") or {}).get("research_disposition") or {}).get("retain", True))
        )
        for item in quick
    )
    return len(confirmed_parent_ids) >= min(CONFIRMATION_ELITE_COUNT, retained_count)


def _next_epoch_allowed(snapshot: dict[str, Any], population: list[dict[str, Any]]) -> bool:
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    epoch_no = int(contract.get("epoch_no") or MAX_EPOCHS_PER_EVIDENCE)
    maximum = int(contract.get("maximum_epochs_per_evidence") or MAX_EPOCHS_PER_EVIDENCE)
    return bool(population) and epoch_no < maximum


def _normalized_weights(raw: dict[str, float]) -> dict[str, float]:
    cleaned = {str(key): float(value) for key, value in raw.items() if abs(float(value)) > 1e-12}
    total = sum(abs(value) for value in cleaned.values())
    return {
        key: round(value / total, 10)
        for key, value in sorted(cleaned.items())
    } if total > 0 else {}


def _seed_weights(factors: list[dict[str, Any]], index: int) -> dict[str, float]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for factor in factors:
        categories.setdefault(str(factor["category"]), []).append(factor)
    selected: list[dict[str, Any]] = []
    category_names = sorted(categories)
    category_offset = index % len(category_names) if category_names else 0
    ordered_categories = category_names[category_offset:] + category_names[:category_offset]
    category_round = index // max(1, len(category_names))
    for category_index, category in enumerate(ordered_categories):
        options = categories[category]
        selected.append(options[(category_round + category_index) % len(options)])
        if len(selected) >= 5:
            break
    if len(selected) < 3:
        selected = factors[index % len(factors):] + factors[:index % len(factors)]
        selected = selected[:min(5, len(selected))]
    return _normalized_weights({
        str(item["factor_key"]): float(item.get("direction") or 1)
        for item in selected
    })


def _factor_set_hash(weights: dict[str, float]) -> str:
    return _hash(sorted(str(key) for key, value in weights.items() if abs(float(value)) > 1e-12))


def _factor_set_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    left_keys = {str(key) for key, value in left.items() if abs(float(value)) > 1e-12}
    right_keys = {str(key) for key, value in right.items() if abs(float(value)) > 1e-12}
    union = left_keys | right_keys
    return len(left_keys & right_keys) / len(union) if union else 0.0


def _deterministic_exploration_weights(
    *,
    snapshot_id: str,
    factors: list[dict[str, Any]],
    exploration_index: int,
    attempt: int,
) -> tuple[dict[str, float], str]:
    seed_material = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "snapshot_id": str(snapshot_id),
        "exploration_index": int(exploration_index),
        "attempt": int(attempt),
    }
    seed_hex = _hash(seed_material)[:16]
    rng = random.Random(int(seed_hex, 16))
    categories: dict[str, list[dict[str, Any]]] = {}
    for factor in factors:
        categories.setdefault(str(factor.get("category") or "unknown"), []).append(factor)
    category_names = sorted(categories)
    rng.shuffle(category_names)
    maximum = min(MAX_FACTOR_COMBINATION_SIZE, len(factors))
    target_size = rng.randint(MIN_FACTOR_COMBINATION_SIZE, maximum)
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for category in category_names:
        options = sorted(categories[category], key=lambda item: str(item["factor_key"]))
        item = options[rng.randrange(len(options))]
        key = str(item["factor_key"])
        if key not in used:
            selected.append(item)
            used.add(key)
        if len(selected) >= target_size:
            break
    remaining = [
        item for item in sorted(factors, key=lambda value: str(value["factor_key"]))
        if str(item["factor_key"]) not in used
    ]
    rng.shuffle(remaining)
    selected.extend(remaining[:max(0, target_size - len(selected))])
    weights = _normalized_weights({
        str(item["factor_key"]): (
            float(item.get("direction") or 1) * rng.uniform(0.6, 1.4)
        )
        for item in selected
    })
    return weights, seed_hex


def _cross_cluster_weights(
    left: dict[str, float],
    right: dict[str, float],
) -> dict[str, float]:
    combined = {
        key: (float(left.get(key, 0.0)) + float(right.get(key, 0.0))) / 2.0
        for key in set(left) | set(right)
    }
    ranked = sorted(combined.items(), key=lambda item: (-abs(item[1]), item[0]))
    return _normalized_weights(dict(ranked[:MAX_FACTOR_COMBINATION_SIZE]))


def _mutate_weights(
    parent: dict[str, float],
    factors: list[dict[str, Any]],
    generation: int,
) -> tuple[dict[str, float], str]:
    universe = {str(item["factor_key"]): item for item in factors}
    mutation = generation % 4
    weights = dict(parent)
    if mutation == 0 and len(weights) > 3:
        weakest = min(weights, key=lambda key: (abs(weights[key]), key))
        weights.pop(weakest)
        mutation_type = "drop_weakest"
    elif mutation == 1:
        available = [key for key in sorted(universe) if key not in weights]
        if available and len(weights) < MAX_FACTOR_COMBINATION_SIZE:
            key = available[generation % len(available)]
            weights[key] = 0.25 * int(universe[key].get("direction") or 1)
        mutation_type = "add_novel_factor"
    elif mutation == 2 and weights:
        key = sorted(weights)[generation % len(weights)]
        weights[key] *= 1.5
        mutation_type = "tilt_existing_factor"
    else:
        available = [key for key in sorted(universe) if key not in weights]
        if available and weights:
            removed = sorted(weights)[generation % len(weights)]
            weights.pop(removed)
            replacement = available[generation % len(available)]
            weights[replacement] = 0.25 * int(universe[replacement].get("direction") or 1)
        mutation_type = "replace_factor"
    return _normalized_weights(weights), mutation_type


def _baseline_research_spec(snapshot: dict[str, Any]) -> dict[str, Any]:
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    package = deepcopy(contract.get("research_package") or {})
    controller = package.get("strategy_controller") if isinstance(package.get("strategy_controller"), dict) else {}
    return {
        "schema_version": "alis_factor_model_strategy_candidate_v1",
        "champion_reference": str(package.get("champion_reference") or ""),
        "champion_version_id": int(package.get("champion_version_id") or 0),
        "selection_strategy": deepcopy(package.get("selection_strategy") or {}),
        "factor_bindings": deepcopy(package.get("factor_bindings") or {}),
        "model_bindings": deepcopy(package.get("model_bindings") or {key: None for key in MODEL_KEYS}),
        "market_state_binding": deepcopy(package.get("market_state_binding") or {
            "production_model": "transparent_four_state_v1",
            "change_model": "bocpd_gaussian_v1",
        }),
        "strategy_controller_version_id": int(controller.get("id") or 0) or None,
        "portfolio_method": "cvar",
        "portfolio_optimizer": deepcopy(package.get("portfolio_optimizer") or {}),
        "execution_contract": deepcopy(package.get("execution_contract") or {}),
        "cost_policy": str(package.get("cost_policy") or "strategy_replay_a_share_cost_v1"),
        "benchmark": str(package.get("benchmark") or "000300.SH"),
        "optimization_lane": "factor",
        "source_experiment_id": None,
    }


def _proposal_identity(
    snapshot: dict[str, Any],
    *,
    weights: dict[str, float],
    research_spec: dict[str, Any],
    research_stage: str,
) -> tuple[str, dict[str, Any]]:
    package = {
        **deepcopy(research_spec),
        "snapshot_id": str(snapshot["snapshot_id"]),
        "research_stage": research_stage,
        "factor_weights": weights,
        "rebalance": {"freq": "weekly", "top_n": 30},
    }
    identity = deepcopy(package)
    for lineage_key in (
        "research_package_hash",
        "source_experiment_id",
        "optimization_lane",
    ):
        identity.pop(lineage_key, None)
    package_hash = _hash(identity)
    package["research_package_hash"] = package_hash
    return package_hash, package


def propose_next_combinations(snapshot: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
    existing = _experiment_rows(str(snapshot["snapshot_id"]))
    generation = max((int(item["generation"]) for item in existing), default=-1) + 1
    factors = list(snapshot["factors"])
    proposals: list[dict[str, Any]] = []
    seen_hashes = {str(item.get("strategy_hash") or "") for item in existing}
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    model_candidates = list(contract.get("model_candidate_pool") or [])
    controller_candidates = list(contract.get("controller_candidate_pool") or [])
    market_state_candidates = list(contract.get("market_state_candidate_pool") or [])
    portfolio_candidates = [
        str(item) for item in (contract.get("portfolio_candidate_pool") or [])
        if str(item) in {"cvar", "equal_weight", "hrp"}
    ]
    strategy_control_candidates = [
        {"kind": "controller", "value": item} for item in controller_candidates
    ] + [
        {"kind": "market_state", "value": item} for item in market_state_candidates
    ] + [
        {"kind": "portfolio", "value": item} for item in portfolio_candidates
    ]
    seed_population = list(contract.get("seed_population") or [])
    parents = [
        item for item in existing
        if str(item.get("status") or "") == "completed"
        and bool(
            ((((item.get("metrics") or {}).get("aggregate") or {}).get("research_disposition") or {}).get("retain", True))
        )
    ][:12]
    lanes = ["factor"]
    if model_candidates:
        lanes.append("model")
    if strategy_control_candidates:
        lanes.append("strategy_control")
    lane_attempt_counts = {
        lane: sum(
            str((item.get("research_spec") or {}).get("optimization_lane") or "factor") == lane
            for item in existing
        )
        for lane in lanes
    }
    lane_candidate_offsets = dict(lane_attempt_counts)
    attempts = 0
    while len(proposals) < max(1, int(count)) and attempts < max(100, int(count) * 50):
        attempts += 1
        lane = min(
            lanes,
            key=lambda item: (
                lane_attempt_counts[item],
                (lanes.index(item) - generation) % len(lanes),
            ),
        )
        parent = None
        cross_parent = None
        prior_seed = None
        exploration_seed = None
        factor_search_mode = "fixed_for_causal_comparison"
        base_spec = _baseline_research_spec(snapshot)
        weights: dict[str, float]
        if lane == "factor":
            factor_slot = lane_attempt_counts["factor"]
            if factor_slot % 2 == 0:
                factor_search_mode = "deterministic_random_exploration"
                weights, exploration_seed = _deterministic_exploration_weights(
                    snapshot_id=str(snapshot["snapshot_id"]),
                    factors=factors,
                    exploration_index=factor_slot // 2,
                    attempt=attempts,
                )
                prior_factor_weights = [
                    item.get("factor_weights") or {}
                    for item in existing
                    if str((item.get("research_spec") or {}).get("optimization_lane") or "factor") == "factor"
                ] + [item.get("factor_weights") or {} for item in proposals]
                maximum_similarity = max(
                    (_factor_set_similarity(weights, item) for item in prior_factor_weights),
                    default=0.0,
                )
                if maximum_similarity > EXPLORATION_FACTOR_SET_SIMILARITY_LIMIT:
                    continue
                mutation_type = "explore_new_factor_set"
            else:
                factor_search_mode = "cluster_lineage_exploitation"
                exploitation_index = factor_slot // 2
                cluster_parents = _cluster_representative_rows(str(snapshot["snapshot_id"]))
                exploitation_parents = cluster_parents or parents
                if exploitation_index % 2 == 1 and len(exploitation_parents) >= 2:
                    parent = exploitation_parents[exploitation_index % len(exploitation_parents)]
                    cross_parent = exploitation_parents[(exploitation_index + 1) % len(exploitation_parents)]
                    weights = _cross_cluster_weights(
                        parent.get("factor_weights") or {},
                        cross_parent.get("factor_weights") or {},
                    )
                    mutation_type = "cross_cluster_recombine"
                    factor_search_mode = "cross_cluster_recombination"
                else:
                    parent = (
                        exploitation_parents[exploitation_index % len(exploitation_parents)]
                        if exploitation_parents else None
                    )
                    parent_weights = parent.get("factor_weights") if parent else None
                    if not parent_weights:
                        weights, exploration_seed = _deterministic_exploration_weights(
                            snapshot_id=str(snapshot["snapshot_id"]),
                            factors=factors,
                            exploration_index=exploitation_index,
                            attempt=attempts,
                        )
                        mutation_type = "explore_new_factor_set"
                        factor_search_mode = "deterministic_random_exploration_fallback"
                    else:
                        weights, mutation_type = _mutate_weights(
                            _normalized_weights(parent_weights), factors, generation + attempts
                        )
                if parent and isinstance(parent.get("research_spec"), dict):
                    base_spec.update(deepcopy(parent["research_spec"]))
        else:
            parent = parents[attempts % len(parents)] if parents else None
            prior_seed = seed_population[attempts % len(seed_population)] if not parent and seed_population else None
            if parent and isinstance(parent.get("research_spec"), dict):
                base_spec.update(deepcopy(parent["research_spec"]))
            elif prior_seed and isinstance(prior_seed.get("research_spec"), dict):
                base_spec.update(deepcopy(prior_seed["research_spec"]))
            parent_weights = (
                parent.get("factor_weights") if parent
                else prior_seed.get("factor_weights") if prior_seed
                else None
            )
            weights = _normalized_weights(parent_weights or {}) if parent_weights else _seed_weights(
                factors, generation * max(1, int(count)) + attempts
            )
        parent_id = str(parent["experiment_id"]) if parent else None
        source_experiment_id = (
            parent_id or str((prior_seed or {}).get("source_experiment_id") or "") or None
        )
        if lane == "model":
            bindings = deepcopy(base_spec.get("model_bindings") or {})
            candidate = None
            start = lane_candidate_offsets["model"] % len(model_candidates)
            lane_candidate_offsets["model"] += 1
            for offset in range(len(model_candidates)):
                item = model_candidates[(start + offset) % len(model_candidates)]
                current = bindings.get(str(item["model_key"]))
                current_id = int((current or {}).get("id") or 0) if isinstance(current, dict) else 0
                if current_id != int(item.get("id") or 0):
                    candidate = item
                    break
            if candidate is None:
                continue
            bindings[str(candidate["model_key"])] = deepcopy(candidate)
            base_spec["model_bindings"] = bindings
            mutation_type = f"replace_model:{candidate['model_key']}"
        elif lane == "strategy_control":
            candidate = strategy_control_candidates[
                lane_candidate_offsets["strategy_control"] % len(strategy_control_candidates)
            ]
            lane_candidate_offsets["strategy_control"] += 1
            if candidate["kind"] == "controller":
                base_spec["strategy_controller_version_id"] = int(candidate["value"]["id"])
                mutation_type = "replace_strategy_controller"
            elif candidate["kind"] == "market_state":
                base_spec["market_state_binding"] = deepcopy(candidate["value"])
                mutation_type = "replace_market_state:market_regime_hsmm_v1"
            else:
                base_spec["portfolio_method"] = str(candidate["value"])
                mutation_type = f"replace_portfolio_method:{candidate['value']}"
        base_spec.update({
            "optimization_lane": lane,
            "source_experiment_id": source_experiment_id,
            "source_experiment_ids": [
                value for value in (
                    source_experiment_id,
                    str(cross_parent["experiment_id"]) if cross_parent else None,
                ) if value
            ],
            "source_cluster_ids": [
                value for value in (
                    str(parent.get("research_cluster_id") or "") if parent else None,
                    str(cross_parent.get("research_cluster_id") or "") if cross_parent else None,
                ) if value
            ],
            "factor_search_mode": factor_search_mode,
            "factor_set_hash": _factor_set_hash(weights),
            "factor_set_size": len(weights),
            "exploration_seed": exploration_seed,
            "factor_set_similarity_limit": (
                EXPLORATION_FACTOR_SET_SIMILARITY_LIMIT
                if factor_search_mode.startswith("deterministic_random_exploration") else None
            ),
        })
        strategy_hash, research_spec = _proposal_identity(
            snapshot,
            weights=weights,
            research_spec=base_spec,
            research_stage="quick_screen",
        )
        if len(weights) < 3 or strategy_hash in seen_hashes:
            continue
        seen_hashes.add(strategy_hash)
        lane_attempt_counts[lane] += 1
        proposals.append({
            "generation": generation,
            "parent_experiment_id": parent_id,
            "mutation_type": mutation_type,
            "research_stage": "quick_screen",
            "strategy_hash": strategy_hash,
            "factor_weights": weights,
            "research_spec": research_spec,
        })
    return proposals


def _confirmation_windows(data_as_of: str) -> list[dict[str, Any]]:
    dates = BacktestDataSource().get_available_trade_dates("2000-01-01", data_as_of)
    required = CONFIRMATION_FOLD_SESSIONS * CONFIRMATION_FOLD_COUNT
    if len(dates) < required:
        raise RuntimeError("连续因子复筛可用交易日不足")
    windows: list[dict[str, Any]] = []
    for offset in range(CONFIRMATION_FOLD_COUNT, 0, -1):
        end_index = len(dates) - 1 - (offset - 1) * CONFIRMATION_FOLD_SESSIONS
        start_index = end_index - CONFIRMATION_FOLD_SESSIONS + 1
        windows.append({
            "index": len(windows) + 1,
            "start": str(dates[start_index]),
            "end": str(dates[end_index]),
            "sessions": CONFIRMATION_FOLD_SESSIONS,
        })
    return windows


def propose_confirmation_candidate(
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    rows = _experiment_rows(str(snapshot["snapshot_id"]))
    quick = [
        item for item in rows
        if item["research_stage"] == "quick_screen"
        and item["status"] == "completed"
        and bool(
            ((((item.get("metrics") or {}).get("aggregate") or {}).get("research_disposition") or {}).get("retain", True))
        )
    ]
    confirmed_parent_ids = {
        str(item.get("parent_experiment_id") or "")
        for item in rows if item["research_stage"] == "confirmation"
    }
    cluster_representative_ids = [
        str(item["representative_experiment_id"])
        for item in refresh_research_clusters(str(snapshot["snapshot_id"]))
    ]
    quick_by_id = {str(item["experiment_id"]): item for item in quick}
    ordered_quick = [
        quick_by_id[experiment_id]
        for experiment_id in cluster_representative_ids
        if experiment_id in quick_by_id
    ]
    ordered_quick.extend(
        item for item in quick
        if str(item["experiment_id"]) not in {
            str(value["experiment_id"]) for value in ordered_quick
        }
    )
    parent = next(
        (
            item for item in ordered_quick
            if str(item["experiment_id"]) not in confirmed_parent_ids
        ),
        None,
    )
    if not parent:
        return None
    windows = _confirmation_windows(str(snapshot["data_as_of"]))
    base_spec = _baseline_research_spec(snapshot)
    if isinstance(parent.get("research_spec"), dict):
        base_spec.update(deepcopy(parent["research_spec"]))
    base_spec.update({
        "optimization_lane": str(base_spec.get("optimization_lane") or "factor"),
        "source_experiment_id": str(parent["experiment_id"]),
        "confirmation_windows_hash": _hash(windows),
    })
    strategy_hash, research_spec = _proposal_identity(
        snapshot,
        weights=parent["factor_weights"],
        research_spec=base_spec,
        research_stage="confirmation",
    )
    return {
        "generation": int(parent["generation"]),
        "parent_experiment_id": str(parent["experiment_id"]),
        "mutation_type": "confirmation_replay",
        "research_stage": "confirmation",
        "strategy_hash": strategy_hash,
        "factor_weights": parent["factor_weights"],
        "research_windows": windows,
        "research_spec": research_spec,
    }


def _fitness(folds: list[dict[str, Any]], *, trial_count: int) -> tuple[float, dict[str, Any]]:
    excess = [float(item.get("excess_return") or 0.0) for item in folds]
    returns = [float(item.get("total_return") or 0.0) for item in folds]
    drawdowns = [float(item.get("max_drawdown") or 0.0) for item in folds]
    fees = [float(item.get("total_fees") or 0.0) for item in folds]
    positive_ratio = sum(value > 0 for value in excess) / max(1, len(excess))
    mean_excess = mean(excess) if excess else -1.0
    variance = mean((value - mean_excess) ** 2 for value in excess) if excess else 0.0
    instability = math.sqrt(variance)
    multiple_testing_penalty = 0.0025 * math.sqrt(2.0 * math.log(max(1, trial_count)))
    fee_penalty = sum(fees) / 200_000.0 / max(1, len(fees))
    components = {
        "mean_excess": mean_excess,
        "absolute_return": 0.25 * (mean(returns) if returns else -1.0),
        "positive_window_bonus": 0.05 * positive_ratio,
        "drawdown": 0.20 * (min(drawdowns) if drawdowns else -1.0),
        "instability_penalty": -0.50 * instability,
        "multiple_testing_penalty": -multiple_testing_penalty,
        "fee_penalty": -fee_penalty,
    }
    score = sum(components.values())
    return score, {
        "mean_excess_return": round(mean_excess, 8),
        "mean_total_return": round(mean(returns) if returns else 0.0, 8),
        "worst_drawdown": round(min(drawdowns) if drawdowns else 0.0, 8),
        "positive_excess_window_ratio": round(positive_ratio, 8),
        "excess_instability": round(instability, 8),
        "multiple_testing_penalty": round(multiple_testing_penalty, 8),
        "trial_count": int(trial_count),
        "fitness": round(score, 8),
        "fitness_components": {
            key: round(value, 8) for key, value in components.items()
        },
    }


def _market_state_summary(folds: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for state in ("S0", "S1", "S2", "S3"):
        observations = 0
        weighted_excess = 0.0
        weighted_return = 0.0
        worst_drawdown = 0.0
        positive_folds = 0
        valid_folds = 0
        for fold in folds:
            matrix = fold.get("market_state_matrix") if isinstance(fold.get("market_state_matrix"), dict) else {}
            item = (matrix.get("states") or {}).get(state) if isinstance(matrix.get("states"), dict) else {}
            count = int((item or {}).get("observations") or 0)
            if count <= 0:
                continue
            excess = float((item or {}).get("excess_return") or 0.0)
            total_return = float((item or {}).get("total_return") or 0.0)
            observations += count
            weighted_excess += excess * count
            weighted_return += total_return * count
            worst_drawdown = min(worst_drawdown, float((item or {}).get("max_drawdown") or 0.0))
            positive_folds += int(excess > 0)
            valid_folds += 1
        result[state] = {
            "observations": observations,
            "valid_folds": valid_folds,
            "mean_excess_return": round(weighted_excess / observations, 8) if observations else 0.0,
            "mean_total_return": round(weighted_return / observations, 8) if observations else 0.0,
            "worst_drawdown": round(worst_drawdown, 8),
            "positive_fold_ratio": round(positive_folds / valid_folds, 8) if valid_folds else 0.0,
        }
    return result


def _research_disposition(
    *,
    pit_passed: bool,
    aggregate: dict[str, Any],
    market_states: dict[str, Any],
) -> dict[str, Any]:
    """Keep weak first generations unless they are invalid or catastrophic everywhere."""
    if not pit_passed:
        return {
            "status": "failed_memory",
            "retain": False,
            "reason": "point_in_time_contract_failed",
        }
    mean_return = float(aggregate.get("mean_total_return") or 0.0)
    mean_excess = float(aggregate.get("mean_excess_return") or 0.0)
    worst_drawdown = float(aggregate.get("worst_drawdown") or 0.0)
    valid_states = [item for item in market_states.values() if int(item.get("observations") or 0) > 0]
    useful_state = any(
        float(item.get("mean_excess_return") or 0.0) > 0
        or float(item.get("worst_drawdown") or 0.0) > -0.20
        for item in valid_states
    )
    catastrophic = (
        mean_return <= -0.50
        and worst_drawdown <= -0.60
        and valid_states
        and not useful_state
    )
    if catastrophic:
        return {
            "status": "failed_memory",
            "retain": False,
            "reason": "catastrophic_in_all_observed_market_states",
        }
    if mean_excess > 0 or any(
        float(item.get("mean_excess_return") or 0.0) > 0 for item in valid_states
    ):
        return {"status": "active", "retain": True, "reason": "global_or_local_market_value"}
    return {"status": "incubating", "retain": True, "reason": "weak_first_generation_kept_for_learning"}


def _strategy_content(
    weights: dict[str, float],
    research_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frozen_strategy = (
        (research_spec or {}).get("selection_strategy")
        if isinstance((research_spec or {}).get("selection_strategy"), dict) else {}
    )
    if isinstance(frozen_strategy.get("content"), dict):
        content = deepcopy(frozen_strategy["content"])
    else:
        champion = resolve_production_strategy_champion()
        strategy = champion.get("strategy") if isinstance(champion.get("strategy"), dict) else {}
        content = deepcopy(strategy.get("content") if isinstance(strategy.get("content"), dict) else {})
    content["schema_version"] = "alis_continuous_factor_research_strategy_v1"
    content["score"] = {"normalization": "rank_percentile", "factors": weights}
    content["rebalance"] = {**(content.get("rebalance") or {}), "freq": "weekly", "top_n": 30}
    content["portfolio_method"] = str((research_spec or {}).get("portfolio_method") or "cvar")
    content["lookback_days"] = max(120, int(content.get("lookback_days") or 0))
    content["research_only"] = True
    return content


def _research_process_initializer() -> None:
    """Lower the OS scheduling priority of CPU-heavy exploratory replays."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _isolated_replay_job(payload: dict[str, Any]) -> dict[str, Any]:
    replay = run_strategy_replay(
        strategy_id=None,
        start_date=str(payload["start"]),
        end_date=str(payload["end"]),
        strategy_content=payload["strategy_content"],
        strategy_name=str(payload["strategy_name"]),
        factor_bindings=payload.get("factor_bindings"),
        model_bindings=payload.get("model_bindings"),
        market_state_binding=payload.get("market_state_binding"),
        strategy_controller_version_id=payload.get("strategy_controller_version_id"),
        research_package_hash=payload.get("research_package_hash"),
        evaluate_market_states=True,
    )
    return {
        "metrics": replay.get("metrics") or {},
        "summary": replay.get("summary") or {},
        "data_quality": {
            "point_in_time": ((replay.get("data_quality") or {}).get("point_in_time") or {})
        },
        "component_contract": replay.get("component_contract") or {},
        "model_action_summary": _model_action_summary(replay),
    }


def _model_action_summary(replay: dict[str, Any]) -> dict[str, Any]:
    by_model: dict[str, dict[str, int]] = {}
    action_count = 0
    for decision in replay.get("decisions") or []:
        for action in decision.get("strategy_action_contracts") or []:
            action_name = str(action.get("action") or "unknown")
            action_count += 1
            for model_key in action.get("trigger_models") or []:
                counts = by_model.setdefault(str(model_key), {})
                counts[action_name] = counts.get(action_name, 0) + 1
    return {
        "schema_version": "alis_research_model_action_contribution_v2",
        "action_count": action_count,
        "by_model": by_model,
    }


def _run_isolated_replay(
    payload: dict[str, Any],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    completed_folds: int,
    total_folds: int,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=context,
        initializer=_research_process_initializer,
    ) as executor:
        future = executor.submit(_isolated_replay_job, payload)
        waited = 0
        while True:
            try:
                return future.result(timeout=5)
            except FutureTimeout:
                waited += 5
                if progress_callback:
                    progress_callback({
                        "completed_folds": completed_folds,
                        "total_folds": total_folds,
                        "heartbeat_only": True,
                        "elapsed_seconds": waited,
                        "execution_isolation": "low_priority_process",
                    })
                if waited >= MAX_REPLAY_SECONDS:
                    future.cancel()
                    raise TimeoutError(
                        f"连续因子研究单折超过 {MAX_REPLAY_SECONDS} 秒资源预算"
                    )


def run_combination_experiment(
    snapshot: dict[str, Any],
    proposal: dict[str, Any],
    *,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ensure_continuous_research_tables()
    experiment_id = f"factor-experiment-{proposal['strategy_hash'][:20]}"
    research_stage = str(proposal.get("research_stage") or "quick_screen")
    research_windows = proposal.get("research_windows") or snapshot["research_windows"]
    now = _now()
    persisted_metrics: dict[str, Any] = {}
    with _conn() as conn:
        existing = conn.execute(
            "SELECT * FROM factor_combination_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if existing:
            row = dict(existing)
            row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
            row["factor_weights"] = _loads(row.pop("factor_weights_json", "{}"), {})
            row["windows"] = _loads(row.pop("windows_json", "[]"), [])
            row["research_spec"] = row["metrics"].get("research_spec") or {}
            if str(row.get("status") or "") != "running":
                return row
            persisted_metrics = row["metrics"]
        else:
            conn.execute(
            """INSERT INTO factor_combination_experiments(
               experiment_id,snapshot_id,generation,parent_experiment_id,mutation_type,
               research_stage,strategy_hash,factor_weights_json,windows_json,metrics_json,fitness,
               status,error,created_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,'running',NULL,?,NULL)""",
            (
                experiment_id, snapshot["snapshot_id"], int(proposal["generation"]),
                proposal.get("parent_experiment_id"), proposal["mutation_type"],
                research_stage,
                proposal["strategy_hash"], _json(proposal["factor_weights"]),
                _json(research_windows), _json({"research_spec": proposal.get("research_spec") or {}}), now,
            ),
            )
    folds: list[dict[str, Any]] = [
        dict(item) for item in (persisted_metrics.get("folds") or [])
        if isinstance(item, dict)
    ]
    try:
        research_spec = (
            persisted_metrics.get("research_spec")
            if isinstance(persisted_metrics.get("research_spec"), dict)
            else proposal.get("research_spec")
            if isinstance(proposal.get("research_spec"), dict)
            else _baseline_research_spec(snapshot)
        )
        content = _strategy_content(proposal["factor_weights"], research_spec)
        for window_index, window in enumerate(research_windows, start=1):
            if window_index <= len(folds):
                continue
            replay_payload = {
                "start": str(window["start"]),
                "end": str(window["end"]),
                "strategy_content": content,
                "strategy_name": f"连续因子研究 {experiment_id[-8:]}",
                "factor_bindings": research_spec.get("factor_bindings"),
                "model_bindings": research_spec.get("model_bindings"),
                "market_state_binding": research_spec.get("market_state_binding"),
                "strategy_controller_version_id": research_spec.get("strategy_controller_version_id"),
                "research_package_hash": research_spec.get("research_package_hash"),
            }
            replay = (
                _run_isolated_replay(
                    replay_payload,
                    progress_callback=progress_callback,
                    completed_folds=window_index - 1,
                    total_folds=len(research_windows),
                )
                if replay_runner is run_strategy_replay
                else replay_runner(
                    strategy_id=None,
                    start_date=replay_payload["start"],
                    end_date=replay_payload["end"],
                    strategy_content=content,
                    strategy_name=replay_payload["strategy_name"],
                    factor_bindings=replay_payload["factor_bindings"],
                    model_bindings=replay_payload["model_bindings"],
                    market_state_binding=replay_payload["market_state_binding"],
                    strategy_controller_version_id=replay_payload["strategy_controller_version_id"],
                    research_package_hash=replay_payload["research_package_hash"],
                    evaluate_market_states=True,
                )
            )
            metrics = replay.get("metrics") if isinstance(replay.get("metrics"), dict) else {}
            summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
            folds.append({
                "window": window,
                "total_return": float(metrics.get("total_return") or 0.0),
                "benchmark_return": float(metrics.get("benchmark_return") or 0.0),
                "excess_return": float(metrics.get("excess_return") or 0.0),
                "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
                "turnover_rate": float(metrics.get("turnover_rate") or 0.0),
                "total_fees": float(summary.get("total_fees") or 0.0),
                "point_in_time_passed": bool(((replay.get("data_quality") or {}).get("point_in_time") or {}).get("passed")),
                "market_state_matrix": metrics.get("market_state_matrix") or {},
                "model_binding_mode": str(((replay.get("component_contract") or {}).get("model_binding_mode") or "")),
                "model_action_summary": replay.get("model_action_summary") or _model_action_summary(replay),
            })
            with _conn() as conn:
                conn.execute(
                    """UPDATE factor_combination_experiments
                       SET metrics_json=? WHERE experiment_id=? AND status='running'""",
                    (_json({
                        "schema_version": RESEARCH_SCHEMA_VERSION,
                        "folds": folds,
                        "research_stage": research_stage,
                        "execution_isolation": (
                            "low_priority_process"
                            if replay_runner is run_strategy_replay else "in_process_test_runner"
                        ),
                        "research_spec": research_spec,
                        "partial": True,
                    }), experiment_id),
                )
            if progress_callback:
                progress_callback({
                    "experiment_id": experiment_id,
                    "completed_folds": window_index,
                    "total_folds": len(research_windows),
                })
        trial_count = _research_trial_count(str(snapshot["snapshot_id"]))
        fitness, aggregate = _fitness(folds, trial_count=trial_count)
        market_states = _market_state_summary(folds)
        aggregate["market_states"] = market_states
        aggregate["model_action_contribution"] = {
            model_key: {
                action: sum(
                    int(
                        (((fold.get("model_action_summary") or {}).get("by_model") or {}).get(model_key) or {}).get(action)
                        or 0
                    )
                    for fold in folds
                )
                for action in sorted({
                    action
                    for fold in folds
                    for action in (
                        (((fold.get("model_action_summary") or {}).get("by_model") or {}).get(model_key) or {})
                    )
                })
            }
            for model_key in MODEL_KEYS
        }
        parent_id = proposal.get("parent_experiment_id")
        if parent_id:
            with _conn() as conn:
                parent = conn.execute(
                    """SELECT fitness,research_stage FROM factor_combination_experiments
                       WHERE experiment_id=? AND status='completed'""",
                    (str(parent_id),),
                ).fetchone()
            if parent:
                parent_fitness = float(parent["fitness"] or 0.0)
                aggregate["parent_fitness"] = round(parent_fitness, 8)
                aggregate["fitness_delta_vs_parent"] = round(fitness - parent_fitness, 8)
                comparable = str(parent["research_stage"] or "quick_screen") == research_stage
                aggregate["parent_comparison_is_like_for_like"] = comparable
                aggregate["mutation_verdict"] = (
                    "improved" if comparable and fitness > parent_fitness
                    else "degraded" if comparable
                    else "confirmation_revalidated"
                )
        pit_passed = all(item["point_in_time_passed"] for item in folds)
        disposition = _research_disposition(
            pit_passed=pit_passed,
            aggregate=aggregate,
            market_states=market_states,
        )
        aggregate["research_disposition"] = disposition
        status = "completed" if pit_passed else "blocked_point_in_time"
        metrics_payload = {
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "folds": folds,
            "aggregate": aggregate,
            "formal_oos_evidence": False,
            "research_stage": research_stage,
            "execution_isolation": (
                "low_priority_process"
                if replay_runner is run_strategy_replay else "in_process_test_runner"
            ),
            "research_spec": research_spec,
        }
        with _conn() as conn:
            conn.execute(
                """UPDATE factor_combination_experiments
                   SET metrics_json=?,fitness=?,status=?,finished_at=?,error=NULL
                   WHERE experiment_id=?""",
                (_json(metrics_payload), fitness, status, _now(), experiment_id),
            )
        v10_failure_feedback_enabled = str(
            os.getenv("ALIS_V10_RESEARCH_ENABLED", "1")
        ).lower() in {"1", "true", "yes", "on"}
        if v10_failure_feedback_enabled and pit_passed and (
            not bool(disposition.get("retain", True))
            or float(aggregate.get("mean_excess_return") or 0.0) <= 0.0
        ):
            from app.services.continuous_strategy_research import record_failure_memory

            reason = str(disposition.get("reason") or "negative_strategy_excess")
            primary_gap = (
                "regime_transition_failure"
                if "market_state" in reason or "catastrophic" in reason
                else "liquidity_and_transaction_cost_failure"
                if float(aggregate.get("fee_penalty") or 0.0) < -0.01
                else "ranking_failure"
            )
            record_failure_memory(
                strategy_hash=str(proposal["strategy_hash"]),
                environment_id=None,
                attribution={
                    "primary_gap": primary_gap,
                    "research_disposition_reason": reason,
                    "mean_excess_return": float(aggregate.get("mean_excess_return") or 0.0),
                    "market_states": market_states,
                    "model_action_contribution": aggregate.get("model_action_contribution") or {},
                },
                evidence={
                    "snapshot_id": str(snapshot["snapshot_id"]),
                    "experiment_id": experiment_id,
                    "research_stage": research_stage,
                    "aggregate": aggregate,
                },
                data_cutoff=str(snapshot.get("data_as_of") or ""),
            )
        return {
            "experiment_id": experiment_id,
            "snapshot_id": snapshot["snapshot_id"],
            "generation": proposal["generation"],
            "parent_experiment_id": proposal.get("parent_experiment_id"),
            "mutation_type": proposal["mutation_type"],
            "research_stage": research_stage,
            "factor_weights": proposal["factor_weights"],
            "research_spec": research_spec,
            "fitness": fitness,
            "status": status,
            "metrics": metrics_payload,
        }
    except Exception as exc:
        with _conn() as conn:
            conn.execute(
                """UPDATE factor_combination_experiments
                   SET status='failed',error=?,finished_at=? WHERE experiment_id=?""",
                (str(exc)[:2000], _now(), experiment_id),
            )
        raise


def _weight_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if left == right and left:
        return 1.0
    keys = set(left) | set(right)
    numerator = sum(float(left.get(key, 0.0)) * float(right.get(key, 0.0)) for key in keys)
    left_norm = math.sqrt(sum(float(left.get(key, 0.0)) ** 2 for key in keys))
    right_norm = math.sqrt(sum(float(right.get(key, 0.0)) ** 2 for key in keys))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))


def _candidate_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_spec = left.get("research_spec") if isinstance(left.get("research_spec"), dict) else {}
    right_spec = right.get("research_spec") if isinstance(right.get("research_spec"), dict) else {}
    structural_keys = (
        "model_bindings",
        "market_state_binding",
        "strategy_controller_version_id",
        "portfolio_method",
    )
    if any(left_spec.get(key) != right_spec.get(key) for key in structural_keys):
        return 0.0
    return abs(_weight_similarity(left["factor_weights"], right["factor_weights"]))


def _behavior_features(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    aggregate = metrics.get("aggregate") if isinstance(metrics.get("aggregate"), dict) else {}
    folds = metrics.get("folds") if isinstance(metrics.get("folds"), list) else []
    states = aggregate.get("market_states") if isinstance(aggregate.get("market_states"), dict) else {}
    return {
        "fold_excess_path": [round(float(item.get("excess_return") or 0.0), 8) for item in folds],
        "mean_excess_return": round(float(aggregate.get("mean_excess_return") or 0.0), 8),
        "worst_drawdown": round(float(aggregate.get("worst_drawdown") or 0.0), 8),
        "mean_turnover_rate": round(
            mean(float(item.get("turnover_rate") or 0.0) for item in folds) if folds else 0.0,
            8,
        ),
        "market_state_excess": {
            state: round(float((states.get(state) or {}).get("mean_excess_return") or 0.0), 8)
            for state in ("S0", "S1", "S2", "S3")
        },
    }


def _bounded_distance_similarity(left: float, right: float, scale: float) -> float:
    return max(0.0, 1.0 - min(1.0, abs(float(left) - float(right)) / max(scale, 1e-12)))


def _path_similarity(left: list[float], right: list[float]) -> float:
    size = max(len(left), len(right))
    if size <= 0:
        return 1.0
    padded_left = list(left) + [0.0] * (size - len(left))
    padded_right = list(right) + [0.0] * (size - len(right))
    return mean(
        _bounded_distance_similarity(a, b, 0.20)
        for a, b in zip(padded_left, padded_right)
    )


def _research_cluster_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    factor_similarity = _factor_set_similarity(left["factor_weights"], right["factor_weights"])
    left_behavior = _behavior_features(left)
    right_behavior = _behavior_features(right)
    state_similarity = mean(
        _bounded_distance_similarity(
            left_behavior["market_state_excess"][state],
            right_behavior["market_state_excess"][state],
            0.20,
        )
        for state in ("S0", "S1", "S2", "S3")
    )
    behavior_similarity = (
        0.35 * _path_similarity(
            left_behavior["fold_excess_path"], right_behavior["fold_excess_path"]
        )
        + 0.25 * state_similarity
        + 0.20 * _bounded_distance_similarity(
            left_behavior["worst_drawdown"], right_behavior["worst_drawdown"], 0.30
        )
        + 0.20 * _bounded_distance_similarity(
            left_behavior["mean_turnover_rate"], right_behavior["mean_turnover_rate"], 2.0
        )
    )
    return round(0.45 * factor_similarity + 0.55 * behavior_similarity, 8)


def refresh_research_clusters(snapshot_id: str) -> list[dict[str, Any]]:
    ensure_continuous_research_tables()
    eligible = [
        item for item in _experiment_rows(snapshot_id)
        if str(item.get("status") or "") == "completed"
        and str(item.get("research_stage") or "") == "quick_screen"
        and str((item.get("research_spec") or {}).get("optimization_lane") or "factor") == "factor"
        and not str(item.get("mutation_type") or "").startswith((
            "replace_model:",
            "replace_strategy_controller",
            "replace_market_state:",
            "replace_portfolio_method:",
        ))
        and bool(item.get("experiment_id"))
        and isinstance(item.get("factor_weights"), dict)
        and bool(
            ((((item.get("metrics") or {}).get("aggregate") or {}).get("research_disposition") or {}).get("retain", True))
        )
    ]
    clusters: list[dict[str, Any]] = []
    for row in eligible:
        best_cluster = None
        best_similarity = -1.0
        for cluster in clusters:
            similarity = _research_cluster_similarity(row, cluster["representative"])
            if similarity > best_similarity:
                best_cluster = cluster
                best_similarity = similarity
        if best_cluster is None or best_similarity < RESEARCH_CLUSTER_SIMILARITY_THRESHOLD:
            clusters.append({"representative": row, "members": [row], "similarities": [1.0]})
            continue
        best_cluster["members"].append(row)
        best_cluster["similarities"].append(best_similarity)
        if float(row.get("fitness") or 0.0) > float(best_cluster["representative"].get("fitness") or 0.0):
            best_cluster["representative"] = row
    now = _now()
    payloads: list[dict[str, Any]] = []
    with _conn() as conn:
        conn.execute("DELETE FROM factor_research_clusters WHERE snapshot_id=?", (snapshot_id,))
        for cluster in clusters:
            representative = cluster["representative"]
            members = cluster["members"]
            member_factor_sets = [set(item["factor_weights"]) for item in members]
            factor_union = sorted(set().union(*member_factor_sets)) if member_factor_sets else []
            factor_intersection = sorted(set.intersection(*member_factor_sets)) if member_factor_sets else []
            maximum_fitness = max(float(item.get("fitness") or 0.0) for item in members)
            member_masses = {
                str(item["experiment_id"]): math.exp(
                    min(20.0, float(item.get("fitness") or 0.0) - maximum_fitness)
                )
                for item in members
            }
            total_mass = sum(member_masses.values()) or 1.0
            distilled_weights = _normalized_weights({
                factor_key: sum(
                    member_masses[str(item["experiment_id"])]
                    * float((item.get("factor_weights") or {}).get(factor_key, 0.0))
                    for item in members
                ) / total_mass
                for factor_key in factor_union
            })
            if len(distilled_weights) > MAX_FACTOR_COMBINATION_SIZE:
                distilled_weights = _normalized_weights(dict(sorted(
                    distilled_weights.items(),
                    key=lambda item: (-abs(item[1]), item[0]),
                )[:MAX_FACTOR_COMBINATION_SIZE]))
            behaviors = [_behavior_features(item) for item in members]
            behavior_centroid = {
                "mean_excess_return": round(mean(item["mean_excess_return"] for item in behaviors), 8),
                "worst_drawdown": round(mean(item["worst_drawdown"] for item in behaviors), 8),
                "mean_turnover_rate": round(mean(item["mean_turnover_rate"] for item in behaviors), 8),
                "market_state_excess": {
                    state: round(mean(item["market_state_excess"][state] for item in behaviors), 8)
                    for state in ("S0", "S1", "S2", "S3")
                },
            }
            factor_centroid = {
                "union": factor_union,
                "intersection": factor_intersection,
                "representative_weights": representative["factor_weights"],
                "distilled_weights": distilled_weights,
                "representative_factor_set_hash": _factor_set_hash(representative["factor_weights"]),
                "distilled_factor_set_hash": _factor_set_hash(distilled_weights),
            }
            cluster_id = f"research-cluster-{_hash({'snapshot_id': snapshot_id, 'representative': representative['experiment_id']})[:16]}"
            evidence = {
                "schema_version": "alis_explainable_factor_research_cluster_v1",
                "member_experiment_ids": [str(item["experiment_id"]) for item in members],
                "member_strategy_hashes": [str(item["strategy_hash"]) for item in members],
                "mean_member_similarity": round(mean(cluster["similarities"]), 8),
                "similarity_threshold": RESEARCH_CLUSTER_SIMILARITY_THRESHOLD,
                "dimensions": [
                    "factor_set", "fold_excess_path", "market_state_excess",
                    "worst_drawdown", "turnover_rate",
                ],
                "distillation_method": "fitness_softmax_weighted_factor_centroid_v1",
            }
            conn.execute(
                """INSERT INTO factor_research_clusters(
                   snapshot_id,cluster_id,representative_experiment_id,member_count,
                   factor_centroid_json,behavior_centroid_json,evidence_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, cluster_id, representative["experiment_id"], len(members),
                    _json(factor_centroid), _json(behavior_centroid), _json(evidence), now,
                ),
            )
            payloads.append({
                "cluster_id": cluster_id,
                "representative_experiment_id": representative["experiment_id"],
                "representative_fitness": representative["fitness"],
                "member_count": len(members),
                "factor_centroid": factor_centroid,
                "behavior_centroid": behavior_centroid,
                "evidence": evidence,
            })
    return sorted(payloads, key=lambda item: (-float(item["representative_fitness"]), item["cluster_id"]))


def _cluster_representative_rows(snapshot_id: str) -> list[dict[str, Any]]:
    clusters = refresh_research_clusters(snapshot_id)
    by_id = {str(item["experiment_id"]): item for item in _experiment_rows(snapshot_id)}
    result: list[dict[str, Any]] = []
    for cluster in clusters:
        experiment_id = str(cluster["representative_experiment_id"])
        if experiment_id not in by_id:
            continue
        row = deepcopy(by_id[experiment_id])
        distilled_weights = (cluster.get("factor_centroid") or {}).get("distilled_weights")
        if isinstance(distilled_weights, dict) and len(distilled_weights) >= MIN_FACTOR_COMBINATION_SIZE:
            row["factor_weights"] = distilled_weights
        row["research_cluster_id"] = cluster["cluster_id"]
        row["cluster_member_experiment_ids"] = (
            cluster.get("evidence") or {}
        ).get("member_experiment_ids") or []
        result.append(row)
    return result


def _diverse_population(
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[tuple[dict[str, Any], str]]:
    if not rows:
        return []
    selected: list[tuple[dict[str, Any], str]] = []
    used: set[str] = set()

    def add(row: dict[str, Any] | None, role: str) -> None:
        if row and str(row["experiment_id"]) not in used and len(selected) < limit:
            selected.append((row, role))
            used.add(str(row["experiment_id"]))

    confirmed = [row for row in rows if row["research_stage"] == "confirmation"]
    add(confirmed[0] if confirmed else None, "confirmed_elite")
    add(rows[0], "fitness_elite")
    for lane in ("factor", "model", "strategy_control"):
        lane_rows = [
            row for row in rows
            if str((row.get("research_spec") or {}).get("optimization_lane") or "factor") == lane
        ]
        add(lane_rows[0] if lane_rows else None, f"optimization_lane_{lane}_elite")
    add(
        max(
            rows,
            key=lambda row: float(
                ((row.get("metrics") or {}).get("aggregate") or {}).get("worst_drawdown")
                or -1.0
            ),
        ),
        "drawdown_elite",
    )
    add(
        min(
            rows,
            key=lambda row: float(
                ((row.get("metrics") or {}).get("aggregate") or {}).get("excess_instability")
                or 1.0
            ),
        ),
        "stability_elite",
    )
    for state in ("S0", "S1", "S2", "S3"):
        state_rows = [
            row for row in rows
            if int(
                ((((row.get("metrics") or {}).get("aggregate") or {}).get("market_states") or {}).get(state) or {}).get("observations")
                or 0
            ) > 0
        ]
        add(
            max(
                state_rows,
                key=lambda row: float(
                    (((((row.get("metrics") or {}).get("aggregate") or {}).get("market_states") or {}).get(state) or {}).get("mean_excess_return"))
                    or -1.0
                ),
            ) if state_rows else None,
            f"market_state_{state}_elite",
        )
    for row in rows:
        if str(row["experiment_id"]) in used:
            continue
        maximum_similarity = max(
            (
                _candidate_similarity(row, item)
                for item, _role in selected
            ),
            default=0.0,
        )
        if maximum_similarity < 0.90:
            add(row, "diversity_survivor")
    for row in rows:
        add(row, "fitness_survivor")
    return selected


def refresh_research_population(snapshot_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    eligible = [
        item for item in _experiment_rows(snapshot_id)
        if str(item.get("status") or "") == "completed"
        and bool(
            ((((item.get("metrics") or {}).get("aggregate") or {}).get("research_disposition") or {}).get("retain", True))
        )
    ]
    rows = _diverse_population(eligible, limit=max(1, int(limit)))
    now = _now()
    with _conn() as conn:
        conn.execute("DELETE FROM factor_research_population WHERE snapshot_id=?", (snapshot_id,))
        for index, (row, role) in enumerate(rows, start=1):
            evidence = {
                "strategy_hash": row["strategy_hash"],
                "generation": row["generation"],
                "research_stage": row["research_stage"],
                "metrics": row["metrics"],
                "research_spec": row.get("research_spec") or {},
            }
            conn.execute(
                """INSERT INTO factor_research_population(
                   snapshot_id,experiment_id,rank_no,role,fitness,evidence_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot_id, row["experiment_id"], index, role,
                    row["fitness"], _json(evidence), now,
                ),
            )
    return [{
        "experiment_id": row["experiment_id"],
        "rank": index,
        "role": role,
        "fitness": row["fitness"],
        "factor_weights": row["factor_weights"],
        "generation": row["generation"],
        "research_stage": row["research_stage"],
        "mutation_type": row["mutation_type"],
        "parent_experiment_id": row.get("parent_experiment_id"),
        "optimization_lane": str((row.get("research_spec") or {}).get("optimization_lane") or "factor"),
        "factor_search_mode": str((row.get("research_spec") or {}).get("factor_search_mode") or ""),
        "factor_set_hash": str((row.get("research_spec") or {}).get("factor_set_hash") or ""),
        "source_cluster_ids": (row.get("research_spec") or {}).get("source_cluster_ids") or [],
        "research_package_hash": str((row.get("research_spec") or {}).get("research_package_hash") or row.get("strategy_hash") or ""),
        "model_bindings": (row.get("research_spec") or {}).get("model_bindings") or {},
        "metrics": (row.get("metrics") or {}).get("aggregate") or {},
    } for index, (row, role) in enumerate(rows, start=1)]


def run_continuous_factor_research_cycle(
    *,
    candidates_per_task: int = DEFAULT_CANDIDATES_PER_TASK,
    replay_runner: Callable[..., dict[str, Any]] = run_strategy_replay,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    refresh_evidence: bool = False,
) -> dict[str, Any]:
    snapshot = (
        _active_research_snapshot(refresh_evidence=True)
        if refresh_evidence else _active_research_snapshot()
    )
    existing = _experiment_rows(str(snapshot["snapshot_id"]))
    completed_count = sum(str(item.get("status") or "") == "completed" for item in existing)
    quick_completed = [
        item for item in existing
        if item["research_stage"] == "quick_screen" and item["status"] == "completed"
    ]
    confirmation_attempts = [
        item for item in existing if item["research_stage"] == "confirmation"
    ]
    max_generation = max(
        (
            int(item.get("generation") or 0) for item in existing
            if item["research_stage"] == "quick_screen"
        ),
        default=-1,
    )
    resumable = sorted(
        (item for item in existing if str(item.get("status") or "") == "running"),
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("experiment_id") or "")),
    )
    if not resumable and (
        _research_epoch_terminal(existing)
        or len(existing) >= MAX_EXPERIMENTS_PER_SNAPSHOT
        or (
            max_generation >= MAX_GENERATIONS_PER_SNAPSHOT
            and len(quick_completed) < QUICK_SCREEN_TARGET
        )
    ):
        population = refresh_research_population(str(snapshot["snapshot_id"]))
        clusters = refresh_research_clusters(str(snapshot["snapshot_id"]))
        distillation = None
        if _research_epoch_complete(existing):
            from app.services.factor_strategy_evolution import (
                distill_confirmed_research_experiments,
            )

            distillation = distill_confirmed_research_experiments(
                snapshot_id=str(snapshot["snapshot_id"]),
                limit=CONFIRMATION_ELITE_COUNT,
            )
        continue_research = _next_epoch_allowed(snapshot, population)
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "epoch_completed" if continue_research else "awaiting_new_data",
            "wait_reason": None if continue_research else "no_new_data_model_factor_or_state_evidence",
            "snapshot": {key: value for key, value in snapshot.items() if key != "factors"},
            "experiment_count": len(existing),
            "completed_count": completed_count,
            "quick_screen_completed": len(quick_completed),
            "confirmation_completed": len(confirmation_attempts),
            "population": population,
            "clusters": clusters,
            "distillation": distillation,
            "continue_research": continue_research,
            "rollover_epoch": continue_research,
        }
    if resumable:
        proposals = [_proposal_from_running_experiment(resumable[0])]
        current_stage = str(resumable[0].get("research_stage") or "quick_screen")
    elif len(quick_completed) < QUICK_SCREEN_TARGET:
        proposals = propose_next_combinations(
            snapshot,
            count=max(1, min(int(candidates_per_task or 1), 3)),
        )
        current_stage = "quick_screen"
    else:
        confirmation = propose_confirmation_candidate(snapshot)
        proposals = [confirmation] if confirmation else []
        current_stage = "confirmation"
    results = [
        run_combination_experiment(
            snapshot,
            proposal,
            replay_runner=replay_runner,
            progress_callback=progress_callback,
        )
        for proposal in proposals
    ]
    population = refresh_research_population(str(snapshot["snapshot_id"]))
    clusters = refresh_research_clusters(str(snapshot["snapshot_id"]))
    total_count = len(_experiment_rows(str(snapshot["snapshot_id"])))
    latest_rows = _experiment_rows(str(snapshot["snapshot_id"]))
    epoch_complete = _research_epoch_complete(latest_rows)
    epoch_terminal = _research_epoch_terminal(latest_rows)
    distillation = None
    if epoch_complete:
        from app.services.factor_strategy_evolution import (
            distill_confirmed_research_experiments,
        )

        distillation = distill_confirmed_research_experiments(
            snapshot_id=str(snapshot["snapshot_id"]),
            limit=CONFIRMATION_ELITE_COUNT,
        )
    continue_research = (
        _next_epoch_allowed(snapshot, population)
        if epoch_terminal else bool(proposals) and total_count < MAX_EXPERIMENTS_PER_SNAPSHOT
    )
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "status": "completed",
        "snapshot": {key: value for key, value in snapshot.items() if key != "factors"},
        "experiments": results,
        "experiment_count": total_count,
        "current_stage": current_stage,
        "quick_screen_completed": sum(
            item["research_stage"] == "quick_screen" and item["status"] == "completed"
            for item in latest_rows
        ),
        "confirmation_completed": sum(
            item["research_stage"] == "confirmation" and item["status"] == "completed"
            for item in latest_rows
        ),
        "population": population,
        "clusters": clusters,
        "distillation": distillation,
        "continue_research": continue_research,
        "rollover_epoch": bool(epoch_terminal and continue_research),
        "next_generation": max((int(item.get("generation") or 0) for item in results), default=max_generation) + 1,
    }


def continuous_factor_research_status() -> dict[str, Any]:
    ensure_continuous_research_tables()
    with _conn() as conn:
        snapshot_row = conn.execute(
            "SELECT * FROM factor_research_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not snapshot_row:
        return {
            "ok": True,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "status": "not_started",
            "experiments": [],
            "population": [],
        }
    snapshot = dict(snapshot_row)
    snapshot["research_windows"] = _loads(snapshot.pop("research_windows_json", "[]"), [])
    snapshot["contract"] = _loads(snapshot.pop("contract_json", "{}"), {})
    experiments = _experiment_rows(str(snapshot["snapshot_id"]))
    population = refresh_research_population(str(snapshot["snapshot_id"]))
    clusters = refresh_research_clusters(str(snapshot["snapshot_id"]))
    quick_completed = sum(
        item["research_stage"] == "quick_screen" and item["status"] == "completed"
        for item in experiments
    )
    confirmation_completed = sum(
        item["research_stage"] == "confirmation" and item["status"] == "completed"
        for item in experiments
    )
    confirmation_running = any(
        item["research_stage"] == "confirmation" and item["status"] == "running"
        for item in experiments
    )
    completed_rows = [item for item in experiments if item["status"] == "completed"]
    confirmed_rows = [
        item for item in completed_rows if item["research_stage"] == "confirmation"
    ]
    epoch_complete = _research_epoch_complete(experiments)
    epoch_terminal = _research_epoch_terminal(experiments)
    rollover_ready = epoch_terminal and _next_epoch_allowed(snapshot, population)
    contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
    return {
        "ok": True,
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "operating_model": "continuous_event_driven_research",
        "status": (
            "epoch_completed" if rollover_ready
            else "awaiting_new_data" if epoch_terminal
            else "running"
        ),
        "wait_reason": (
            "no_new_data_model_factor_or_state_evidence"
            if epoch_terminal and not rollover_ready else None
        ),
        "epoch": {
            "current": int(contract.get("epoch_no") or 1),
            "maximum_per_evidence": int(
                contract.get("maximum_epochs_per_evidence") or MAX_EPOCHS_PER_EVIDENCE
            ),
            "rollover_ready": rollover_ready,
        },
        "optimization_lanes": contract.get("optimization_lanes") or [
            "factor", "model", "strategy_control"
        ],
        "research_package": contract.get("research_package") or {},
        "current_stage": (
            "confirmation"
            if confirmation_running or quick_completed >= QUICK_SCREEN_TARGET
            else "quick_screen"
        ),
        "snapshot": snapshot,
        "experiment_count": len(experiments),
        "best_experiment": (
            confirmed_rows[0] if confirmed_rows
            else completed_rows[0] if completed_rows else None
        ),
        "quick_screen_completed": quick_completed,
        "quick_screen_target": QUICK_SCREEN_TARGET,
        "confirmation_completed": confirmation_completed,
        "confirmation_target": CONFIRMATION_ELITE_COUNT,
        "population": population,
        "clusters": clusters,
    }

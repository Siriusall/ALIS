from __future__ import annotations

import json
import math
import pickle
from pathlib import Path
from typing import Any

from app.db.store import _conn as business_conn, get_active_model_version
from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services.plan_model_control import PLAN_MODELS, score_plan_model_features


MODEL_KEYS = tuple(PLAN_MODELS)
MODEL_WEIGHTS = {
    "medium_horizon_lambdarank_v1": 0.25,
    "capital_persistence_multitask_v1": 0.10,
    "leader_lambdarank_v1": 0.15,
    "event_surprise_lgbm_v1": 0.05,
    "narrative_transition_multitask_v1": 0.0,
}


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _artifact(version: dict[str, Any] | None) -> dict[str, Any] | None:
    path = str((version or {}).get("artifact_path") or "")
    if not path or not Path(path).is_file():
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        model_path = str(payload.get("model_path") or "")
        if model_path and Path(model_path).is_file():
            with Path(model_path).open("rb") as handle:
                payload["_model"] = pickle.load(handle)
        return payload
    except Exception:
        return None


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if not ordered:
        return {}
    denominator = max(1, len(ordered) - 1)
    result: dict[str, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        percentile = ((start + end - 1) / 2.0) / denominator
        for index in range(start, end):
            result[ordered[index][0]] = percentile
        start = end
    return result


def _live_model_controls(versions: dict[str, dict[str, Any] | None]) -> dict[str, dict[str, Any]]:
    """Read persisted live-health state without making monitoring a display-only feature."""
    controls = {
        key: {"status": "healthy", "weight_multiplier": 1.0, "reason": "no_live_health_override"}
        for key in MODEL_KEYS
    }
    try:
        with business_conn() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT model_key,active_version,status,weight_multiplier,reason FROM model_live_health"
            ).fetchall()]
    except Exception:
        return controls
    for row in rows:
        key = str(row.get("model_key") or "")
        version = versions.get(key) or {}
        # A health record for an older version must never penalize its successor.
        if key not in controls or str(row.get("active_version") or "") != str(version.get("version") or ""):
            continue
        status = str(row.get("status") or "healthy")
        multiplier = max(0.0, min(1.0, _safe(row.get("weight_multiplier") or 1.0)))
        controls[key] = {
            "status": status,
            "weight_multiplier": multiplier,
            "reason": str(row.get("reason") or ""),
        }
    return controls


def _theme_evidence(themes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    lifecycle_score = {"发现": 0.55, "启动": 0.80, "扩散": 1.0, "加速": 0.90, "亢奋": 0.35, "分歧": 0.20, "退潮": 0.0, "沉寂": 0.0}
    for theme in themes:
        lifecycle = str(theme.get("lifecycle") or "")
        strength = max(0.0, min(1.0, _safe(theme.get("score")) / 100.0))
        score = strength * lifecycle_score.get(lifecycle, 0.4)
        for company in theme.get("companies") or []:
            symbol = str(company.get("ts_code") or "").split(".", 1)[0].zfill(6)
            if not symbol or score <= _safe((evidence.get(symbol) or {}).get("score")):
                continue
            evidence[symbol] = {
                "theme": str(theme.get("theme_name") or theme.get("theme") or ""),
                "lifecycle": lifecycle or "未识别",
                "score": score,
            }
    return evidence


def _features(
    row: SnapshotRow,
    factors: dict[str, float],
    history: list[HistBar],
    theme: dict[str, Any],
) -> dict[str, float]:
    recent = history[-21:]
    amounts = [_safe(item.turnover) for item in recent if _safe(item.turnover) > 0]
    short = sum(amounts[-5:]) / max(1, len(amounts[-5:])) if amounts else 0.0
    long = sum(amounts) / max(1, len(amounts)) if amounts else 0.0
    turnover_slope = short / long - 1.0 if long > 0 else 0.0
    momentum_20 = _safe(factors.get("momentum_20"))
    momentum_60 = _safe(factors.get("momentum_60"))
    volatility_20 = _safe(factors.get("volatility_20"))
    capital_persistence = max(-3.0, min(3.0, turnover_slope + momentum_20))
    narrative_strength = max(-3.0, min(3.0, _safe(theme.get("score")) * 2.0 - 1.0))
    event_surprise = max(
        -3.0,
        min(3.0, _safe(row.change_pct) / 100.0 - volatility_20),
    )
    return {
        "momentum_20": momentum_20,
        "momentum_60": momentum_60,
        "volatility_20": volatility_20,
        "turnover_slope": turnover_slope,
        "capital_persistence": capital_persistence,
        "narrative_strength": narrative_strength,
        "event_surprise": event_surprise,
    }


def apply_production_model_signals(
    *,
    ranked: list[tuple[SnapshotRow, float]],
    factor_values: dict[str, dict[str, float]],
    histories: dict[str, list[HistBar]],
    themes: list[dict[str, Any]],
) -> tuple[list[tuple[SnapshotRow, float]], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Fuse production model families into ranking and deterministic trade gates."""
    versions = {key: get_active_model_version(key) for key in MODEL_KEYS}
    artifacts = {key: _artifact(version) for key, version in versions.items()}
    health_controls = _live_model_controls(versions)
    theme_by_symbol = _theme_evidence(themes)
    raw_scores: dict[str, dict[str, float]] = {key: {} for key in MODEL_KEYS}
    features_by_symbol: dict[str, dict[str, float]] = {}
    for row, _base in ranked:
        symbol = str(row.symbol)
        features = _features(
            row,
            factor_values.get(symbol) or {},
            histories.get(symbol) or [],
            theme_by_symbol.get(symbol) or {},
        )
        features_by_symbol[symbol] = features
        for key in MODEL_KEYS:
            if not versions.get(key) or not artifacts.get(key):
                continue
            result = score_plan_model_features(key, features, artifact=artifacts[key])
            raw_scores[key][symbol] = _safe(result.get("score"))

    percentiles = {key: _percentiles(values) for key, values in raw_scores.items()}
    base_values = {str(row.symbol): _safe(score) for row, score in ranked}
    base_percentile = _percentiles(base_values)
    evidence: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    reranked: list[tuple[SnapshotRow, float]] = []
    for row, _base in ranked:
        symbol = str(row.symbol)
        theme = theme_by_symbol.get(symbol) or {}
        model_values = {
            key: {
                "raw_score": raw_scores[key].get(symbol),
                "percentile": percentiles[key].get(symbol),
                "version_id": (versions.get(key) or {}).get("id"),
                "version": (versions.get(key) or {}).get("version"),
                "status": (
                    health_controls[key]["status"]
                    if versions.get(key) and artifacts.get(key) else "proxy_or_unavailable"
                ),
                "weight_multiplier": health_controls[key]["weight_multiplier"],
                "health_reason": health_controls[key]["reason"],
            }
            for key in MODEL_KEYS
        }
        fused = 0.45 * base_percentile.get(symbol, 0.0)
        for key, weight in MODEL_WEIGHTS.items():
            fused += weight * health_controls[key]["weight_multiplier"] * float(percentiles[key].get(symbol, 0.5))
        capital_pct = percentiles["capital_persistence_multitask_v1"].get(symbol)
        capital_gate_active = bool(
            versions.get("capital_persistence_multitask_v1")
            and artifacts.get("capital_persistence_multitask_v1")
            and health_controls["capital_persistence_multitask_v1"]["status"] != "degraded"
        )
        narrative_exit = str(theme.get("lifecycle") or "") in {"退潮", "沉寂"}
        capital_block = capital_gate_active and capital_pct is not None and capital_pct <= 0.10
        if capital_block or narrative_exit:
            blocked.append({
                "symbol": symbol,
                "ts_code": symbol,
                "side": "BUY",
                "code": "capital_persistence_gate_failed" if capital_block else "narrative_exit_gate",
                "message": "生产资金持续模型位于截面后10%，禁止新增" if capital_block else "叙事处于退潮/沉寂，禁止新增",
            })
            continue
        event_pct = percentiles["event_surprise_lgbm_v1"].get(symbol)
        leader_pct = percentiles["leader_lambdarank_v1"].get(symbol)
        evidence[symbol] = {
            "schema_version": "production_model_signal_fusion_v1",
            "fused_score": round(fused * 100.0, 6),
            "features": features_by_symbol.get(symbol) or {},
            "models": model_values,
            "capital_confirmation": (
                "strong" if capital_pct is not None and capital_pct >= 0.70
                else "weak" if capital_pct is not None and capital_pct < 0.35
                else "confirmed"
            ),
            "capital_gate_active": capital_gate_active,
            "add_position_eligible": not capital_gate_active or capital_pct is None or capital_pct >= 0.35,
            "leader_eligible": leader_pct is not None and leader_pct >= 0.70,
            "event_eligible": event_pct is not None and event_pct >= 0.70,
            "narrative": theme or {"lifecycle": "未识别", "score": 0.0},
            "trigger_models": [
                key for key in MODEL_KEYS
                if versions.get(key) and artifacts.get(key)
            ],
        }
        reranked.append((row, fused * 100.0))
    reranked.sort(key=lambda item: (-item[1], str(item[0].symbol)))
    status = {
        "schema_version": "production_model_signal_fusion_v1",
        "production_models": [
            {
                "model_key": key,
                "version_id": (versions.get(key) or {}).get("id"),
                "version": (versions.get(key) or {}).get("version"),
                "consumed": bool(versions.get(key) and artifacts.get(key)),
                "health_status": health_controls[key]["status"],
                "weight_multiplier": health_controls[key]["weight_multiplier"],
            }
            for key in MODEL_KEYS
        ],
        "input_count": len(ranked),
        "output_count": len(reranked),
        "blocked_count": len(blocked),
        "policy": {
            "base_rank_weight": 0.45,
            "model_weights": MODEL_WEIGHTS,
            "effective_model_weights": {
                key: round(weight * health_controls[key]["weight_multiplier"], 6)
                for key, weight in MODEL_WEIGHTS.items()
            },
            "capital_new_position_block_percentile": 0.10,
            "add_position_min_capital_percentile": 0.35,
            "disabled_gates": [
                "capital_persistence_gate"
                for key in ("capital_persistence_multitask_v1",)
                if not (versions.get(key) and artifacts.get(key)) or health_controls[key]["status"] == "degraded"
            ],
        },
    }
    return reranked, evidence, blocked, status

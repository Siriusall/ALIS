from __future__ import annotations

import hashlib
import json
import math
import pickle
from datetime import datetime
from typing import Any

from app.db.store import _conn as business_conn, get_active_model_version, get_model_version
from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services.model_artifacts import resolve_model_artifact_path
from app.services.plan_model_control import (
    NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
    PLAN_MODELS,
    audit_plan_model_production_eligibility,
    score_plan_model_feature_batch,
    score_plan_model_features,
)

_DEFAULT_SCORE_PLAN_MODEL_FEATURES = score_plan_model_features


MODEL_KEYS = tuple(PLAN_MODELS)
MODEL_WEIGHTS = {
    "medium_horizon_lambdarank_v1": 0.25,
    "capital_persistence_multitask_v1": 0.10,
    "leader_lambdarank_v1": 0.15,
    "event_surprise_lgbm_v1": 0.05,
    "narrative_transition_multitask_v1": 0.0,
}
MAX_THEME_RANK_BONUS = 0.08
MAX_NARRATIVE_MODEL_RANK_ADJUSTMENT = 0.06
NARRATIVE_MODEL_MIN_CONFIDENCE = 0.55
NARRATIVE_MODEL_EXIT_CONFIDENCE = 0.65
NARRATIVE_MODEL_HARD_VETO_RISK = 0.80
ENVIRONMENT_MODEL_MULTIPLIERS = {
    "S0": {
        "medium_horizon_lambdarank_v1": 1.15, "capital_persistence_multitask_v1": 0.90,
        "leader_lambdarank_v1": 1.20, "event_surprise_lgbm_v1": 1.00,
    },
    "S1": {key: 1.0 for key in MODEL_KEYS},
    "S2": {
        "medium_horizon_lambdarank_v1": 1.10, "capital_persistence_multitask_v1": 1.20,
        "leader_lambdarank_v1": 0.85, "event_surprise_lgbm_v1": 0.80,
    },
    "S3": {
        "medium_horizon_lambdarank_v1": 0.80, "capital_persistence_multitask_v1": 1.25,
        "leader_lambdarank_v1": 0.60, "event_surprise_lgbm_v1": 0.60,
    },
}
_ARTIFACT_CACHE: dict[
    tuple[str, int, int],
    dict[str, Any] | None,
] = {}
_ARTIFACT_CACHE_LIMIT = 64
_FROZEN_BINDING_CACHE: dict[
    str,
    dict[str, dict[str, Any] | None],
] = {}
_FROZEN_BINDING_CACHE_LIMIT = 64


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _artifact(version: dict[str, Any] | None) -> dict[str, Any] | None:
    path = resolve_model_artifact_path((version or {}).get("artifact_path"))
    if not path.is_file():
        return None
    stat = path.stat()
    cache_key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    if cache_key in _ARTIFACT_CACHE:
        return _ARTIFACT_CACHE[cache_key]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("model_key") and str(payload["model_key"]) != str((version or {}).get("model_key") or ""):
            return None
        model_path = str(payload.get("model_path") or "")
        resolved_model_path = resolve_model_artifact_path(model_path)
        if model_path and not resolved_model_path.is_file():
            return None
        if model_path:
            expected_sha = str(payload.get("model_sha256") or "")
            if expected_sha and hashlib.sha256(resolved_model_path.read_bytes()).hexdigest() != expected_sha:
                return None
            payload["model_path"] = str(resolved_model_path)
            with resolved_model_path.open("rb") as handle:
                payload["_model"] = pickle.load(handle)
        if len(_ARTIFACT_CACHE) >= _ARTIFACT_CACHE_LIMIT:
            _ARTIFACT_CACHE.pop(next(iter(_ARTIFACT_CACHE)))
        _ARTIFACT_CACHE[cache_key] = payload
        return payload
    except Exception:
        _ARTIFACT_CACHE[cache_key] = None
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


def _resolve_model_versions(
    model_bindings: dict[str, Any] | None,
) -> dict[str, dict[str, Any] | None]:
    """Resolve either the live production bundle or an explicitly frozen bundle."""
    if model_bindings is None:
        versions: dict[str, dict[str, Any] | None] = {}
        for key in MODEL_KEYS:
            version = get_active_model_version(key)
            versions[key] = (
                version
                if audit_plan_model_production_eligibility(version).get("passed")
                else None
            )
        return versions
    unknown = sorted(set(model_bindings) - set(MODEL_KEYS))
    if unknown:
        raise ValueError(f"未知模型绑定：{','.join(unknown)}")
    cache_key = hashlib.sha256(
        json.dumps(
            model_bindings,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    cached = _FROZEN_BINDING_CACHE.get(cache_key)
    if cached is not None:
        return {
            key: dict(value) if isinstance(value, dict) else None
            for key, value in cached.items()
        }
    versions: dict[str, dict[str, Any] | None] = {}
    for key in MODEL_KEYS:
        binding = model_bindings.get(key)
        if binding is None:
            versions[key] = None
            continue
        if isinstance(binding, dict) and binding.get("id"):
            version = get_model_version(int(binding["id"]))
        elif isinstance(binding, int) or (isinstance(binding, str) and binding.isdigit()):
            version = get_model_version(int(binding))
        else:
            raise ValueError(f"模型绑定格式无效：{key}")
        if not version:
            raise ValueError(f"模型版本不存在：{key}")
        if str(version.get("model_key") or "") != key:
            raise ValueError(
                f"模型版本类型不匹配：期望 {key}，实际 {version.get('model_key')}"
            )
        if isinstance(binding, dict) and binding.get("point_in_time_fold_artifact"):
            override = binding["point_in_time_fold_artifact"]
            if not isinstance(override, dict) or str(
                override.get("binding_contract") or ""
            ) != "point_in_time_fold_model_v1":
                raise ValueError(f"逐窗模型绑定合同无效：{key}")
            artifact_path = resolve_model_artifact_path(override.get("artifact_path"))
            if not artifact_path.is_file():
                raise ValueError(f"逐窗模型产物不存在：{key}")
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"逐窗模型产物不可读：{key}") from exc
            model_path = resolve_model_artifact_path(artifact.get("model_path"))
            expected_sha = str(override.get("model_sha256") or "")
            actual_sha = (
                hashlib.sha256(model_path.read_bytes()).hexdigest()
                if model_path.is_file()
                else ""
            )
            if (
                str(artifact.get("binding_contract") or "")
                != "point_in_time_fold_model_v1"
                or str(artifact.get("model_key") or "") != key
                or int(artifact.get("fold") or 0) != int(override.get("fold") or 0)
                or not expected_sha
                or actual_sha != expected_sha
                or str(artifact.get("model_sha256") or "") != expected_sha
            ):
                raise ValueError(f"逐窗模型产物身份或哈希不匹配：{key}")
            training = artifact.get("training_dataset") or {}
            if (
                str(training.get("test_start") or "") != str(override.get("test_start") or "")
                or str(training.get("test_end") or "") != str(override.get("test_end") or "")
                or str(training.get("validation_end") or "")
                != str(override.get("validation_end") or "")
                or str(training.get("validation_end") or "")
                >= str(training.get("test_start") or "")
            ):
                raise ValueError(f"逐窗模型时点合同不匹配：{key}")
            version = {
                **version,
                "artifact_path": str(artifact_path),
                "created_at": str(override.get("available_at") or ""),
            }
        versions[key] = version
    if len(_FROZEN_BINDING_CACHE) >= _FROZEN_BINDING_CACHE_LIMIT:
        _FROZEN_BINDING_CACHE.pop(next(iter(_FROZEN_BINDING_CACHE)))
    _FROZEN_BINDING_CACHE[cache_key] = {
        key: dict(value) if isinstance(value, dict) else None
        for key, value in versions.items()
    }
    return versions


def freeze_model_bindings(
    model_bindings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Return the stable, serializable identity consumed by research replays."""
    versions = _resolve_model_versions(model_bindings)
    return {
        key: (
            {
                "id": int(version["id"]),
                "model_key": key,
                "version": str(version.get("version") or ""),
                "status": str(version.get("status") or ""),
                "artifact_path": str(version.get("artifact_path") or ""),
                "created_at": str(version.get("created_at") or ""),
            }
            if version else None
        )
        for key, version in versions.items()
    }


def _point_in_time_model_availability(
    version: dict[str, Any] | None,
    artifact: dict[str, Any] | None,
    *,
    as_of_time: str | None,
) -> dict[str, Any]:
    if not version or not artifact:
        return {"available": False, "reason": "version_or_artifact_unavailable"}
    if not as_of_time:
        return {"available": True, "reason": "current_runtime"}
    cutoff = str(as_of_time)
    cutoff_date = cutoff[:10].replace("-", "")
    created_at = str(version.get("created_at") or "")
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at)
            cutoff_dt = datetime.fromisoformat(cutoff)
            if created_dt.tzinfo is not None and cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=created_dt.tzinfo)
            elif created_dt.tzinfo is None and cutoff_dt.tzinfo is not None:
                created_dt = created_dt.replace(tzinfo=cutoff_dt.tzinfo)
            if created_dt > cutoff_dt:
                return {"available": False, "reason": "model_version_created_after_cutoff"}
        except ValueError:
            if created_at > cutoff:
                return {"available": False, "reason": "model_version_created_after_cutoff"}
    training = artifact.get("training_dataset") if isinstance(artifact.get("training_dataset"), dict) else {}
    training_cutoff = str(
        training.get("validation_end")
        or training.get("train_end")
        or ""
    ).replace("-", "")[:8]
    if training_cutoff and cutoff_date and training_cutoff > cutoff_date:
        return {"available": False, "reason": "model_training_evidence_after_cutoff"}
    return {"available": True, "reason": "point_in_time_available"}


def _live_model_controls(
    versions: dict[str, dict[str, Any] | None],
    *,
    enabled: bool = True,
) -> dict[str, dict[str, Any]]:
    """Read persisted live-health state without making monitoring a display-only feature."""
    controls = {
        key: {"status": "observing", "weight_multiplier": 1.0, "reason": "live_health_evidence_missing"}
        for key in MODEL_KEYS
    }
    if not enabled:
        for value in controls.values():
            value["status"] = "healthy"
            value["reason"] = "frozen_research_bundle_ignores_live_health_drift"
        return controls
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
    lifecycle_score = {
        "形成": 0.70,
        "扩散": 1.0,
        "成熟/拥挤": 0.35,
        "衰退": 0.0,
        # Historical snapshots remain readable during the contract migration.
        "发现": 0.55,
        "启动": 0.80,
        "加速": 0.90,
        "亢奋": 0.35,
        "分歧": 0.20,
        "退潮": 0.0,
        "沉寂": 0.0,
    }
    for theme in themes:
        lifecycle = str(theme.get("lifecycle") or "")
        strength = max(0.0, min(1.0, _safe(theme.get("score")) / 100.0))
        score = strength * lifecycle_score.get(lifecycle, 0.4)
        payload = theme.get("payload") if isinstance(theme.get("payload"), dict) else {}
        state = str(theme.get("state") or payload.get("state") or "").lower()
        explicit_risk = bool(
            theme.get("risk_flag")
            or payload.get("risk_flag")
            or payload.get("verified") is False
            or state in {"false", "invalid", "risk", "rejected", "fabricated"}
        )
        hard_veto = lifecycle in {"衰退", "退潮", "沉寂"} or explicit_risk
        for company in theme.get("companies") or []:
            symbol = str(company.get("ts_code") or "").split(".", 1)[0].zfill(6)
            if not symbol:
                continue
            current = evidence.setdefault(symbol, {
                "theme": "",
                "lifecycle": "未识别",
                "score": 0.0,
                "promotable": False,
                "hard_veto": False,
                "veto_reasons": [],
                "matches": [],
            })
            match = {
                "theme": str(theme.get("theme_name") or theme.get("theme") or ""),
                "lifecycle": lifecycle or "未识别",
                "score": score,
                "promotable": bool(theme.get("promotable")),
                "hard_veto": hard_veto,
                "state": state,
            }
            current["matches"].append(match)
            if hard_veto:
                current["hard_veto"] = True
                current["veto_reasons"].append(
                    "explicit_theme_risk" if explicit_risk else f"theme_lifecycle_{lifecycle}"
                )
            if score > _safe(current.get("score")):
                current.update({
                    "theme": match["theme"],
                    "lifecycle": match["lifecycle"],
                    "score": score,
                    "promotable": match["promotable"],
                })
    for item in evidence.values():
        item["veto_reasons"] = list(dict.fromkeys(item["veto_reasons"]))
        item["rank_bonus"] = round(
            min(MAX_THEME_RANK_BONUS, MAX_THEME_RANK_BONUS * _safe(item.get("score")))
            if item.get("promotable") and not item.get("hard_veto") else 0.0,
            8,
        )
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


def apply_model_signals(
    *,
    ranked: list[tuple[SnapshotRow, float]],
    factor_values: dict[str, dict[str, float]],
    histories: dict[str, list[HistBar]],
    themes: list[dict[str, Any]],
    model_bindings: dict[str, Any] | None = None,
    use_live_health: bool | None = None,
    as_of_time: str | None = None,
    market_state: str | None = None,
) -> tuple[list[tuple[SnapshotRow, float]], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Fuse the existing model families using live or explicitly frozen versions."""
    explicit_bundle = model_bindings is not None
    versions = _resolve_model_versions(model_bindings)
    artifacts = {key: _artifact(version) for key, version in versions.items()}
    availability = {
        key: _point_in_time_model_availability(
            versions.get(key), artifacts.get(key), as_of_time=as_of_time,
        )
        for key in MODEL_KEYS
    }
    health_enabled = not explicit_bundle if use_live_health is None else bool(use_live_health)
    health_controls = (
        _live_model_controls(versions)
        if health_enabled else _live_model_controls(versions, enabled=False)
    )
    environment_key = str(market_state or "S1")
    environment_multipliers = ENVIRONMENT_MODEL_MULTIPLIERS.get(environment_key, ENVIRONMENT_MODEL_MULTIPLIERS["S1"])
    maturity_multipliers = {
        key: 0.85 if str(health_controls[key].get("status") or "") in {"observing", "watch"} else 1.0
        for key in MODEL_KEYS
    }
    raw_effective_weights = {
        key: (
            float(weight)
            * float(environment_multipliers.get(key, 1.0))
            * float(health_controls[key]["weight_multiplier"])
            * float(maturity_multipliers[key])
            if availability[key]["available"]
            else 0.0
        )
        for key, weight in MODEL_WEIGHTS.items()
    }
    raw_total = sum(raw_effective_weights.values())
    scale = min(1.0, 0.55 / raw_total) if raw_total > 0 else 1.0
    effective_weights = {key: value * scale for key, value in raw_effective_weights.items()}
    base_rank_weight = max(0.45, 1.0 - sum(effective_weights.values()))
    theme_by_symbol = _theme_evidence(themes)
    raw_scores: dict[str, dict[str, float]] = {key: {} for key in MODEL_KEYS}
    raw_outputs: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in MODEL_KEYS}
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
        if not availability[key]["available"]:
            continue
        if score_plan_model_features is _DEFAULT_SCORE_PLAN_MODEL_FEATURES:
            results = score_plan_model_feature_batch(
                key,
                features_by_symbol,
                artifact=artifacts[key],
            )
        else:
            # Preserve the narrow scorer injection boundary used by research
            # diagnostics and contract tests.
            results = {
                symbol: score_plan_model_features(
                    key,
                    features,
                    artifact=artifacts[key],
                )
                for symbol, features in features_by_symbol.items()
            }
        for symbol, result in results.items():
            raw_scores[key][symbol] = _safe(result.get("score"))
            if isinstance(result.get("outputs"), dict):
                raw_outputs[key][symbol] = dict(result["outputs"])

    percentiles = {key: _percentiles(values) for key, values in raw_scores.items()}
    base_values = {str(row.symbol): _safe(score) for row, score in ranked}
    base_percentile = _percentiles(base_values)
    evidence: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    reranked: list[tuple[SnapshotRow, float]] = []
    narrative_adjusted_count = 0
    narrative_veto_count = 0
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
                    if availability[key]["available"] else "proxy_or_unavailable"
                ),
                "weight_multiplier": health_controls[key]["weight_multiplier"],
                "health_reason": health_controls[key]["reason"],
                "point_in_time_available": bool(availability[key]["available"]),
                "availability_reason": str(availability[key]["reason"]),
                "outputs": raw_outputs[key].get(symbol) or {},
            }
            for key in MODEL_KEYS
        }
        fused = base_rank_weight * base_percentile.get(symbol, 0.0)
        for key, weight in effective_weights.items():
            fused += weight * float(percentiles[key].get(symbol, 0.5))
        theme_bonus = float(theme.get("rank_bonus") or 0.0)
        fused += theme_bonus
        narrative_outputs = raw_outputs[
            "narrative_transition_multitask_v1"
        ].get(symbol) or {}
        narrative_probabilities = (
            narrative_outputs.get("next_state_probabilities")
            if isinstance(
                narrative_outputs.get("next_state_probabilities"),
                dict,
            )
            else {}
        )
        narrative_confidence = _safe(narrative_outputs.get("confidence"))
        narrative_exit_risk = _safe(narrative_outputs.get("exit_risk"))
        narrative_growth_probability = sum(
            _safe(narrative_probabilities.get(key))
            for key in ("形成", "扩散", "发现", "启动", "加速")
        )
        narrative_model_available = bool(
            versions.get("narrative_transition_multitask_v1")
            and availability["narrative_transition_multitask_v1"]["available"]
        )
        narrative_model_adjustment = 0.0
        if (
            narrative_model_available
            and theme
            and narrative_confidence >= NARRATIVE_MODEL_MIN_CONFIDENCE
        ):
            signal = narrative_growth_probability - narrative_exit_risk
            if signal > 0 and not bool(theme.get("promotable")):
                signal = 0.0
            narrative_model_adjustment = max(
                -MAX_NARRATIVE_MODEL_RANK_ADJUSTMENT,
                min(MAX_NARRATIVE_MODEL_RANK_ADJUSTMENT, signal * 0.06),
            )
            if abs(narrative_model_adjustment) > 1e-12:
                narrative_adjusted_count += 1
                fused += narrative_model_adjustment
        narrative_model_hard_veto = bool(
            narrative_model_available
            and theme
            and narrative_confidence >= NARRATIVE_MODEL_EXIT_CONFIDENCE
            and narrative_exit_risk >= NARRATIVE_MODEL_HARD_VETO_RISK
        )
        if narrative_model_hard_veto:
            narrative_veto_count += 1
        capital_pct = percentiles["capital_persistence_multitask_v1"].get(symbol)
        capital_outputs = raw_outputs["capital_persistence_multitask_v1"].get(symbol) or {}
        capital_multihead_ready = all(
            key in capital_outputs
            for key in ("direction_probability", "strength_p10", "failure_hazard")
        )
        capital_gate_active = bool(
            versions.get("capital_persistence_multitask_v1")
            and availability["capital_persistence_multitask_v1"]["available"]
            and health_controls["capital_persistence_multitask_v1"]["status"] in {"healthy", "production"}
            and capital_multihead_ready
        )
        narrative_exit = bool(
            theme.get("hard_veto") or narrative_model_hard_veto
        )
        capital_block = bool(
            capital_gate_active
            and float(capital_outputs.get("direction_probability") or 0.0) < 0.35
            and float(capital_outputs.get("strength_p10") or 0.0) < 0.0
            and float(capital_outputs.get("failure_hazard") or 0.0) > 0.65
        )
        if capital_block or narrative_exit:
            blocked.append({
                "symbol": symbol,
                "ts_code": symbol,
                "side": "BUY",
                "code": (
                    "capital_persistence_gate_failed"
                    if capital_block
                    else "narrative_model_exit_gate"
                    if narrative_model_hard_veto
                    and not bool(theme.get("hard_veto"))
                    else "narrative_exit_gate"
                ),
                "message": (
                    "生产资金持续模型位于截面后10%，禁止新增"
                    if capital_block
                    else "监督叙事模型识别到高置信生命周期退出风险，禁止新增"
                    if narrative_model_hard_veto
                    and not bool(theme.get("hard_veto"))
                    else "叙事处于退潮、沉寂、虚假或风险状态，禁止新增"
                ),
            })
            continue
        event_pct = percentiles["event_surprise_lgbm_v1"].get(symbol)
        leader_pct = percentiles["leader_lambdarank_v1"].get(symbol)
        evidence[symbol] = {
            "schema_version": "model_signal_fusion_v2",
            "fused_score": round(fused * 100.0, 6),
            "features": features_by_symbol.get(symbol) or {},
            "models": model_values,
            "capital_confirmation": (
                "strong" if capital_pct is not None and capital_pct >= 0.70
                else "weak" if capital_pct is not None and capital_pct < 0.35
                else "confirmed"
            ),
            "capital_gate_active": capital_gate_active,
                "add_position_eligible": (
                    not capital_gate_active
                    or (
                        float(capital_outputs.get("direction_probability") or 0.0) >= 0.45
                        and float(capital_outputs.get("failure_hazard") or 1.0) <= 0.55
                    )
                ),
            "leader_eligible": leader_pct is not None and leader_pct >= 0.70,
            "event_eligible": event_pct is not None and event_pct >= 0.70,
            "narrative": theme or {"lifecycle": "未识别", "score": 0.0},
            "theme_rank_bonus": round(theme_bonus * 100.0, 6),
            "narrative_model": {
                "effect_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
                "available": narrative_model_available,
                "confidence": round(narrative_confidence, 8),
                "growth_probability": round(
                    narrative_growth_probability,
                    8,
                ),
                "exit_risk": round(narrative_exit_risk, 8),
                "rank_adjustment": round(
                    narrative_model_adjustment * 100.0,
                    6,
                ),
                "hard_veto": narrative_model_hard_veto,
                "outputs": narrative_outputs,
            },
            "trigger_models": [
                key for key in MODEL_KEYS
                if availability[key]["available"]
            ],
        }
        reranked.append((row, fused * 100.0))
    reranked.sort(key=lambda item: (-item[1], str(item[0].symbol)))
    status = {
        "schema_version": "model_signal_fusion_v2",
        "binding_mode": "frozen_research_bundle" if explicit_bundle else "active_production_bundle",
        "production_models": [
            {
                "model_key": key,
                "version_id": (versions.get(key) or {}).get("id"),
                "version": (versions.get(key) or {}).get("version"),
                "consumed": bool(availability[key]["available"]),
                "effective": bool(
                    availability[key]["available"]
                    and (
                        narrative_adjusted_count > 0
                        or narrative_veto_count > 0
                    )
                    if key == "narrative_transition_multitask_v1"
                    else availability[key]["available"]
                    and float(effective_weights.get(key) or 0.0) > 0
                ),
                "point_in_time_available": bool(availability[key]["available"]),
                "availability_reason": str(availability[key]["reason"]),
                "health_status": health_controls[key]["status"],
                "weight_multiplier": health_controls[key]["weight_multiplier"],
                "effect_contract": (
                    NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
                    if key == "narrative_transition_multitask_v1"
                    else None
                ),
            }
            for key in MODEL_KEYS
        ],
        "input_count": len(ranked),
        "output_count": len(reranked),
        "blocked_count": len(blocked),
        "policy": {
            "base_rank_weight": round(base_rank_weight, 6),
            "model_weights": MODEL_WEIGHTS,
            "environment": environment_key,
            "environment_multipliers": environment_multipliers,
            "narrative_model_lifecycle_control": {
                "effect_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
                "rank_adjusted_count": narrative_adjusted_count,
                "hard_veto_count": narrative_veto_count,
                "maximum_rank_adjustment": (
                    MAX_NARRATIVE_MODEL_RANK_ADJUSTMENT
                ),
                "minimum_confidence": NARRATIVE_MODEL_MIN_CONFIDENCE,
                "hard_veto_minimum_confidence": (
                    NARRATIVE_MODEL_EXIT_CONFIDENCE
                ),
                "hard_veto_exit_risk": NARRATIVE_MODEL_HARD_VETO_RISK,
                "unthemed_candidates_remain_eligible": True,
            },
            "maturity_multipliers": maturity_multipliers,
            "effective_model_weights": {key: round(weight, 6) for key, weight in effective_weights.items()},
            "theme_policy": {
                "maximum_rank_bonus": MAX_THEME_RANK_BONUS,
                "high_confidence_theme_is_bonus_only": True,
                "unmapped_stock_allowed": True,
                "hard_veto_states": ["衰退", "退潮", "沉寂", "false", "invalid", "risk", "rejected", "fabricated"],
            },
            "capital_new_position_block_percentile": 0.10,
            "capital_multihead_gate": {
                "direction_probability_below": 0.35,
                "strength_p10_below": 0.0,
                "failure_hazard_above": 0.65,
                "all_conditions_required": True,
            },
            "add_position_multihead_gate": {
                "direction_probability_minimum": 0.45,
                "failure_hazard_maximum": 0.55,
            },
            "disabled_gates": [
                "capital_persistence_gate"
                for key in ("capital_persistence_multitask_v1",)
                if not availability[key]["available"] or health_controls[key]["status"] not in {"healthy", "production"}
            ],
        },
    }
    return reranked, evidence, blocked, status


def apply_production_model_signals(
    *,
    ranked: list[tuple[SnapshotRow, float]],
    factor_values: dict[str, dict[str, float]],
    histories: dict[str, list[HistBar]],
    themes: list[dict[str, Any]],
) -> tuple[list[tuple[SnapshotRow, float]], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Backward-compatible production entry point."""
    return apply_model_signals(
        ranked=ranked,
        factor_values=factor_values,
        histories=histories,
        themes=themes,
    )

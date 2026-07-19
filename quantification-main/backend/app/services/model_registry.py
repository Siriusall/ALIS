from __future__ import annotations

from pathlib import Path
from typing import Any
import math

from app.db.store import (
    get_active_model_version,
    get_latest_model_train_run,
    get_model_train_run,
    get_model_version,
    list_model_versions,
    update_model_version_status,
)


MODEL_KEY = "main"

# Fixed model constitution from the engineering plan.  Training/promotion must
# report which components are available instead of silently replacing them
# with the legacy formula scorer.
MODEL_CONSTITUTION: dict[str, dict[str, Any]] = {
    "regime": {"model": "HSMM+BOCPD", "status": "hsmm_duration_bocpd_v1"},
    "dynamic_factors": {"model": "Kalman/state-space", "status": "local_level_kalman_v1"},
    "cross_section": {"model": "LightGBM", "status": "legacy_hgbr_fallback"},
    "volatility": {"model": "GARCH", "status": "garch11_v1"},
    "portfolio_risk": {"model": "CVaR", "status": "empirical_cvar_v1"},
}


def population_stability_index(expected: list[float], actual: list[float], bins: int = 10) -> float:
    """Compute PSI for a feature drift check; zero means identical bins."""
    if not expected or not actual:
        return 0.0
    ordered = sorted(float(x) for x in expected)
    cuts = [ordered[min(len(ordered) - 1, int(len(ordered) * i / bins))] for i in range(1, bins)]
    def bucket(value: float) -> int:
        return sum(1 for cut in cuts if value > cut)
    e_counts = [0] * bins
    a_counts = [0] * bins
    for value in expected:
        e_counts[bucket(float(value))] += 1
    for value in actual:
        a_counts[bucket(float(value))] += 1
    e_total, a_total = len(expected), len(actual)
    psi = 0.0
    for e, a in zip(e_counts, a_counts):
        ep = max(e / e_total, 1e-6)
        ap = max(a / a_total, 1e-6)
        psi += (ap - ep) * math.log(ap / ep)
    return round(float(psi), 6)


def evaluate_model_drift(expected: dict[str, list[float]], actual: dict[str, list[float]]) -> dict[str, Any]:
    values = {key: population_stability_index(expected.get(key, []), actual.get(key, [])) for key in set(expected) | set(actual)}
    max_psi = max(values.values(), default=0.0)
    return {"features": values, "max_psi": max_psi, "status": "alert" if max_psi >= 0.25 else "watch" if max_psi >= 0.10 else "stable", "thresholds": {"watch": 0.10, "alert": 0.25}}


def compare_champion_challenger(champion: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    """Compare versions without allowing a noisy single metric to win."""
    cm, xm = champion.get("metrics") or {}, challenger.get("metrics") or {}
    keys = ("lift", "walk_forward_lift", "hit_rate", "walk_forward_hit_rate")
    deltas = {key: round(float(xm.get(key, 0) or 0) - float(cm.get(key, 0) or 0), 6) for key in keys}
    validation = challenger.get("validation") or {}
    from app.services.review_learning import model_feedback_gate
    feedback = model_feedback_gate(str(challenger.get("version") or ""))
    gates = {
        "validation_passed": str(validation.get("status")) == "passed",
        "holdout_lift_positive": float(xm.get("lift", 0) or 0) > 0,
        "walk_forward_lift_positive": float(xm.get("walk_forward_lift", 0) or 0) > 0,
        "hit_rate_not_materially_lower": deltas["hit_rate"] >= -0.02,
        "review_feedback_passed": bool(feedback.get("passed")),
    }
    config_a, config_b = champion.get("config") or {}, challenger.get("config") or {}
    config_keys = sorted(set(config_a) | set(config_b))
    config_diff = {key: {"champion": config_a.get(key), "challenger": config_b.get(key)} for key in config_keys if config_a.get(key) != config_b.get(key)}
    reasons = [key for key, passed in gates.items() if not passed]
    return {
        "champion_id": champion.get("id"), "challenger_id": challenger.get("id"),
        "champion_version": champion.get("version"), "challenger_version": challenger.get("version"),
        "deltas": deltas, "config_diff": config_diff, "gates": gates, "review_feedback": feedback,
        "decision": "promote_challenger" if not reasons else "keep_champion",
        "blocked_reasons": reasons,
    }


def models_root() -> Path:
    root = Path(__file__).resolve().parents[2] / ".cache" / "models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def model_artifact_status(version: dict[str, Any] | None) -> dict[str, Any]:
    if not version:
        return {"ok": False, "status": "missing_version", "reason": "model_version_not_found"}
    path = str(version.get("artifact_path") or "").strip()
    if not path:
        return {"ok": False, "status": "missing_artifact_path", "reason": "artifact_path_empty"}
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "status": "artifact_missing", "reason": "artifact_file_not_found", "artifact_path": path}
    return {"ok": True, "status": "ready", "artifact_path": path}


def reconcile_active_model_artifact(model_key: str = MODEL_KEY) -> dict[str, Any] | None:
    active = get_active_model_version(model_key)
    status = model_artifact_status(active)
    if active and not status.get("ok"):
        update_model_version_status(int(active["id"]), status=str(status["status"]), is_active=False)
        return {**active, "artifact_status": status, "is_active": False, "status": str(status["status"])}
    return None


def get_model_status() -> dict[str, Any]:
    invalid_active = reconcile_active_model_artifact(MODEL_KEY)
    active = get_active_model_version(MODEL_KEY)
    latest_run = get_latest_model_train_run()
    versions = list_model_versions(MODEL_KEY, limit=10)
    active_artifact = model_artifact_status(active) if active else {"ok": False, "status": "no_active_model"}
    from app.services.review_learning import model_feedback_gate
    version_feedback = {str(item.get("version") or ""): model_feedback_gate(str(item.get("version") or "")) for item in versions}
    return {
        "model_key": MODEL_KEY,
        "active_version": active,
        "active_artifact": active_artifact,
        "invalid_active_version": invalid_active,
        "latest_train_run": latest_run,
        "versions": versions,
        "review_feedback": version_feedback,
        "has_active_model": active is not None and bool(active_artifact.get("ok")),
        "constitution": MODEL_CONSTITUTION,
    }


def get_model_constitution() -> dict[str, Any]:
    return {"model_key": MODEL_KEY, "components": MODEL_CONSTITUTION}


def get_train_run_status(run_id: int) -> dict[str, Any] | None:
    return get_model_train_run(run_id)


def get_model_version_or_active(model_version_id: int | None = None) -> dict[str, Any] | None:
    if model_version_id:
        return get_model_version(int(model_version_id))
    return get_active_model_version(MODEL_KEY)

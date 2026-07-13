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
    challenger_wins = deltas["lift"] > 0 and deltas["walk_forward_lift"] > 0 and deltas["hit_rate"] >= -0.02
    return {"champion_id": champion.get("id"), "challenger_id": challenger.get("id"), "deltas": deltas, "decision": "promote_challenger" if challenger_wins else "keep_champion"}


def models_root() -> Path:
    root = Path(__file__).resolve().parents[2] / ".cache" / "models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_model_status() -> dict[str, Any]:
    active = get_active_model_version(MODEL_KEY)
    latest_run = get_latest_model_train_run()
    versions = list_model_versions(MODEL_KEY, limit=10)
    return {
        "model_key": MODEL_KEY,
        "active_version": active,
        "latest_train_run": latest_run,
        "versions": versions,
        "has_active_model": active is not None,
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

from __future__ import annotations

import json
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.db.store import get_active_model_version, get_model_version
from app.services.backtest_data_source import BacktestDataSource, SnapshotRow
from app.services.backtest_engine import _factor_value
from app.services.model_registry import MODEL_KEY
from app.services.quant_risk import model_risk_multiplier


def _safe_float(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


@lru_cache(maxsize=16)
def _load_artifact(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"model artifact not found: {path}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    if payload.get("model_path"):
        try:
            with Path(str(payload["model_path"])).open("rb") as f:
                payload["_model"] = pickle.load(f)
        except Exception:
            payload["_model"] = None
    return payload


def _version(model_version_id: int | None = None) -> dict[str, Any] | None:
    if model_version_id:
        return get_model_version(int(model_version_id))
    return get_active_model_version(MODEL_KEY)


def score_snapshot_rows(
    *,
    ds: BacktestDataSource,
    rows: list[SnapshotRow],
    signal_date: str,
    model_version_id: int | None = None,
) -> list[tuple[SnapshotRow, float]]:
    version = _version(model_version_id)
    if not version:
        raise ValueError("没有可用模型版本，请先在模型策略页训练模型")
    artifact = _load_artifact(str(version.get("artifact_path") or ""))
    features = list(artifact.get("features") or [])
    daily_basic = ds.get_daily_basic_map(signal_date.replace("-", ""))
    matrix: list[list[float]] = []
    for row in rows:
        hist = ds.get_hist_window(row.symbol, signal_date, 60)
        matrix.append([_safe_float(_factor_value(f, row, hist, daily_basic)) for f in features])

    if artifact.get("model_type") == "sklearn_hgbr" and artifact.get("_model") is not None:
        preds = [float(x) for x in artifact["_model"].predict(matrix).tolist()]
    else:
        weights = dict(artifact.get("weights") or {})
        preds = [
            sum(_safe_float(weights.get(features[i], 0)) * _safe_float(v) for i, v in enumerate(row))
            for row in matrix
        ]
    ranked = list(zip(rows, preds))
    # Apply the constitution's bounded market-risk overlay to every candidate
    # using its own recent return evidence. Ranking remains model-driven, while
    # contraction/high-tail-risk regimes reduce portfolio aggressiveness.
    adjusted = []
    for row, pred in ranked:
        hist = ds.get_hist_window(row.symbol, signal_date, 30)
        closes = [_safe_float(getattr(x, "close", 0)) for x in hist]
        returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
        gate = model_risk_multiplier(returns)
        adjusted.append((row, float(pred) * float(gate["multiplier"])))
    ranked = adjusted
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def explain_model_version(model_version_id: int | None = None) -> dict[str, Any]:
    version = _version(model_version_id)
    if not version:
        return {}
    artifact = _load_artifact(str(version.get("artifact_path") or ""))
    return {
        "version": version,
        "features": artifact.get("features") or [],
        "model_type": artifact.get("model_type"),
        "horizon": artifact.get("horizon"),
        "metrics": artifact.get("metrics") or version.get("metrics") or {},
    }

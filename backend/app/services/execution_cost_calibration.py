from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from statistics import mean
from typing import Any

from app.db.store import (
    _conn,
    create_model_version,
    get_active_model_version,
    get_model_version,
    list_model_versions,
    set_active_model_version,
    update_model_version_status,
    update_model_version_validation,
)


EXECUTION_COST_MODEL_KEY = "execution_cost_model_v1"
MINIMUM_CANDIDATE_OPERATIONS = 20
MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE = 8
MINIMUM_ACTIVATION_OPERATIONS = 50
MINIMUM_ACTIVATION_OPERATIONS_PER_SIDE = 15
FALLBACK_COSTS = {
    "buy_rate": 0.0015,
    "sell_rate": 0.0025,
    "round_trip_rate": 0.0040,
    "source": "conservative_builtin_fallback",
    "includes_fees_and_slippage": True,
    "component_policy": {
        "commission": "actual_fee_schedule_with_cny5_minimum",
        "transfer_fee": "stocks_only",
        "stamp_tax": "stock_sell_only",
        "slippage": "0.10%_stock_0.05%_etf_each_side",
        "calibration_buffer": "rate_minus_explicit_components_if_positive",
    },
}


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil((len(ordered) - 1) * quantile)))
    return ordered[index]


def _calibration_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_side: dict[str, list[float]] = {"BUY": [], "SELL": []}
    fee_rates: list[float] = []
    slippages: list[float] = []
    for row in rows:
        side = str(row.get("side") or "").upper()
        if side not in by_side:
            continue
        notional = _safe(row.get("price")) * _safe(row.get("quantity"))
        if notional <= 0:
            continue
        fee = (
            _safe(row.get("commission"))
            + _safe(row.get("transfer_fee"))
            + _safe(row.get("stamp_tax"))
        ) / notional
        slippage = abs(_safe(row.get("slippage_bps"))) / 10000.0
        rate = max(0.0, fee + slippage)
        by_side[side].append(rate)
        fee_rates.append(fee)
        slippages.append(slippage)
    buy = max(0.0003, min(0.02, _percentile(by_side["BUY"], 0.75) or FALLBACK_COSTS["buy_rate"]))
    sell = max(0.0003, min(0.02, _percentile(by_side["SELL"], 0.75) or FALLBACK_COSTS["sell_rate"]))
    return {
        "operation_count": len(by_side["BUY"]) + len(by_side["SELL"]),
        "buy_count": len(by_side["BUY"]),
        "sell_count": len(by_side["SELL"]),
        "buy_rate": round(buy, 8),
        "sell_rate": round(sell, 8),
        "round_trip_rate": round(buy + sell, 8),
        "average_fee_rate": round(mean(fee_rates), 8) if fee_rates else 0.0,
        "average_slippage_rate": round(mean(slippages), 8) if slippages else 0.0,
        "estimator": "side_specific_p75_realized_fee_plus_simulated_slippage_v1",
    }


def _sample_gate(metrics: dict[str, Any]) -> dict[str, bool]:
    """A side-specific estimator is not trustworthy without both trade sides."""
    total = int(metrics.get("operation_count") or 0)
    buy = int(metrics.get("buy_count") or 0)
    sell = int(metrics.get("sell_count") or 0)
    return {
        "minimum_20_operations": total >= MINIMUM_CANDIDATE_OPERATIONS,
        "minimum_8_operations_per_side": (
            buy >= MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE
            and sell >= MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE
        ),
        "automatic_activation_50_operations": total >= MINIMUM_ACTIVATION_OPERATIONS,
        "automatic_activation_15_operations_per_side": (
            buy >= MINIMUM_ACTIVATION_OPERATIONS_PER_SIDE
            and sell >= MINIMUM_ACTIVATION_OPERATIONS_PER_SIDE
        ),
    }


def active_execution_cost_contract() -> dict[str, Any]:
    active = get_active_model_version(EXECUTION_COST_MODEL_KEY)
    config = (active or {}).get("config") if isinstance((active or {}).get("config"), dict) else {}
    rates = config.get("rates") if isinstance(config.get("rates"), dict) else {}
    if not active or not rates:
        return {
            **FALLBACK_COSTS,
            "model_version_id": None,
            "version": "builtin",
            "sample_count": 0,
        }
    return {
        "buy_rate": _safe(rates.get("buy_rate")) or FALLBACK_COSTS["buy_rate"],
        "sell_rate": _safe(rates.get("sell_rate")) or FALLBACK_COSTS["sell_rate"],
        "round_trip_rate": _safe(rates.get("round_trip_rate")) or FALLBACK_COSTS["round_trip_rate"],
        "source": "active_simulation_cost_calibration",
        "includes_fees_and_slippage": True,
        "component_policy": FALLBACK_COSTS["component_policy"],
        "model_version_id": int(active.get("id") or 0),
        "version": str(active.get("version") or ""),
        "sample_count": int(((active.get("metrics") or {}).get("operation_count")) or 0),
    }


def calibrate_execution_cost_model(*, limit: int = 500) -> dict[str, Any]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT id,side,price,quantity,commission,transfer_fee,
                          stamp_tax,slippage_bps,at
                   FROM operations
                   WHERE quantity>0 AND price>0
                   ORDER BY id DESC LIMIT ?""",
                (max(20, min(int(limit), 5000)),),
            ).fetchall()
        ]
    metrics = _calibration_metrics(rows)
    sample_gate = _sample_gate(metrics)
    candidate_ready = (
        sample_gate["minimum_20_operations"]
        and sample_gate["minimum_8_operations_per_side"]
    )
    if not candidate_ready:
        return {
            "ok": True,
            "status": (
                "insufficient_observations"
                if not sample_gate["minimum_20_operations"] else "insufficient_side_coverage"
            ),
            "minimum_operations": MINIMUM_CANDIDATE_OPERATIONS,
            "minimum_operations_per_side": MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE,
            "gates": sample_gate,
            "metrics": metrics,
            "active": active_execution_cost_contract(),
        }
    signature = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    latest = list_model_versions(EXECUTION_COST_MODEL_KEY, limit=1)
    if latest and str(((latest[0].get("config") or {}).get("signature")) or "") == signature:
        return {
            "ok": True,
            "status": "unchanged",
            "candidate": latest[0],
            "active": active_execution_cost_contract(),
        }
    now = datetime.now().astimezone()
    version = f"execution-cost-{now.strftime('%Y%m%d%H%M%S')}-{signature[:6]}"
    validation = {
        "status": (
            "passed"
            if sample_gate["automatic_activation_50_operations"]
            and sample_gate["automatic_activation_15_operations_per_side"] else "observing"
        ),
        "production_eligible": (
            sample_gate["automatic_activation_50_operations"]
            and sample_gate["automatic_activation_15_operations_per_side"]
        ),
        "gates": {
            **sample_gate,
            "bounded_rate": (
                0.0003 <= float(metrics["buy_rate"]) <= 0.02
                and 0.0003 <= float(metrics["sell_rate"]) <= 0.02
            ),
        },
        "simulation_only": True,
    }
    version_id = create_model_version(
        model_key=EXECUTION_COST_MODEL_KEY,
        version=version,
        status="candidate",
        artifact_path="builtin://execution-cost-calibration/operations-ledger",
        config={
            "signature": signature,
            "rates": {
                "buy_rate": metrics["buy_rate"],
                "sell_rate": metrics["sell_rate"],
                "round_trip_rate": metrics["round_trip_rate"],
            },
            "history_immutable": True,
        },
        metrics=metrics,
        validation=validation,
        is_active=False,
    )
    promoted = False
    if validation["production_eligible"] and all(validation["gates"].values()):
        set_active_model_version(EXECUTION_COST_MODEL_KEY, version_id)
        update_model_version_status(version_id, "active")
        update_model_version_validation(
            version_id,
            {
                **validation,
                "status": "passed",
                "auto_governance": {
                    "decision": "promoted",
                    "rule": "50_operation_15_per_side_bounded_simulation_cost_gate_v2",
                    "at": now.isoformat(),
                },
            },
        )
        promoted = True
    return {
        "ok": True,
        "status": "promoted" if promoted else "observing",
        "candidate": get_model_version(version_id),
        "active": active_execution_cost_contract(),
        "metrics": metrics,
    }

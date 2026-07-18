"""Deterministic market sentiment and theme/narrative scoring primitives.

The engine consumes normalized observations; it does not scrape or infer
facts itself. This keeps source provenance in the data platform while making
the scoring contract reproducible in backtests and simulation.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime
import json
import math
import os
from statistics import median

from app.data_platform.db import _conn
from app.data_platform.redis_store import get_json, set_json


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def score_market_sentiment(
    *,
    breadth: float,
    average_return: float,
    volume_ratio: float = 1.0,
    limit_up_ratio: float = 0.0,
    limit_down_ratio: float = 0.0,
    northbound_flow_z: float = 0.0,
    leader_persistence: float = 0.0,
    price_fundamental_divergence: float = 0.0,
    crowding: float = 0.0,
) -> dict[str, Any]:
    """Return a bounded sentiment score with fixed plan weights.

    Inputs use normalized ratios/z-scores supplied by the data layer. The
    weights exactly follow plan A3.10. Crowding is a separate 0-20 penalty.
    """
    breadth_component = _clip((float(breadth) - 0.5) * 2.0)
    return_component = _clip(float(average_return) / 0.03)
    volume_component = _clip((float(volume_ratio) - 1.0) / 0.8)
    limit_component = _clip((float(limit_up_ratio) - float(limit_down_ratio)) / 0.08)
    risk_preference_component = _clip(((float(limit_up_ratio) - float(limit_down_ratio)) / 0.08 + float(northbound_flow_z) / 2.0) / 2.0)
    leader_component = _clip(float(leader_persistence))
    divergence_component = _clip(-float(price_fundamental_divergence))
    raw = (
        0.20 * breadth_component
        + 0.20 * return_component
        + 0.15 * volume_component
        + 0.15 * risk_preference_component
        + 0.15 * leader_component
        + 0.15 * divergence_component
    )
    crowding_penalty = round(20.0 * max(0.0, min(1.0, float(crowding))), 4)
    base_score = 50.0 + 50.0 * _clip(raw)
    score = round(max(0.0, base_score - crowding_penalty), 4)
    state = "risk_on" if score >= 65 else "risk_off" if score <= 35 else "neutral"
    return {
        "score": score,
        "state": state,
        "components": {
            "breadth": round(breadth_component, 6),
            "return": round(return_component, 6),
            "volume": round(volume_component, 6),
            "limit_up_down": round(limit_component, 6),
            "risk_preference": round(risk_preference_component, 6),
            "leader_persistence": round(leader_component, 6),
            "price_fundamental_alignment": round(divergence_component, 6),
        },
        "weights": {"breadth_limit_structure": 0.20, "money_effect": 0.20, "risk_preference": 0.15, "volume_liquidity": 0.15, "leader_persistence": 0.15, "price_fundamental_alignment": 0.15},
        "base_score": round(base_score, 4), "crowding_penalty": crowding_penalty,
    }


def score_theme_narratives(themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank themes using strength, breadth, persistence and source diversity.

    A theme is not promotable unless it has at least two independent source
    types, five related instruments, and three observed sessions. These are
    the plan's anti-single-source and anti-one-day-noise gates.
    """
    result: list[dict[str, Any]] = []
    for item in themes:
        name = str(item.get("theme") or item.get("name") or "").strip()
        if not name:
            continue
        strength = _clip(float(item.get("narrative_strength", item.get("strength", 0.0)) or 0.0))
        price_confirmation = _clip(float(item.get("price_confirmation", 0.0) or 0.0))
        breadth = _clip(float(item.get("breadth", 0.0) or 0.0))
        persistence = _clip(float(item.get("capital_persistence", item.get("persistence", 0.0)) or 0.0))
        fundamental = _clip(float(item.get("fundamental_verifiability", 0.0) or 0.0))
        catalyst = _clip(float(item.get("catalyst_duration", item.get("persistence", 0.0)) or 0.0))
        credibility = _clip(float(item.get("source_policy_credibility", item.get("source_diversity", 0.0)) or 0.0))
        crowding = max(0.0, min(1.0, float(item.get("crowding", 0.0) or 0.0)))
        base_score = 100.0 * (0.15 * strength + 0.15 * price_confirmation + 0.15 * breadth + 0.15 * persistence + 0.20 * fundamental + 0.10 * catalyst + 0.10 * credibility)
        crowding_penalty = 20.0 * crowding
        score = round(max(0.0, base_score - crowding_penalty), 4)
        source_types = int(item.get("source_types", 0) or 0)
        related_instruments = int(item.get("related_instruments", 0) or 0)
        sessions = int(item.get("sessions", 0) or 0)
        evidence_qualified = source_types >= 2 and related_instruments >= 5 and sessions >= 3
        promotable = evidence_qualified and score >= 60
        result.append({
            "theme": name,
            "score": score,
            "promotable": promotable,
            "evidence_qualified": evidence_qualified,
            "state": "confirmed" if promotable else "watch",
            "components": {"narrative_strength": strength, "price_confirmation": price_confirmation, "breadth": breadth, "capital_persistence": persistence, "fundamental_verifiability": fundamental, "catalyst_duration": catalyst, "source_policy_credibility": credibility},
            "weights": {"narrative_strength": .15, "price_confirmation": .15, "breadth": .15, "capital_persistence": .15, "fundamental_verifiability": .20, "catalyst_duration": .10, "source_policy_credibility": .10},
            "base_score": round(base_score, 4), "crowding_penalty": round(crowding_penalty, 4),
            "evidence": {"source_types": source_types, "related_instruments": related_instruments, "sessions": sessions},
        })
    return sorted(result, key=lambda x: float(x["score"]), reverse=True)


REGIME_LABELS = {"S0": "风险扩张", "S1": "震荡分化", "S2": "风险收缩", "S3": "恐慌修复"}
HSMM_MINIMUM_LIVE_OBSERVATION_WEEKS = max(
    1, int(os.getenv("HSMM_MINIMUM_LIVE_OBSERVATION_WEEKS", "4"))
)


def _transparent_regime(score: float, average_return: float, previous_state: str = "") -> str:
    if score >= 65 and average_return > 0:
        return "S0"
    if score <= 40 and average_return < 0:
        return "S2"
    if previous_state == "S2" and score >= 50 and average_return > 0:
        return "S3"
    return "S1"


def _normal_pdf(value: float, mean: float, variance: float) -> float:
    variance = max(1e-6, float(variance))
    return math.exp(-0.5 * (value - mean) ** 2 / variance) / math.sqrt(2 * math.pi * variance)


def bocpd_change_alarm(signals: list[float], *, hazard: float = 1 / 60, max_run_length: int = 120) -> dict[str, Any]:
    """Bayesian online change-point detection on a robust composite market signal."""
    if not signals:
        return {"p_change_1d": 0.0, "p_change_5d": 0.0, "run_length": 0, "status": "insufficient_data", "risk_multiplier": 1.0, "method": "bocpd_gaussian_v1"}
    run_probs = [1.0]
    change_history: list[float] = []
    values: list[float] = []
    for signal in signals:
        predictive: list[float] = []
        for run_length in range(len(run_probs)):
            segment = values[-run_length:] if run_length else []
            center = sum(segment) / len(segment) if segment else 0.0
            variance = sum((item - center) ** 2 for item in segment) / max(1, len(segment) - 1) if len(segment) >= 2 else 1.0
            predictive.append(_normal_pdf(float(signal), center, variance + .25))
        cp_likelihood = _normal_pdf(float(signal), 0.0, 2.0)
        next_probs = [sum(run_probs) * hazard * cp_likelihood]
        next_probs.extend(run_probs[index] * (1.0 - hazard) * predictive[index] for index in range(len(run_probs)))
        next_probs = next_probs[:max(2, int(max_run_length) + 1)]
        total = sum(next_probs) or 1.0
        run_probs = [value / total for value in next_probs]
        change_history.append(run_probs[0])
        values.append(float(signal))
    p1, p5 = float(change_history[-1]), max(change_history[-5:])
    status = "critical" if p1 >= .80 else "pending_confirmation" if p1 >= .60 else "stable"
    return {"p_change_1d": round(p1, 6), "p_change_5d": round(p5, 6), "run_length": max(range(len(run_probs)), key=run_probs.__getitem__), "status": status, "risk_multiplier": .70 if p1 >= .80 else 1.0, "method": "bocpd_gaussian_v1"}


def _market_change_signal(history: list[dict[str, Any]], current: dict[str, Any]) -> float:
    keys = ("breadth", "average_return", "volume_ratio", "limit_up_ratio", "limit_down_ratio", "crowding")
    z_values: list[float] = []
    for key in keys:
        values = [float(item[key]) for item in history[-30:] if item.get(key) is not None]
        if len(values) < 5 or current.get(key) is None:
            continue
        center = median(values)
        mad = median(abs(value - center) for value in values) or 1e-6
        z_values.append(max(-6.0, min(6.0, (float(current[key]) - center) / (1.4826 * mad))))
    return sum(z_values) / len(z_values) if z_values else 0.0


def market_state_model_governance() -> dict[str, Any]:
    with _conn() as c:
        row = c.execute(
            "SELECT MIN(trade_date) first_date,MAX(trade_date) last_date,COUNT(DISTINCT trade_date) sessions FROM dc_tushare_raw_records WHERE api_name='daily'"
        ).fetchone()
        training_row = c.execute(
            "SELECT MIN(feature_date) first_date,MAX(feature_date) last_date,COUNT(*) observations FROM model_training_history WHERE feature_set='market_regime_index_v1'"
        ).fetchone()
    item = dict(row) if row else {}
    first, last = str(item.get("first_date") or ""), str(item.get("last_date") or "")
    try:
        years = (datetime.strptime(last, "%Y%m%d") - datetime.strptime(first, "%Y%m%d")).days / 365.25
    except ValueError:
        years = 0.0
    training = dict(training_row) if training_row else {}
    try:
        index_years = (datetime.strptime(str(training.get("last_date") or ""), "%Y%m%d") - datetime.strptime(str(training.get("first_date") or ""), "%Y%m%d")).days / 365.25
    except ValueError:
        index_years = 0.0
    training_ready = index_years >= 6.0
    try:
        from app.db.store import list_model_versions
        shadow_versions = list_model_versions("market_regime", limit=5)
    except Exception:  # noqa: BLE001
        shadow_versions = []
    latest_shadow = shadow_versions[0] if shadow_versions else None
    shadow_validation = (latest_shadow or {}).get("validation") or {}
    review = shadow_validation.get("review") or {}
    oos_end = str(shadow_validation.get("oos_end") or "")
    live_observation_weeks = 0.0
    live_observation_sessions = 0
    live_status = "shadow_blocked_insufficient_history"
    blocking_reason = "训练历史不足六年"
    live_observation_start = None
    live_observation_end = last or None
    if shadow_validation.get("status") == "passed":
        blocking_reason = "历史OOS已通过，仍需累计真实影子期并完成上线评审"
        live_status = "shadow_oos_validated_live_observation_pending"
        if oos_end and last:
            try:
                oos_end_dt = datetime.strptime(oos_end, "%Y%m%d")
                last_dt = datetime.strptime(last, "%Y%m%d")
                delta_days = max(0, (last_dt - oos_end_dt).days)
                live_observation_weeks = delta_days / 7.0
                live_observation_sessions = max(0, int(item.get("sessions") or 0) - int(training.get("observations") or 0))
                if delta_days > 0:
                    live_observation_start = oos_end
                    live_status = "shadow_live_observation"
                else:
                    # OOS validated with all available data; start live observation from now
                    live_observation_start = last
                    live_status = "shadow_oos_validated_live_observation_pending"
                    live_observation_weeks = 0.0
                if live_observation_weeks >= HSMM_MINIMUM_LIVE_OBSERVATION_WEEKS:
                    live_status = "shadow_live_observation_review_ready"
                    blocking_reason = "历史OOS已通过且真实影子观察期达标，待上线评审"
            except ValueError:
                pass
    elif latest_shadow:
        live_status = "shadow_validation_failed"
        blocking_reason = "HSMM参数拟合或OOS验证尚未通过"
    elif training_ready:
        live_status = "shadow_ready_for_implementation_and_validation"
        blocking_reason = "HSMM参数拟合或OOS验证尚未通过"
    review_status = str(review.get("status") or "")
    review_required = live_status == "shadow_live_observation_review_ready"
    review_completed = review_status in {"approved", "rejected"}
    production_eligible = review_required and review_status == "approved"
    if production_eligible:
        live_status = "shadow_promoted_ready"
        blocking_reason = ""
    elif review_status == "rejected":
        live_status = "shadow_review_rejected"
        blocking_reason = str(review.get("comment") or "上线评审未通过")
    return {
        # Having enough history only opens the shadow-validation gate.  It must
        # never silently promote a regime model without persisted OOS evidence.
        "production_model": "transparent_four_state_v1",
        "hsmm": {"status": live_status, "history_years": round(index_years, 3), "breadth_history_years": round(years, 3), "minimum_years": 6, "state_count": 4, "observation_distribution": "multivariate_student_t", "duration_distribution": "truncated_negative_binomial", "duration_weeks": [2, 52], "production_eligible": production_eligible, "blocking_reason": blocking_reason, "latest_shadow_version": (latest_shadow or {}).get("version"), "validation": shadow_validation, "review_required": review_required, "review_completed": review_completed, "review": review, "live_observation_start": live_observation_start, "live_observation_end": live_observation_end, "live_observation_weeks": round(live_observation_weeks, 3), "live_observation_sessions": live_observation_sessions, "minimum_live_observation_weeks": HSMM_MINIMUM_LIVE_OBSERVATION_WEEKS},
        "bocpd": {"status": "active", "model": "bocpd_gaussian_v1"},
        "history": {"first_date": first, "last_date": last, "sessions": int(item.get("sessions") or 0)},
        "index_training_history": {"first_date": training.get("first_date"), "last_date": training.get("last_date"), "observations": int(training.get("observations") or 0), "history_years": round(index_years, 3), "status": "ready" if index_years >= 6 else "insufficient"},
    }


def materialize_market_training_history() -> dict[str, Any]:
    """Build the small 6-10 year index feature layer required by the model constitution."""
    index_codes = ("000300.SH", "000905.SH", "000852.SH")
    placeholders = ",".join("?" for _ in index_codes)
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT ts_code,trade_date,payload_json,available_time FROM dc_tushare_raw_records
                WHERE api_name='index_daily' AND ts_code IN ({placeholders})
                ORDER BY ts_code,trade_date""", index_codes,
        ).fetchall()]
        by_code: dict[str, dict[str, dict[str, Any]]] = {code: {} for code in index_codes}
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            close = payload.get("close")
            if close is None:
                continue
            by_code[str(row.get("ts_code") or "")][str(row.get("trade_date") or "")] = {"close": float(close), "amount": float(payload.get("amount") or 0), "available_time": row.get("available_time")}
        common_dates = sorted(set.intersection(*(set(by_code[code]) for code in index_codes))) if all(by_code.values()) else []
        written = 0
        now = datetime.now().isoformat(timespec="seconds")
        for index, feature_date in enumerate(common_dates):
            if index < 120:
                continue
            features: dict[str, Any] = {}
            source_times: list[str] = []
            for code in index_codes:
                closes = [by_code[code][value]["close"] for value in common_dates[:index + 1]]
                returns = [closes[position] / closes[position - 1] - 1.0 for position in range(max(1, len(closes) - 120), len(closes)) if closes[position - 1] > 0]
                prefix = code.split(".")[0]
                for window in (20, 60, 120):
                    features[f"{prefix}_return_{window}d"] = closes[-1] / closes[-window - 1] - 1.0 if len(closes) > window and closes[-window - 1] > 0 else None
                recent_returns = returns[-20:]
                features[f"{prefix}_realized_vol_20d"] = (sum(value * value for value in recent_returns) / len(recent_returns)) ** .5 if recent_returns else None
                features[f"{prefix}_amount"] = by_code[code][feature_date]["amount"]
                source_times.append(str(by_code[code][feature_date].get("available_time") or ""))
            c.execute(
                """INSERT INTO model_training_history(feature_date,feature_set,features_json,source_as_of_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(feature_date) DO UPDATE SET feature_set=excluded.feature_set,
                   features_json=excluded.features_json,source_as_of_time=excluded.source_as_of_time,updated_at=excluded.updated_at""",
                (feature_date, "market_regime_index_v1", json.dumps(features, ensure_ascii=False), max(source_times), now, now),
            )
            written += 1
    return {"ok": True, "feature_set": "market_regime_index_v1", "source_rows": len(rows), "common_dates": len(common_dates), "written": written, "range": [common_dates[0], common_dates[-1]] if common_dates else []}


def live_market_context() -> dict[str, Any]:
    cached = get_json("alis:market-context:live:v2")
    if isinstance(cached, dict):
        return cached
    with _conn() as c:
        dates = [str(dict(row)["trade_date"]) for row in c.execute(
            """SELECT DISTINCT trade_date FROM dc_tushare_raw_records
               WHERE api_name='daily' ORDER BY trade_date DESC LIMIT 6"""
        ).fetchall()]
        if len(dates) < 2:
            return {"ok": False, "status": "insufficient_data", "reason": "至少需要两个交易日行情"}
        latest, previous = dates[0], dates[1]
        breadth_row = dict(c.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision>0 THEN 1 ELSE 0 END) rising,
                      AVG(NULLIF(payload_json::jsonb->>'pct_chg','')::double precision/100.0) average_return
               FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?""", (latest,)
        ).fetchone())
        limit_row = dict(c.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision>=9.7 THEN 1 ELSE 0 END) limit_up,
                      SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision<=-9.7 THEN 1 ELSE 0 END) limit_down
               FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?""", (latest,)
        ).fetchone())
        amount_rows = [dict(row) for row in c.execute(
            """SELECT trade_date,SUM(NULLIF(payload_json::jsonb->>'amount','')::double precision) total_amount
               FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date IN (?,?,?,?,?,?)
               GROUP BY trade_date""", tuple(dates)
        ).fetchall()]
        concentration_row = dict(c.execute(
            """WITH values AS (
                 SELECT NULLIF(payload_json::jsonb->>'amount','')::double precision amount
                 FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?),
               ranked AS (SELECT amount,NTILE(20) OVER(ORDER BY amount DESC) bucket FROM values WHERE amount IS NOT NULL)
               SELECT SUM(CASE WHEN bucket=1 THEN amount ELSE 0 END)/NULLIF(SUM(amount),0) concentration FROM ranked""", (latest,)
        ).fetchone())
        previous_context = c.execute(
            "SELECT market_state FROM dc_market_context_snapshot WHERE trade_date<? ORDER BY trade_date DESC LIMIT 1",
            (latest,),
        ).fetchone()
        history_rows = [dict(row) for row in c.execute(
            "SELECT observations_json,result_json FROM dc_market_context_snapshot WHERE trade_date<? ORDER BY trade_date DESC LIMIT 60",
            (latest,),
        ).fetchall()]
    total = int(breadth_row.get("total") or 0)
    limit_total = int(limit_row.get("total") or total or 1)
    amount_by_date = {str(x["trade_date"]): float(x.get("total_amount") or 0) for x in amount_rows}
    prior_amounts = [amount_by_date[x] for x in dates[1:] if amount_by_date.get(x)]
    volume_ratio = amount_by_date.get(latest, 0) / (sum(prior_amounts) / len(prior_amounts)) if prior_amounts else 1.0
    concentration = float(concentration_row.get("concentration") or 0)
    crowding = max(0.0, min(1.0, (concentration - .30) / .30))
    observations = {
        "breadth": int(breadth_row.get("rising") or 0) / total if total else .5,
        "average_return": float(breadth_row.get("average_return") or 0),
        "volume_ratio": volume_ratio,
        "limit_up_ratio": int(limit_row.get("limit_up") or 0) / limit_total,
        "limit_down_ratio": int(limit_row.get("limit_down") or 0) / limit_total,
        "northbound_flow_z": 0.0,
        "leader_persistence": 0.0,
        "price_fundamental_divergence": 0.0,
        "crowding": crowding,
    }
    observation_history: list[dict[str, Any]] = []
    prior_signals: list[float] = []
    for row in reversed(history_rows):
        try:
            observation = json.loads(row.get("observations_json") or "{}")
            prior_result = json.loads(row.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        prior_signals.append(float((prior_result.get("change_alarm") or {}).get("composite_signal") or _market_change_signal(observation_history, observation)))
        observation_history.append(observation)
    composite_signal = _market_change_signal(observation_history, observations)
    change_alarm = bocpd_change_alarm(prior_signals + [composite_signal])
    change_alarm["composite_signal"] = round(composite_signal, 6)
    sentiment = score_market_sentiment(**observations)
    score = float(sentiment["score"])
    average_return = observations["average_return"]
    previous_state = str(dict(previous_context).get("market_state") or "") if previous_context else ""
    market_state = _transparent_regime(score, average_return, previous_state)
    confidence = min(.85, .45 + abs(score - 50) / 100)
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "ok": True, "status": "ready", "trade_date": latest,
        "market_state": {"state": market_state, "label": REGIME_LABELS[market_state], "confidence": round(confidence, 4), "model": "transparent_four_state_v1", "reason": "可用历史不足六年，按计划书使用透明四状态机；HSMM不冒充生产状态"},
        "sentiment": sentiment, "observations": observations, "change_alarm": change_alarm,
        "model_governance": market_state_model_governance(),
        "evidence": {"latest_date": latest, "previous_date": previous, "symbols": total, "amount_dates": len(amount_rows)},
        "limitations": ["北向资金、龙头持续性和价格基本面背离尚无合格日度观测，当前按中性值处理"],
        "available_time": now,
    }
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_market_context_snapshot(trade_date,market_state,state_confidence,sentiment_score,observations_json,result_json,model_version,available_time,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET market_state=excluded.market_state,state_confidence=excluded.state_confidence,
               sentiment_score=excluded.sentiment_score,observations_json=excluded.observations_json,result_json=excluded.result_json,model_version=excluded.model_version,
               available_time=excluded.available_time,updated_at=excluded.updated_at""",
            (latest, market_state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_four_state_v1", now, now, now),
        )
    set_json("alis:market-context:live:v2", payload, ttl=300)
    return payload


def market_context_history(limit: int = 120) -> dict[str, Any]:
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT trade_date,market_state,state_confidence,sentiment_score,observations_json,
                      model_version,available_time FROM dc_market_context_snapshot
               ORDER BY trade_date DESC LIMIT ?""", (limit,)
        ).fetchall()]
    for row in rows:
        try:
            row["observations"] = json.loads(row.pop("observations_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            row["observations"] = {}
    rows.reverse()
    transitions = []
    for previous, current in zip(rows, rows[1:]):
        if previous["market_state"] != current["market_state"]:
            transitions.append({"trade_date": current["trade_date"], "from": previous["market_state"], "to": current["market_state"], "confidence": current["state_confidence"]})
    return {"ok": True, "items": rows, "transitions": transitions, "count": len(rows)}


def market_context_as_of(as_of_time: str) -> dict[str, Any]:
    """Return the latest market context whose observation date is visible at the cutoff."""
    cutoff = str(as_of_time or "")
    trade_date = cutoff[:10].replace("-", "")
    with _conn() as c:
        row = c.execute(
            """SELECT trade_date,result_json,model_version,available_time
               FROM dc_market_context_snapshot WHERE trade_date<=?
               ORDER BY trade_date DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
    if not row:
        return {"ok": False, "status": "insufficient_data", "as_of_time": cutoff, "reason": "no_market_context_at_cutoff"}
    item = dict(row)
    try:
        payload = json.loads(item.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    payload["as_of_time"] = cutoff
    payload["snapshot_trade_date"] = item.get("trade_date")
    payload["point_in_time_method"] = "observation_date_cutoff_v1"
    return payload


def rebuild_market_context_history(limit: int = 120) -> dict[str, Any]:
    """Materialize historical transparent-state snapshots from raw daily bars."""
    with _conn() as c:
        date_rows = c.execute(
            """SELECT DISTINCT trade_date FROM dc_tushare_raw_records
               WHERE api_name='daily' ORDER BY trade_date DESC LIMIT ?""", (max(2, min(500, limit)),)
        ).fetchall()
        dates = list(reversed([str(dict(row).get("trade_date") or "") for row in date_rows]))
        written = 0
        observation_history: list[dict[str, Any]] = []
        change_signals: list[float] = []
        for index, trade_date in enumerate(dates):
            prior_dates = dates[max(0, index - 5):index]
            if not prior_dates:
                continue
            row = dict(c.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision>0 THEN 1 ELSE 0 END) rising,
                          AVG(NULLIF(payload_json::jsonb->>'pct_chg','')::double precision/100.0) average_return,
                          SUM(NULLIF(payload_json::jsonb->>'amount','')::double precision) amount,
                          SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision>=9.7 THEN 1 ELSE 0 END) limit_up,
                          SUM(CASE WHEN NULLIF(payload_json::jsonb->>'pct_chg','')::double precision<=-9.7 THEN 1 ELSE 0 END) limit_down
                   FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?""", (trade_date,)
            ).fetchone())
            placeholders = ",".join("?" for _ in prior_dates)
            prior = [dict(x) for x in c.execute(
                f"""SELECT trade_date,SUM(NULLIF(payload_json::jsonb->>'amount','')::double precision) amount
                    FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date IN ({placeholders}) GROUP BY trade_date""", tuple(prior_dates)
            ).fetchall()]
            prior_amounts = [float(x.get("amount") or 0) for x in prior if float(x.get("amount") or 0) > 0]
            total = int(row.get("total") or 0)
            if total <= 0:
                continue
            observations = {
                "breadth": int(row.get("rising") or 0) / total,
                "average_return": float(row.get("average_return") or 0),
                "volume_ratio": float(row.get("amount") or 0) / (sum(prior_amounts) / len(prior_amounts)) if prior_amounts else 1.0,
                "limit_up_ratio": int(row.get("limit_up") or 0) / total,
                "limit_down_ratio": int(row.get("limit_down") or 0) / total,
                "northbound_flow_z": 0.0, "leader_persistence": 0.0, "price_fundamental_divergence": 0.0, "crowding": 0.0,
            }
            sentiment = score_market_sentiment(**observations)
            composite_signal = _market_change_signal(observation_history, observations)
            change_signals.append(composite_signal)
            change_alarm = bocpd_change_alarm(change_signals)
            change_alarm["composite_signal"] = round(composite_signal, 6)
            score = float(sentiment["score"])
            average_return = float(observations["average_return"])
            previous_state = state if written else ""
            state = _transparent_regime(score, average_return, previous_state)
            confidence = min(.85, .45 + abs(score - 50) / 100)
            now = datetime.now().isoformat(timespec="seconds")
            payload = {"ok": True, "status": "ready", "trade_date": trade_date, "market_state": {"state": state, "label": REGIME_LABELS[state], "confidence": round(confidence, 4), "model": "transparent_four_state_v1", "reason": "historical_raw_daily_backfill"}, "sentiment": sentiment, "observations": observations, "change_alarm": change_alarm, "model_governance": {"production_model": "transparent_four_state_v1", "hsmm": {"status": "shadow_blocked_insufficient_history", "minimum_years": 6}, "bocpd": {"status": "active", "model": "bocpd_gaussian_v1"}}, "evidence": {"symbols": total, "prior_amount_dates": len(prior_amounts)}, "limitations": ["northbound/leader/fundamental observations unavailable and neutralized"], "available_time": now}
            c.execute(
                """INSERT INTO dc_market_context_snapshot(trade_date,market_state,state_confidence,sentiment_score,observations_json,result_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET market_state=excluded.market_state,state_confidence=excluded.state_confidence,sentiment_score=excluded.sentiment_score,observations_json=excluded.observations_json,result_json=excluded.result_json,model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (trade_date, state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_four_state_v1", now, now, now),
            )
            written += 1
            observation_history.append(observations)
    return {"ok": True, "dates_considered": len(dates), "snapshots_written": written}

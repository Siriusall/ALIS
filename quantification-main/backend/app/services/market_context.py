"""Deterministic market sentiment and theme/narrative scoring primitives.

The engine consumes normalized observations; it does not scrape or infer
facts itself. This keeps source provenance in the data platform while making
the scoring contract reproducible in backtests and simulation.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime
import json

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
        promotable = source_types >= 2 and related_instruments >= 5 and sessions >= 3
        result.append({
            "theme": name,
            "score": score,
            "promotable": promotable,
            "state": "confirmed" if promotable and score >= 60 else "watch",
            "components": {"narrative_strength": strength, "price_confirmation": price_confirmation, "breadth": breadth, "capital_persistence": persistence, "fundamental_verifiability": fundamental, "catalyst_duration": catalyst, "source_policy_credibility": credibility},
            "weights": {"narrative_strength": .15, "price_confirmation": .15, "breadth": .15, "capital_persistence": .15, "fundamental_verifiability": .20, "catalyst_duration": .10, "source_policy_credibility": .10},
            "base_score": round(base_score, 4), "crowding_penalty": round(crowding_penalty, 4),
            "evidence": {"source_types": source_types, "related_instruments": related_instruments, "sessions": sessions},
        })
    return sorted(result, key=lambda x: float(x["score"]), reverse=True)


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
    sentiment = score_market_sentiment(**observations)
    score = float(sentiment["score"])
    average_return = observations["average_return"]
    if score >= 65 and average_return > 0:
        market_state = "bull"
    elif score >= 55 and average_return > 0:
        market_state = "recovery"
    elif score <= 30 and average_return < 0:
        market_state = "stress"
    elif score <= 40 and average_return < 0:
        market_state = "bear"
    else:
        market_state = "sideways"
    confidence = min(.85, .45 + abs(score - 50) / 100)
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "ok": True, "status": "ready", "trade_date": latest,
        "market_state": {"state": market_state, "confidence": round(confidence, 4), "model": "transparent_state_machine_v1", "reason": "可用历史不足六年，按计划书使用透明状态机；HSMM不冒充生产状态"},
        "sentiment": sentiment, "observations": observations,
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
            (latest, market_state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_state_machine_v1", now, now, now),
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


def rebuild_market_context_history(limit: int = 120) -> dict[str, Any]:
    """Materialize historical transparent-state snapshots from raw daily bars."""
    with _conn() as c:
        date_rows = c.execute(
            """SELECT DISTINCT trade_date FROM dc_tushare_raw_records
               WHERE api_name='daily' ORDER BY trade_date DESC LIMIT ?""", (max(2, min(500, limit)),)
        ).fetchall()
        dates = list(reversed([str(dict(row).get("trade_date") or "") for row in date_rows]))
        written = 0
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
            score = float(sentiment["score"])
            average_return = float(observations["average_return"])
            state = "bull" if score >= 65 and average_return > 0 else "recovery" if score >= 55 and average_return > 0 else "stress" if score <= 30 and average_return < 0 else "bear" if score <= 40 and average_return < 0 else "sideways"
            confidence = min(.85, .45 + abs(score - 50) / 100)
            now = datetime.now().isoformat(timespec="seconds")
            payload = {"ok": True, "status": "ready", "trade_date": trade_date, "market_state": {"state": state, "confidence": round(confidence, 4), "model": "transparent_state_machine_v1", "reason": "historical_raw_daily_backfill"}, "sentiment": sentiment, "observations": observations, "evidence": {"symbols": total, "prior_amount_dates": len(prior_amounts)}, "limitations": ["northbound/leader/fundamental observations unavailable and neutralized"], "available_time": now}
            c.execute(
                """INSERT INTO dc_market_context_snapshot(trade_date,market_state,state_confidence,sentiment_score,observations_json,result_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET market_state=excluded.market_state,state_confidence=excluded.state_confidence,sentiment_score=excluded.sentiment_score,observations_json=excluded.observations_json,result_json=excluded.result_json,model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (trade_date, state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_state_machine_v1", now, now, now),
            )
            written += 1
    return {"ok": True, "dates_considered": len(dates), "snapshots_written": written}

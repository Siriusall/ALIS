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
import re
import threading
import time
from statistics import median

from app.data_platform.db import _conn, _read_conn
from app.data_platform.services.market_calendar import target_trade_date
from app.data_platform.services.observation_clock import (
    _explicit_timestamp_sql,
    _table_columns,
    _timestamp_value_sql,
    _trade_date_cutoff_sql,
    load_raw_record_versions,
    load_snapshot_market_aggregates,
    load_snapshot_versions,
    now_observed_at,
)
from app.data_platform.redis_store import get_json, set_json
from app.services.hsmm_shadow import (
    HSMM_MAX_DURATION_WEEKS,
    HSMM_MIN_DURATION_WEEKS,
)
from app.services.point_in_time import (
    normalize_explicit_timestamp as _normalize_explicit_pit_timestamp,
    normalize_timestamp as _normalize_pit_timestamp,
)


_market_enrichment_cache_lock = threading.Lock()
_market_enrichment_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_MARKET_ENRICHMENT_CACHE_TTL_SECONDS = 300.0


def _strict_raw_payload_rows(
    conn: Any,
    api_name: str,
    *,
    cutoff: str = "",
    cutoff_by_trade_date: bool = False,
    ts_codes: list[str] | tuple[str, ...] | None = None,
    trade_date_start: str = "",
    trade_date_end: str = "",
    period_limit: int = 0,
) -> list[dict[str, Any]] | None:
    rows = load_raw_record_versions(
        conn, api_name, cutoff=cutoff, ts_codes=ts_codes,
        cutoff_by_trade_date=cutoff_by_trade_date,
        trade_date_start=trade_date_start, trade_date_end=trade_date_end,
        period_limit=period_limit,
    )
    if rows is None:
        return None
    result = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        item["available_time"] = str(item.get("observed_at") or "")
        item.update({key: value for key, value in payload.items() if key not in item})
        result.append(item)
    return result


def _strict_snapshot_payload_rows(
    conn: Any,
    *,
    cutoff: str = "",
    cutoff_by_trade_date: bool = False,
    trade_date_start: str = "",
    trade_date_end: str = "",
    trade_date: str = "",
) -> list[dict[str, Any]] | None:
    """Expand immutable normalized rows without consulting the overwrite table."""

    rows = load_snapshot_versions(
        conn,
        cutoff=cutoff,
        cutoff_by_trade_date=cutoff_by_trade_date,
        trade_date_start=trade_date_start,
        trade_date_end=trade_date_end,
        trade_date=trade_date,
    )
    if rows is None:
        return None
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        item["available_time"] = str(item.get("observed_at") or "")
        item.update({key: value for key, value in payload.items() if key not in item})
        result.append(item)
    return result


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
MARKET_PROFILE_LABELS = {
    "P01": "全面扩张", "P02": "流动性驱动", "P03": "拥挤扩张",
    "P04": "均衡震荡", "P05": "宽度分化", "P06": "缩量震荡",
    "P07": "趋势收缩", "P08": "流动性收缩", "P09": "恐慌抛售",
    "P10": "宽度修复", "P11": "流动性修复", "P12": "脆弱反弹",
}
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


def classify_market_profile(market_state: str, observations: dict[str, Any]) -> dict[str, Any]:
    """Map a stable production state to one of twelve explainable market profiles.

    Profiles add decision detail without replacing the four-state production
    risk ceiling.  Their multiplier can only preserve or reduce that ceiling.
    """
    def number(key: str, default: float) -> float:
        value = observations.get(key)
        return default if value is None else float(value)

    breadth = number("breadth", 0.5)
    average_return = number("average_return", 0.0)
    volume_ratio = number("volume_ratio", 1.0)
    limit_up = number("limit_up_ratio", 0.0)
    limit_down = number("limit_down_ratio", 0.0)
    crowding = number("crowding", 0.0)

    if market_state == "S0":
        code = "P03" if crowding >= .65 or limit_up >= .05 else "P01" if breadth >= .62 and average_return >= .008 else "P02"
    elif market_state == "S2":
        code = "P09" if average_return <= -.025 or limit_down >= .05 else "P08" if volume_ratio <= .85 else "P07"
    elif market_state == "S3":
        code = "P10" if breadth >= .55 else "P11" if volume_ratio >= 1.10 else "P12"
    else:
        code = "P06" if volume_ratio <= .85 else "P05" if breadth <= .38 or breadth >= .62 or abs(average_return) >= .015 else "P04"

    risk_multipliers = {
        "P01": 1.00, "P02": .98, "P03": .90,
        "P04": 1.00, "P05": .95, "P06": .90,
        "P07": .95, "P08": .90, "P09": .85,
        "P10": 1.00, "P11": .98, "P12": .90,
    }
    dimensions = [
        "宽度偏强" if breadth >= .60 else "宽度偏弱" if breadth <= .40 else "宽度中性",
        "上涨" if average_return >= .005 else "下跌" if average_return <= -.005 else "收益平稳",
        "放量" if volume_ratio >= 1.10 else "缩量" if volume_ratio <= .90 else "流动性正常",
        "拥挤" if crowding >= .65 else "拥挤度可控",
        "跌停压力" if limit_down >= .03 else "涨停扩散" if limit_up >= .03 else "涨跌停平稳",
    ]
    separation = max(
        abs(breadth - .5) * 2,
        min(1.0, abs(average_return) / .03),
        min(1.0, abs(volume_ratio - 1.0)),
        min(1.0, max(limit_up, limit_down) / .08),
        min(1.0, crowding),
    )
    return {
        "code": code,
        "label": MARKET_PROFILE_LABELS[code],
        "parent_state": market_state,
        "parent_label": REGIME_LABELS.get(market_state, market_state),
        "confidence": round(min(.90, .55 + .35 * separation), 4),
        "risk_budget_multiplier": risk_multipliers[code],
        "risk_ceiling_policy": "cannot_exceed_parent_state",
        "dimensions": dimensions,
        "method": "hierarchical_market_profile_v1",
        "profile_count": len(MARKET_PROFILE_LABELS),
    }


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
    with _read_conn() as c:
        try:
            row = c.execute(
                "SELECT MIN(trade_date) first_date,MAX(trade_date) last_date,COUNT(*) sessions FROM dc_available_trade_dates"
            ).fetchone()
        except Exception:
            row = c.execute(
                "SELECT MIN(trade_date) first_date,MAX(trade_date) last_date,COUNT(*) sessions FROM (SELECT trade_date FROM dc_tushare_raw_records WHERE api_name='daily' GROUP BY trade_date LIMIT 5000)"
            ).fetchone()
        training_row = c.execute(
            "SELECT MIN(feature_date) first_date,MAX(feature_date) last_date,COUNT(*) observations FROM model_training_history WHERE feature_set='market_regime_full_market_v2'"
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
        "hsmm": {"status": live_status, "history_years": round(index_years, 3), "breadth_history_years": round(years, 3), "minimum_years": 6, "state_count": 4, "profile_state_count": len(MARKET_PROFILE_LABELS), "profile_model": "hierarchical_market_profile_v1", "observation_distribution": "multivariate_student_t", "duration_distribution": "truncated_negative_binomial", "duration_weeks": [HSMM_MIN_DURATION_WEEKS, HSMM_MAX_DURATION_WEEKS], "production_eligible": production_eligible, "blocking_reason": blocking_reason, "latest_shadow_version": (latest_shadow or {}).get("version"), "validation": shadow_validation, "review_required": review_required, "review_completed": review_completed, "review": review, "live_observation_start": live_observation_start, "live_observation_end": live_observation_end, "live_observation_weeks": round(live_observation_weeks, 3), "live_observation_sessions": live_observation_sessions, "minimum_live_observation_weeks": HSMM_MINIMUM_LIVE_OBSERVATION_WEEKS},
        "bocpd": {"status": "active", "model": "bocpd_gaussian_v1"},
        "history": {"first_date": first, "last_date": last, "sessions": int(item.get("sessions") or 0)},
        "market_training_history": {"feature_set": "market_regime_full_market_v2", "first_date": training.get("first_date"), "last_date": training.get("last_date"), "observations": int(training.get("observations") or 0), "history_years": round(index_years, 3), "status": "ready" if index_years >= 6 else "insufficient"},
    }


def materialize_market_training_history() -> dict[str, Any]:
    """Build point-in-time market-regime features from the full equity market.

    Individual stocks are aggregated into stable daily market observations;
    the three broad indices remain as size/style anchors.  This avoids feeding
    thousands of changing instruments directly into the HSMM while still
    using the complete cross-section available in the data platform.
    """
    index_codes = ("000300.SH", "000905.SH", "000852.SH")
    # Reads use a server-enforced autocommit connection so Python-side
    # payload decoding and feature calculation cannot leave an
    # ``idle in transaction`` session on PostgreSQL.
    with _read_conn() as c:
        versioned_index = load_raw_record_versions(
            c, "index_daily", ts_codes=list(index_codes), cutoff_by_trade_date=True,
        )
        if versioned_index is not None:
            rows = [dict(row) for row in versioned_index]
        else:
            # Training features are historical evidence.  A mutable raw
            # projection cannot tell whether a late correction replaced the
            # version visible at that session's 15:00 cutoff, even on a local
            # SQLite fixture.  Do not silently downgrade this path.
            return {"ok": False, "status": "blocked", "reason": "observation_clock_unavailable", "written": 0}
        # The raw daily table is a large JSON archive.  Aggregating it on
        # every autonomy cycle causes a full JSONB scan and can monopolize the
        # data database.  The normalized, point-in-time snapshot is the
        # production market-data contract and has the typed fields this model
        # actually needs.
        snapshot_columns = _table_columns(c, "dc_stock_daily_snapshot_version")
        if {
            "observed_at", "first_observed_at", "economic_available_time",
            "created_at", "source_code", "content_hash", "observation_provenance",
        }.issubset(snapshot_columns):
            is_postgres = bool(getattr(c, "_is_postgres", False))
            # The mutable snapshot projection is the accepted winner for
            # each (ts_code, trade_date).  When it is present, bind the
            # immutable archive to that exact hash before aggregating.  The
            # old window over every version can sort years of duplicate
            # provider rows and was a frequent source of raw_ranked
            # statement timeouts.  An accepted hash whose observation clocks
            # are not visible at the session cutoff simply contributes no
            # row; do not fall back to an older/future version or the mutable
            # payload, since that would violate PIT semantics.
            projection_columns = _table_columns(c, "dc_stock_daily_snapshot")
            has_accepted_projection = {
                "ts_code", "trade_date", "content_hash",
            }.issubset(projection_columns)
            observed_at = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
            first_observed_at = _timestamp_value_sql("v.first_observed_at", is_postgres=is_postgres)
            created_at = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
            session_cutoff = _trade_date_cutoff_sql("v.trade_date", is_postgres=is_postgres)
            snapshot_evidence = (
                "v.content_hash<>'' "
                "AND NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL "
                "AND LOWER(COALESCE(v.observation_provenance,'')) NOT LIKE 'legacy_%' "
                f"AND {_explicit_timestamp_sql('v.observed_at', is_postgres=is_postgres)} "
                f"AND {_explicit_timestamp_sql('v.first_observed_at', is_postgres=is_postgres)} "
                f"AND {_explicit_timestamp_sql('v.economic_available_time', is_postgres=is_postgres)} "
                f"AND {_explicit_timestamp_sql('v.created_at', is_postgres=is_postgres)} "
                f"AND {session_cutoff} IS NOT NULL "
                f"AND {observed_at}<={session_cutoff} "
                f"AND {first_observed_at}<={session_cutoff} "
                f"AND {created_at}<={session_cutoff}"
            )
            if is_postgres:
                change_expr = "NULLIF(payload_json::jsonb->>'change_pct','')::double precision"
                turnover_expr = "NULLIF(payload_json::jsonb->>'turnover','')::double precision"
            else:
                change_expr = "CAST(json_extract(payload_json,'$.change_pct') AS REAL)"
                turnover_expr = "CAST(json_extract(payload_json,'$.turnover') AS REAL)"
            if has_accepted_projection:
                visible_cte = """accepted AS (
                    SELECT v.ts_code,v.trade_date,v.payload_json,v.observed_at
                      FROM dc_stock_daily_snapshot p
                      JOIN dc_stock_daily_snapshot_version v
                        ON v.ts_code=p.ts_code
                       AND v.trade_date=p.trade_date
                       AND v.content_hash=p.content_hash
                     WHERE {snapshot_evidence}
                ), latest AS (
                    SELECT * FROM accepted
                )""".format(snapshot_evidence=snapshot_evidence)
            else:
                # Isolated pre-projection fixtures retain the historical
                # immutable ranking path.  Production has the accepted
                # projection, so this branch is only a controlled schema
                # compatibility fallback and never reads the mutable table.
                visible_cte = """ranked AS (
                    SELECT v.ts_code,v.trade_date,v.payload_json,v.observed_at,
                           ROW_NUMBER() OVER (
                              PARTITION BY v.ts_code,v.trade_date
                              ORDER BY {observed_at} DESC,{created_at} DESC,v.id DESC
                           ) AS row_rank
                      FROM dc_stock_daily_snapshot_version v
                     WHERE {snapshot_evidence}
                ), latest AS (
                    SELECT * FROM ranked WHERE row_rank=1
                )""".format(
                    observed_at=observed_at,
                    created_at=created_at,
                    snapshot_evidence=snapshot_evidence,
                )
            market_sql = """WITH {visible_cte}
                SELECT trade_date,
                      COUNT(*) AS instrument_count,
                      AVG(CASE WHEN {change_expr} > 0 THEN 1.0 ELSE 0.0 END) AS breadth,
                      AVG({change_expr} / 100.0) AS average_return,
                      0.0 AS return_dispersion,
                      AVG(CASE WHEN {change_expr} >= 9.7 THEN 1.0 ELSE 0.0 END) AS limit_up_ratio,
                      AVG(CASE WHEN {change_expr} <= -9.7 THEN 1.0 ELSE 0.0 END) AS limit_down_ratio,
                      SUM(COALESCE({turnover_expr},0.0)) AS total_amount,
                      MAX(observed_at) AS source_as_of_time
                 FROM latest
                GROUP BY trade_date
                ORDER BY trade_date""".format(
                visible_cte=visible_cte,
                change_expr=change_expr,
                turnover_expr=turnover_expr,
            )
            market_rows = [dict(row) for row in c.execute(market_sql).fetchall()]
        else:
            # This is a historical feature materialization.  A mutable
            # snapshot can no longer prove whether an earlier, complete
            # version was replaced by a later partial refresh.
            return {"ok": False, "status": "blocked", "reason": "snapshot_observation_clock_unavailable", "written": 0}
    by_code: dict[str, dict[str, dict[str, Any]]] = {code: {} for code in index_codes}
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        close = payload.get("close")
        if close is None:
            continue
        by_code[str(row.get("ts_code") or "")][str(row.get("trade_date") or "")] = {"close": float(close), "amount": float(payload.get("amount") or 0), "available_time": row.get("observed_at")}
    market_by_date = {str(row.get("trade_date") or ""): row for row in market_rows}
    common_dates = sorted(
        set.intersection(*(set(by_code[code]) for code in index_codes), set(market_by_date))
    ) if all(by_code.values()) and market_by_date else []
    written = 0
    pending_writes: list[tuple[Any, ...]] = []
    now = now_observed_at()
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
        market = market_by_date[feature_date]
        recent_dates = common_dates[max(0, index - 19):index + 1]
        recent_amounts = [float(market_by_date[value].get("total_amount") or 0.0) for value in recent_dates]
        amount_baseline = sum(recent_amounts[:-1]) / len(recent_amounts[:-1]) if len(recent_amounts) > 1 else 0.0
        total_amount = float(market.get("total_amount") or 0.0)
        features.update({
            "market_breadth": float(market.get("breadth") or 0.0),
            "market_average_return": float(market.get("average_return") or 0.0),
            "market_return_dispersion": float(market.get("return_dispersion") or 0.0),
            "market_limit_up_ratio": float(market.get("limit_up_ratio") or 0.0),
            "market_limit_down_ratio": float(market.get("limit_down_ratio") or 0.0),
            "market_limit_balance": float(market.get("limit_up_ratio") or 0.0) - float(market.get("limit_down_ratio") or 0.0),
            "market_amount_ratio_20d": total_amount / amount_baseline if amount_baseline > 0 else 1.0,
            "market_instrument_count": int(market.get("instrument_count") or 0),
        })
        source_times.append(str(market.get("source_as_of_time") or ""))
        pending_writes.append(
            (feature_date, "market_regime_full_market_v2", json.dumps(features, ensure_ascii=False), max(source_times), now, now)
        )
        written += 1
    if pending_writes:
        with _conn() as writer:
            for params in pending_writes:
                writer.execute(
                    """INSERT INTO model_training_history(feature_date,feature_set,features_json,source_as_of_time,created_at,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(feature_date) DO UPDATE SET feature_set=excluded.feature_set,
                       features_json=excluded.features_json,source_as_of_time=excluded.source_as_of_time,updated_at=excluded.updated_at""",
                    params,
                )
    return {
        "ok": bool(written),
        "feature_set": "market_regime_full_market_v2",
        "sources": ["full_market_daily", *index_codes],
        "source_rows": len(rows),
        "market_sessions": len(market_rows),
        "common_dates": len(common_dates),
        "written": written,
        "range": [common_dates[0], common_dates[-1]] if common_dates else [],
    }


def _production_regime_reason(governance: dict[str, Any]) -> str:
    """Explain the active production regime without conflating history and promotion gates."""
    hsmm = governance.get("hsmm") or {}
    history_years = float(hsmm.get("history_years") or 0)
    minimum_years = float(hsmm.get("minimum_years") or 6)
    live_weeks = float(hsmm.get("live_observation_weeks") or 0)
    minimum_live_weeks = float(hsmm.get("minimum_live_observation_weeks") or 4)
    validation = hsmm.get("validation") or {}
    review = hsmm.get("review") or {}

    if hsmm.get("production_eligible"):
        return "HSMM已通过历史OOS、真实影子观察和上线评审，可按审批结果接管生产状态"
    if str(review.get("status") or "") == "rejected":
        return "HSMM上线评审未通过，生产状态继续使用透明四状态机"
    if str(validation.get("status") or "") == "passed":
        if live_weeks >= minimum_live_weeks:
            return "HSMM历史OOS和真实影子观察期已通过，等待上线评审；评审前生产状态继续使用透明四状态机"
        return (
            f"HSMM历史OOS已通过，正在进行真实影子观察（{live_weeks:.2f}/{minimum_live_weeks:g}周）；"
            "观察期和上线评审完成前生产状态继续使用透明四状态机"
        )
    if history_years >= minimum_years:
        return "HSMM训练历史已满足六年要求，仍需通过历史OOS验证、真实影子观察和上线评审；当前生产状态使用透明四状态机"
    return (
        f"HSMM训练历史仅{history_years:.2f}年，尚未达到{minimum_years:g}年要求；"
        "当前生产状态使用透明四状态机"
    )


def _market_enrichment_observations(
    latest: str,
    *,
    as_of_time: str | None = None,
    include_expensive: bool = True,
) -> dict[str, Any]:
    """Build the three previously-neutralized daily market observations."""
    cutoff = _normalize_pit_timestamp(as_of_time or f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}T23:59:59+08:00")
    cache_key = (str(latest), cutoff)
    now = time.monotonic()
    with _market_enrichment_cache_lock:
        cached = _market_enrichment_cache.get(cache_key)
        if cached and now - cached[0] < _MARKET_ENRICHMENT_CACHE_TTL_SECONDS:
            return {**cached[1], "coverage": dict(cached[1].get("coverage") or {})}
    result: dict[str, Any] = {
        "status": "ready" if cutoff else "blocked",
        "northbound_flow_z": None,
        "leader_persistence": None,
        "price_fundamental_divergence": None,
        "coverage": {"northbound": 0, "leader_sessions": 0, "fundamental_codes": 0},
        "missing_sources": [],
    }
    if not cutoff:
        result["missing_sources"] = ["invalid_observation_cutoff"]
        return result

    def _float_value(value: Any) -> float | None:
        try:
            return float(value) if value is not None and str(value).strip() != "" else None
        except (TypeError, ValueError):
            return None

    # The transparent production regime is already determined by bounded
    # daily breadth, returns, limit ratios, turnover and concentration.  The
    # three research enrichments below are useful explanations but can require
    # wide immutable-history scans (especially financial PIT).  Do not make a
    # complete close-data slice wait for them before it can freeze today's
    # market-state evidence.  They remain available to the research/history
    # path and are explicitly labelled neutral when skipped here.
    if not include_expensive:
        result["status"] = "partial"
        result["missing_sources"] = [
            "northbound_deferred", "leader_persistence_deferred", "fundamental_divergence_deferred",
        ]
        return result

    with _conn() as c:
        north_rows = _strict_raw_payload_rows(
            c, "moneyflow_hsgt", cutoff=cutoff, trade_date_end=latest,
        )
        if north_rows is None:
            result["status"] = "partial"
            result["missing_sources"].append("moneyflow_hsgt")
            north_rows = []
        north_by_date: dict[str, dict[str, Any]] = {}
        for row in north_rows:
            date = str(row.get("trade_date") or "")
            if date and date <= latest:
                north_by_date.setdefault(date, row)
        north_rows = [north_by_date[date] for date in sorted(north_by_date, reverse=True)[:61]]
        north_values: list[float] = []
        for row in north_rows:
            value = _float_value(row.get("north_money"))
            if value is not None:
                north_values.append(value)
        if north_values:
            center = median(north_values[1:]) if len(north_values) > 1 else north_values[0]
            mad = median(abs(value - center) for value in north_values[1:]) if len(north_values) > 2 else 0.0
            scale = max(1e-6, 1.4826 * mad)
            result["northbound_flow_z"] = round(max(-3.0, min(3.0, (north_values[0] - center) / scale)), 6)
            result["coverage"]["northbound"] = len(north_values)

        date_rows = [str(dict(row).get("trade_date") or "") for row in c.execute(
            "SELECT trade_date FROM dc_available_trade_dates WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 21",
            (latest,),
        ).fetchall()]
        snapshot_rows = _strict_snapshot_payload_rows(
            c,
            cutoff=cutoff,
            trade_date_start=min(date_rows) if date_rows else "",
            trade_date_end=latest,
        )
        if snapshot_rows is None:
            result["status"] = "partial"
            result["missing_sources"].append("daily_snapshot")
            snapshot_rows = []
        if date_rows:
            leaders_by_date: dict[str, list[tuple[float, str]]] = {}
            for row in snapshot_rows:
                date = str(row.get("trade_date") or "")
                if date not in date_rows:
                    continue
                change = _float_value(row.get("change_pct", row.get("pct_chg")))
                turnover = _float_value(row.get("turnover", row.get("amount")))
                code = str(row.get("ts_code") or "")
                if change is not None and turnover is not None and change > 0 and turnover > 0 and code:
                    leaders_by_date.setdefault(date, []).append((turnover, code))
            leader_sets = {
                date: {code for _turnover, code in sorted(values, key=lambda item: (-item[0], item[1]))[:50]}
                for date, values in leaders_by_date.items()
            }
            current = leader_sets.get(latest, set())
            prior_sets = [leader_sets.get(day, set()) for day in date_rows[1:] if leader_sets.get(day)]
            if current and prior_sets:
                result["leader_persistence"] = round(sum(len(current & previous) / max(1, len(current | previous)) for previous in prior_sets) / len(prior_sets), 6)
                result["coverage"]["leader_sessions"] = len(prior_sets) + 1

        # The divergence signal compares the latest two reporting periods;
        # avoid reading the complete immutable financial archive on every
        # historical enrichment request.
        financial_rows = _strict_raw_payload_rows(
            c, "fina_indicator", cutoff=cutoff, period_limit=2,
        )
        if financial_rows is None:
            result["status"] = "partial"
            result["missing_sources"].append("fina_indicator")
            financial_rows = []
        periods = sorted({str(row.get("period") or "") for row in financial_rows if str(row.get("period") or "")}, reverse=True)[:2]
        fundamental_by_period: dict[str, dict[str, float]] = {}
        for period in periods:
            values: dict[str, float] = {}
            for payload in (row for row in financial_rows if str(row.get("period") or "") == period):
                # TuShare 的 q_profit_yoy 在部分财报批次为空；使用同一口径的
                # q_sales_yoy，再退化到 ROE，避免整段基本面观测被误判为缺失。
                value = next((payload.get(key) for key in ("q_profit_yoy", "q_sales_yoy", "roe") if payload.get(key) is not None), None)
                parsed = _float_value(value)
                code = str(payload.get("ts_code") or "")
                if parsed is not None and code:
                    values[code] = parsed
            fundamental_by_period[period] = values
        if fundamental_by_period:
            current_values = fundamental_by_period[periods[0]]
            prior_values = fundamental_by_period.get(periods[1], {}) if len(periods) > 1 else {}
            comparable = [current_values[code] - prior_values[code] for code in current_values.keys() & prior_values.keys()]
            fundamental_signal = median(current_values.values()) / 50.0 if current_values else 0.0
            if comparable:
                fundamental_signal = (fundamental_signal + median(comparable) / 50.0) / 2.0
            price_by_date: dict[str, list[float]] = {}
            for row in snapshot_rows:
                date = str(row.get("trade_date") or "")
                change = _float_value(row.get("change_pct", row.get("pct_chg")))
                if date in date_rows and change is not None:
                    price_by_date.setdefault(date, []).append(change)
            price_values = [sum(values) / len(values) for values in price_by_date.values() if values]
            price_signal = (sum(price_values[-20:]) / max(1, len(price_values[-20:]))) / 3.0
            result["price_fundamental_divergence"] = round(max(-1.0, min(1.0, price_signal - fundamental_signal)), 6)
            result["coverage"]["fundamental_codes"] = len(current_values)
    with _market_enrichment_cache_lock:
        if len(_market_enrichment_cache) > 16:
            oldest = min(_market_enrichment_cache, key=lambda key: _market_enrichment_cache[key][0])
            _market_enrichment_cache.pop(oldest, None)
        _market_enrichment_cache[cache_key] = (time.monotonic(), result)
    return {**result, "coverage": dict(result.get("coverage") or {})}


def live_market_context(*, persist: bool = False) -> dict[str, Any]:
    with _conn() as c:
        target_date = target_trade_date(c)
        cached = get_json("alis:market-context:live:v5")
        if (
            isinstance(cached, dict)
            and str(cached.get("trade_date") or "").replace("-", "")[:8] == target_date
            and bool(cached.get("ok"))
        ):
            return cached
        # The normalized snapshot is the production market-data contract. The
        # raw JSON archive remains evidence storage and must not be scanned by
        # an interactive read.
        dates = [str(dict(row)["trade_date"]) for row in c.execute(
            """SELECT trade_date FROM dc_available_trade_dates
               WHERE trade_date<=?
               ORDER BY trade_date DESC LIMIT 6""",
            (target_date,),
        ).fetchall()]
        if not dates or dates[0] != target_date:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "target_market_context_snapshot_missing",
                "trade_date": target_date,
                "latest_available_trade_date": dates[0] if dates else None,
            }
        if len(dates) < 2:
            return {
                "ok": False,
                "status": "insufficient_data",
                "reason": "至少需要两个交易日行情",
                "trade_date": target_date,
            }
        latest, previous = dates[0], dates[1]
        # Redis is a transport cache; the database snapshot is the durable
        # read model. Reuse it after a Redis miss only when it was refreshed
        # after the latest trade-date index, otherwise rebuild from source.
        stored_snapshot = c.execute(
            """SELECT result_json,created_at,updated_at
                 FROM dc_market_context_snapshot
                WHERE trade_date=?""",
            (latest,),
        ).fetchone()
        latest_index = c.execute(
            "SELECT updated_at FROM dc_available_trade_dates WHERE trade_date=?",
            (latest,),
        ).fetchone()
        if stored_snapshot and latest_index:
            stored = dict(stored_snapshot)
            index = dict(latest_index)
            if str(stored.get("updated_at") or "") >= str(index.get("updated_at") or ""):
                try:
                    snapshot_payload = json.loads(stored.get("result_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    snapshot_payload = {}
                snapshot_clocks_valid = isinstance(snapshot_payload, dict) and all(
                    _normalize_explicit_pit_timestamp(value)
                    for value in (
                        snapshot_payload.get("available_time"),
                        stored.get("created_at"),
                        stored.get("updated_at"),
                    )
                )
                if (
                    snapshot_clocks_valid
                    and snapshot_payload.get("trade_date") == latest
                    and snapshot_payload.get("ok")
                ):
                    set_json("alis:market-context:live:v5", snapshot_payload, ttl=300)
                    return snapshot_payload
        # A live context is still a production decision dependency after the
        # Redis/read-model cache expires.  Never rebuild it from the mutable
        # normalized projection: a late repair there can silently rewrite the
        # market state used by today's controller.  The immutable aggregate
        # reader binds every row to the accepted provider hash and its own
        # T15 observation boundary.
        # Close data is economically stamped at T15 but is commonly observed
        # after the close.  The live decision path may consume it only up to
        # the actual captured observation clock; historical PIT readers keep
        # the strict T15 default in ``load_snapshot_market_aggregates``.
        live_observation_cutoff = now_observed_at()
        aggregates = load_snapshot_market_aggregates(
            c,
            trade_dates=dates,
            observation_cutoff=live_observation_cutoff,
        )
        if aggregates is None:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "snapshot_observation_clock_unavailable",
                "trade_date": latest,
                "missing_dates": dates,
            }
        missing_dates = [date for date in dates if date not in aggregates]
        if latest not in aggregates:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_snapshot_market_aggregates_missing",
                "trade_date": latest,
                "missing_dates": missing_dates,
            }
        stock_count_row = c.execute("SELECT COUNT(*) AS cnt FROM dc_stock_master").fetchone()
        stock_count = int(dict(stock_count_row).get("cnt") or 0) if stock_count_row else 0
        minimum_rows = max(1, int(math.ceil(stock_count * 0.80))) if stock_count else 1
        incomplete_dates = {
            date: {
                "rows_total": int(dict(aggregates[date]).get("rows_total") or 0),
                "market_rows": int(dict(aggregates[date]).get("market_rows") or 0),
            }
            for date in dates
            if date in aggregates
            and int(dict(aggregates[date]).get("market_rows") or 0) < minimum_rows
        }
        if latest in incomplete_dates:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_snapshot_market_aggregates_incomplete",
                "trade_date": latest,
                "minimum_rows": minimum_rows,
                "incomplete_dates": incomplete_dates,
            }

        latest_rows = load_snapshot_versions(
            c,
            cutoff=live_observation_cutoff,
            trade_date=latest,
        )
        if latest_rows is None or not latest_rows:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_snapshot_concentration_rows_missing",
                "trade_date": latest,
            }
        amounts: list[float] = []
        for item in latest_rows:
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                value = float(payload.get("turnover"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                amounts.append(value)
        if not amounts:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_snapshot_turnover_missing",
                "trade_date": latest,
            }
        amounts.sort(reverse=True)
        top_count = max(1, int(math.ceil(len(amounts) / 20)))
        total_amount = sum(amounts)
        concentration = sum(amounts[:top_count]) / total_amount if total_amount > 0 else 0.0
        breadth_row = dict(aggregates[latest])
        limit_row = dict(aggregates[latest])
        amount_rows = [
            {"trade_date": date, "total_amount": float(dict(aggregates[date]).get("amount") or 0.0)}
            for date in dates
            if date in aggregates
        ]
        previous_context = c.execute(
            "SELECT market_state FROM dc_market_context_snapshot WHERE trade_date<? ORDER BY trade_date DESC LIMIT 1",
            (latest,),
        ).fetchone()
        history_rows = [dict(row) for row in c.execute(
            "SELECT observations_json,result_json FROM dc_market_context_snapshot WHERE trade_date<? ORDER BY trade_date DESC LIMIT 60",
            (latest,),
        ).fetchall()]
    total = int(breadth_row.get("market_rows") or 0)
    limit_total = total or 1
    amount_by_date = {str(x["trade_date"]): float(x.get("total_amount") or 0) for x in amount_rows}
    prior_amounts = [amount_by_date[x] for x in dates[1:] if amount_by_date.get(x)]
    volume_ratio = amount_by_date.get(latest, 0) / (sum(prior_amounts) / len(prior_amounts)) if prior_amounts else 1.0
    concentration = float(concentration)
    crowding = max(0.0, min(1.0, (concentration - .30) / .30))
    observations = {
        "breadth": float(breadth_row.get("breadth") or 0.5),
        "average_return": float(breadth_row.get("average_return") or 0),
        "volume_ratio": volume_ratio,
        "limit_up_ratio": float(limit_row.get("limit_up_ratio") or 0),
        "limit_down_ratio": float(limit_row.get("limit_down_ratio") or 0),
        "northbound_flow_z": 0.0,
        "leader_persistence": 0.0,
        "price_fundamental_divergence": 0.0,
        "crowding": crowding,
    }
    try:
        enrichment = _market_enrichment_observations(latest, include_expensive=False)
    except TypeError:
        # Retain the one-argument seam used by isolated legacy fixtures and
        # third-party read-only extensions.  Production implementation above
        # accepts ``include_expensive`` and therefore stays on the bounded
        # close-time path.
        enrichment = _market_enrichment_observations(latest)
    observations.update({
        key: float(enrichment[key]) if enrichment.get(key) is not None else 0.0
        for key in ("northbound_flow_z", "leader_persistence", "price_fundamental_divergence")
    })
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
    market_profile = classify_market_profile(market_state, observations)
    confidence = min(.85, .45 + abs(score - 50) / 100)
    now = now_observed_at()
    model_governance = market_state_model_governance()
    history_degraded = bool(missing_dates or incomplete_dates)
    payload = {
        "ok": True, "status": "degraded" if history_degraded else "ready", "trade_date": latest,
        "market_state": {"state": market_state, "label": REGIME_LABELS[market_state], "confidence": round(confidence, 4), "model": "transparent_four_state_v1", "reason": _production_regime_reason(model_governance)},
        "market_profile": market_profile,
        "sentiment": sentiment, "observations": observations, "change_alarm": change_alarm,
        "model_governance": model_governance,
        "evidence": {"latest_date": latest, "previous_date": previous, "symbols": total, "amount_dates": len(amount_rows)},
        "observation_evidence": enrichment.get("coverage") or {},
        "limitations": [
            *([f"历史市场聚合缺少 {len(missing_dates)} 个比较交易日，成交量比率按可见样本计算" ] if missing_dates else []),
            *(["部分历史市场聚合行数不足，当前状态仅使用最新完整交易日"] if incomplete_dates else []),
            *([] if enrichment.get("coverage", {}).get("northbound") else ["北向资金原始观测暂不可用，当前按中性值处理"]),
            *([] if enrichment.get("coverage", {}).get("leader_sessions") else ["龙头持续性日度样本不足，当前按中性值处理"]),
            *([] if enrichment.get("coverage", {}).get("fundamental_codes") else ["价格与基本面背离日度样本不足，当前按中性值处理"]),
        ],
        "available_time": now,
    }
    if persist:
        with _conn() as c:
            c.execute(
                """INSERT INTO dc_market_context_snapshot(trade_date,market_state,state_confidence,sentiment_score,observations_json,result_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET market_state=excluded.market_state,state_confidence=excluded.state_confidence,
                   sentiment_score=excluded.sentiment_score,observations_json=excluded.observations_json,result_json=excluded.result_json,model_version=excluded.model_version,
                   available_time=excluded.available_time,created_at=excluded.created_at,updated_at=excluded.updated_at""",
                (latest, market_state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_four_state_v1", now, now, now),
            )
        set_json("alis:market-context:live:v5", payload, ttl=300)
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
        row["market_profile"] = classify_market_profile(str(row.get("market_state") or "S1"), row["observations"])
    rows.reverse()
    transitions = []
    for previous, current in zip(rows, rows[1:]):
        if previous["market_state"] != current["market_state"]:
            transitions.append({"trade_date": current["trade_date"], "from": previous["market_state"], "to": current["market_state"], "confidence": current["state_confidence"]})
    return {"ok": True, "items": rows, "transitions": transitions, "count": len(rows)}


def market_context_as_of(as_of_time: str, *, trade_date: str | None = None) -> dict[str, Any]:
    """Return a derived market state only when its own evidence was visible.

    ``dc_market_context_snapshot`` is a mutable read model.  Selecting by
    ``trade_date`` alone therefore allowed a snapshot rebuilt today to be
    replayed as if it had existed on the historical decision date.  The
    derived row now has to carry real observation/materialization timestamps
    that are all at or before the requested cutoff, and it must correspond to
    the exact requested session.  A prior-session state is not a valid formal
    decision input; callers receive a blocked result instead of a stale green
    fallback.
    """
    raw_cutoff = str(as_of_time or "").strip()
    cutoff = _normalize_pit_timestamp(raw_cutoff)
    if not cutoff or not re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", raw_cutoff):
        return {
            "ok": False,
            "status": "blocked",
            "as_of_time": str(as_of_time or ""),
            "reason": "invalid_market_context_observation_cutoff",
        }
    requested_trade_date = str(trade_date or cutoff[:10]).replace("-", "")[:8]
    if len(requested_trade_date) != 8 or not requested_trade_date.isdigit():
        return {
            "ok": False,
            "status": "blocked",
            "as_of_time": cutoff,
            "snapshot_trade_date": None,
            "reason": "invalid_market_context_trade_date",
            "point_in_time_method": "derived_snapshot_visibility_v2",
        }

    def visible(value: Any) -> bool:
        raw_value = str(value or "").strip()
        if not re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", raw_value):
            return False
        normalized = _normalize_pit_timestamp(raw_value)
        return bool(normalized) and normalized <= cutoff

    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT trade_date,result_json,model_version,available_time,created_at,updated_at
                 FROM dc_market_context_snapshot
                WHERE trade_date<=?
                ORDER BY trade_date DESC""",
            (requested_trade_date,),
        ).fetchall()]

    visible_rows = [
        row for row in rows
        if visible(row.get("available_time"))
        and visible(row.get("created_at"))
        and visible(row.get("updated_at"))
    ]
    if not visible_rows:
        return {
            "ok": False,
            "status": "blocked",
            "as_of_time": cutoff,
            "snapshot_trade_date": None,
            "reason": "no_market_context_visible_at_cutoff",
            "point_in_time_method": "derived_snapshot_visibility_v2",
        }
    item = visible_rows[0]
    snapshot_date = str(item.get("trade_date") or "").replace("-", "")[:8]
    if snapshot_date != requested_trade_date:
        return {
            "ok": False,
            "status": "blocked",
            "as_of_time": cutoff,
            "snapshot_trade_date": snapshot_date or None,
            "reason": "market_context_exact_trade_date_missing",
            "point_in_time_method": "derived_snapshot_visibility_v2",
        }
    try:
        payload = json.loads(item.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or not payload.get("market_state"):
        return {
            "ok": False,
            "status": "blocked",
            "as_of_time": cutoff,
            "snapshot_trade_date": snapshot_date,
            "reason": "market_context_snapshot_payload_invalid",
            "point_in_time_method": "derived_snapshot_visibility_v2",
        }
    payload["as_of_time"] = cutoff
    payload["snapshot_trade_date"] = snapshot_date
    payload["snapshot_available_time"] = _normalize_pit_timestamp(item.get("available_time"))
    payload["snapshot_created_at"] = _normalize_pit_timestamp(item.get("created_at"))
    payload["snapshot_updated_at"] = _normalize_pit_timestamp(item.get("updated_at"))
    payload["point_in_time_method"] = "derived_snapshot_visibility_v2"
    return payload


def rebuild_market_context_history(limit: int = 120) -> dict[str, Any]:
    """Materialize historical transparent-state snapshots from PIT daily bars.

    The overwrite raw table is intentionally not a fallback here.  Rebuilding
    a historical state from its latest projection would let a late correction
    silently rewrite the evidence that was visible at that session's close.
    """
    with _conn() as c:
        requested = max(2, min(500, limit))
        try:
            calendar_rows = c.execute(
                """SELECT trade_date FROM dc_available_trade_dates
                   ORDER BY trade_date DESC LIMIT ?""",
                (requested,),
            ).fetchall()
        except Exception:
            calendar_rows = []
        requested_dates = sorted({
            str(dict(row).get("trade_date") or "")
            for row in calendar_rows
            if len(str(dict(row).get("trade_date") or "")) == 8
        })
        if not requested_dates:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "trade_calendar_unavailable",
                "dates_considered": 0,
                "snapshots_written": 0,
            }
        daily_rows = _strict_raw_payload_rows(
            c,
            "daily",
            cutoff_by_trade_date=True,
            trade_date_start=requested_dates[0],
            trade_date_end=requested_dates[-1],
        )
        if daily_rows is None:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "observation_clock_unavailable",
                "dates_considered": 0,
                "snapshots_written": 0,
            }
        rows_by_date: dict[str, list[dict[str, Any]]] = {}
        for row in daily_rows:
            date = str(row.get("trade_date") or "")
            if len(date) == 8 and date.isdigit():
                rows_by_date.setdefault(date, []).append(row)
        dates = [date for date in requested_dates if date in rows_by_date]
        written = 0
        observation_history: list[dict[str, Any]] = []
        change_signals: list[float] = []
        for index, trade_date in enumerate(dates):
            prior_dates = dates[max(0, index - 5):index]
            if not prior_dates:
                continue
            current_rows = rows_by_date.get(trade_date, [])
            parsed_current: list[tuple[float | None, float | None]] = []
            for item in current_rows:
                try:
                    change = float(item.get("pct_chg")) if item.get("pct_chg") is not None else None
                except (TypeError, ValueError):
                    change = None
                try:
                    amount = float(item.get("amount")) if item.get("amount") is not None else None
                except (TypeError, ValueError):
                    amount = None
                parsed_current.append((change, amount))
            valid_current = [(change, amount) for change, amount in parsed_current if change is not None]
            total = len(valid_current)
            if total <= 0:
                continue
            current_amount = sum(amount or 0.0 for _change, amount in valid_current)
            prior_amounts: list[float] = []
            for prior_date in prior_dates:
                amount = sum(
                    float(item.get("amount") or 0.0)
                    for item in rows_by_date.get(prior_date, [])
                    if item.get("amount") is not None
                )
                if amount > 0:
                    prior_amounts.append(amount)
            observations = {
                "breadth": sum(1 for change, _amount in valid_current if change > 0) / total,
                "average_return": sum(change for change, _amount in valid_current) / total / 100.0,
                "volume_ratio": current_amount / (sum(prior_amounts) / len(prior_amounts)) if prior_amounts else 1.0,
                "limit_up_ratio": sum(1 for change, _amount in valid_current if change >= 9.7) / total,
                "limit_down_ratio": sum(1 for change, _amount in valid_current if change <= -9.7) / total,
                "northbound_flow_z": 0.0, "leader_persistence": 0.0, "price_fundamental_divergence": 0.0, "crowding": 0.0,
            }
            enrichment = _market_enrichment_observations(
                trade_date,
                as_of_time=f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00+08:00",
            )
            observations.update({
                key: float(enrichment[key]) if enrichment.get(key) is not None else 0.0
                for key in ("northbound_flow_z", "leader_persistence", "price_fundamental_divergence")
            })
            sentiment = score_market_sentiment(**observations)
            composite_signal = _market_change_signal(observation_history, observations)
            change_signals.append(composite_signal)
            change_alarm = bocpd_change_alarm(change_signals)
            change_alarm["composite_signal"] = round(composite_signal, 6)
            score = float(sentiment["score"])
            average_return = float(observations["average_return"])
            previous_state = state if written else ""
            state = _transparent_regime(score, average_return, previous_state)
            market_profile = classify_market_profile(state, observations)
            confidence = min(.85, .45 + abs(score - 50) / 100)
            now = now_observed_at()
            payload = {"ok": True, "status": "ready", "trade_date": trade_date, "market_state": {"state": state, "label": REGIME_LABELS[state], "confidence": round(confidence, 4), "model": "transparent_four_state_v1", "reason": "历史回放固定使用当时可解释的透明四状态机，不代表当前HSMM训练历史不足"}, "market_profile": market_profile, "sentiment": sentiment, "observations": observations, "observation_evidence": enrichment.get("coverage") or {}, "change_alarm": change_alarm, "model_governance": {"production_model": "transparent_four_state_v1", "hsmm": {"status": "historical_transparent_rule", "minimum_years": 6, "state_count": 4, "profile_state_count": 12}, "bocpd": {"status": "active", "model": "bocpd_gaussian_v1"}}, "evidence": {"symbols": total, "prior_amount_dates": len(prior_amounts)}, "limitations": [], "available_time": now}
            c.execute(
                """INSERT INTO dc_market_context_snapshot(trade_date,market_state,state_confidence,sentiment_score,observations_json,result_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET market_state=excluded.market_state,state_confidence=excluded.state_confidence,sentiment_score=excluded.sentiment_score,observations_json=excluded.observations_json,result_json=excluded.result_json,model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (trade_date, state, confidence, score, json.dumps(observations, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), "transparent_four_state_v1", now, now, now),
            )
            written += 1
            observation_history.append(observations)
    return {"ok": True, "dates_considered": len(dates), "snapshots_written": written}

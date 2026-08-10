from __future__ import annotations

import hashlib
import json
import math
import pickle
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from app.db.store import (
    _conn as business_conn,
    get_active_model_version,
    get_model_version,
    list_model_versions,
)
from app.data_platform.db import _conn as data_conn
from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services.model_artifacts import resolve_model_artifact_path
from app.services.event_impact_features import (
    STRUCTURED_DISCLOSURE_APIS,
    build_analyst_consensus_index,
    disclosure_expectation_gap,
    materialize_event_features,
    merge_event_candidates,
    payload_dict,
    point_in_time_analyst_consensus,
    report_period_key,
    structured_event_candidate,
)
from app.services.plan_model_control import (
    EVENT_STRUCTURED_SOURCES,
    NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
    PLAN_MODELS,
    _event_news_direction,
    _event_usable_trade_date,
    _payload_dict,
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
    "event_impact_assessor_v2": 0.05,
    "narrative_transition_multitask_v1": 0.0,
}
LIMITED_LIVE_MODEL_KEYS = frozenset({
    "medium_horizon_lambdarank_v1",
    "capital_persistence_multitask_v1",
    "leader_lambdarank_v1",
})
LIMITED_LIVE_WEIGHT_SCALE = 0.0
LIMITED_LIVE_PASSED_SCALE = 0.0
LIMITED_LIVE_WATCH_SCALE = 0.0
LIMITED_LIVE_AGGREGATE_CAP = 0.0
MAX_EVENT_RISK_PENALTY = 0.03
MAX_THEME_RANK_BONUS = 0.08
MAX_NARRATIVE_MODEL_RANK_ADJUSTMENT = 0.06
NARRATIVE_MODEL_MIN_CONFIDENCE = 0.55
NARRATIVE_MODEL_EXIT_CONFIDENCE = 0.65
NARRATIVE_MODEL_HARD_VETO_RISK = 0.80
ENVIRONMENT_MODEL_MULTIPLIERS = {
    "S0": {
        "medium_horizon_lambdarank_v1": 1.15, "capital_persistence_multitask_v1": 0.90,
        "leader_lambdarank_v1": 1.20, "event_impact_assessor_v2": 1.00,
    },
    "S1": {key: 1.0 for key in MODEL_KEYS},
    "S2": {
        "medium_horizon_lambdarank_v1": 1.10, "capital_persistence_multitask_v1": 1.20,
        "leader_lambdarank_v1": 0.85, "event_impact_assessor_v2": 0.80,
    },
    "S3": {
        "medium_horizon_lambdarank_v1": 0.80, "capital_persistence_multitask_v1": 1.25,
        "leader_lambdarank_v1": 0.60, "event_impact_assessor_v2": 0.60,
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
_EVENT_FEATURE_CACHE: dict[
    tuple[str, str, str],
    dict[str, dict[str, Any]],
] = {}
_EVENT_FEATURE_CACHE_LIMIT = 32


def _safe(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _point_in_time_event_features(
    symbols: list[str],
    *,
    trade_date: str,
    as_of_time: str | None,
) -> dict[str, dict[str, Any]]:
    unique_symbols = sorted({str(value) for value in symbols if str(value)})
    compact_date = str(trade_date or "").replace("-", "")[:8]
    if not unique_symbols or len(compact_date) != 8:
        return {}
    cutoff = str(as_of_time or "").strip()
    if not cutoff:
        cutoff = (
            f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}"
            "T15:00:00+08:00"
        )
    symbol_hash = hashlib.sha256(
        "|".join(unique_symbols).encode("utf-8")
    ).hexdigest()
    cache_key = (compact_date, cutoff, symbol_hash)
    cached = _EVENT_FEATURE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    start_date = (
        datetime.strptime(compact_date, "%Y%m%d") - timedelta(days=14)
    ).strftime("%Y%m%d")
    placeholders = ",".join("?" for _ in unique_symbols)
    with data_conn() as conn:
        trade_rows = conn.execute(
            """SELECT trade_date FROM dc_available_trade_dates
               WHERE trade_date>=? AND trade_date<=?
               ORDER BY trade_date""",
            (start_date, compact_date),
        ).fetchall()
        trade_dates = [
            str(dict(row).get("trade_date") or "")
            for row in trade_rows
            if dict(row).get("trade_date")
        ]
        if compact_date not in trade_dates:
            trade_dates.append(compact_date)
            trade_dates.sort()
        try:
            announcement_rows = conn.execute(
                f"""SELECT r.ts_code,r.publish_time AS event_time,
                           r.available_time,r.title,r.raw_text,e.event_type,
                           e.impact_direction,e.severity,e.confidence,e.metrics_json
                    FROM dc_announcement_event e
                    JOIN dc_announcement_raw r
                      ON r.source_code=e.source_code
                     AND r.announcement_id=e.announcement_id
                    WHERE r.ts_code IN ({placeholders})
                      AND r.available_time<=?
                      AND REPLACE(SUBSTRING(r.publish_time,1,10),'-','')>=?""",
                (*unique_symbols, cutoff, start_date),
            ).fetchall()
        except Exception:
            announcement_rows = []
        structured_event_rows: list[Any] = []
        try:
            structured_start = (
                datetime.strptime(compact_date, "%Y%m%d") - timedelta(days=400)
            ).strftime("%Y-%m-%d")
            for api_name in STRUCTURED_DISCLOSURE_APIS:
                structured_event_rows.extend(
                    conn.execute(
                        f"""SELECT api_name,ts_code,ann_date,period,available_time,payload_json
                            FROM dc_tushare_raw_records
                            WHERE api_name=?
                              AND ts_code IN ({placeholders})
                              AND available_time>=? AND available_time<=?
                            ORDER BY available_time,id""",
                        (api_name, *unique_symbols, structured_start, cutoff),
                    ).fetchall()
                )
        except Exception:
            structured_event_rows = []
        try:
            news_rows = conn.execute(
                """SELECT available_time,payload_json
                   FROM dc_tushare_raw_records
                   WHERE api_name='major_news'
                     AND available_time<=?
                     AND available_time>=?
                   ORDER BY available_time""",
                (
                    cutoff,
                    f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
                ),
            ).fetchall()
        except Exception:
            news_rows = []
        try:
            top_list_rows = conn.execute(
                f"""SELECT ts_code,trade_date,available_time,payload_json
                    FROM dc_tushare_raw_records
                    WHERE api_name='top_list'
                      AND ts_code IN ({placeholders})
                      AND trade_date>=? AND trade_date<=?
                      AND available_time<=?
                    ORDER BY trade_date,ts_code""",
                (*unique_symbols, start_date, compact_date, cutoff),
            ).fetchall()
        except Exception:
            top_list_rows = []
        try:
            benchmark_rows = conn.execute(
                """SELECT trade_date,payload_json
                   FROM dc_tushare_raw_records
                   WHERE api_name='index_daily'
                     AND ts_code IN ('000300.SH','000300')
                     AND trade_date<=?
                   ORDER BY trade_date DESC LIMIT 2""",
                (compact_date,),
            ).fetchall()
        except Exception:
            benchmark_rows = []
        try:
            report_start = (
                datetime.strptime(compact_date, "%Y%m%d") - timedelta(days=200)
            ).strftime("%Y%m%d")
            analyst_report_rows = conn.execute(
                f"""SELECT ts_code,trade_date,available_time,payload_json
                    FROM dc_tushare_raw_records
                    WHERE api_name='report_rc'
                      AND ts_code IN ({placeholders})
                      AND trade_date>=? AND trade_date<=?
                      AND available_time<=?
                    ORDER BY ts_code,trade_date,id""",
                (*unique_symbols, report_start, compact_date, cutoff),
            ).fetchall()
        except Exception:
            analyst_report_rows = []

    observations: dict[str, dict[str, Any]] = {}

    def record(symbol: str, candidate: dict[str, Any]) -> None:
        symbol = str(symbol or "")
        if symbol not in unique_symbols:
            return
        observations[symbol] = merge_event_candidates(
            observations.get(symbol),
            candidate,
        )

    for row in announcement_rows:
        item = dict(row)
        usable = _event_usable_trade_date(
            str(item.get("event_time") or ""),
            str(item.get("available_time") or ""),
            trade_dates,
        )
        if usable != compact_date:
            continue
        record(
            str(item.get("ts_code") or ""),
            structured_event_candidate(
                source="announcement",
                event_type=str(item.get("event_type") or ""),
                direction=str(item.get("impact_direction") or "neutral"),
                severity=str(item.get("severity") or "medium"),
                confidence=_safe(item.get("confidence")),
                title=str(item.get("title") or ""),
                text=str(item.get("raw_text") or ""),
                metrics=item.get("metrics_json"),
            ),
        )

    analyst_consensus_index = build_analyst_consensus_index(analyst_report_rows)

    guidance_by_symbol_period: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in sorted(
        structured_event_rows,
        key=lambda value: (
            str(dict(value).get("available_time") or ""),
            str(dict(value).get("ts_code") or ""),
        ),
    ):
        item = dict(row)
        payload = payload_dict(item.get("payload_json"))
        api_name = str(item.get("api_name") or "")
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        available_time = str(item.get("available_time") or "")
        usable = _event_usable_trade_date(
            str(item.get("ann_date") or payload.get("ann_date") or available_time),
            available_time,
            trade_dates,
        )
        period = str(item.get("period") or payload.get("end_date") or "")
        period_key = report_period_key(period)
        prior = guidance_by_symbol_period.get((symbol, period_key))
        analyst_consensus = point_in_time_analyst_consensus(
            analyst_consensus_index,
            symbol=symbol,
            period=period_key,
            as_of_date=usable,
        )
        expectation_gap, expectation_basis, current_guidance = (
            disclosure_expectation_gap(
                api_name=api_name,
                payload=payload,
                consensus=analyst_consensus,
                prior_guidance=prior,
            )
        )
        if api_name == "forecast" and period_key:
            guidance_by_symbol_period[(symbol, period_key)] = current_guidance
        if usable == compact_date:
            record(
                symbol,
                structured_event_candidate(
                    source="structured_disclosure",
                    api_name=api_name,
                    confidence=0.98,
                    title=api_name,
                    payload=payload,
                    expectation_gap=expectation_gap,
                    expectation_basis=expectation_basis,
                    analyst_consensus=analyst_consensus,
                ),
            )

    if news_rows:
        from app.services.news_theme_cluster import _match_companies

        symbol_set = set(unique_symbols)
        for row in news_rows:
            item = dict(row)
            payload = _payload_dict(item.get("payload_json"))
            text = f"{payload.get('title') or ''} {payload.get('content') or ''}".strip()
            if not text:
                continue
            event_time = str(payload.get("pub_time") or item.get("available_time") or "")
            usable = _event_usable_trade_date(
                event_time,
                str(item.get("available_time") or event_time),
                trade_dates,
            )
            if usable != compact_date:
                continue
            direction, severity, confidence = _event_news_direction(text)
            for company in _match_companies(text):
                symbol = str(company.get("ts_code") or "")
                if symbol in symbol_set:
                    record(
                        symbol,
                        structured_event_candidate(
                            source="major_news",
                            direction=direction,
                            severity=severity,
                            confidence=confidence,
                            title=str(payload.get("title") or ""),
                            text=str(payload.get("content") or ""),
                        ),
                    )

    analyst_by_symbol: dict[str, list[dict[str, Any]]] = {}
    rating_scores = {"买入": 5.0, "增持": 4.0, "中性": 3.0, "减持": 2.0, "卖出": 1.0}
    for row in analyst_report_rows:
        item = dict(row)
        payload = payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        targets = [
            float(payload[key])
            for key in ("min_price", "max_price")
            if payload.get(key) is not None
        ]
        if symbol:
            analyst_by_symbol.setdefault(symbol, []).append({
                "trade_date": str(item.get("trade_date") or "")[:8],
                "rating": rating_scores.get(str(payload.get("rating") or "")),
                "target": sum(targets) / len(targets) if targets else None,
            })
    current_date = datetime.strptime(compact_date, "%Y%m%d").date()
    recent_start = (current_date - timedelta(days=27)).strftime("%Y%m%d")
    prior_start = (current_date - timedelta(days=55)).strftime("%Y%m%d")
    for symbol, rows in analyst_by_symbol.items():
        if symbol not in observations:
            continue
        recent = [
            float(item["rating"])
            for item in rows
            if item.get("rating") is not None
            and recent_start <= str(item["trade_date"]) <= compact_date
        ]
        prior = [
            float(item["rating"])
            for item in rows
            if item.get("rating") is not None
            and prior_start <= str(item["trade_date"]) < recent_start
        ]
        targets = [
            float(item["target"])
            for item in rows
            if item.get("target") is not None
            and prior_start <= str(item["trade_date"]) <= compact_date
        ]
        observations[symbol]["event_analyst_revision_raw"] = (
            sum(recent) / len(recent) - sum(prior) / len(prior)
            if recent and prior else None
        )
        observations[symbol]["event_analyst_target_price_raw"] = (
            sum(targets) / len(targets) if targets else None
        )

    for row in top_list_rows:
        item = dict(row)
        payload = _payload_dict(item.get("payload_json"))
        usable = _event_usable_trade_date(
            str(item.get("trade_date") or payload.get("trade_date") or ""),
            str(item.get("available_time") or ""),
            trade_dates,
        )
        if usable != compact_date:
            continue
        net_rate = _safe(payload.get("net_rate"))
        pct_change = _safe(payload.get("pct_change"))
        sentiment = max(
            -1.0,
            min(
                1.0,
                0.7 * math.tanh(net_rate / 10.0)
                + 0.3 * math.tanh(pct_change / 10.0),
            ),
        )
        confidence = min(0.95, 0.70 + 0.25 * abs(math.tanh(net_rate / 10.0)))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "")
        if symbol not in observations:
            continue
        observations[symbol].update({
            "event_source_top_list": 1.0,
            "event_top_list_signal": sentiment,
            "event_top_list_confidence": confidence,
        })

    benchmark_closes: list[tuple[str, float]] = []
    for row in benchmark_rows:
        item = dict(row)
        close = _safe(_payload_dict(item.get("payload_json")).get("close"))
        if close > 0:
            benchmark_closes.append((str(item.get("trade_date") or ""), close))
    benchmark_closes.sort()
    benchmark_return = 0.0
    if len(benchmark_closes) >= 2 and benchmark_closes[-2][1] > 0:
        benchmark_return = benchmark_closes[-1][1] / benchmark_closes[-2][1] - 1.0
    for observation in observations.values():
        sentiment = float(observation.get("sentiment") or 0.0)
        confidence = float(observation.get("confidence") or 0.0)
        source = str(observation.get("source") or "unknown")
        observed_sources = set(observation.get("observed_sources") or [source])
        observation.update({
            "event_text_sentiment": sentiment,
            "event_text_confidence": confidence,
            "event_surprise": sentiment * confidence,
            "event_source_type": source,
            **{
                f"event_source_{item}": float(
                    item == source or item in observed_sources
                )
                for item in EVENT_STRUCTURED_SOURCES
            },
        })
        observation["event_benchmark_return"] = benchmark_return
    if len(_EVENT_FEATURE_CACHE) >= _EVENT_FEATURE_CACHE_LIMIT:
        _EVENT_FEATURE_CACHE.pop(next(iter(_EVENT_FEATURE_CACHE)))
    _EVENT_FEATURE_CACHE[cache_key] = observations
    return observations


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


def _resolve_online_inference_versions(
    authorized_versions: dict[str, dict[str, Any] | None],
    model_bindings: dict[str, Any] | None,
) -> dict[str, dict[str, Any] | None]:
    """Resolve versions allowed to score live data, separately from trade authority.

    Frozen research replays must remain exact and never discover newer candidates.
    Live decisions may score the newest artifact-bearing version for observation,
    while the authorized bundle below still controls weights and hard gates.
    """
    if model_bindings is not None:
        return dict(authorized_versions)
    online: dict[str, dict[str, Any] | None] = {}
    for key in MODEL_KEYS:
        authorized = authorized_versions.get(key)
        if authorized:
            online[key] = authorized
            continue
        try:
            candidates = list_model_versions(key, limit=20)
        except Exception:
            candidates = []
        online[key] = next(
            (
                version for version in candidates
                if str(version.get("artifact_path") or "")
                and str(version.get("status") or "")
                not in {"superseded", "rolled_back"}
            ),
            None,
        )
    return online


def _limited_live_authorization(version: dict[str, Any] | None) -> dict[str, Any]:
    """Candidates remain observation-only until complete production promotion."""
    validation = version.get("validation") if version and isinstance(
        version.get("validation"), dict
    ) else {}
    shadow = validation.get("model_shadow") if isinstance(
        validation.get("model_shadow"), dict
    ) else {}
    return {
        "authorized": False,
        "tier": "shadow_only",
        "weight_scale": 0.0,
        "reason": "candidate_requires_complete_production_qualification",
        "observed_return_sessions": int(shadow.get("observed_return_sessions") or 0),
    }

def _limited_live_authorized(version: dict[str, Any] | None) -> bool:
    return bool(_limited_live_authorization(version)["authorized"])


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
    event: dict[str, Any] | None = None,
    market_state: str = "S1",
) -> dict[str, float]:
    event = event or {}
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
    event_market_reaction = (
        _safe(row.change_pct) / 100.0
        - _safe(event.get("event_benchmark_return"))
    )
    top_list_confirmed = bool(
        _safe(event.get("event_source_top_list")) > 0.0
    )
    event_crowding = (
        max(
            0.0,
            min(
                3.0,
                abs(event_market_reaction) / max(0.005, volatility_20)
                + max(0.0, turnover_slope)
                + abs(_safe(event.get("event_top_list_signal"))),
            ),
        )
        if top_list_confirmed
        else max(0.0, min(3.0, max(0.0, turnover_slope)))
    )
    structured_features = materialize_event_features(
        event,
        total_mv=factors.get("total_mv"),
        close=row.close,
        momentum_20=momentum_20,
        volatility_20=volatility_20,
        market_reaction=event_market_reaction,
        crowding=event_crowding,
        analyst_revision=event.get("event_analyst_revision_raw"),
        analyst_target_price=event.get("event_analyst_target_price_raw"),
    )
    normalized_market_state = str(market_state or "S1").upper()
    return {
        "momentum_20": momentum_20,
        "momentum_60": momentum_60,
        "volatility_20": volatility_20,
        "turnover_slope": turnover_slope,
        "capital_persistence": capital_persistence,
        "narrative_strength": narrative_strength,
        "event_surprise": _safe(event.get("event_surprise")),
        "event_source_type": str(event.get("event_source_type") or "unknown"),
        "event_type_group": str(event.get("event_type_group") or "other"),
        "event_text_sentiment": _safe(event.get("event_text_sentiment")),
        "event_text_confidence": _safe(event.get("event_text_confidence")),
        "event_market_reaction": event_market_reaction,
        "event_crowding": event_crowding,
        "event_source_announcement": _safe(event.get("event_source_announcement")),
        "event_source_structured_disclosure": _safe(
            event.get("event_source_structured_disclosure")
        ),
        "event_source_major_news": _safe(event.get("event_source_major_news")),
        "event_source_top_list": _safe(event.get("event_source_top_list")),
        **{
            f"market_state_s{state}": float(normalized_market_state == f"S{state}")
            for state in range(4)
        },
        **structured_features,
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
    online_versions = _resolve_online_inference_versions(versions, model_bindings)
    artifacts = {key: _artifact(version) for key, version in online_versions.items()}
    availability = {
        key: _point_in_time_model_availability(
            online_versions.get(key), artifacts.get(key), as_of_time=as_of_time,
        )
        for key in MODEL_KEYS
    }
    authorized_availability = {
        key: _point_in_time_model_availability(
            versions.get(key), _artifact(versions.get(key)), as_of_time=as_of_time,
        )
        for key in MODEL_KEYS
    }
    limited_authorization = {
        key: _limited_live_authorization(online_versions.get(key))
        if (
            not authorized_availability[key]["available"]
            and availability[key]["available"]
        )
        else {
            "authorized": False,
            "tier": "not_applicable",
            "weight_scale": 0.0,
            "reason": "production_authorized_or_unavailable",
            "observed_return_sessions": 0,
        }
        for key in MODEL_KEYS
    }
    health_enabled = not explicit_bundle if use_live_health is None else bool(use_live_health)
    health_controls = (
        _live_model_controls(online_versions)
        if health_enabled else _live_model_controls(online_versions, enabled=False)
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
            if authorized_availability[key]["available"] or limited_authorization[key]["authorized"]
            else 0.0
        )
        for key, weight in MODEL_WEIGHTS.items()
    }
    raw_effective_weights = {
        key: value * (
            float(limited_authorization[key]["weight_scale"])
            if limited_authorization[key]["authorized"]
            and not authorized_availability[key]["available"]
            else 1.0
        )
        for key, value in raw_effective_weights.items()
    }
    limited_total = sum(
        value for key, value in raw_effective_weights.items()
        if limited_authorization[key]["authorized"] and not authorized_availability[key]["available"]
    )
    if limited_total > LIMITED_LIVE_AGGREGATE_CAP:
        limited_scale = LIMITED_LIVE_AGGREGATE_CAP / limited_total
        raw_effective_weights = {
            key: value * limited_scale
            if limited_authorization[key]["authorized"] and not authorized_availability[key]["available"]
            else value
            for key, value in raw_effective_weights.items()
        }
    raw_total = sum(raw_effective_weights.values())
    scale = min(1.0, 0.55 / raw_total) if raw_total > 0 else 1.0
    effective_weights = {key: value * scale for key, value in raw_effective_weights.items()}
    base_rank_weight = max(0.45, 1.0 - sum(effective_weights.values()))
    theme_by_symbol = _theme_evidence(themes)
    raw_scores: dict[str, dict[str, float]] = {key: {} for key in MODEL_KEYS}
    raw_outputs: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in MODEL_KEYS}
    features_by_symbol: dict[str, dict[str, float]] = {}
    event_features: dict[str, dict[str, Any]] = {}
    if availability.get("event_impact_assessor_v2", {}).get("available"):
        try:
            event_features = _point_in_time_event_features(
                [str(row.symbol) for row, _base in ranked],
                trade_date=str(ranked[0][0].date) if ranked else "",
                as_of_time=as_of_time,
            )
        except Exception:
            event_features = {}
    for row, _base in ranked:
        symbol = str(row.symbol)
        features = _features(
            row,
            factor_values.get(symbol) or {},
            histories.get(symbol) or [],
            theme_by_symbol.get(symbol) or {},
            event_features.get(symbol) or {},
            market_state=environment_key,
        )
        features_by_symbol[symbol] = features
    for key in MODEL_KEYS:
        if not availability[key]["available"]:
            continue
        scoring_features = (
            {
                symbol: features
                for symbol, features in features_by_symbol.items()
                if float(features.get("event_text_confidence") or 0.0) > 0.0
            }
            if key == "event_impact_assessor_v2"
            else features_by_symbol
        )
        if not scoring_features:
            continue
        if score_plan_model_features is _DEFAULT_SCORE_PLAN_MODEL_FEATURES:
            results = score_plan_model_feature_batch(
                key,
                scoring_features,
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
                for symbol, features in scoring_features.items()
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
                # An online event model is intentionally sparse: most stocks
                # have no eligible point-in-time event on a given day. Keep
                # that distinct from an invalid numeric score so the frozen
                # plan can validate the actual per-symbol consumption.
                "score_available": symbol in raw_scores[key],
                "score_status": (
                    "scored" if symbol in raw_scores[key]
                    else "no_observable_event" if key == "event_impact_assessor_v2"
                    and availability[key]["available"]
                    else "not_scored"
                ),
                "percentile": percentiles[key].get(symbol),
                "version_id": (online_versions.get(key) or {}).get("id"),
                "version": (online_versions.get(key) or {}).get("version"),
                "status": (
                    health_controls[key]["status"]
                    if availability[key]["available"] else "proxy_or_unavailable"
                ),
                "weight_multiplier": health_controls[key]["weight_multiplier"],
                "health_reason": health_controls[key]["reason"],
                "point_in_time_available": bool(availability[key]["available"]),
                "availability_reason": str(availability[key]["reason"]),
                "online": bool(availability[key]["available"]),
                "trade_authorized": bool(
                    authorized_availability[key]["available"]
                    or limited_authorization[key]["authorized"]
                ),
                "authorization_scope": (
                    "full_production"
                    if authorized_availability[key]["available"]
                    else str(limited_authorization[key]["tier"])
                    if limited_authorization[key]["authorized"]
                    else "risk_only"
                    if key == "event_impact_assessor_v2"
                    and availability[key]["available"]
                    else "observation_only"
                ),
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
        narrative_model_online = bool(
            online_versions.get("narrative_transition_multitask_v1")
            and availability["narrative_transition_multitask_v1"]["available"]
        )
        narrative_model_available = bool(
            narrative_model_online
            and authorized_availability["narrative_transition_multitask_v1"]["available"]
        )
        narrative_effect_authorized = bool(
            narrative_outputs.get("lifecycle_effect_authorized")
            and str(narrative_outputs.get("lifecycle_effect_contract") or "")
            == NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT
        )
        narrative_model_adjustment = 0.0
        if (
            narrative_model_available
            and narrative_effect_authorized
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
            and narrative_effect_authorized
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
            and authorized_availability["capital_persistence_multitask_v1"]["available"]
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
        event_outputs = raw_outputs["event_impact_assessor_v2"].get(symbol) or {}
        event_model_online = bool(
            online_versions.get("event_impact_assessor_v2")
            and availability["event_impact_assessor_v2"]["available"]
        )
        event_model_available = bool(
            event_model_online
            and authorized_availability["event_impact_assessor_v2"]["available"]
        )
        event_contract_ready = bool(
            event_outputs.get("impact_contract") in {
                "alis_conditional_event_impact_assessment_v2",
                "alis_conditional_event_impact_assessment_v3",
                "alis_conditional_event_impact_assessment_v4",
            }
        )
        event_block = bool(
            event_model_available
            and event_contract_ready
            and event_outputs.get("event_hard_veto") is True
        )
        event_risk_penalty = 0.0
        # Non-production event candidates are observation-only. Their scores
        # cannot change ranking, vetoes, exits, or risk in live decisions.
        if capital_block or narrative_exit or event_block:
            blocked.append({
                "symbol": symbol,
                "ts_code": symbol,
                "side": "BUY",
                "code": (
                    "capital_persistence_gate_failed"
                    if capital_block
                    else "event_impact_hard_veto"
                    if event_block
                    else "narrative_model_exit_gate"
                    if narrative_model_hard_veto
                    and not bool(theme.get("hard_veto"))
                    else "narrative_exit_gate"
                ),
                "message": (
                    "生产资金持续模型位于截面后10%，禁止新增"
                    if capital_block
                    else "事件影响评估器识别到成本后高置信负面尾部，禁止新增"
                    if event_block
                    else "监督叙事模型识别到高置信生命周期退出风险，禁止新增"
                    if narrative_model_hard_veto
                    and not bool(theme.get("hard_veto"))
                    else "叙事处于退潮、沉寂、虚假或风险状态，禁止新增"
                ),
            })
            continue
        event_pct = percentiles["event_impact_assessor_v2"].get(symbol)
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
            "event_eligible": bool(
                event_model_available
                and
                event_contract_ready
                and float(
                    event_outputs.get("positive_impact_probability") or 0.0
                ) >= 0.55
                and float(event_outputs.get("priced_in_probability") or 1.0) <= 0.60
                and float(event_outputs.get("abnormal_return_q10") or -1.0) > -0.02
            ),
            "event_impact": {
                "online": event_model_online,
                "available": event_model_available,
                "authorization_scope": (
                    "full_production" if event_model_available else "risk_only"
                    if event_model_online else "unavailable"
                ),
                "contract_ready": event_contract_ready,
                "rank_percentile": event_pct,
                "risk_penalty": round(event_risk_penalty * 100.0, 6),
                "positive_rank_uplift_forbidden": not event_model_available,
                "hard_veto_authorized": event_model_available,
                "outputs": event_outputs,
            },
            "narrative": theme or {"lifecycle": "未识别", "score": 0.0},
            "theme_rank_bonus": round(theme_bonus * 100.0, 6),
            "narrative_model": {
                "effect_contract": NARRATIVE_MODEL_LIFECYCLE_EFFECT_CONTRACT,
                "online": narrative_model_online,
                "available": narrative_model_available,
                "effect_authorized": narrative_effect_authorized,
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
                and (explicit_bundle or authorized_availability[key]["available"])
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
                "version_id": (online_versions.get(key) or {}).get("id"),
                "version": (online_versions.get(key) or {}).get("version"),
                "consumed": bool(
                    availability[key]["available"]
                    and (explicit_bundle or authorized_availability[key]["available"])
                ),
                "online": bool(availability[key]["available"]),
                "trade_authorized": bool(
                    authorized_availability[key]["available"]
                    or limited_authorization[key]["authorized"]
                ),
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
                "authorization_reason": (
                    "production_contract_passed"
                    if authorized_availability[key]["available"]
                    else str(limited_authorization[key]["reason"])
                    if limited_authorization[key]["authorized"]
                    else "risk_only_online_observation"
                    if key == "event_impact_assessor_v2"
                    and availability[key]["available"]
                    else "online_observation_only"
                ),
                "authorization_tier": (
                    "production"
                    if authorized_availability[key]["available"]
                    else str(limited_authorization[key]["tier"])
                    if limited_authorization[key]["authorized"]
                    else "risk_only"
                    if key == "event_impact_assessor_v2"
                    and availability[key]["available"]
                    else "shadow_only"
                ),
                "future_observed_sessions": int(
                    limited_authorization[key]["observed_return_sessions"]
                ),
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
            "online_inference_enabled": True,
            "trade_authority_separate_from_inference": True,
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
                "validation_final_state_must_beat_persistence": True,
                "unthemed_candidates_remain_eligible": True,
            },
            "maturity_multipliers": maturity_multipliers,
            "effective_model_weights": {key: round(weight, 6) for key, weight in effective_weights.items()},
            "limited_live_authorization": {
                "enabled": False,
                "policy": "complete_production_qualification_required",
                "model_keys": sorted(LIMITED_LIVE_MODEL_KEYS),
                "initial_weight_scale": LIMITED_LIVE_WEIGHT_SCALE,
                "passed_weight_scale": LIMITED_LIVE_PASSED_SCALE,
                "inconclusive_weight_scale": LIMITED_LIVE_WATCH_SCALE,
                "aggregate_cap": LIMITED_LIVE_AGGREGATE_CAP,
                "first_review_sessions": 20,
                "authorized_models": [
                    key for key, value in limited_authorization.items()
                    if value["authorized"]
                ],
                "model_tiers": limited_authorization,
            },
            "event_risk_only": {
                "online": bool(availability["event_impact_assessor_v2"]["available"]),
                "maximum_rank_penalty": MAX_EVENT_RISK_PENALTY,
                "positive_rank_uplift_forbidden": True,
                "hard_veto_requires_full_production_authority": True,
            },
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

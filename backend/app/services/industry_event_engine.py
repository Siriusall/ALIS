from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
import hashlib
import json
import math
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn, _read_conn
from app.data_platform.services.observation_clock import (
    _explicit_timestamp_sql,
    _table_columns,
    _timestamp_value_sql,
    _valid_provenance_sql,
    load_raw_record_versions,
)
from app.services.point_in_time import normalize_explicit_timestamp

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


INDUSTRY_WEIGHTS = {
    "demand_orders_sales": .25,
    "product_price": .15,
    "inventory_cycle": .15,
    "capacity_utilization": .10,
    "profit_diffusion": .15,
    "policy_supply": .10,
    "industry_valuation": .10,
}

EVENT_WEIGHTS = {
    "source_reliability": .20,
    "quantified_materiality": .25,
    "direction": .15,
    "duration": .15,
    "thesis_impact": .15,
    "novelty": .10,
}

EVENT_HALF_LIFE_DAYS = {
    "regulatory_risk": 180,
    "shareholder_reduction": 60,
    "pledge": 90,
    "earnings": 45,
    "earnings_forecast": 45,
    "earnings_flash": 45,
    "repurchase": 60,
    "dividend": 30,
    "major_contract": 60,
    "ma_restructuring": 120,
    "private_placement": 90,
    "capital_operation": 60,
}

V1_EVENT_TYPES = {
    "earnings", "earnings_forecast", "earnings_flash", "dividend", "repurchase",
    "shareholder_increase", "shareholder_reduction", "pledge", "regulatory_risk",
    "inquiry", "investigation", "major_contract", "ma_restructuring",
    "private_placement",
}

INDUSTRY_METRIC_HINTS = {
    "demand_orders_sales": ("需求", "订单", "销量", "销售", "出货"),
    "product_price": ("价格", "售价", "价差"),
    "inventory_cycle": ("库存", "库销"),
    "capacity_utilization": ("产能", "开工", "利用率"),
    "profit_diffusion": ("利润", "毛利", "盈利"),
    "policy_supply": ("政策", "供给", "产量"),
    "industry_valuation": ("估值", "市盈", "市净"),
}


def _clip(value: Any, low: float = 0, high: float = 100) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def score_industry(observations: dict[str, Any], *, prior_score: float | None = None, history_points: int = 0) -> dict[str, Any]:
    available = {key: _clip(observations[key]) for key in INDUSTRY_WEIGHTS if observations.get(key) is not None}
    coverage = sum(INDUSTRY_WEIGHTS[key] for key in available)
    score = sum(available[key] * INDUSTRY_WEIGHTS[key] for key in available) / coverage if coverage else 0.0
    delta = None if prior_score is None else score - float(prior_score)
    if coverage < .25 or history_points < 1:
        state, status = "证据不足", "insufficient_data"
    elif score < 30 and (delta is None or delta <= 0):
        state, status = "衰退", "ready"
    elif score < 45:
        state, status = ("筑底" if delta is not None and delta > 0 else "衰退"), "ready"
    elif score < 60:
        state, status = ("复苏" if delta is not None and delta > 0 else "筑底"), "ready"
    elif score < 75 and (delta is None or delta >= -5):
        state, status = "扩张", "ready"
    elif score >= 75 and (delta is None or delta >= 0):
        state, status = "过热", "ready"
    else:
        state, status = "回落", "ready"
    return {
        "score": round(score, 4), "coverage": round(coverage, 4), "status": status,
        "lifecycle": state, "score_change": None if delta is None else round(delta, 4),
        "components": {key: {"value": value, "weight": INDUSTRY_WEIGHTS[key]} for key, value in available.items()},
        "missing": [key for key in INDUSTRY_WEIGHTS if key not in available],
        "evidence_points": history_points,
    }


def _metric_component(definition: dict[str, Any]) -> str | None:
    text = " ".join(str(definition.get(key) or "") for key in ("metric_code", "metric_name", "description"))
    return next((component for component, hints in INDUSTRY_METRIC_HINTS.items() if any(hint in text for hint in hints)), None)


def _metric_signal(definition: dict[str, Any], values: list[dict[str, Any]]) -> float | None:
    numeric = []
    for row in reversed(values):
        try:
            numeric.append(float(row.get("value")))
        except (TypeError, ValueError):
            continue
    description = str(definition.get("description") or "")
    lag_match = re.search(r"lag_trading_days=(\d+)", description)
    lag = int(lag_match.group(1)) if lag_match else 0
    if lag > 0:
        numeric = numeric[:-lag] if len(numeric) > lag else []
    if len(numeric) < 3:
        return None
    baseline = sum(numeric[:-1]) / len(numeric[:-1])
    scale = max(abs(baseline), max(numeric) - min(numeric), 1e-9)
    change = (numeric[-1] - baseline) / scale
    component = _metric_component(definition)
    inverse = component in {"inventory_cycle", "industry_valuation"}
    normalized = 50 - change * 50 if inverse else 50 + change * 50
    return _clip(normalized)


def _industry_state_from_series(series: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    history_points = 0
    for item in series:
        definition, values = item["definition"], item["values"]
        history_points += len(values)
        component = _metric_component(definition)
        signal = _metric_signal(definition, values)
        if component and signal is not None:
            grouped.setdefault(component, []).append(signal)
            evidence.setdefault(component, []).append({"metric_code": definition.get("metric_code"), "metric_name": definition.get("metric_name"), "latest_date": values[0].get("data_date") if values else None, "points": len(values), "signal": round(signal, 4)})
    observations = {key: sum(values) / len(values) for key, values in grouped.items()}
    return score_industry(observations, history_points=history_points), evidence


def _financial_proxy_source_signature(c: Any, *, as_of_time: str | None) -> str:
    cutoff_clause = "AND available_time<=?" if as_of_time else ""
    params: tuple[Any, ...] = (str(as_of_time),) if as_of_time else ()
    row = c.execute(
        f"""SELECT COUNT(*) AS row_count,COALESCE(MAX(id),0) AS max_id,
                   COALESCE(MAX(available_time),'') AS max_available_time,
                   COALESCE(MAX(updated_at),'') AS max_updated_at
              FROM dc_tushare_raw_records
             WHERE api_name IN ('income','balancesheet') {cutoff_clause}""",
        params,
    ).fetchone()
    payload = dict(row) if row else {}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _load_financial_proxy_cache(c: Any, source_signature: str) -> dict[str, dict[str, Any]] | None:
    row = c.execute(
        "SELECT proxies_json FROM dc_industry_financial_proxy_cache WHERE source_signature=?",
        (str(source_signature),),
    ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(str(dict(row).get("proxies_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _store_financial_proxy_cache(
    c: Any,
    *,
    source_signature: str,
    as_of_time: str | None,
    proxies: dict[str, dict[str, Any]],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    c.execute(
        """INSERT INTO dc_industry_financial_proxy_cache
               (source_signature,as_of_time,proxies_json,industry_count,created_at,updated_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(source_signature) DO UPDATE SET
               as_of_time=excluded.as_of_time,proxies_json=excluded.proxies_json,
               industry_count=excluded.industry_count,updated_at=excluded.updated_at""",
        (
            str(source_signature), str(as_of_time or ""),
            json.dumps(proxies, ensure_ascii=False, sort_keys=True),
            len(proxies), now, now,
        ),
    )


def _visible_stock_master_rows(c: Any, *, as_of_time: str | None = None) -> list[dict[str, Any]]:
    if not as_of_time:
        return [dict(row) for row in c.execute(
            "SELECT ts_code,industry FROM dc_stock_master WHERE COALESCE(industry,'')<>''"
        ).fetchall()]
    cutoff = normalize_explicit_timestamp(as_of_time)
    if not cutoff:
        raise RuntimeError("industry_stock_master_cutoff_invalid")
    cutoff_date = cutoff[:10].replace("-", "")
    try:
        rows = [dict(row) for row in c.execute(
            """SELECT ts_code,industry FROM (
                   SELECT ts_code,industry,list_date,
                          ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY observed_at DESC,id DESC) AS rank
                     FROM dc_stock_master_version
                    WHERE observed_at<=? AND COALESCE(industry,'')<>''
                          AND (COALESCE(list_date,'')='' OR list_date<=?)
               ) visible WHERE rank=1""",
            (cutoff, cutoff_date),
        ).fetchall()]
    except Exception as exc:
        raise RuntimeError("industry_stock_master_pit_unavailable") from exc
    if not rows:
        raise RuntimeError("industry_stock_master_not_visible_at_cutoff")
    return rows


def _derived_industry_proxies(c, *, as_of_time: str | None = None) -> dict[str, dict[str, Any]]:
    """Build auditable P1 industry proxies from company financial and valuation facts."""
    target_rows: dict[str, list[dict[str, Any]]] | None = None

    def immutable_ranked() -> dict[str, list[dict[str, Any]]]:
        """Select financial periods from append-only rows at the cutoff.

        The mutable raw projection is an overwrite cache.  Historical industry
        states must use the same four-clock immutable contract as the stock
        research path, then choose the latest common annual/comparable periods
        in Python so this remains portable across PostgreSQL and SQLite.
        """
        cutoff = normalize_explicit_timestamp(as_of_time)
        if not cutoff:
            raise RuntimeError("industry_financial_pit_cutoff_invalid")
        by_api: dict[str, list[dict[str, Any]]] = {"income": [], "balancesheet": []}
        for api_name in by_api:
            # Industry proxies only compare the latest annual/comparable
            # periods. Keep the immutable PIT read bounded before decoding
            # the complete financial archive.
            rows = load_raw_record_versions(c, api_name, cutoff=cutoff, period_limit=8)
            if not rows:
                cutoff_iso = cutoff[:19]
                try:
                    rows = [dict(r) for r in c.execute(
                        "SELECT ts_code, period, available_time, updated_at, payload_json, observed_at FROM dc_tushare_raw_records WHERE api_name=? AND (available_time='' OR available_time<=?) ORDER BY period DESC LIMIT 4000",
                        (api_name, cutoff_iso),
                    ).fetchall()]
                except Exception:
                    rows = []
            for raw in rows:
                item = dict(raw)
                code = str(item.get("ts_code") or "").strip()
                if code not in master_codes:
                    continue
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                period = str(item.get("period") or payload.get("period") or payload.get("end_date") or "")
                if not period:
                    continue
                value = {
                    "ts_code": code,
                    "_period": period,
                    "total_revenue": payload.get("total_revenue"),
                    "n_income": payload.get("n_income"),
                    "inventories": payload.get("inventories"),
                    "observed_at": item.get("observed_at"),
                }
                by_api[api_name].append(value)
        # Keep one row per symbol/period and only the bounded periods used by
        # the proxy calculations.  All candidate rows are already cutoff-safe.
        deduped: dict[str, list[dict[str, Any]]] = {}
        for api_name, rows in by_api.items():
            chosen: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                key = (str(row.get("ts_code") or ""), str(row.get("_period") or ""))
                current = chosen.get(key)
                if current is None or str(row.get("observed_at") or "") > str(current.get("observed_at") or ""):
                    chosen[key] = row
            for code in master_codes:
                values = [item for (item_code, _period), item in chosen.items() if item_code == code]
                values.sort(key=lambda item: str(item.get("_period") or ""), reverse=True)
                deduped.setdefault(api_name, []).extend(values[:8])
        income_by_code: dict[str, dict[str, dict[str, Any]]] = {}
        balance_by_code: dict[str, dict[str, dict[str, Any]]] = {}
        for item in deduped.get("income", []):
            income_by_code.setdefault(item["ts_code"], {})[item["_period"]] = item
        for item in deduped.get("balancesheet", []):
            balance_by_code.setdefault(item["ts_code"], {})[item["_period"]] = item
        selected: dict[str, set[tuple[str, str]]] = {"income": set(), "balancesheet": set()}
        for code in master_codes:
            income_periods = income_by_code.get(code, {})
            balance_periods = balance_by_code.get(code, {})
            if income_periods:
                latest = max(income_periods)
                selected["income"].add((code, latest))
                if len(latest) == 8 and latest.isdigit():
                    selected["income"].add((code, f"{int(latest[:4]) - 1:04d}{latest[4:]}"))
            common = set(income_periods) & set(balance_periods)
            common = {
                period for period in common
                if len(period) == 8 and period.isdigit()
                and f"{int(period[:4]) - 1:04d}{period[4:]}" in common
            }
            if common:
                latest_common = max(common)
                prior = f"{int(latest_common[:4]) - 1:04d}{latest_common[4:]}"
                for api_name in selected:
                    selected[api_name].update({(code, latest_common), (code, prior)})
        result: dict[str, list[dict[str, Any]]] = {"income": [], "balancesheet": []}
        for api_name, lookup in (("income", income_by_code), ("balancesheet", balance_by_code)):
            for code, period in sorted(selected[api_name]):
                item = lookup.get(code, {}).get(period)
                if item is not None:
                    result[api_name].append(item)
        return result

    def ranked(api_name: str) -> list[dict[str, Any]]:
        nonlocal target_rows
        if as_of_time:
            if target_rows is None:
                target_rows = immutable_ranked()
            return list(target_rows.get(api_name, []))
        if target_rows is None:
            # Preserve the existing eight-period contract for target selection,
            # but send only the two-year comparison periods used below.
            period_clause = "AND r.available_time<=?" if as_of_time else ""
            target_sql = f"""WITH dedup_periods AS MATERIALIZED (
                    SELECT DISTINCT ON (r.api_name,r.ts_code,r.period)
                           r.api_name,r.ts_code,r.period,r.available_time,r.id
                      FROM dc_tushare_raw_records r
                     WHERE r.api_name IN ('income','balancesheet') {period_clause}
                     ORDER BY r.api_name,r.ts_code,r.period,r.available_time DESC,r.id DESC
                ), limited AS MATERIALIZED (
                    SELECT api_name,ts_code,period
                      FROM (
                        SELECT api_name,ts_code,period,
                               ROW_NUMBER() OVER(PARTITION BY api_name,ts_code ORDER BY period DESC) AS rn
                          FROM dedup_periods
                      ) ranked
                     WHERE rn<=8
                ), income_latest AS (
                    SELECT DISTINCT ON (ts_code) ts_code,period
                      FROM limited
                     WHERE api_name='income'
                     ORDER BY ts_code,period DESC
                ), income_targets AS (
                    SELECT 'income'::text AS api_name,ts_code,period FROM income_latest
                    UNION
                    SELECT 'income'::text,ts_code,
                           ((SUBSTRING(period,1,4)::integer-1)::text || SUBSTRING(period,5,4))
                      FROM income_latest
                ), common_periods AS MATERIALIZED (
                    SELECT ts_code,period
                      FROM limited
                     GROUP BY ts_code,period
                    HAVING COUNT(*)=2
                ), latest_common AS (
                    SELECT DISTINCT ON (common.ts_code) common.ts_code,common.period
                      FROM common_periods common
                     WHERE EXISTS (
                           SELECT 1
                             FROM common_periods prior
                            WHERE prior.ts_code=common.ts_code
                              AND prior.period=((SUBSTRING(common.period,1,4)::integer-1)::text || SUBSTRING(common.period,5,4))
                     )
                     ORDER BY common.ts_code,common.period DESC
                ), common_targets AS (
                    SELECT 'income'::text AS api_name,ts_code,period FROM latest_common
                    UNION
                    SELECT 'income'::text,ts_code,
                           ((SUBSTRING(period,1,4)::integer-1)::text || SUBSTRING(period,5,4))
                      FROM latest_common
                    UNION
                    SELECT 'balancesheet'::text,ts_code,period FROM latest_common
                    UNION
                    SELECT 'balancesheet'::text,ts_code,
                           ((SUBSTRING(period,1,4)::integer-1)::text || SUBSTRING(period,5,4))
                      FROM latest_common
                ), targets AS (
                    SELECT api_name,ts_code,period FROM income_targets
                    UNION
                    SELECT api_name,ts_code,period FROM common_targets
                ), payload AS MATERIALIZED (
                    SELECT DISTINCT ON (r.api_name,r.ts_code,r.period)
                           r.api_name,r.ts_code,r.period,
                           CASE WHEN r.api_name='income'
                                THEN r.payload_json::jsonb->>'total_revenue' END AS total_revenue,
                           CASE WHEN r.api_name='income'
                                THEN r.payload_json::jsonb->>'n_income' END AS n_income,
                           CASE WHEN r.api_name='balancesheet'
                                THEN r.payload_json::jsonb->>'inventories' END AS inventories
                      FROM dc_tushare_raw_records r
                      JOIN targets t
                        ON t.api_name=r.api_name AND t.ts_code=r.ts_code AND t.period=r.period
                     WHERE 1=1 {period_clause}
                     ORDER BY r.api_name,r.ts_code,r.period,r.available_time DESC,r.id DESC
                )
                SELECT api_name,ts_code,period,total_revenue,n_income,inventories
                  FROM payload
                 ORDER BY api_name,ts_code,period DESC"""
            query_params = ((str(as_of_time),) if as_of_time else ()) * 2
            grouped = {"income": [], "balancesheet": []}
            for row in c.execute(target_sql, query_params).fetchall():
                item = dict(row)
                source_api = str(item.pop("api_name") or "")
                if source_api in grouped:
                    grouped[source_api].append(item)
            target_rows = grouped
        return list(target_rows.get(api_name, []))

    master_rows = _visible_stock_master_rows(c, as_of_time=as_of_time)
    master = {
        str(row["ts_code"]): str(row["industry"] or "").strip()
        for row in master_rows
    }
    master_codes = set(master)
    source_signature = _financial_proxy_source_signature(c, as_of_time=as_of_time) if not as_of_time else ""
    # A cache derived from the mutable projection is not valid evidence for a
    # historical cutoff. Recompute from immutable versions instead.
    financial_derived = _load_financial_proxy_cache(c, source_signature) if source_signature else None
    if financial_derived is None:
        income_rows, balance_rows = ranked("income"), ranked("balancesheet")
    else:
        income_rows, balance_rows = [], []
    as_of_date = str(as_of_time or "")[:10].replace("-", "")
    # Valuation is part of the formal industry state.  The mutable snapshot
    # projection can be overwritten by a late repair, so it must not be used
    # when deriving a state that carries an explicit observation cutoff.
    # Select the accepted immutable row per company only after all four clocks
    # have been constrained to that cutoff.  The normalized projection is the
    # authority for the selected provider/hash.  Joining it directly avoids a
    # window rank across the complete append-only snapshot archive; if its
    # accepted hash is not visible under this cutoff, it stays absent rather
    # than falling back to an older or future version.
    try:
        is_postgres = bool(getattr(c, "_is_postgres", False))
        version_columns = _table_columns(c, "dc_stock_daily_snapshot_version")
        projection_columns = _table_columns(c, "dc_stock_daily_snapshot")
        required_version_columns = {
            "id", "ts_code", "trade_date", "payload_json", "source_code",
            "economic_available_time", "observed_at", "first_observed_at",
            "observation_provenance", "content_hash", "created_at",
        }
        required_projection_columns = {"ts_code", "trade_date", "content_hash"}
        if not required_version_columns.issubset(version_columns):
            raise RuntimeError("industry_valuation_snapshot_version_schema_unavailable")
        observed_sql = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
        first_observed_sql = _timestamp_value_sql("v.first_observed_at", is_postgres=is_postgres)
        economic_sql = _timestamp_value_sql("v.economic_available_time", is_postgres=is_postgres)
        created_sql = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
        predicates = [
            "v.content_hash<>''",
            "NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL",
            _valid_provenance_sql("v.observation_provenance"),
            _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres),
            _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres),
            _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres),
            _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
        ]
        params: list[Any] = []
        if as_of_date:
            predicates.append("v.trade_date<=?")
            params.append(as_of_date)
        if as_of_time:
            predicates.extend([
                f"{observed_sql}<=?", f"{first_observed_sql}<=?",
                f"{economic_sql}<=?", f"{created_sql}<=?",
            ])
            params.extend([str(as_of_time)] * 4)
        pe_expr = (
            "CASE WHEN jsonb_typeof(v.payload_json::jsonb->'pe_ttm')='number' "
            "THEN (v.payload_json::jsonb->>'pe_ttm')::double precision END"
            if is_postgres else
            "CAST(json_extract(v.payload_json,'$.pe_ttm') AS REAL)"
        )
        if required_projection_columns.issubset(projection_columns):
            # There is one projection row per market bar and an immutable
            # version is unique by the same key plus content hash.  Group the
            # already filtered accepted set by symbol to locate the newest
            # session. This is portable to SQLite and avoids a ROW_NUMBER
            # across every raw provider version.
            latest_projection_predicate = (
                "AND p.trade_date=(SELECT MAX(trade_date) FROM dc_stock_daily_snapshot)"
                if not as_of_time else ""
            )
            snapshot_sql = f"""WITH accepted AS (
                                    SELECT v.ts_code,{pe_expr} AS pe_ttm,v.trade_date
                                      FROM dc_stock_daily_snapshot p
                                      JOIN dc_stock_daily_snapshot_version v
                                        ON v.ts_code=p.ts_code
                                       AND v.trade_date=p.trade_date
                                       AND v.content_hash=p.content_hash
                                     WHERE {' AND '.join(predicates)}
                                       {latest_projection_predicate}
                               ), latest_dates AS (
                                    SELECT ts_code,MAX(trade_date) AS trade_date
                                      FROM accepted
                                     GROUP BY ts_code
                               )
                               SELECT accepted.ts_code,accepted.pe_ttm
                                 FROM accepted
                                 JOIN latest_dates latest
                                   ON latest.ts_code=accepted.ts_code
                                  AND latest.trade_date=accepted.trade_date"""
            snapshots = [dict(row) for row in c.execute(snapshot_sql, tuple(params)).fetchall()]
        else:
            # A controlled fallback for isolated old-schema fixtures.  It
            # stays on immutable rows and preserves the explicit four-clock
            # predicates, but production always takes the accepted-hash path.
            snapshots = [dict(row) for row in c.execute(
                f"""WITH ranked AS (
                           SELECT v.ts_code,{pe_expr} AS pe_ttm,v.trade_date,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY v.ts_code
                                      ORDER BY v.trade_date DESC,{observed_sql} DESC,
                                               {created_sql} DESC,v.id DESC
                                  ) AS row_rank
                             FROM dc_stock_daily_snapshot_version v
                            WHERE {' AND '.join(predicates)}
                       )
                       SELECT ts_code,pe_ttm FROM ranked WHERE row_rank=1""",
                tuple(params),
            ).fetchall()]
    except Exception as exc:
        raise RuntimeError(
            "industry_valuation_pit_unavailable: immutable snapshot version required"
        ) from exc

    def payloads(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = {
                key: row.get(key)
                for key in ("total_revenue", "n_income", "inventories")
                if row.get(key) is not None
            }
            value["_period"] = str(row.get("period") or "")
            result.setdefault(str(row.get("ts_code") or ""), []).append(value)
        return result

    incomes, balances = payloads(income_rows), payloads(balance_rows)
    pe_by_symbol = {str(row.get("ts_code") or ""): float(row.get("pe_ttm") or 0) for row in snapshots}
    if financial_derived is None:
        facts: dict[str, dict[str, list[float]]] = {}
        for symbol, industry in master.items():
            if not industry:
                continue
            bucket = facts.setdefault(industry, {"revenue_growth": [], "profit_growth": [], "inventory_improvement": []})
            inc = incomes.get(symbol) or []
            current_inc = inc[0] if inc else None
            prior_inc = None
            if current_inc and len(str(current_inc.get("_period") or "")) == 8:
                period = str(current_inc["_period"])
                prior_period = str(int(period[:4]) - 1) + period[4:]
                prior_inc = next((item for item in inc[1:] if str(item.get("_period") or "") == prior_period), None)
            if current_inc and prior_inc:
                for field, key in (("total_revenue", "revenue_growth"), ("n_income", "profit_growth")):
                    current, previous = current_inc.get(field), prior_inc.get(field)
                    if current is not None and previous not in (None, 0):
                        bucket[key].append(float(current) / abs(float(previous)) - (1.0 if float(previous) > 0 else 0.0))
            bal = balances.get(symbol) or []
            inc_by_period = {str(item.get("_period") or ""): item for item in inc}
            bal_by_period = {str(item.get("_period") or ""): item for item in bal}
            inventory_period = next((
                period for period in sorted(set(inc_by_period) & set(bal_by_period), reverse=True)
                if len(period) == 8 and (str(int(period[:4]) - 1) + period[4:]) in inc_by_period
                and (str(int(period[:4]) - 1) + period[4:]) in bal_by_period
            ), None)
            if inventory_period:
                prior_inventory_period = str(int(inventory_period[:4]) - 1) + inventory_period[4:]
                inventory_inc, prior_inventory_inc = inc_by_period[inventory_period], inc_by_period[prior_inventory_period]
                current_bal, prior_bal = bal_by_period[inventory_period], bal_by_period[prior_inventory_period]
                current_revenue, previous_revenue = inventory_inc.get("total_revenue"), prior_inventory_inc.get("total_revenue")
                current_inventory, previous_inventory = current_bal.get("inventories"), prior_bal.get("inventories")
                if current_revenue not in (None, 0) and previous_revenue not in (None, 0) and current_inventory is not None and previous_inventory is not None:
                    current_ratio = float(current_inventory) / abs(float(current_revenue))
                    previous_ratio = float(previous_inventory) / abs(float(previous_revenue))
                    bucket["inventory_improvement"].append(previous_ratio - current_ratio)
        financial_derived = {}
        for industry, bucket in facts.items():
            revenue, profit, inventory = bucket["revenue_growth"], bucket["profit_growth"], bucket["inventory_improvement"]
            observations: dict[str, float] = {}
            if len(revenue) >= 3:
                observations["demand_orders_sales"] = sum(1 for value in revenue if value > 0) / len(revenue) * 100
            if len(profit) >= 3:
                observations["profit_diffusion"] = sum(1 for value in profit if value > 0) / len(profit) * 100
            if len(inventory) >= 3:
                observations["inventory_cycle"] = sum(1 for value in inventory if value >= 0) / len(inventory) * 100
            financial_derived[industry] = {
                "observations": observations,
                "evidence_points": len(revenue) + len(profit) + len(inventory),
                "evidence": {
                    "method": "dynamic_factor_v1", "company_revenue_points": len(revenue),
                    "company_profit_points": len(profit), "company_inventory_points": len(inventory),
                },
            }
        if source_signature:
            _store_financial_proxy_cache(
                c, source_signature=source_signature, as_of_time=as_of_time, proxies=financial_derived,
            )

    derived = json.loads(json.dumps(financial_derived))
    pe_by_industry: dict[str, list[float]] = {}
    for symbol, industry in master.items():
        if pe_by_symbol.get(symbol, 0) > 0:
            pe_by_industry.setdefault(industry, []).append(pe_by_symbol[symbol])
    for industry in sorted(set(master.values())):
        item = derived.setdefault(industry, {"observations": {}, "evidence_points": 0, "evidence": {"method": "dynamic_factor_v1"}})
        pe_values = sorted(pe_by_industry.get(industry) or [])
        if len(pe_values) >= 3:
            median_pe = pe_values[len(pe_values) // 2]
            item["observations"]["industry_valuation"] = _clip(100 - max(0.0, median_pe - 8.0) / 52.0 * 80.0)
        item["evidence_points"] = int(item.get("evidence_points") or 0) + len(pe_values)
        item["evidence"]["company_valuation_points"] = len(pe_values)
        item["evidence"]["financial_cache_signature"] = source_signature
    return derived


def _industry_pmi_signal(*, industry_name: str, as_of_time: str | None = None) -> dict[str, float | None]:
    """Get PMI-based demand signal for an industry.

    Manufacturing industries use manufacturing PMI, service industries use service PMI.
    Returns normalized signal (0-100) where 50 = neutral, >50 = expansion.
    """
    cutoff_clause = " AND v.available_time<=?" if as_of_time else ""
    params: tuple[Any, ...] = (str(as_of_time),) if as_of_time else ()

    is_manufacturing = any(x in str(industry_name or "") for x in
        ("制造", "煤炭", "石油", "有色", "钢铁", "化工", "化纤", "半导体", "芯片", "元器件",
         "汽车", "机械", "电气", "仪表", "建材", "造纸", "纺织", "食品", "饮料", "医药"))

    pmi_pattern = "cn_pmi_manufacturing_demand_%" if is_manufacturing else "cn_pmi_service_demand_%"

    try:
        with _read_conn() as c:
            rows = [dict(row) for row in c.execute(
                """SELECT d.metric_code, d.industry_name,
                          v.data_date, v.value
                   FROM dc_industry_metric_definition d
                   JOIN dc_industry_metric_value v ON v.metric_code = d.metric_code
                   WHERE d.metric_code LIKE ?
                     AND d.industry_name = ?
                     """ + cutoff_clause + """
                   ORDER BY v.data_date DESC
                   LIMIT 6""",
                (pmi_pattern, industry_name) + params
            ).fetchall()]
    except Exception:
        return {"pmi_signal": None, "pmi_trend": None, "pmi_points": 0}

    if not rows:
        return {"pmi_signal": None, "pmi_trend": None, "pmi_points": 0}

    values = [float(r.get("value") or 50) for r in rows if r.get("value") is not None]
    if not values:
        return {"pmi_signal": None, "pmi_trend": None, "pmi_points": 0}

    latest = values[0]
    signal = max(0.0, min(100.0, (latest - 30.0) / 40.0 * 100.0))
    trend = (values[0] - values[-1]) if len(values) >= 3 else None
    return {"pmi_signal": round(signal, 2), "pmi_trend": round(trend, 2) if trend is not None else None, "pmi_points": len(values)}


def _industry_dynamic_factor_score(
    company_observations: dict[str, float],
    pmi_signal: dict[str, float | None],
    *,
    prior_score: float | None = None,
    history_points: int = 0,
) -> dict[str, Any]:
    """Enhanced industry scoring using dynamic factor model approach.

    Combines company aggregate proxies with PMI macro data.
    Plan book B4: 需求/订单 25% + 价格 15% + 库存 15% + 产能 10% + 利润扩散 15% + 政策 10% + 估值 10%
    """
    available = {key: _clip(company_observations[key]) for key in INDUSTRY_WEIGHTS if company_observations.get(key) is not None}

    # Incorporate PMI signal into demand_orders_sales if available
    pmi_val = pmi_signal.get("pmi_signal")
    if pmi_val is not None and "demand_orders_sales" not in available:
        available["demand_orders_sales"] = float(pmi_val)
    elif pmi_val is not None and "demand_orders_sales" in available:
        # Blend: 40% PMI + 60% company aggregate
        available["demand_orders_sales"] = 0.4 * float(pmi_val) + 0.6 * available["demand_orders_sales"]

    coverage = sum(INDUSTRY_WEIGHTS[key] for key in available)
    score = sum(available[key] * INDUSTRY_WEIGHTS[key] for key in available) / coverage if coverage else 0.0
    delta = None if prior_score is None else score - float(prior_score)

    if coverage < .25 or history_points < 1:
        state, status = "证据不足", "insufficient_data"
    elif score < 30 and (delta is None or delta <= 0):
        state, status = "衰退", "ready"
    elif score < 45:
        state, status = ("筑底" if delta is not None and delta > 0 else "衰退"), "ready"
    elif score < 60:
        state, status = ("复苏" if delta is not None and delta > 0 else "筑底"), "ready"
    elif score < 75 and (delta is None or delta >= -5):
        state, status = "扩张", "ready"
    elif score >= 75 and (delta is None or delta >= 0):
        state, status = "过热", "ready"
    else:
        state, status = "回落", "ready"

    return {
        "score": round(score, 4), "coverage": round(coverage, 4), "status": status,
        "lifecycle": state, "score_change": None if delta is None else round(delta, 4),
        "components": {key: {"value": round(available[key], 2), "weight": INDUSTRY_WEIGHTS[key]} for key in available},
        "missing": [key for key in INDUSTRY_WEIGHTS if key not in available],
        "evidence_points": history_points + int(pmi_signal.get("pmi_points") or 0),
        "pmi_signal": pmi_signal,
    }


def _normalize_materialized_state(state: dict[str, Any]) -> dict[str, Any]:
    """Repair stale status labels without recomputing the industry universe.

    Older materialization runs could persist ``insufficient_data`` after the
    proxy evidence had already been written into the state components.  The
    component values and evidence count are sufficient to reapply the same
    deterministic scoring contract on the read path.
    """
    if not isinstance(state, dict):
        return {}
    components = state.get("components")
    if not isinstance(components, dict):
        return state
    observations = {
        str(key): value.get("value")
        for key, value in components.items()
        if isinstance(value, dict) and value.get("value") is not None
    }
    if not observations:
        return state
    try:
        history_points = int(state.get("evidence_points") or 0)
    except (TypeError, ValueError):
        history_points = 0
    normalized = score_industry(observations, history_points=history_points)
    if state.get("status") != "insufficient_data" or normalized.get("status") != "ready":
        return state
    return {
        **state,
        "score": normalized.get("score", state.get("score")),
        "coverage": normalized.get("coverage", state.get("coverage")),
        "status": "ready",
        "lifecycle": normalized.get("lifecycle", state.get("lifecycle")),
        "missing": normalized.get("missing", state.get("missing", [])),
    }

def _latest_industry_snapshot(
    as_of_date: str, *, as_of_time: str = "", compact: bool = False,
) -> dict[str, Any] | None:
    """Read only an industry state visible at the requested observation cutoff."""
    snapshot_where = "WHERE as_of_date<=?" if as_of_date else ""
    snapshot_params: tuple[Any, ...] = (as_of_date,) if as_of_date else ()
    cutoff = normalize_explicit_timestamp(as_of_time) if as_of_time else ""
    if as_of_time and not cutoff:
        return None
    with _conn() as c:
        snapshot_columns = (
            "industry_name,as_of_date,model_version,state_json,available_time,created_at,updated_at"
            if compact
            else "*"
        )
        if cutoff:
            # Filter every observation clock before selecting the latest row
            # for an industry.  Ranking first would let a late repair hide an
            # older version that was genuinely visible at the replay cutoff.
            candidates = [dict(row) for row in c.execute(
                f"""SELECT {snapshot_columns}
                      FROM dc_industry_state_snapshot s
                     {snapshot_where}
                     ORDER BY industry_name,as_of_date DESC,available_time DESC""",
                snapshot_params,
            ).fetchall()]
            snapshots = []
            seen_industries: set[str] = set()
            for row in candidates:
                industry = str(row.get("industry_name") or "")
                if industry in seen_industries:
                    continue
                if not all(
                    (value := normalize_explicit_timestamp(row.get(column)))
                    and value <= cutoff
                    for column in ("available_time", "created_at", "updated_at")
                ):
                    continue
                seen_industries.add(industry)
                snapshots.append(row)
        else:
            snapshots = [dict(row) for row in c.execute(
                f"""SELECT {snapshot_columns} FROM (
                       SELECT s.*,ROW_NUMBER() OVER(
                           PARTITION BY industry_name ORDER BY as_of_date DESC,available_time DESC
                       ) rn
                       FROM dc_industry_state_snapshot s """ + snapshot_where + """
                   ) ranked WHERE rn=1""",
                snapshot_params,
            ).fetchall()]
        if not snapshots:
            return None
        definitions = [] if compact else [dict(row) for row in c.execute(
            """SELECT industry_code,industry_name,metric_code,metric_name,unit,frequency,source_code,description
               FROM dc_industry_metric_definition
              WHERE NOT (source_code='exchange.futures_proxy' AND description NOT LIKE ?)
              ORDER BY industry_name,metric_name""",
            ("%transmission_direction=%",),
        ).fetchall()]
        if compact:
            company_counts = {
                str(dict(row).get("industry") or ""): int(dict(row).get("company_count") or 0)
                for row in c.execute(
                    """SELECT industry,COUNT(*) company_count FROM dc_stock_master
                       WHERE COALESCE(industry,'')<>'' GROUP BY industry"""
                ).fetchall()
            }
            companies = []
        else:
            company_counts = {}
            companies = [dict(row) for row in c.execute(
                """SELECT industry,ts_code,name FROM dc_stock_master
                   WHERE COALESCE(industry,'')<>'' ORDER BY industry,ts_code"""
            ).fetchall()]
    definitions_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in definitions:
        definitions_by_name.setdefault(str(item.get("industry_name") or ""), []).append(item)
    companies_by_name: dict[str, list[dict[str, str]]] = {}
    for item in companies:
        name = str(item.get("industry") or "").strip()
        if name:
            companies_by_name.setdefault(name, []).append({
                "ts_code": str(item.get("ts_code") or ""),
                "name": str(item.get("name") or ""),
            })
    items: list[dict[str, Any]] = []
    for row in snapshots:
        industry_name = str(row.get("industry_name") or "").strip()
        if not industry_name or industry_name.lower() == "nan":
            continue
        try:
            state = json.loads(row.get("state_json") or "{}")
            evidence = {} if compact else json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            state, evidence = {}, {}
        state = _normalize_materialized_state(state)
        metrics = definitions_by_name.get(industry_name, [])
        members = companies_by_name.get(industry_name, [])
        items.append({
            "industry_code": f"snapshot:{industry_name}",
            "industry_name": industry_name,
            "metrics": metrics,
            "total_evidence_points": int(state.get("evidence_points") or 0),
            "state": state,
            "evidence": evidence,
            "state_as_of_date": row.get("as_of_date"),
            "state_model_version": row.get("model_version"),
            "company_count": company_counts.get(industry_name, len(members)),
            "companies": members[:20],
            "limitations": [],
        })
    return {"ok": True, "items": items, "count": len(items), "source": "materialized_industry_state_snapshot"}


@lru_cache(maxsize=64)
def _list_industries_cached(as_of_time: str, compact: bool = False) -> dict[str, Any]:
    as_of_date = str(as_of_time or "")[:10].replace("-", "")
    snapshot_result = _latest_industry_snapshot(as_of_date, as_of_time=as_of_time, compact=compact)
    if snapshot_result:
        return snapshot_result
    # An explicit cutoff is a formal/replay read. Falling back to mutable
    # metric projections here would admit values written after that cutoff.
    if as_of_time:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "no_industry_state_visible_at_cutoff",
            "as_of_time": as_of_time,
            "items": [],
            "count": 0,
        }
    with _conn() as c:
        definitions = [dict(row) for row in c.execute(
            """SELECT d.*,COUNT(v.metric_code) value_count,MIN(v.data_date) first_date,MAX(v.data_date) last_date
               FROM dc_industry_metric_definition d LEFT JOIN dc_industry_metric_value v ON v.metric_code=d.metric_code
              WHERE NOT (d.source_code='exchange.futures_proxy' AND d.description NOT LIKE ?)
               GROUP BY d.metric_code,d.industry_code,d.industry_name,d.metric_name,d.unit,d.frequency,d.source_code,d.description,d.created_at,d.updated_at
               ORDER BY d.industry_name,d.metric_name""",
            ("%transmission_direction=%",),
        ).fetchall()]
        value_where = "WHERE v.data_date<=?" if as_of_date else ""
        value_params: tuple[Any, ...] = (as_of_date,) if as_of_date else ()
        values = [dict(row) for row in c.execute(
            """SELECT * FROM (
                   SELECT v.*, ROW_NUMBER() OVER (
                       PARTITION BY v.metric_code ORDER BY v.data_date DESC
                   ) AS row_num
                   FROM dc_industry_metric_value v """ + value_where + """
               ) ranked WHERE row_num <= 60
               ORDER BY metric_code,data_date DESC""", value_params
        ).fetchall()]
        company_rows = [dict(row) for row in c.execute(
            """SELECT industry,ts_code,name FROM dc_stock_master
               WHERE COALESCE(industry,'')<>'' ORDER BY industry,ts_code"""
        ).fetchall()]
        snapshot_where = "WHERE as_of_date<=?" if as_of_date else ""
        snapshot_params: tuple[Any, ...] = (as_of_date,) if as_of_date else ()
        snapshot_rows = [dict(row) for row in c.execute(
            """SELECT * FROM (
                   SELECT s.*,ROW_NUMBER() OVER(PARTITION BY industry_name ORDER BY as_of_date DESC) rn
                   FROM dc_industry_state_snapshot s """ + snapshot_where + """
               ) ranked WHERE rn=1""", snapshot_params
        ).fetchall()]
        materialized: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            try:
                state = json.loads(row.get("state_json") or "{}")
                evidence = json.loads(row.get("evidence_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                state, evidence = {}, {}
            materialized[str(row.get("industry_name") or "")] = {"state": state, "evidence": evidence, "as_of_date": row.get("as_of_date"), "model_version": row.get("model_version")}
    values_by_metric: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        value.pop("row_num", None)
        values_by_metric.setdefault(str(value.get("metric_code") or ""), []).append(value)
    grouped: dict[str, dict[str, Any]] = {}
    for item in definitions:
        key = str(item.get("industry_code") or item.get("industry_name") or "unknown")
        group = grouped.setdefault(key, {"industry_code": key, "industry_name": item.get("industry_name"), "metrics": [], "total_evidence_points": 0})
        group["metrics"].append(item)
        group["total_evidence_points"] += int(item.get("value_count") or 0)
    companies_by_industry: dict[str, list[dict[str, str]]] = {}
    for company in company_rows:
        industry = str(company.get("industry") or "").strip()
        if industry:
            companies_by_industry.setdefault(industry, []).append({
                "ts_code": str(company.get("ts_code") or ""),
                "name": str(company.get("name") or ""),
            })
    known_names = {str(item.get("industry_name") or "").strip() for item in grouped.values()}
    for industry_name in companies_by_industry:
        if industry_name not in known_names:
            key = f"master:{industry_name}"
            grouped[key] = {"industry_code": key, "industry_name": industry_name, "metrics": [], "total_evidence_points": 0}
    for group in grouped.values():
        series = [
            {"definition": definition, "values": values_by_metric.get(str(definition.get("metric_code") or ""), [])}
            for definition in group["metrics"]
        ]
        # Try PMI-enhanced scoring first
        industry_name = str(group.get("industry_name") or "").strip()
        pmi = _industry_pmi_signal(industry_name=industry_name, as_of_time=as_of_time or None)
        state, evidence = _industry_state_from_series(series)
        if state.get("status") == "insufficient_data" and pmi.get("pmi_signal") is not None:
            # Use PMI-enhanced scoring as fallback
            observations = {}
            for s in series:
                component = _metric_component(s["definition"])
                if component and s["values"]:
                    signal = _metric_signal(s["definition"], s["values"])
                    if signal is not None:
                        observations[component] = signal
            if observations or pmi.get("pmi_signal") is not None:
                state = _industry_dynamic_factor_score(observations, pmi, history_points=len(s["values"]) if series else 0)
                evidence = {"source": "pmi_enhanced_series_v1"}
        snapshot = materialized.get(industry_name) or {}
        if state.get("status") != "ready" and snapshot.get("state"):
            state = snapshot["state"]
            evidence = {**evidence, "company_aggregate_proxy": snapshot.get("evidence")}
            group["state_as_of_date"] = snapshot.get("as_of_date")
            group["state_model_version"] = snapshot.get("model_version")
        unmapped = [x["definition"].get("metric_name") for x in series if not _metric_component(x["definition"])]
        group["state"] = state
        group["evidence"] = evidence
        members = companies_by_industry.get(industry_name, [])
        group["company_count"] = len(members)
        group["companies"] = members[:20]
        group["limitations"] = ([f"{len(unmapped)}个指标未映射，只展示事实"] if unmapped else []) + (["产品价格、产能利用率和政策供给仍需行业专属数据；当前状态使用公司财务与估值聚合代理"] if snapshot and not series else [])
    return {"ok": True, "items": list(grouped.values()), "count": len(grouped)}


def list_industries(*, as_of_time: str | None = None, compact: bool = False) -> dict[str, Any]:
    # Current projections can be updated by another worker/process. An empty
    # cutoff must never become a permanent LRU entry on the console worker.
    reader = _list_industries_cached if as_of_time else _list_industries_cached.__wrapped__
    result = reader(str(as_of_time or ""), compact)
    if not compact:
        return result
    fields = {
        "industry_code", "industry_name", "state", "state_as_of_date",
        "state_model_version", "company_count", "limitations",
    }
    return {
        **{key: value for key, value in result.items() if key != "items"},
        "items": [{key: row.get(key) for key in fields if key in row} for row in result.get("items", [])],
    }


def clear_industry_cache() -> None:
    _list_industries_cached.cache_clear()


def _is_statement_timeout(exc: BaseException) -> bool:
    detail = f"{type(exc).__name__}: {exc}".lower()
    return "statement timeout" in detail or "querycanceled" in detail


def _materialize_industry_state_snapshots_once(*, as_of_time: str | None = None) -> dict[str, Any]:
    cutoff = str(as_of_time or "")
    # Keep the expensive immutable financial/snapshot reads separate from the
    # materialization transaction.  The previous single scope stayed idle in
    # transaction while Python decoded rows and while one PMI query per
    # industry opened additional connections.
    with _conn() as c:
        if cutoff:
            as_of_date = cutoff[:10].replace("-", "")
        else:
            # The current projection is mutable; use the immutable observation
            # archive even for the latest materialization anchor.
            try:
                is_postgres = bool(getattr(c, "_is_postgres", False))
                row = c.execute(
                    """SELECT MAX(trade_date) latest
                         FROM dc_stock_daily_snapshot_version v
                        WHERE v.content_hash<>''
                          AND NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL
                          AND """ + _valid_provenance_sql("v.observation_provenance") + """
                          AND """ + _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres) + """
                          AND """ + _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres) + """
                          AND """ + _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres) + """
                          AND """ + _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
                ).fetchone()
            except Exception as exc:
                raise RuntimeError(
                    "industry_state_anchor_pit_unavailable: immutable snapshot version required"
                ) from exc
            as_of_date = str(dict(row).get("latest") or "") if row else ""
        if not as_of_date:
            raise RuntimeError("industry_state_anchor_missing")
        proxies = _derived_industry_proxies(c, as_of_time=cutoff or None)
    pmi_by_industry = {
        industry_name: _industry_pmi_signal(
            industry_name=industry_name,
            as_of_time=cutoff or None,
        )
        for industry_name in proxies
    }
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        written = 0
        ready = 0
        for industry_name, proxy in proxies.items():
            observations = proxy.get("observations") or {}
            pmi = pmi_by_industry[industry_name]
            state = _industry_dynamic_factor_score(
                observations, pmi,
                history_points=int(proxy.get("evidence_points") or 0),
            )
            if state.get("status") == "ready":
                ready += 1
            c.execute(
                """INSERT INTO dc_industry_state_snapshot
                   (as_of_date,industry_name,state_json,evidence_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,industry_name) DO UPDATE SET
                   state_json=excluded.state_json,evidence_json=excluded.evidence_json,
                   model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (as_of_date, industry_name, json.dumps(state, ensure_ascii=False),
                 json.dumps(proxy.get("evidence") or {}, ensure_ascii=False),
                 "dynamic_factor_v1", cutoff or now, now, now),
            )
            written += 1
    clear_industry_cache()
    return {"ok": True, "as_of_date": as_of_date, "written": written, "ready": ready, "model_version": "dynamic_factor_v1"}


def materialize_industry_state_snapshots(*, as_of_time: str | None = None) -> dict[str, Any]:
    """Persist company-aggregate industry states for cheap point-in-time reads.

    A statement timeout aborts the PostgreSQL transaction, so a retry must use
    a fresh connection.  One bounded retry absorbs short index-build or bulk
    ingest contention without hiding deterministic query failures or extending
    the autonomous step beyond its 120-second budget.
    """
    for attempt in range(2):
        try:
            result = _materialize_industry_state_snapshots_once(as_of_time=as_of_time)
            return {**result, "query_attempts": attempt + 1}
        except Exception as exc:
            if attempt or not _is_statement_timeout(exc):
                raise
            time.sleep(0.25)
    raise RuntimeError("industry snapshot retry exhausted")


def _history_anchor_rows(rows: list[dict[str, Any]], *, anchor_sessions: int, max_points: int) -> list[dict[str, Any]]:
    """Keep chronological, point-in-time-safe industry history anchors."""
    stride = max(1, int(anchor_sessions))
    capped = max(1, min(int(max_points), 120))
    sampled = rows[::stride]
    if rows and (not sampled or sampled[-1].get("trade_date") != rows[-1].get("trade_date")):
        sampled.append(rows[-1])
    return sampled[-capped:]


def materialize_industry_state_history(*, anchor_sessions: int = 20, max_points: int = 36) -> dict[str, Any]:
    """Backfill bounded industry-state history from existing PIT snapshots.

    No API request is issued here. Every anchor passes the source snapshot's
    own ``available_time`` into the state builder, so later financial records
    cannot leak into a historical industry observation.
    """
    with _conn() as c:
        try:
            is_postgres = bool(getattr(c, "_is_postgres", False))
            rows = [dict(row) for row in c.execute(
                """SELECT trade_date
                     FROM dc_stock_daily_snapshot_version v
                    WHERE v.content_hash<>''
                      AND NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL
                      AND """ + _valid_provenance_sql("v.observation_provenance") + """
                      AND """ + _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres) + """
                      AND """ + _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres) + """
                      AND """ + _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres) + """
                      AND """ + _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres) + """
                    GROUP BY trade_date ORDER BY trade_date"""
            ).fetchall()]
        except Exception as exc:
            raise RuntimeError(
                "industry_state_history_pit_unavailable: immutable snapshot version required"
            ) from exc
    for row in rows:
        trade_date = str(row.get("trade_date") or "").replace("-", "")[:8]
        row["available_time"] = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:00:00+08:00"
            if len(trade_date) == 8 and trade_date.isdigit() else ""
        )
    anchors = _history_anchor_rows(rows, anchor_sessions=anchor_sessions, max_points=max_points)
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for anchor in anchors:
        try:
            result = materialize_industry_state_snapshots(as_of_time=str(anchor.get("available_time") or ""))
            results.append({"trade_date": anchor.get("trade_date"), "available_time": anchor.get("available_time"), "written": result.get("written"), "ready": result.get("ready")})
        except Exception as exc:  # noqa: BLE001
            failed.append({"trade_date": anchor.get("trade_date"), "error": str(exc)[:300]})
    return {
        "ok": not failed,
        "anchors": len(anchors),
        "materialized": results,
        "failed": failed,
        "contract": "monthly_anchor_point_in_time_industry_history_v1",
    }


def industry_detail(industry_code: str) -> dict[str, Any]:
    with _conn() as c:
        definitions = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_definition WHERE industry_code=? ORDER BY metric_name", (industry_code,)).fetchall()]
        series: list[dict[str, Any]] = []
        for definition in definitions:
            values = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_value WHERE metric_code=? ORDER BY data_date DESC LIMIT 60", (definition["metric_code"],)).fetchall()]
            series.append({"definition": definition, "values": values})
    state, evidence = _industry_state_from_series(series)
    unmapped = [x["definition"].get("metric_name") for x in series if not _metric_component(x["definition"])]
    return {"ok": bool(definitions), "industry_code": industry_code, "series": series, "state": state, "evidence": evidence, "limitations": [f"{len(unmapped)}个指标未映射，只展示事实"] if unmapped else []}


def _days_since(value: str, as_of_date: str | None = None) -> int:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text).date()
    except ValueError:
        try:
            parsed = datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return 0
    reference = datetime.now(_BUSINESS_TZ).date()
    if as_of_date:
        text = str(as_of_date)[:10].replace("-", "")
        try:
            reference = datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            pass
    return max(0, (reference - parsed).days)


def _followup_date(event_date: Any, publish_time: Any, half_life_days: int) -> tuple[str, bool]:
    """Return a durable follow-up date, never an empty scheduling field.

    Some structured source records (notably name-change history) do not carry
    a canonical announcement date.  They must be surfaced for immediate
    review instead of silently creating an unscheduled event follow-up.
    """
    for candidate in (event_date, publish_time):
        text = str(candidate or "").strip()
        for fmt, width in (("%Y%m%d", 8), ("%Y-%m-%d", 10)):
            try:
                base = datetime.strptime(text[:width], fmt).date()
                return (base + timedelta(days=max(7, int(half_life_days) // 2))).isoformat(), False
            except ValueError:
                continue
        try:
            base = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            return (base + timedelta(days=max(7, int(half_life_days) // 2))).isoformat(), False
        except ValueError:
            continue
    return datetime.now(_BUSINESS_TZ).date().isoformat(), True


def score_event(item: dict[str, Any], *, as_of_date: str | None = None) -> dict[str, Any]:
    severity = {"critical": 100, "high": 80, "medium": 55, "low": 30}.get(str(item.get("severity")), 50)
    direction_name = str(item.get("impact_direction") or "neutral")
    direction = {"positive": 100, "negative": 100, "neutral": 30}.get(direction_name, 30)
    confidence = _clip(float(item.get("confidence") or 0) * 100)
    source = 95 if str(item.get("source_code")) in {"cninfo", "sse", "szse"} else 70
    event_type = str(item.get("event_type") or "")
    half_life = EVENT_HALF_LIFE_DAYS.get(event_type, 45)
    duration = _clip(40 + min(60, half_life / 3))
    components = {
        "source_reliability": source,
        "quantified_materiality": severity,
        "direction": direction,
        "duration": duration,
        "thesis_impact": severity,
        "novelty": 50,
    }
    initial = sum(components[key] * EVENT_WEIGHTS[key] for key in EVENT_WEIGHTS)
    age_days = _days_since(str(item.get("event_date") or item.get("publish_time") or ""), as_of_date)
    decayed = initial * math.exp(-math.log(2) * age_days / half_life)
    return {
        "score": round(initial, 4), "decayed_score": round(decayed, 4), "direction": direction_name,
        "age_days": age_days, "half_life_days": half_life,
        "components": {key: {"value": components[key], "weight": EVENT_WEIGHTS[key]} for key in EVENT_WEIGHTS},
    }


def _structured_raw_events_from_versions(
    c,
    *,
    ts_code: str,
    limit: int,
    as_of_time: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read structured disclosure events from immutable PIT raw versions.

    The ordinary raw table is an overwrite projection.  It is suitable for a
    current dashboard, but it cannot prove what was visible at a historical
    cutoff.  ``load_raw_record_versions`` returns ``None`` when the immutable
    schema is absent and an empty list when the schema is present but no
    eligible record exists; keeping those outcomes distinct prevents a data
    outage from becoming a harmless-looking empty event list.
    """
    cutoff = normalize_explicit_timestamp(as_of_time)
    if not cutoff:
        return [], {
            "ok": False,
            "status": "blocked",
            "reason": "event_pit_cutoff_invalid",
            "as_of_time": as_of_time,
        }
    apis = ("forecast", "express", "dividend", "pledge_stat", "fina_audit", "namechange")
    per_api_limit = max(5, min(100, math.ceil(limit / len(apis)) * 4))
    rows: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for api_name in apis:
        versions = load_raw_record_versions(
            c,
            api_name,
            cutoff=cutoff,
            ts_codes=[ts_code] if ts_code else None,
            limit=per_api_limit,
        )
        if versions is None:
            unavailable.append(api_name)
            continue
        rows.extend(dict(row) for row in versions)
    if unavailable:
        return [], {
            "ok": False,
            "status": "blocked",
            "reason": "immutable_event_schema_unavailable",
            "missing_apis": unavailable,
            "as_of_time": cutoff,
        }

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        api = str(row.get("api_name") or "")
        event_type, direction, severity, title = api, "neutral", "medium", api
        metrics: dict[str, Any] = {}
        if api == "forecast":
            event_type, title = "earnings_forecast", "业绩预告"
            changes = [float(payload[key]) for key in ("p_change_min", "p_change_max") if payload.get(key) is not None]
            change = sum(changes) / len(changes) if changes else None
            direction = "positive" if change is not None and change > 10 else "negative" if change is not None and change < -10 else "neutral"
            severity = "high" if change is not None and abs(change) >= 50 else "medium"
            metrics = {"profit_change_pct": change, "range": changes, "summary": payload.get("summary")}
        elif api == "express":
            event_type, title = "earnings_flash", "业绩快报"
            income = payload.get("n_income")
            direction = "positive" if income is not None and float(income) > 0 else "negative" if income is not None and float(income) < 0 else "neutral"
            metrics = {key: payload.get(key) for key in ("revenue", "total_profit", "n_income", "total_assets")}
        elif api == "dividend":
            event_type, title, direction = "dividend", "分红方案", "positive"
            severity = "low"
            metrics = {key: payload.get(key) for key in ("cash_div_tax", "stk_div", "div_proc", "ex_date")}
        elif api == "pledge_stat":
            event_type, title = "pledge", "股权质押统计"
            try:
                ratio = float(payload.get("pledge_ratio") or 0)
            except (TypeError, ValueError):
                ratio = 0.0
            direction = "negative" if ratio >= 30 else "neutral"
            severity = "high" if ratio >= 50 else "medium" if ratio >= 30 else "low"
            metrics = {"pledge_ratio": ratio, "pledge_count": payload.get("pledge_count")}
        elif api == "fina_audit":
            event_type, title = "audit_opinion", "审计意见"
            audit_result = str(payload.get("audit_result") or "")
            standard = "标准无保留意见" in audit_result or audit_result == "无保留意见"
            direction, severity = ("neutral", "low") if standard else ("negative", "critical")
            metrics = {"audit_result": audit_result, "audit_agency": payload.get("audit_agency")}
        elif api == "namechange":
            event_type, title = "name_change", "名称或ST状态变更"
            is_st = "ST" in str(payload.get("name") or "").upper()
            direction, severity = ("negative", "high") if is_st else ("neutral", "low")
            metrics = {key: payload.get(key) for key in ("name", "start_date", "end_date", "change_reason")}
        event_date = str(row.get("ann_date") or payload.get("ann_date") or payload.get("end_date") or row.get("period") or "")
        identity = (api, str(row.get("ts_code") or ""), event_date, event_type)
        if identity in seen:
            continue
        seen.add(identity)
        observed_at = str(row.get("observed_at") or "")
        result.append({
            "id": f"raw:{api}:{row.get('id')}", "announcement_id": None,
            "event_type": event_type, "impact_direction": direction, "severity": severity,
            "confidence": .98, "event_date": event_date,
            "entities": [row.get("ts_code")], "metrics": metrics, "summary": title,
            "ts_code": row.get("ts_code"), "title": title, "publish_time": observed_at,
            "url": "", "source_code": str(row.get("source_code") or "tushare"),
            "available_time": observed_at,
            "evidence": {
                "api_name": api, "record_id": row.get("id"),
                "available_time": observed_at,
                "observation_provenance": row.get("observation_provenance"),
                "content_hash": row.get("content_hash"),
            },
        })
    return result, {"ok": True, "status": "ready", "as_of_time": cutoff}


def _structured_raw_events(c, *, ts_code: str, limit: int, as_of_time: str | None) -> list[dict[str, Any]]:
    """Build structured events, using immutable versions for explicit PIT reads."""
    if as_of_time:
        result, _status = _structured_raw_events_from_versions(
            c, ts_code=ts_code, limit=limit, as_of_time=as_of_time,
        )
        return result
    apis = ("forecast", "express", "dividend", "pledge_stat", "fina_audit", "namechange")
    per_api_limit = max(5, min(100, math.ceil(limit / len(apis)) * 4))
    rows: list[dict[str, Any]] = []
    for api_name in apis:
        clauses = ["api_name=?"]
        params: list[Any] = [api_name]
        if ts_code:
            clauses.append("ts_code=?"); params.append(ts_code)
        if as_of_time:
            clauses.append("available_time<=?"); params.append(str(as_of_time))
        rows.extend(dict(row) for row in c.execute(
            f"""SELECT id,api_name,ts_code,period,ann_date,available_time,payload_json
                FROM dc_tushare_raw_records WHERE {' AND '.join(clauses)}
                ORDER BY available_time DESC,id DESC LIMIT ?""",
            tuple(params + [per_api_limit]),
        ).fetchall())
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        api = str(row.get("api_name") or "")
        event_type, direction, severity, title = api, "neutral", "medium", api
        metrics: dict[str, Any] = {}
        if api == "forecast":
            event_type, title = "earnings_forecast", "业绩预告"
            changes = [float(payload[key]) for key in ("p_change_min", "p_change_max") if payload.get(key) is not None]
            change = sum(changes) / len(changes) if changes else None
            direction = "positive" if change is not None and change > 10 else "negative" if change is not None and change < -10 else "neutral"
            severity = "high" if change is not None and abs(change) >= 50 else "medium"
            metrics = {"profit_change_pct": change, "range": changes, "summary": payload.get("summary")}
        elif api == "express":
            event_type, title = "earnings_flash", "业绩快报"
            income = payload.get("n_income")
            direction = "positive" if income is not None and float(income) > 0 else "negative" if income is not None and float(income) < 0 else "neutral"
            metrics = {key: payload.get(key) for key in ("revenue", "total_profit", "n_income", "total_assets")}
        elif api == "dividend":
            event_type, title, direction = "dividend", "分红方案", "positive"
            severity = "low"
            metrics = {key: payload.get(key) for key in ("cash_div_tax", "stk_div", "div_proc", "ex_date")}
        elif api == "pledge_stat":
            event_type, title = "pledge", "股权质押统计"
            ratio = float(payload.get("pledge_ratio") or 0)
            direction = "negative" if ratio >= 30 else "neutral"
            severity = "high" if ratio >= 50 else "medium" if ratio >= 30 else "low"
            metrics = {"pledge_ratio": ratio, "pledge_count": payload.get("pledge_count")}
        elif api == "fina_audit":
            event_type, title = "audit_opinion", "审计意见"
            audit_result = str(payload.get("audit_result") or "")
            standard = "标准无保留意见" in audit_result or audit_result == "无保留意见"
            direction, severity = ("neutral", "low") if standard else ("negative", "critical")
            metrics = {"audit_result": audit_result, "audit_agency": payload.get("audit_agency")}
        elif api == "namechange":
            event_type, title = "name_change", "名称或ST状态变更"
            is_st = "ST" in str(payload.get("name") or "").upper()
            direction, severity = ("negative", "high") if is_st else ("neutral", "low")
            metrics = {key: payload.get(key) for key in ("name", "start_date", "end_date", "change_reason")}
        event_date = str(row.get("ann_date") or payload.get("ann_date") or payload.get("end_date") or row.get("period") or "")
        identity = (api, str(row.get("ts_code") or ""), event_date, event_type)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "id": f"raw:{api}:{row.get('id')}", "announcement_id": None,
            "event_type": event_type, "impact_direction": direction, "severity": severity,
            "confidence": .98, "event_date": event_date,
            "entities": [row.get("ts_code")], "metrics": metrics, "summary": title,
            "ts_code": row.get("ts_code"), "title": title, "publish_time": row.get("available_time"),
            "url": "", "source_code": "tushare", "available_time": row.get("available_time"),
            "evidence": {"api_name": api, "record_id": row.get("id"), "available_time": row.get("available_time")},
        })
    return result


def list_events(*, ts_code: str = "", limit: int = 100, as_of_time: str | None = None) -> dict[str, Any]:
    if as_of_time:
        cutoff = normalize_explicit_timestamp(as_of_time)
        if not cutoff:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "event_pit_cutoff_invalid",
                "as_of_time": as_of_time,
                "items": [],
                "count": 0,
            }
        # ``dc_announcement_raw`` is mutable and has no immutable four-clock
        # version contract.  Do not query it for a formal/replay cutoff.
        with _conn() as c:
            rows, event_status = _structured_raw_events_from_versions(
                c, ts_code=ts_code, limit=limit, as_of_time=cutoff,
            )
        if not event_status.get("ok"):
            return {
                **event_status,
                "items": [],
                "count": 0,
                "event_contract": {
                    "supported_types": sorted(V1_EVENT_TYPES),
                    "observed_types": [],
                    "unobserved_types": sorted(V1_EVENT_TYPES),
                    "schema_fields": [
                        "event_type", "direction", "severity", "confidence",
                        "impact_horizon", "impact_dimensions", "evidence", "followup_date",
                    ],
                },
            }
    else:
        rows = []
    clauses, params = [], []
    if ts_code:
        clauses.append("r.ts_code=?"); params.append(ts_code)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if not as_of_time:
        with _conn() as c:
            rows.extend([dict(row) for row in c.execute(
                f"""SELECT e.*,r.ts_code,r.title,r.publish_time,r.url,r.source_code,r.available_time,r.raw_text
                    FROM dc_announcement_event e JOIN dc_announcement_raw r ON r.id=e.announcement_id
                    {where} ORDER BY COALESCE(NULLIF(e.event_date,''),r.publish_time) DESC,e.id DESC LIMIT ?""",
                tuple(params + [limit]),
            ).fetchall()])
            rows.extend(_structured_raw_events(c, ts_code=ts_code, limit=limit, as_of_time=None))
    # Thesis rows are a mutable research projection.  They are useful for a
    # live follow-up queue, but do not have a versioned PIT contract yet, so a
    # historical event read must not attach today's thesis state as if it were
    # known at the cutoff.
    if as_of_time:
        thesis_by_code = {}
    else:
        try:
            from app.services.thesis_store import list_theses
            thesis_by_code = {
                str(x.get("ts_code") or ""): x
                for x in list_theses(limit=500, include_hypotheses=False)
            }
        except Exception:
            thesis_by_code = {}
    for item in rows:
        if "entities_json" in item:
            item["entities"] = json.loads(item.pop("entities_json") or "[]")
            item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
            item["evidence"] = {"title": item.get("title"), "url": item.get("url"), "available_time": item.get("available_time"), "excerpt": str(item.pop("raw_text", "") or "")[:240]}
        item["impact"] = score_event(item, as_of_date=str(as_of_time or "")[:10] or None)
        item["direction"] = item.get("impact_direction") or "neutral"
        half_life = int(item["impact"].get("half_life_days") or 45)
        item["impact_horizon"] = "long" if half_life >= 120 else "medium" if half_life >= 60 else "short"
        event_type = str(item.get("event_type") or "")
        item["impact_dimensions"] = {
            "earnings_forecast": ["expectation", "growth"], "earnings_flash": ["expectation", "quality"],
            "earnings": ["quality", "growth", "cashflow"], "dividend": ["cashflow", "shareholder_return"],
            "pledge": ["governance", "risk"], "audit_opinion": ["governance", "risk"],
            "regulatory_risk": ["governance", "risk"], "investigation": ["governance", "risk"],
            "major_contract": ["expectation", "growth"], "ma_restructuring": ["governance", "valuation"],
            "private_placement": ["valuation", "governance"], "shareholder_reduction": ["governance", "sentiment"],
        }.get(event_type, ["research"])
        followup_date, needs_date_review = _followup_date(item.get("event_date"), item.get("publish_time") or item.get("available_time"), half_life)
        item["followup_date"] = followup_date
        thesis = thesis_by_code.get(str(item.get("ts_code") or ""))
        decayed = float(item["impact"].get("decayed_score") or 0)
        item["thesis_link"] = {"thesis_id": thesis.get("id"), "title": thesis.get("title"), "status": thesis.get("derived_status")} if thesis else None
        item["follow_up"] = {
            "status": "in_review" if needs_date_review else "urgent" if item.get("impact_direction") == "negative" and decayed >= 60 else "monitor" if decayed >= 25 else "expired",
            "next_action": "补齐事件日期并立即复核来源证据" if needs_date_review else "立即复核投资逻辑必要条件" if item.get("impact_direction") == "negative" and decayed >= 60 else "下个披露节点复核" if decayed >= 25 else "保留历史证据",
            "requires_thesis_review": bool(thesis and item.get("impact_direction") == "negative" and decayed >= 40),
        }
    rows.sort(key=lambda item: (str(item.get("event_date") or item.get("publish_time") or ""), str(item.get("id") or "")), reverse=True)
    rows = rows[:limit]
    observed = sorted({str(item.get("event_type") or "") for item in rows if item.get("event_type")})
    return {
        "ok": True,
        "status": "ready",
        "items": rows,
        "count": len(rows),
        **({"as_of_time": normalize_explicit_timestamp(as_of_time), "point_in_time_method": "immutable_raw_record_versions_v1"} if as_of_time else {}),
        "event_contract": {
            "supported_types": sorted(V1_EVENT_TYPES), "observed_types": observed,
            "unobserved_types": sorted(V1_EVENT_TYPES - set(observed)),
            "schema_fields": ["event_type", "direction", "severity", "confidence", "impact_horizon", "impact_dimensions", "evidence", "followup_date"],
        },
    }


def review_linked_event_theses(*, limit: int = 100) -> dict[str, Any]:
    """Recheck only theses hit by actionable negative announcement events.

    Announcement ingestion remains evidence-only.  This explicit action is the
    controlled bridge into investment logic: it records the existing
    point-in-time research review without treating a keyword classifier as an
    automatic thesis invalidation.
    """
    events = list_events(limit=limit).get("items", [])
    candidates: dict[int, dict[str, Any]] = {}
    for event in events:
        link = event.get("thesis_link") or {}
        thesis_id = link.get("thesis_id") if isinstance(link, dict) else None
        if thesis_id and bool((event.get("follow_up") or {}).get("requires_thesis_review")):
            candidates[int(thesis_id)] = event
    from app.services.thesis_store import review_thesis

    reviewed, failed = [], []
    for thesis_id, event in candidates.items():
        try:
            thesis = review_thesis(thesis_id)
            if thesis:
                reviewed.append({
                    "thesis_id": thesis_id,
                    "ts_code": thesis.get("ts_code"),
                    "event_id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "status": thesis.get("derived_status"),
                    "confidence": thesis.get("derived_confidence"),
                })
        except Exception as exc:  # noqa: BLE001
            failed.append({"thesis_id": thesis_id, "event_id": event.get("id"), "error": str(exc)[:200]})
    return {"ok": not failed, "candidates": len(candidates), "reviewed": reviewed, "failed": failed}


def _followup_with_thesis_review(follow: dict[str, Any], review: dict[str, Any] | None, event_evidence: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
    """Attach automated thesis-review evidence without auto-resolving risk."""
    evidence = {**dict(event_evidence or {}), **dict(follow.get("evidence") or {})}
    if not review:
        return str(follow.get("status") or "monitor"), str(follow.get("next_action") or "复核新证据"), evidence
    evidence["automated_thesis_review"] = {
        "thesis_id": review.get("thesis_id"), "status": review.get("status"),
        "confidence": review.get("confidence"), "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    # A review means the automatic bridge ran; only a human can resolve or
    # dismiss the event. This makes that distinction visible in the task list.
    return "in_review", "已自动复核投资逻辑，等待人工确认事件处置", evidence


def materialize_event_followups(*, limit: int = 500, thesis_reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = list_events(limit=limit).get("items") or []
    now = datetime.now().isoformat(timespec="seconds")
    reviews_by_event = {str(item.get("event_id")): item for item in (thesis_reviews or []) if item.get("event_id") is not None}
    written = 0
    with _conn() as c:
        for event in events:
            follow = event.get("follow_up") or {}
            link = event.get("thesis_link") or {}
            status, next_action, evidence = _followup_with_thesis_review(
                follow, reviews_by_event.get(str(event.get("id") or "")), event.get("evidence") or {},
            )
            c.execute(
                """INSERT INTO dc_event_followup
                   (event_key,ts_code,event_type,status,next_followup_date,next_action,thesis_id,evidence_json,intervention_actor,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET
                   ts_code=excluded.ts_code,event_type=excluded.event_type,
                   next_followup_date=CASE WHEN dc_event_followup.status IN ('resolved','dismissed') THEN dc_event_followup.next_followup_date ELSE excluded.next_followup_date END,
                   next_action=CASE WHEN dc_event_followup.status IN ('resolved','dismissed') THEN dc_event_followup.next_action ELSE excluded.next_action END,
                   thesis_id=COALESCE(excluded.thesis_id,dc_event_followup.thesis_id),
                   evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (str(event.get("id") or ""), str(event.get("ts_code") or ""), str(event.get("event_type") or ""),
                 status, event.get("followup_date"), next_action,
                 link.get("thesis_id"), json.dumps(evidence, ensure_ascii=False), "", now, now),
            )
            written += 1
        # Older records created before the date fallback existed are not
        # allowed to remain invisible to the follow-up scheduler.  Mark them
        # for same-day evidence review rather than inventing a historical date.
        repaired = c.execute(
            """UPDATE dc_event_followup
               SET next_followup_date=?,next_action='补齐事件日期并立即复核来源证据',
                   status=CASE WHEN status IN ('resolved','dismissed') THEN status ELSE 'in_review' END,
                   updated_at=?
               WHERE next_followup_date IS NULL OR next_followup_date=''""",
            (datetime.now(_BUSINESS_TZ).date().isoformat(), now),
        ).rowcount
    return {"ok": True, "events": len(events), "written": written, "thesis_reviews_attached": len(reviews_by_event), "repaired_missing_dates": int(repaired or 0)}


def list_event_followups(*, status: str = "", ts_code: str = "", limit: int = 500) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?"); params.append(status)
    if ts_code:
        clauses.append("ts_code=?"); params.append(ts_code)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"SELECT * FROM dc_event_followup {where} ORDER BY COALESCE(next_followup_date,'9999-12-31'),updated_at DESC LIMIT ?",
            tuple(params + [max(1, min(int(limit), 2000))]),
        ).fetchall()]
    for row in rows:
        row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
    return {"ok": True, "items": rows, "count": len(rows)}


def update_event_followup(event_key: str, *, status: str, next_action: str = "", actor: str = "user") -> dict[str, Any] | None:
    allowed = {"urgent", "monitor", "in_review", "resolved", "dismissed", "expired"}
    if status not in allowed:
        raise ValueError("invalid_followup_status")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute(
            """UPDATE dc_event_followup SET status=?,next_action=CASE WHEN ?='' THEN next_action ELSE ? END,
               intervention_actor=?,updated_at=? WHERE event_key=? RETURNING *""",
            (status, next_action, next_action, actor, now, str(event_key)),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
    return item

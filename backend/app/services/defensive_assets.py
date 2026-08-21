from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn
from app.data_platform.services.data_center import sync_tushare_api_raw
from app.data_platform.services.observation_clock import (
    _table_columns,
    load_raw_record_versions,
    normalize_explicit_timestamp,
)


DEFENSIVE_ASSETS: tuple[dict[str, str], ...] = (
    {"ts_code": "511880.SH", "name": "银华日利ETF", "asset_class": "money", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "511010.SH", "name": "国债ETF", "asset_class": "bond", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "518880.SH", "name": "黄金ETF", "asset_class": "gold", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "512890.SH", "name": "红利低波ETF", "asset_class": "low_volatility", "risk_group": "equity_like", "equity_exposure": "1"},
    {"ts_code": "510880.SH", "name": "红利ETF", "asset_class": "dividend", "risk_group": "equity_like", "equity_exposure": "1"},
)
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

# The frozen T+1 ETF executor needs an exact, tradable daily bar rather than a
# row which merely happens to exist.  Keep this contract in one module so the
# close-data updater, fallback writer, and executor cannot silently drift into
# accepting different subsets of a `fund_daily` response.
DEFENSIVE_ASSET_EXECUTION_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "vol",
    "amount",
)
DEFENSIVE_ASSET_EXECUTION_CONTRACT = "alis_defensive_asset_execution_slice_v2"


def _fund_daily_observation_sql(connection: Any) -> tuple[str, str, str] | None:
    """Return the strict observation-clock SQL for ``fund_daily`` rows.

    Production data-platform connections must have the 0011 observation-clock
    columns.  A pre-migration PostgreSQL database is therefore fail-closed.
    Small legacy SQLite fixtures used by isolated unit tests may still expose
    only ``available_time``; that compatibility path is deliberately never
    enabled for PostgreSQL.
    """
    columns = _table_columns(connection, "dc_tushare_raw_records")
    clock_columns = {
        "economic_available_time", "observed_at", "first_observed_at",
        "observation_provenance", "content_hash",
    }
    if clock_columns.issubset(columns):
        return (
            "observed_at AS available_time,observed_at",
            "julianday(observed_at)<=julianday(?) AND observed_at<>'' AND content_hash<>'' "
            "AND (SUBSTR(TRIM(observed_at),-1)='Z' OR SUBSTR(TRIM(observed_at),-6,1) IN ('+','-')) "
            "AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'",
            "julianday(observed_at)",
        )
    if bool(getattr(connection, "_is_postgres", False)):
        return None
    if "available_time" in columns:
        return (
            "available_time AS available_time,available_time AS observed_at",
            "julianday(available_time)<=julianday(?) AND "
            "(SUBSTR(TRIM(available_time),-1)='Z' OR SUBSTR(TRIM(available_time),-6,1) IN ('+','-'))",
            "julianday(available_time)",
        )
    return None


def _fund_daily_history_start(trade_date: str, limit: int) -> str:
    """Return a bounded calendar start for a per-ETF history request.

    ``fund_daily`` is a daily series, but exchange holidays and suspensions mean
    a calendar window must include some slack to yield ``limit`` observations.
    Keeping the slack bounded still prevents a history request from scanning the
    append-only archive when the caller only needs a short return window.
    """
    compact = str(trade_date or "").replace("-", "")[:8]
    try:
        requested = max(1, int(limit))
        end = datetime.strptime(compact, "%Y%m%d").date()
    except (TypeError, ValueError):
        # ``list_defensive_assets()`` uses an open-ended sentinel date.  Use
        # the current business date for that read so the sentinel cannot turn
        # a bounded momentum request back into a full archive scan.
        if compact in {"", "99999999"}:
            end = datetime.now(_BUSINESS_TZ).date()
        else:
            return ""
    calendar_days = max(30, min(3650, requested * 4))
    return (end - timedelta(days=calendar_days)).strftime("%Y%m%d")


def _fund_daily_rows(
    connection: Any,
    ts_codes: Iterable[str],
    *,
    trade_date: str = "",
    exact_date: bool = False,
    cutoff: str = "",
    limit: int = 0,
) -> list[dict[str, Any]] | None:
    """Read ETF bars from immutable raw versions whenever available.

    The mutable ``dc_tushare_raw_records`` table is only a compatibility seam
    for isolated legacy SQLite fixtures.  PostgreSQL production callers get a
    strict empty result when the observation-clock migration is unavailable.
    """
    codes = sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()})
    if not codes:
        return []
    target = str(trade_date or "").replace("-", "")[:8]
    if not cutoff:
        cutoff = (
            f"{target[:4]}-{target[4:6]}-{target[6:8]}T15:00:00+08:00"
            if len(target) == 8 and target.isdigit()
            else "9999-12-31T23:59:59+08:00"
        )
    else:
        # A caller-supplied observation clock is evidence, not a display
        # value.  Reject naive/invalid timestamps instead of comparing them as
        # strings against an explicit source clock.
        cutoff = normalize_explicit_timestamp(cutoff)
        if not cutoff:
            return []
    # The immutable loader's legacy ``limit`` is a global result cap.  Split a
    # bounded request by code so one ETF cannot consume another ETF's window,
    # and use a calendar slack window to keep the SQL result itself small.
    history_start = (
        _fund_daily_history_start(target, limit)
        if limit and target and not exact_date else ""
    )
    if limit and target and not exact_date:
        versioned_parts: list[dict[str, Any]] = []
        versioned: list[dict[str, Any]] | None = versioned_parts
        for code in codes:
            rows_for_code = load_raw_record_versions(
                connection,
                "fund_daily",
                cutoff=cutoff,
                ts_codes=[code],
                trade_date_start=history_start,
                trade_date_end=target,
                limit=0,
            )
            if rows_for_code is None:
                versioned = None
                break
            versioned_parts.extend(rows_for_code)
    else:
        versioned = load_raw_record_versions(
            connection,
            "fund_daily",
            cutoff=cutoff,
            ts_codes=codes,
            trade_date_start=target if exact_date and target else "",
            trade_date_end=target if target else "",
            limit=0,
        )
    if versioned is not None:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in versioned:
            item = dict(row)
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            parsed = {
                **payload,
                "ts_code": str(item.get("ts_code") or "").upper(),
                "trade_date": str(item.get("trade_date") or ""),
                "available_time": item.get("observed_at") or "",
                "observed_at": item.get("observed_at") or "",
                "first_observed_at": item.get("first_observed_at") or "",
                "economic_available_time": item.get("economic_available_time") or "",
                "observation_provenance": item.get("observation_provenance") or "",
                "content_hash": item.get("content_hash") or "",
                "created_at": item.get("created_at") or "",
                "source_code": item.get("source_code") or "",
                "_immutable_observation": True,
            }
            key = (parsed["ts_code"], parsed["trade_date"])
            previous = by_key.get(key)
            if previous is None or str(parsed.get("observed_at") or "") > str(previous.get("observed_at") or ""):
                by_key[key] = parsed
        grouped: dict[str, list[dict[str, Any]]] = {}
        for parsed in by_key.values():
            grouped.setdefault(parsed["ts_code"], []).append(parsed)
        result = []
        for code in sorted(grouped):
            values = sorted(
                grouped[code],
                key=lambda item: (str(item.get("trade_date") or ""), str(item.get("observed_at") or "")),
                reverse=True,
            )
            result.extend(values[:max(1, int(limit))] if limit else values)
        return result
    if bool(getattr(connection, "_is_postgres", False)):
        return []

    observation_sql = _fund_daily_observation_sql(connection)
    if observation_sql is None:
        return []
    _selected_clock, clock_filter, order_clock = observation_sql
    placeholders = ",".join("?" for _ in codes)
    date_operator = "=" if exact_date else "<="
    date_start_clause = " AND trade_date>=?" if history_start else ""
    sql = f"""SELECT ts_code,trade_date,payload_json,{_selected_clock}
                FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code IN ({placeholders})
                 {date_start_clause}
                 AND trade_date{date_operator}?
                 AND {clock_filter}
               ORDER BY ts_code,trade_date DESC,{order_clock} DESC"""
    params: tuple[Any, ...] = (*codes, *((history_start,) if history_start else ()), target, cutoff)
    rows = connection.execute(sql, params).fetchall()
    result = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        key = (str(item.get("ts_code") or "").upper(), str(item.get("trade_date") or ""))
        if key in seen:
            continue
        seen.add(key)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            result.append({
                **payload,
                "ts_code": key[0],
                "trade_date": key[1],
                "available_time": item.get("available_time") or "",
                "observed_at": item.get("observed_at") or item.get("available_time") or "",
                "_immutable_observation": False,
            })
    if limit:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in result:
            grouped.setdefault(str(item.get("ts_code") or "").upper(), []).append(item)
        result = [item for code in sorted(grouped) for item in grouped[code][:max(1, int(limit))]]
    return result


def _registered_assets(ts_codes: Iterable[str] | None = None) -> list[dict[str, str]]:
    if ts_codes is None:
        return list(DEFENSIVE_ASSETS)
    requested = {str(value or "").strip().upper() for value in ts_codes if str(value or "").strip()}
    requested_symbols = {value.split(".")[0] for value in requested}
    return [
        asset for asset in DEFENSIVE_ASSETS
        if asset["ts_code"] in requested or asset["ts_code"].split(".")[0] in requested_symbols
    ]


def defensive_asset_meta(symbol: str) -> dict[str, str] | None:
    """Return registered risk metadata without inferring risk from a code prefix."""
    compact = str(symbol or "").strip().upper().split(".", 1)[0]
    return next((dict(item) for item in DEFENSIVE_ASSETS if item["ts_code"].split(".", 1)[0] == compact), None)


def defensive_asset_execution_missing_fields(payload: Any) -> list[str]:
    """Return every non-positive, non-finite, or absent frozen-execution field.

    A daily ETF response can retain its natural key while being incomplete.
    Treating that as a usable bar would defer a deterministic execution failure
    until order creation.  Do not infer missing values from another date or
    source here: the caller must keep the exact raw record and its source
    evidence intact.
    """
    item = payload if isinstance(payload, dict) else {}
    missing: list[str] = []
    for field in DEFENSIVE_ASSET_EXECUTION_FIELDS:
        value = item.get(field)
        if isinstance(value, bool):
            missing.append(field)
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            missing.append(field)
            continue
        if not math.isfinite(numeric) or numeric <= 0:
            missing.append(field)
    return missing


def _strict_observation_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_BUSINESS_TZ)


def defensive_asset_execution_evidence_missing(
    record: Any,
    *,
    observed_at: str,
) -> list[str]:
    """Validate append-only ETF evidence for an execution-time read."""

    item = record if isinstance(record, dict) else {}
    if not item.get("_immutable_observation"):
        # Only isolated SQLite compatibility fixtures reach this branch. A
        # PostgreSQL production read returns no bar when the version table is
        # unavailable, so it cannot use this exception.
        return []
    checked = _strict_observation_timestamp(observed_at)
    if checked is None:
        return ["observation.checked_at:invalid"]
    missing: list[str] = []
    for field in (
        "source_code", "observed_at", "first_observed_at",
        "economic_available_time", "observation_provenance", "content_hash", "created_at",
    ):
        if not str(item.get(field) or "").strip():
            missing.append(f"observation.{field}")
    provenance = str(item.get("observation_provenance") or "").strip().lower()
    if provenance.startswith("legacy_"):
        missing.append("observation.provenance_legacy")
    observed = _strict_observation_timestamp(item.get("observed_at"))
    first = _strict_observation_timestamp(item.get("first_observed_at"))
    economic = _strict_observation_timestamp(item.get("economic_available_time"))
    created = _strict_observation_timestamp(item.get("created_at"))
    for field, value in (
        ("observed_at", observed),
        ("first_observed_at", first),
        ("economic_available_time", economic),
        ("created_at", created),
    ):
        if value is None:
            missing.append(f"observation.{field}:invalid")
    if observed is not None and observed > checked:
        missing.append("observation.observed_at:after_check")
    if created is not None and created > checked:
        missing.append("observation.created_at:after_check")
    if first is not None and observed is not None and first > observed:
        missing.append("observation.first_observed_at:after_observed")
    if economic is not None and observed is not None and economic > observed:
        missing.append("observation.economic_available_time:after_observed")
    return list(dict.fromkeys(missing))


def materialize_defensive_asset_history(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    ts_codes: Iterable[str] | None = None,
    provider: str = "tushare",
) -> dict[str, Any]:
    business_today = datetime.now(_BUSINESS_TZ).date()
    end = str(end_date or business_today.strftime("%Y%m%d")).replace("-", "")
    start = str(start_date or (business_today - timedelta(days=800)).strftime("%Y%m%d")).replace("-", "")
    results = []
    for asset in _registered_assets(ts_codes):
        try:
            result = sync_tushare_api_raw(
                "fund_daily",
                {"ts_code": asset["ts_code"], "start_date": start, "end_date": end},
                provider=provider,
            )
            results.append({**asset, "ok": True, "rows": int(result.get("rows_written") or 0)})
        except Exception as exc:  # noqa: BLE001
            results.append({**asset, "ok": False, "error": str(exc)[:300]})
    return {"ok": all(item["ok"] for item in results), "range": [start, end], "items": results}


def list_defensive_assets(*, as_of_date: str | None = None) -> dict[str, Any]:
    cutoff = str(as_of_date or "99999999").replace("-", "")
    available_cutoff = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
        if len(cutoff) == 8 and cutoff.isdigit() else "9999-12-31T23:59:59+08:00"
    )
    items = []
    try:
        with _conn() as c:
            rows = _fund_daily_rows(
                c,
                [asset["ts_code"] for asset in DEFENSIVE_ASSETS],
                trade_date=cutoff,
                cutoff=available_cutoff,
                # Only the latest close plus the 20-session momentum window
                # is consumed below; do not scan the full fund_daily archive.
                limit=21,
            )
            result_rows: dict[str, list[dict[str, Any]]] = {
                asset["ts_code"]: [] for asset in DEFENSIVE_ASSETS
            }
            for row in rows or []:
                result_rows.setdefault(str(row.get("ts_code") or "").upper(), []).append(row)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "missing", "as_of_date": cutoff, "items": [{**asset, "status": "missing", "latest": None, "momentum_20d": None, "observations": 0} for asset in DEFENSIVE_ASSETS], "reason": str(exc)[:200]}
    for asset in DEFENSIVE_ASSETS:
        rows = result_rows.get(asset["ts_code"], [])
        history = []
        for record in reversed(rows[-21:]):
            try:
                if record.get("close") is not None:
                    history.append({"trade_date": record.get("trade_date"), "close": float(record["close"]), "open": float(record.get("open") or 0), "amount": float(record.get("amount") or 0), "available_time": record.get("available_time")})
            except (TypeError, ValueError):
                continue
        latest = history[-1] if history else None
        momentum = history[-1]["close"] / history[0]["close"] - 1.0 if len(history) >= 2 and history[0]["close"] > 0 else None
        items.append({**asset, "status": "ready" if latest else "missing", "latest": latest, "momentum_20d": None if momentum is None else round(momentum, 6), "observations": len(history)})
    ready = [item for item in items if item["status"] == "ready"]
    return {"ok": bool(ready), "status": "ready" if len(ready) == len(items) else "partial" if ready else "missing", "as_of_date": cutoff, "items": items}


def defensive_allocation_for_state(
    market_state: str,
    *,
    as_of_date: str,
    equity_target_weight: float | None = None,
) -> dict[str, Any]:
    """Allocate every non-equity yuan to true defensive assets only.

    The market state supplies preferences, while the requested sleeve is the
    exact complement of the admitted equity target. Momentum only tilts the
    mix; it never turns a healthy account back into idle cash.
    """
    state = str(market_state or "unknown")
    equity_defaults = {
        "S0": .85, "S1": .75, "S2": .70, "S3": .65,
        "S4": .50, "S5": .55, "S6": .50, "S7": .45,
        "S8": .35, "S9": .25, "S10": .10, "S11": .40,
    }
    equity_weight = equity_defaults.get(state, .50) if equity_target_weight is None else max(0.0, min(1.0, float(equity_target_weight)))
    defensive_target = round(1.0 - equity_weight, 6)
    defensive_states = {"S7", "S8", "S9", "S10", "S11"}
    base_scores = {
        "money": .28 if state in defensive_states else .12,
        "bond": .30 if state in defensive_states else .16,
        "gold": .24 if state in defensive_states else .12,
    }
    assets = list_defensive_assets(as_of_date=as_of_date)
    ready = [
        item for item in assets["items"]
        if item["status"] == "ready" and item.get("risk_group") == "true_defensive"
    ]
    scores = {}
    for item in ready:
        momentum = float(item.get("momentum_20d") or 0)
        scores[item["ts_code"]] = max(.01, base_scores.get(item["asset_class"], .10) * (1.0 + max(-.5, min(.5, momentum * 5.0))))
    total_score = sum(scores.values())
    weights = {code: defensive_target * score / total_score for code, score in scores.items()} if total_score > 0 else {}
    # Cap concentration, then redistribute the excess to names with room.
    from app.services.risk_limit_contract import DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT
    cap = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT
    for _ in range(len(weights) + 1):
        excess = sum(max(0.0, weight - cap) for weight in weights.values())
        weights = {code: min(cap, weight) for code, weight in weights.items()}
        room = {code: cap - weight for code, weight in weights.items() if weight < cap - 1e-9}
        if excess <= 1e-9 or not room:
            break
        room_total = sum(room.values())
        for code, available in room.items():
            weights[code] += excess * available / room_total
    weights = {code: round(weight, 6) for code, weight in weights.items() if weight > 0}
    missing = [
        item["asset_class"] for item in DEFENSIVE_ASSETS
        if item.get("risk_group") == "true_defensive"
        and not any(x["asset_class"] == item["asset_class"] for x in ready)
    ]
    allocated = round(sum(weights.values()), 6)
    unallocated = round(max(0.0, defensive_target - allocated), 6)
    return {
        "status": "ready" if abs(allocated - defensive_target) <= .001 else "partial" if weights else "missing",
        "market_state": state,
        "as_of_date": str(as_of_date).replace("-", ""),
        "target_weights": weights,
        "target_weight": round(allocated, 6),
        "unallocated_cash_weight": unallocated,
        "equity_target_weight": round(equity_weight, 6),
        "full_investment_target": round(equity_weight + allocated, 6),
        "missing_asset_classes": missing,
        "excluded_equity_like_assets": [
            item["ts_code"] for item in DEFENSIVE_ASSETS if item.get("risk_group") == "equity_like"
        ],
        "reason": (
            "股票风险预算之外的资金只配置到货币、国债和黄金；红利类权益ETF不得作为非权益预算的自动去向"
            if unallocated <= .001 else
            "真实防御资产受单资产硬上限约束，未承接部分保留为现金；不得用红利类权益ETF伪装为防御配置"
        ),
    }


def defensive_asset_bar(
    ts_code: str,
    trade_date: str,
    *,
    exact_date: bool = False,
    available_as_of_time: str | None = None,
) -> dict[str, Any] | None:
    cutoff = str(trade_date).replace("-", "")
    available_cutoff = str(available_as_of_time or "").strip()
    if not available_cutoff:
        available_cutoff = (
            f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
            if len(cutoff) == 8 and cutoff.isdigit()
            else "9999-12-31T23:59:59+08:00"
        )
    with _conn() as c:
        rows = _fund_daily_rows(
            c,
            [str(ts_code)],
            trade_date=cutoff,
            exact_date=exact_date,
            cutoff=available_cutoff,
            limit=1,
        )
    if not rows:
        return None
    return dict(rows[0])


def defensive_asset_bars(
    ts_codes: Iterable[str],
    trade_date: str,
    *,
    exact_date: bool = False,
    available_as_of_time: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the latest eligible bars for several defensive assets in one query."""
    codes = sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()})
    if not codes:
        return {}
    cutoff = str(trade_date).replace("-", "")[:8]
    available_cutoff = str(available_as_of_time or "").strip() or (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59+08:00"
    )
    with _conn() as c:
        rows = _fund_daily_rows(
            c,
            codes,
            trade_date=cutoff,
            exact_date=exact_date,
            cutoff=available_cutoff,
            limit=1,
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        code = str(item.get("ts_code") or "").upper()
        if code in result:
            continue
        result[code] = item
    return result


def defensive_asset_series(
    ts_code: str,
    as_of_date: str,
    *,
    limit: int = 60,
    available_as_of_time: str | None = None,
    exact_date: bool = False,
) -> tuple[dict[str, Any] | None, list[float]]:
    """Read one ETF's latest point-in-time bar and return history in one query."""
    cutoff = str(as_of_date).replace("-", "")[:8]
    available_cutoff = str(available_as_of_time or "").strip() or (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59+08:00"
    )
    with _conn() as c:
        rows = _fund_daily_rows(
            c,
            [str(ts_code).strip().upper()],
            trade_date=cutoff,
            # Keep the historical window; ``exact_date`` below validates the
            # newest bar without throwing away the preceding observations.
            exact_date=False,
            cutoff=available_cutoff,
            limit=max(2, min(int(limit) + 1, 251)),
        )
    parsed: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        parsed.append(item)
    if not parsed:
        return None, []
    if exact_date and str(parsed[0].get("trade_date") or "").replace("-", "")[:8] != cutoff:
        return None, []
    closes = [
        float(item["close"])
        for item in reversed(parsed)
        if item.get("close") is not None and float(item.get("close") or 0) > 0
    ]
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    return parsed[0], returns


def defensive_asset_series_batch(
    ts_codes: Iterable[str],
    as_of_date: str,
    *,
    limit: int = 60,
    connection: Any | None = None,
    available_as_of_time: str | None = None,
    exact_date: bool = False,
) -> dict[str, tuple[dict[str, Any] | None, list[float]]]:
    """Read several ETF series in one point-in-time result set."""
    codes = sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()})
    if not codes:
        return {}
    cutoff = str(as_of_date).replace("-", "")[:8]
    available_cutoff = str(available_as_of_time or "").strip() or (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59+08:00"
    )
    placeholders = ",".join("?" for _ in codes)
    def read_rows(c: Any) -> list[dict[str, Any]]:
        return _fund_daily_rows(
            c,
            codes,
            trade_date=cutoff,
            exact_date=False,
            cutoff=available_cutoff,
            # Keep one independent history window per ETF.  A global result
            # limit would let the first code starve the remaining holdings.
            limit=max(2, min(int(limit) + 1, 251)),
        ) or []
    if connection is None:
        with _conn() as c:
            rows = read_rows(c)
    else:
        rows = read_rows(connection)
    grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    for row in rows:
        item = dict(row)
        grouped.setdefault(str(item.get("ts_code") or "").upper(), []).append(item)
    result: dict[str, tuple[dict[str, Any] | None, list[float]]] = {}
    for code, parsed in grouped.items():
        if not parsed:
            result[code] = (None, [])
            continue
        if exact_date and str(parsed[0].get("trade_date") or "").replace("-", "")[:8] != cutoff:
            result[code] = (None, [])
            continue
        closes = [
            float(item["close"])
            for item in reversed(parsed)
            if item.get("close") is not None and float(item.get("close") or 0) > 0
        ]
        returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
        result[code] = (parsed[0], returns)
    return result


def defensive_asset_execution_status(
    trade_date: str,
    ts_codes: Iterable[str],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Audit exact-session ETF fields needed by a frozen T+1 execution."""
    target = str(trade_date or "").replace("-", "")[:8]
    observation_time = str(observed_at or datetime.now().astimezone().isoformat(timespec="seconds"))
    assets = _registered_assets(ts_codes)
    items: list[dict[str, Any]] = []
    for asset in assets:
        bar = defensive_asset_bar(
            asset["ts_code"],
            target,
            exact_date=True,
            available_as_of_time=observation_time,
        )
        actual_date = str((bar or {}).get("trade_date") or "").replace("-", "")[:8]
        missing_fields = defensive_asset_execution_missing_fields(bar)
        evidence_missing = defensive_asset_execution_evidence_missing(
            bar,
            observed_at=observation_time,
        )
        missing_fields.extend(evidence_missing)
        ready = actual_date == target and not missing_fields
        items.append({
            "ts_code": asset["ts_code"],
            "name": asset["name"],
            "ready": ready,
            "record_present": bool(bar),
            "trade_date": actual_date,
            "available_time": str((bar or {}).get("available_time") or ""),
            "source_code": str((bar or {}).get("source_code") or ""),
            "content_hash": str((bar or {}).get("content_hash") or ""),
            "missing_fields": missing_fields,
        })
    missing = [item["ts_code"] for item in items if not item["ready"]]
    return {
        "schema_version": DEFENSIVE_ASSET_EXECUTION_CONTRACT,
        "trade_date": target,
        "observed_at": observation_time,
        "required_fields": list(DEFENSIVE_ASSET_EXECUTION_FIELDS),
        "required": [asset["ts_code"] for asset in assets],
        "ready": [item["ts_code"] for item in items if item["ready"]],
        "missing": missing,
        "items": items,
        "ok": not missing,
    }


def defensive_asset_returns(
    ts_code: str,
    as_of_date: str,
    *,
    limit: int = 60,
    available_as_of_time: str | None = None,
    require_exact_latest: bool = False,
) -> list[float]:
    cutoff = str(as_of_date).replace("-", "")[:8]
    available_cutoff = str(available_as_of_time or "").strip() or (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00+08:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59+08:00"
    )
    with _conn() as c:
        rows = _fund_daily_rows(
            c,
            [str(ts_code)],
            trade_date=cutoff,
            cutoff=available_cutoff,
            limit=max(2, min(int(limit) + 1, 251)),
        ) or []
    if require_exact_latest and (
        not rows or str(rows[0].get("trade_date") or "").replace("-", "")[:8] != cutoff
    ):
        return []
    closes = []
    for row in reversed(rows):
        close = dict(row).get("close")
        if close is not None and float(close) > 0:
            closes.append(float(close))
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn
from app.data_platform.services.data_center import sync_tushare_api_raw


DEFENSIVE_ASSETS: tuple[dict[str, str], ...] = (
    {"ts_code": "511880.SH", "name": "银华日利ETF", "asset_class": "money", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "511010.SH", "name": "国债ETF", "asset_class": "bond", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "518880.SH", "name": "黄金ETF", "asset_class": "gold", "risk_group": "true_defensive", "equity_exposure": "0"},
    {"ts_code": "512890.SH", "name": "红利低波ETF", "asset_class": "low_volatility", "risk_group": "equity_like", "equity_exposure": "1"},
    {"ts_code": "510880.SH", "name": "红利ETF", "asset_class": "dividend", "risk_group": "equity_like", "equity_exposure": "1"},
)
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


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


def materialize_defensive_asset_history(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    ts_codes: Iterable[str] | None = None,
) -> dict[str, Any]:
    business_today = datetime.now(_BUSINESS_TZ).date()
    end = str(end_date or business_today.strftime("%Y%m%d")).replace("-", "")
    start = str(start_date or (business_today - timedelta(days=800)).strftime("%Y%m%d")).replace("-", "")
    results = []
    for asset in _registered_assets(ts_codes):
        try:
            result = sync_tushare_api_raw("fund_daily", {"ts_code": asset["ts_code"], "start_date": start, "end_date": end})
            results.append({**asset, "ok": True, "rows": int(result.get("rows_written") or 0)})
        except Exception as exc:  # noqa: BLE001
            results.append({**asset, "ok": False, "error": str(exc)[:300]})
    return {"ok": all(item["ok"] for item in results), "range": [start, end], "items": results}


def list_defensive_assets(*, as_of_date: str | None = None) -> dict[str, Any]:
    cutoff = str(as_of_date or "99999999").replace("-", "")
    available_cutoff = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00"
        if len(cutoff) == 8 and cutoff.isdigit() else "9999-12-31T23:59:59"
    )
    items = []
    try:
        with _conn() as c:
            result_rows = {
                asset["ts_code"]: c.execute(
                    """SELECT trade_date,payload_json,available_time FROM dc_tushare_raw_records
                       WHERE api_name='fund_daily' AND ts_code=? AND trade_date<=? AND available_time<=?
                       ORDER BY trade_date DESC LIMIT 21""",
                    (asset["ts_code"], cutoff, available_cutoff),
                ).fetchall()
                for asset in DEFENSIVE_ASSETS
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "missing", "as_of_date": cutoff, "items": [{**asset, "status": "missing", "latest": None, "momentum_20d": None, "observations": 0} for asset in DEFENSIVE_ASSETS], "reason": str(exc)[:200]}
    for asset in DEFENSIVE_ASSETS:
        rows = result_rows[asset["ts_code"]]
        history = []
        for row in reversed(rows):
            record = dict(row)
            try:
                payload = json.loads(record.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if payload.get("close") is not None:
                history.append({"trade_date": record.get("trade_date"), "close": float(payload["close"]), "open": float(payload.get("open") or 0), "amount": float(payload.get("amount") or 0), "available_time": record.get("available_time")})
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
            f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00"
            if len(cutoff) == 8 and cutoff.isdigit()
            else "9999-12-31T23:59:59"
        )
    date_operator = "=" if exact_date else "<="
    with _conn() as c:
        row = c.execute(
            f"""SELECT trade_date,payload_json,available_time FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code=? AND trade_date{date_operator}? AND available_time<=?
               ORDER BY trade_date DESC, available_time DESC LIMIT 1""",
            (str(ts_code), cutoff, available_cutoff),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return {
        **payload,
        "trade_date": str(item.get("trade_date") or ""),
        "available_time": item.get("available_time"),
    }


def defensive_asset_bars(
    ts_codes: Iterable[str],
    trade_date: str,
    *,
    exact_date: bool = False,
) -> dict[str, dict[str, Any]]:
    """Read the latest eligible bars for several defensive assets in one query."""
    codes = sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()})
    if not codes:
        return {}
    cutoff = str(trade_date).replace("-", "")[:8]
    available_cutoff = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59"
    )
    date_operator = "=" if exact_date else "<="
    placeholders = ",".join("?" for _ in codes)
    with _conn() as c:
        rows = c.execute(
            f"""SELECT ts_code,trade_date,payload_json,available_time
                  FROM dc_tushare_raw_records
                 WHERE api_name='fund_daily'
                   AND ts_code IN ({placeholders})
                   AND trade_date{date_operator}?
                   AND available_time<=?
                 ORDER BY ts_code,trade_date DESC,available_time DESC""",
            (*codes, cutoff, available_cutoff),
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        code = str(item.get("ts_code") or "").upper()
        if code in result:
            continue
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        result[code] = {
            **payload,
            "trade_date": str(item.get("trade_date") or ""),
            "available_time": item.get("available_time"),
        }
    return result


def defensive_asset_series(
    ts_code: str,
    as_of_date: str,
    *,
    limit: int = 60,
) -> tuple[dict[str, Any] | None, list[float]]:
    """Read one ETF's latest point-in-time bar and return history in one query."""
    cutoff = str(as_of_date).replace("-", "")[:8]
    available_cutoff = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59"
    )
    with _conn() as c:
        rows = c.execute(
            """SELECT trade_date,payload_json,available_time
                 FROM dc_tushare_raw_records
                WHERE api_name='fund_daily' AND ts_code=?
                  AND trade_date<=? AND available_time<=?
                ORDER BY trade_date DESC, available_time DESC
                LIMIT ?""",
            (
                str(ts_code).strip().upper(),
                cutoff,
                available_cutoff,
                max(2, min(int(limit) + 1, 251)),
            ),
        ).fetchall()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        parsed.append({
            **payload,
            "trade_date": str(item.get("trade_date") or ""),
            "available_time": item.get("available_time"),
        })
    if not parsed:
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
) -> dict[str, tuple[dict[str, Any] | None, list[float]]]:
    """Read several ETF series in one point-in-time result set."""
    codes = sorted({str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()})
    if not codes:
        return {}
    cutoff = str(as_of_date).replace("-", "")[:8]
    available_cutoff = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:8]}T15:00:00"
        if len(cutoff) == 8 and cutoff.isdigit()
        else "9999-12-31T23:59:59"
    )
    placeholders = ",".join("?" for _ in codes)
    def read_rows(c: Any) -> list[Any]:
        return c.execute(
            f"""WITH dated AS (
                       SELECT ts_code,trade_date,payload_json,available_time,
                              ROW_NUMBER() OVER (
                                  PARTITION BY ts_code,trade_date
                                  ORDER BY available_time DESC
                              ) AS date_rank
                         FROM dc_tushare_raw_records
                        WHERE api_name='fund_daily'
                          AND ts_code IN ({placeholders})
                          AND trade_date<=?
                          AND available_time<=?
                     ), ranked AS (
                       SELECT ts_code,trade_date,payload_json,available_time,
                              ROW_NUMBER() OVER (
                                  PARTITION BY ts_code ORDER BY trade_date DESC
                              ) AS series_rank
                         FROM dated
                        WHERE date_rank=1
                     )
                     SELECT ts_code,trade_date,payload_json,available_time
                       FROM ranked
                      WHERE series_rank<=?
                      ORDER BY ts_code,trade_date DESC""",
            (*codes, cutoff, available_cutoff, max(2, min(int(limit) + 1, 251))),
        ).fetchall()
    if connection is None:
        with _conn() as c:
            rows = read_rows(c)
    else:
        rows = read_rows(connection)
    grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        grouped.setdefault(str(item.get("ts_code") or "").upper(), []).append({
            **payload,
            "trade_date": str(item.get("trade_date") or ""),
            "available_time": item.get("available_time"),
        })
    result: dict[str, tuple[dict[str, Any] | None, list[float]]] = {}
    for code, parsed in grouped.items():
        if not parsed:
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
        missing_fields = [
            field for field in ("open", "pre_close", "vol")
            if float((bar or {}).get(field) or 0) <= 0
        ]
        ready = actual_date == target and not missing_fields
        items.append({
            "ts_code": asset["ts_code"],
            "name": asset["name"],
            "ready": ready,
            "trade_date": actual_date,
            "missing_fields": missing_fields,
        })
    missing = [item["ts_code"] for item in items if not item["ready"]]
    return {
        "schema_version": "alis_defensive_asset_execution_slice_v1",
        "trade_date": target,
        "observed_at": observation_time,
        "required": [asset["ts_code"] for asset in assets],
        "ready": [item["ts_code"] for item in items if item["ready"]],
        "missing": missing,
        "items": items,
        "ok": not missing,
    }


def defensive_asset_returns(ts_code: str, as_of_date: str, *, limit: int = 60) -> list[float]:
    with _conn() as c:
        rows = c.execute(
            """SELECT payload_json FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code=? AND trade_date<=?
               ORDER BY trade_date DESC LIMIT ?""",
            (str(ts_code), str(as_of_date).replace("-", ""), max(2, min(int(limit) + 1, 251))),
        ).fetchall()
    closes = []
    for row in reversed(rows):
        try:
            close = json.loads(dict(row).get("payload_json") or "{}").get("close")
        except (TypeError, json.JSONDecodeError):
            close = None
        if close is not None and float(close) > 0:
            closes.append(float(close))
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]

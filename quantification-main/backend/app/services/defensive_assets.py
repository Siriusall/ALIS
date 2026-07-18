from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.data_center import sync_tushare_api_raw


DEFENSIVE_ASSETS: tuple[dict[str, str], ...] = (
    {"ts_code": "511880.SH", "name": "银华日利ETF", "asset_class": "money"},
    {"ts_code": "511010.SH", "name": "国债ETF", "asset_class": "bond"},
    {"ts_code": "518880.SH", "name": "黄金ETF", "asset_class": "gold"},
    {"ts_code": "512890.SH", "name": "红利低波ETF", "asset_class": "low_volatility"},
    {"ts_code": "510880.SH", "name": "红利ETF", "asset_class": "dividend"},
)


def materialize_defensive_asset_history(*, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    end = str(end_date or date.today().strftime("%Y%m%d")).replace("-", "")
    start = str(start_date or (date.today() - timedelta(days=800)).strftime("%Y%m%d")).replace("-", "")
    results = []
    for asset in DEFENSIVE_ASSETS:
        try:
            result = sync_tushare_api_raw("fund_daily", {"ts_code": asset["ts_code"], "start_date": start, "end_date": end})
            results.append({**asset, "ok": True, "rows": int(result.get("rows_written") or 0)})
        except Exception as exc:  # noqa: BLE001
            results.append({**asset, "ok": False, "error": str(exc)[:300]})
    return {"ok": all(item["ok"] for item in results), "range": [start, end], "items": results}


def list_defensive_assets(*, as_of_date: str | None = None) -> dict[str, Any]:
    cutoff = str(as_of_date or "99999999").replace("-", "")
    items = []
    try:
        with _conn() as c:
            result_rows = {
                asset["ts_code"]: c.execute(
                    """SELECT trade_date,payload_json,available_time FROM dc_tushare_raw_records
                       WHERE api_name='fund_daily' AND ts_code=? AND trade_date<=?
                       ORDER BY trade_date DESC LIMIT 21""",
                    (asset["ts_code"], cutoff),
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


def defensive_allocation_for_state(market_state: str, *, as_of_date: str) -> dict[str, Any]:
    """Return the fixed, explainable defensive sleeve permitted by B11.2."""
    state = str(market_state or "unknown")
    policies = {
        "S2": {"money": .20, "bond": .15, "gold": .10},
        "S3": {"low_volatility": .12, "dividend": .10, "gold": .08},
    }
    requested = policies.get(state, {})
    assets = list_defensive_assets(as_of_date=as_of_date)
    by_class = {item["asset_class"]: item for item in assets["items"] if item["status"] == "ready"}
    weights = {
        by_class[asset_class]["ts_code"]: weight
        for asset_class, weight in requested.items()
        if asset_class in by_class
    }
    missing = [asset_class for asset_class in requested if asset_class not in by_class]
    return {
        "status": "ready" if requested and not missing else "partial" if weights else "not_required" if not requested else "missing",
        "market_state": state,
        "as_of_date": str(as_of_date).replace("-", ""),
        "target_weights": weights,
        "target_weight": round(sum(weights.values()), 4),
        "missing_asset_classes": missing,
        "reason": "S2/S3风险收缩期将非权益预算配置到计划书允许的防御ETF" if requested else "当前市场状态不强制启用防御袖套",
    }


def defensive_asset_bar(ts_code: str, trade_date: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            """SELECT payload_json,available_time FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND ts_code=? AND trade_date=?
               ORDER BY available_time DESC LIMIT 1""",
            (str(ts_code), str(trade_date).replace("-", "")),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return {**payload, "available_time": item.get("available_time")}


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

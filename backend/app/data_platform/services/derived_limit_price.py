"""Audited exchange-rule fallback for a missing daily ``stk_limit`` slice."""

from __future__ import annotations

import bisect
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import upsert_raw_record
from app.data_platform.tushare_client import tushare_compatible_source_codes


SOURCE_EXCHANGE_RULE = "exchange_rule_derived"
SCHEMA_VERSION = "alis_exchange_rule_limit_price_v1"
MAIN_BOARD_ST_RATE_CHANGE_DATE = "20260706"
MIN_CALIBRATION_OBSERVATIONS = 500
MIN_CALIBRATION_MATCH_RATE = 0.995


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _economic_available_time(trade_date: str) -> str:
    value = str(trade_date or "").replace("-", "")[:8]
    return (
        f"{value[:4]}-{value[4:6]}-{value[6:8]}T08:40:00+08:00"
        if len(value) == 8 and value.isdigit()
        else _now()
    )


def _ensure_source() -> None:
    now = _now()
    with _conn() as c:
        c.execute(
            """INSERT INTO data_sources
               (code,name,source_type,priority,status,description,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
               name=excluded.name,source_type=excluded.source_type,priority=excluded.priority,
               status=excluded.status,description=excluded.description,updated_at=excluded.updated_at""",
            (
                SOURCE_EXCHANGE_RULE,
                "交易所规则涨跌停价派生源",
                "deterministic_derived",
                90,
                "fallback",
                "仅在兼容代理 stk_limit 空切片时启用；按前收盘、板块、风险状态、上市交易日和价格最小单位复算，并以前一完整切片校准。",
                now,
                now,
            ),
        )


def _normalise_date(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


def _board_and_rate(
    ts_code: str,
    *,
    trade_date: str,
    is_st: bool,
    name: str,
) -> tuple[str, Decimal] | tuple[None, None]:
    code, _, exchange = str(ts_code or "").partition(".")
    exchange = exchange.upper()
    upper_name = str(name or "").upper().replace("＊", "*")
    if exchange == "BJ" or code.startswith(("4", "8", "92")):
        return "beijing", Decimal("0.30")
    if exchange == "SH" and code.startswith(("68",)):
        return "star", Decimal("0.20")
    if exchange == "SZ" and code.startswith("30"):
        return "chinext", Decimal("0.20")
    if exchange in {"SH", "SZ"} and code.startswith(
        ("000", "001", "002", "003", "600", "601", "603", "605")
    ):
        if upper_name.startswith("S") and not upper_name.startswith(("ST", "SST")):
            return "legacy_s_share", Decimal("0.05")
        if is_st and _normalise_date(trade_date) < MAIN_BOARD_ST_RATE_CHANGE_DATE:
            return "main_board_risk_warning_legacy", Decimal("0.05")
        return "main_board", Decimal("0.10")
    return None, None


def _round_price(pre_close: Decimal, rate: Decimal, direction: int) -> Decimal:
    return (pre_close * (Decimal("1") + rate * Decimal(direction))).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def calculate_limit_prices(
    *,
    ts_code: str,
    trade_date: str,
    pre_close: Any,
    list_date: str,
    listing_open_session_number: int | None,
    is_st: bool,
    name: str,
) -> dict[str, Any]:
    """Return a reproducible price-limit result or an explicit safe rejection."""

    value = _normalise_date(trade_date)
    listed = _normalise_date(list_date)
    if len(value) != 8 or len(listed) != 8:
        return {"ok": False, "reason": "missing_trade_or_list_date"}
    if listing_open_session_number is None or listing_open_session_number <= 0:
        return {"ok": False, "reason": "listing_session_unknown"}
    # Registration-based A-share IPOs have an initial no-limit window.  Five
    # sessions is deliberately conservative for every board: skipping a few
    # calculable Beijing-board days is safer than inventing an execution cap.
    if listing_open_session_number <= 5:
        return {"ok": False, "reason": "initial_no_limit_window"}
    board, rate = _board_and_rate(
        ts_code,
        trade_date=value,
        is_st=bool(is_st),
        name=name,
    )
    if board is None or rate is None:
        return {"ok": False, "reason": "unsupported_security_board"}
    try:
        previous = Decimal(str(pre_close))
    except (InvalidOperation, TypeError, ValueError):
        return {"ok": False, "reason": "missing_pre_close"}
    if not previous.is_finite() or previous <= 0:
        return {"ok": False, "reason": "invalid_pre_close"}
    upper = _round_price(previous, rate, 1)
    lower = _round_price(previous, rate, -1)
    if upper <= lower or lower <= 0:
        return {"ok": False, "reason": "invalid_derived_limits"}
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "board": board,
        "limit_rate": float(rate),
        "price_tick": 0.01,
        "rounding": "decimal_round_half_up_to_price_tick",
        "pre_close": float(previous),
        "up_limit": float(upper),
        "down_limit": float(lower),
        "listing_open_session_number": int(listing_open_session_number),
        "is_st": bool(is_st),
    }


def _load_open_dates(c: Any, end_date: str) -> list[str]:
    rows = c.execute(
        """SELECT cal_date FROM dc_trade_calendar
           WHERE is_open=1 AND cal_date<=? ORDER BY cal_date""",
        (_normalise_date(end_date),),
    ).fetchall()
    return [str(dict(row).get("cal_date") or "") for row in rows]


def _listing_session_number(open_dates: list[str], list_date: str, trade_date: str) -> int | None:
    listed = _normalise_date(list_date)
    target = _normalise_date(trade_date)
    if len(listed) != 8 or listed > target:
        return None
    left = bisect.bisect_left(open_dates, listed)
    right = bisect.bisect_right(open_dates, target)
    return right - left if right > left else None


def _load_st_codes(trade_date: str) -> tuple[set[str], dict[str, Any]]:
    target = _normalise_date(trade_date)
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT ts_code,source_code FROM dc_tushare_raw_records
                   WHERE api_name='stock_st' AND trade_date=?""",
                (target,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return set(), {"ok": False, "reason": "stock_st_read_failed", "error": str(exc)[:300]}
    if not rows:
        from app.data_platform.services.data_center import sync_tushare_api_raw

        result = sync_tushare_api_raw("stock_st", {"trade_date": target})
        if not result.get("ok"):
            return set(), {
                "ok": False,
                "reason": "stock_st_sync_failed",
                "error": str(result.get("error") or result.get("status") or "")[:300],
                "result": result,
            }
        try:
            with _conn() as conn:
                rows = conn.execute(
                    """SELECT ts_code,source_code FROM dc_tushare_raw_records
                       WHERE api_name='stock_st' AND trade_date=?""",
                    (target,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            return set(), {"ok": False, "reason": "stock_st_read_failed", "error": str(exc)[:300]}
    codes = {str(dict(row).get("ts_code") or "") for row in rows if str(dict(row).get("ts_code") or "")}
    if not codes:
        return set(), {"ok": False, "reason": "stock_st_empty"}
    sources = sorted({str(dict(row).get("source_code") or "") for row in rows if str(dict(row).get("source_code") or "")})
    return codes, {"ok": True, "rows": len(codes), "sources": sources}


def _candidate_rows(c: Any, trade_date: str, *, require_reference: bool) -> list[dict[str, Any]]:
    join = (
        """JOIN dc_tushare_raw_records limits
             ON limits.api_name='stk_limit' AND limits.trade_date=daily.trade_date
            AND limits.ts_code=daily.ts_code"""
        if require_reference else ""
    )
    select = ",limits.payload_json AS limit_payload_json" if require_reference else ""
    rows = c.execute(
        f"""SELECT daily.ts_code,daily.payload_json AS daily_payload_json,
                   stock.name,stock.list_date{select}
            FROM dc_tushare_raw_records daily
            JOIN dc_stock_master stock ON stock.ts_code=daily.ts_code
            {join}
            WHERE daily.api_name='daily' AND daily.trade_date=?
            ORDER BY daily.ts_code""",
        (_normalise_date(trade_date),),
    ).fetchall()
    return [dict(row) for row in rows]


def calibrate_rule_engine(trade_date: str) -> dict[str, Any]:
    """Compare the rule engine with the latest earlier proxy reference slice."""

    target = _normalise_date(trade_date)
    source_codes = tushare_compatible_source_codes()
    source_placeholders = ",".join("?" for _ in source_codes)
    with _conn() as c:
        reference = c.execute(
            f"""SELECT MAX(trade_date) AS value FROM dc_tushare_raw_records
               WHERE api_name='stk_limit' AND source_code IN ({source_placeholders})
                 AND trade_date<?
               GROUP BY trade_date HAVING COUNT(*)>=? ORDER BY trade_date DESC LIMIT 1""",
            (*source_codes, target, MIN_CALIBRATION_OBSERVATIONS),
        ).fetchone()
        reference_date = str(dict(reference).get("value") or "") if reference else ""
        if not reference_date:
            return {"ok": False, "reason": "missing_prior_reference_slice"}
        rows = _candidate_rows(c, reference_date, require_reference=True)
        open_dates = _load_open_dates(c, reference_date)
    st_codes, st_evidence = _load_st_codes(reference_date)
    if reference_date < MAIN_BOARD_ST_RATE_CHANGE_DATE and not st_evidence.get("ok"):
        return {"ok": False, "reason": "historical_st_status_unavailable", "st_evidence": st_evidence}
    compared = matched = 0
    mismatches: list[dict[str, Any]] = []
    for row in rows:
        daily = json.loads(row.get("daily_payload_json") or "{}")
        actual = json.loads(row.get("limit_payload_json") or "{}")
        result = calculate_limit_prices(
            ts_code=str(row.get("ts_code") or ""),
            trade_date=reference_date,
            pre_close=daily.get("pre_close"),
            list_date=str(row.get("list_date") or ""),
            listing_open_session_number=_listing_session_number(
                open_dates, str(row.get("list_date") or ""), reference_date,
            ),
            is_st=str(row.get("ts_code") or "") in st_codes,
            name=str(row.get("name") or ""),
        )
        if not result.get("ok"):
            continue
        compared += 1
        same = (
            abs(float(result["up_limit"]) - float(actual.get("up_limit") or 0)) <= 0.001
            and abs(float(result["down_limit"]) - float(actual.get("down_limit") or 0)) <= 0.001
        )
        if same:
            matched += 1
        elif len(mismatches) < 20:
            mismatches.append({
                "ts_code": row.get("ts_code"),
                "derived": [result["up_limit"], result["down_limit"]],
                "reference": [actual.get("up_limit"), actual.get("down_limit")],
            })
    match_rate = matched / compared if compared else 0.0
    return {
        "ok": compared >= MIN_CALIBRATION_OBSERVATIONS and match_rate >= MIN_CALIBRATION_MATCH_RATE,
        "schema_version": SCHEMA_VERSION,
        "reference_trade_date": reference_date,
        "observations": compared,
        "matched": matched,
        "match_rate": round(match_rate, 8),
        "required_observations": MIN_CALIBRATION_OBSERVATIONS,
        "required_match_rate": MIN_CALIBRATION_MATCH_RATE,
        "mismatches": mismatches,
        "st_evidence": st_evidence,
    }


def repair_stk_limit_from_exchange_rules(trade_date: str) -> dict[str, Any]:
    """Materialise only safely derivable limits after a calibrated empty slice."""

    target = _normalise_date(trade_date)
    calibration = calibrate_rule_engine(target)
    if not calibration.get("ok"):
        return {
            "ok": False,
            "status": "calibration_failed",
            "rows_written": 0,
            "calibration": calibration,
        }
    with _conn() as c:
        rows = _candidate_rows(c, target, require_reference=False)
        open_dates = _load_open_dates(c, target)
    st_codes, st_evidence = _load_st_codes(target)
    if target < MAIN_BOARD_ST_RATE_CHANGE_DATE and not st_evidence.get("ok"):
        return {
            "ok": False,
            "status": "target_st_status_unavailable",
            "rows_written": 0,
            "calibration": calibration,
            "st_evidence": st_evidence,
        }

    eligible: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    for row in rows:
        ts_code = str(row.get("ts_code") or "")
        daily = json.loads(row.get("daily_payload_json") or "{}")
        result = calculate_limit_prices(
            ts_code=ts_code,
            trade_date=target,
            pre_close=daily.get("pre_close"),
            list_date=str(row.get("list_date") or ""),
            listing_open_session_number=_listing_session_number(
                open_dates, str(row.get("list_date") or ""), target,
            ),
            is_st=ts_code in st_codes,
            name=str(row.get("name") or ""),
        )
        if result.get("ok"):
            eligible[ts_code] = result
        else:
            rejected[ts_code] = str(result.get("reason") or "not_derivable")

    tencent_quotes: dict[str, dict[str, Any]] = {}
    tencent_evidence: dict[str, Any] = {"attempted": False, "reason": "historical_date"}
    if target == date.today().strftime("%Y%m%d") and eligible:
        from app.data_platform.services.tencent_limit_fallback import fetch_tencent_limit_quotes

        tencent_evidence = {"attempted": True}
        try:
            tencent_quotes = fetch_tencent_limit_quotes(list(eligible), target)
            tencent_evidence.update({"ok": True, "rows": len(tencent_quotes)})
        except Exception as exc:  # noqa: BLE001
            tencent_evidence.update({"ok": False, "error": str(exc)[:300]})

    accepted: dict[str, dict[str, Any]] = {}
    cross_check_mismatches: list[str] = []
    for ts_code, result in eligible.items():
        quote = tencent_quotes.get(ts_code)
        if quote and (
            abs(float(quote["up_limit"]) - float(result["up_limit"])) > 0.001
            or abs(float(quote["down_limit"]) - float(result["down_limit"])) > 0.001
        ):
            cross_check_mismatches.append(ts_code)
            rejected[ts_code] = "tencent_rule_mismatch"
            continue
        accepted[ts_code] = result

    _ensure_source()
    now = _now()
    economic_time = _economic_available_time(target)
    with _conn() as c:
        for ts_code, result in accepted.items():
            quote = tencent_quotes.get(ts_code)
            payload = {
                "ts_code": ts_code,
                "trade_date": target,
                "up_limit": result["up_limit"],
                "down_limit": result["down_limit"],
                "pre_close": result["pre_close"],
                "source_code": SOURCE_EXCHANGE_RULE,
                "verification_status": "tencent_and_rule_cross_checked" if quote else "prior_slice_calibrated_rule",
                "rule_evidence": result,
                "calibration_reference_trade_date": calibration["reference_trade_date"],
                "calibration_match_rate": calibration["match_rate"],
                "tencent_quote_time": str((quote or {}).get("quote_time") or ""),
            }
            upsert_raw_record(
                c, api_name="stk_limit", record_key=f"stk_limit:{target}:{ts_code}",
                ts_code=ts_code, trade_date=target, payload=payload, source_code=SOURCE_EXCHANGE_RULE,
                observed_at=now, economic_time=economic_time,
                # This is a gap repair.  Do not overwrite a provider that won
                # the same publication race after the missing set was read.
                provenance="exchange_rule_derived:calibrated",
            )
    return {
        "ok": bool(accepted),
        "status": "completed" if accepted else "no_safe_rows",
        "schema_version": SCHEMA_VERSION,
        "trade_date": target,
        "requested": len(rows),
        "eligible": len(eligible),
        "rows_written": len(accepted),
        "rejected": len(rejected),
        "rejection_reasons": {
            reason: sum(1 for value in rejected.values() if value == reason)
            for reason in sorted(set(rejected.values()))
        },
        "cross_check_mismatches": cross_check_mismatches[:50],
        "calibration": calibration,
        "st_evidence": st_evidence,
        "tencent_evidence": tencent_evidence,
        "policy": "proxy_primary_then_calibrated_exchange_rule_with_same_day_tencent_cross_check_when_available",
    }

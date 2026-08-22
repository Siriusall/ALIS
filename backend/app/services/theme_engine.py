from __future__ import annotations

from datetime import datetime
import json
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import load_snapshot_versions
from app.services.market_context import score_theme_narratives
from app.services.narrative_lifecycle_contract import (
    ALLOWED_TRANSITIONS as NARRATIVE_LIFECYCLE_ALLOWED_TRANSITIONS,
    SCHEMA_VERSION as NARRATIVE_LIFECYCLE_SCHEMA_VERSION,
    STATES as NARRATIVE_LIFECYCLE_STATES,
    candidate_state as _candidate_lifecycle_state,
    contract_payload as _lifecycle_contract_payload,
    crowding_risk as _crowding_risk,
    transition_state as _transition_lifecycle_state,
)
from app.services.point_in_time import is_available_at, normalize_explicit_timestamp


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
_THEME_PIT_CONTRACT = "alis_theme_snapshot_pit_v1"


def _theme_pit_marker(cutoff: str, *, rows: int, sessions: list[str]) -> dict[str, Any]:
    return {
        "contract": _THEME_PIT_CONTRACT,
        "source": "dc_stock_daily_snapshot_version",
        "source_observation_cutoff": cutoff,
        "source_rows": int(rows),
        "source_sessions": list(sessions),
    }


def _theme_snapshot_visible_at(
    payload: Any,
    cutoff: str,
    *,
    row: dict[str, Any] | None = None,
) -> bool:
    """Accept only a derived snapshot whose source and row clocks are visible."""

    if not isinstance(payload, dict):
        return False
    marker = payload.get("point_in_time")
    if not isinstance(marker, dict) or marker.get("contract") != _THEME_PIT_CONTRACT:
        return False
    source_cutoff = normalize_explicit_timestamp(marker.get("source_observation_cutoff"))
    boundary = normalize_explicit_timestamp(cutoff)
    if not (
        source_cutoff
        and boundary
        and is_available_at(source_cutoff, boundary, require_explicit_offset=True)
    ):
        return False
    # ``point_in_time`` proves the inputs used by the derivation.  The
    # materialized row still has its own observation, creation and update
    # clocks; accepting a row written after the requested cutoff would turn a
    # current recomputation into historical evidence.
    if row is None:
        return True
    return all(
        (clock := normalize_explicit_timestamp(row.get(field)))
        and is_available_at(clock, boundary, require_explicit_offset=True)
        for field in ("available_time", "created_at", "updated_at")
    )


def _theme_mapping_visible_at(row: dict[str, Any], cutoff: str) -> bool:
    """Validate the mutable mapping projection's write clock for PIT reads.

    Theme mappings are keyed by ``(as_of_date, theme_code, ts_code)`` and are
    updated in place.  The snapshot envelope alone therefore cannot prove
    that a company association existed at an earlier cutoff.
    """
    if not cutoff:
        return True
    updated = normalize_explicit_timestamp(row.get("updated_at"))
    return bool(updated and is_available_at(updated, cutoff, require_explicit_offset=True))


def _decode_snapshot_version(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.loads(item.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    row = dict(payload)
    row["ts_code"] = str(item.get("ts_code") or row.get("ts_code") or "")
    row["trade_date"] = str(item.get("trade_date") or row.get("trade_date") or "").replace("-", "")[:8]
    return row


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _pit_theme_inputs(
    c: Any,
    *,
    cutoff: str,
    cutoff_date: str,
    lookback: int,
) -> dict[str, Any]:
    """Load bounded immutable market inputs for a historical theme materialization."""

    calendar_rows = c.execute(
        """SELECT cal_date FROM dc_trade_calendar
           WHERE is_open=1 AND cal_date<=?
           ORDER BY cal_date DESC LIMIT ?""",
        (cutoff_date, max(5, min(int(lookback), 60))),
    ).fetchall()
    calendar_dates = sorted({
        str(dict(row).get("cal_date") or "").replace("-", "")[:8]
        for row in calendar_rows
        if len(str(dict(row).get("cal_date") or "").replace("-", "")[:8]) == 8
    })
    if len(calendar_dates) < 3:
        return {"ok": False, "status": "insufficient_data", "reason": "fewer_than_three_market_sessions"}
    versions = load_snapshot_versions(
        c,
        cutoff=cutoff,
        trade_date_start=calendar_dates[0],
        trade_date_end=calendar_dates[-1],
    )
    if versions is None:
        return {
            "ok": False,
            "status": "blocked_point_in_time",
            "reason": "snapshot_version_schema_unavailable",
            "contract": _THEME_PIT_CONTRACT,
        }
    visible_rows = [
        row for item in versions
        if (row := _decode_snapshot_version(dict(item))) is not None
    ]
    rows_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in visible_rows:
        if row.get("trade_date") in calendar_dates:
            rows_by_date.setdefault(str(row["trade_date"]), []).append(row)
    missing_dates = [date for date in calendar_dates if not rows_by_date.get(date)]
    if missing_dates:
        return {
            "ok": False,
            "status": "blocked_point_in_time",
            "reason": "snapshot_versions_missing_dates",
            "missing_dates": missing_dates,
            "contract": _THEME_PIT_CONTRACT,
        }
    stock_count_row = c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()
    stock_count = int(dict(stock_count_row).get("cnt") or 0) if stock_count_row else 0
    minimum_rows = max(1, int(ceil(stock_count * .80))) if stock_count else 1
    undercovered = {
        date: len(rows_by_date[date])
        for date in calendar_dates
        if len(rows_by_date[date]) < minimum_rows
    }
    if undercovered:
        return {
            "ok": False,
            "status": "blocked_point_in_time",
            "reason": "snapshot_versions_incomplete",
            "minimum_rows": minimum_rows,
            "undercovered": undercovered,
            "contract": _THEME_PIT_CONTRACT,
        }

    aggregates_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in visible_rows:
        industry = str(row.get("industry") or "").strip()
        trade_date = str(row.get("trade_date") or "")
        if not industry or trade_date not in calendar_dates:
            continue
        key = (industry, trade_date)
        aggregate = aggregates_by_key.setdefault(key, {
            "industry": industry,
            "trade_date": trade_date,
            "company_count": 0,
            "change_sum": 0.0,
            "change_count": 0,
            "positive_count": 0,
            "turnover": 0.0,
        })
        aggregate["company_count"] += 1
        change = _number(row.get("change_pct"))
        if change is not None:
            aggregate["change_sum"] += change
            aggregate["change_count"] += 1
            aggregate["positive_count"] += int(change > 0)
        turnover = _number(row.get("turnover"))
        if turnover is not None:
            aggregate["turnover"] += turnover
    aggregates = []
    for item in sorted(aggregates_by_key.values(), key=lambda value: (value["industry"], value["trade_date"])):
        count = max(1, int(item["company_count"]))
        aggregates.append({
            "industry": item["industry"],
            "trade_date": item["trade_date"],
            "company_count": item["company_count"],
            "average_change_pct": item["change_sum"] / item["change_count"] if item["change_count"] else None,
            "breadth": item["positive_count"] / count,
            "turnover": item["turnover"],
        })
    latest_date = calendar_dates[-1]
    latest_companies = [
        {
            key: row.get(key)
            for key in ("ts_code", "name", "industry", "change_pct", "turnover", "total_mv", "pe_ttm", "pb")
        }
        for row in rows_by_date[latest_date]
        if str(row.get("industry") or "").strip()
    ]
    state_candidates = [dict(row) for row in c.execute(
        """SELECT industry_name,state_json,evidence_json,as_of_date,available_time,created_at,updated_at,model_version
             FROM dc_industry_state_snapshot WHERE as_of_date<=?
             ORDER BY industry_name,as_of_date DESC""",
        (latest_date,),
    ).fetchall()]
    state_rows: list[dict[str, Any]] = []
    seen_industries: set[str] = set()
    for row in state_candidates:
        industry = str(row.get("industry_name") or "").strip()
        available = normalize_explicit_timestamp(row.get("available_time"))
        if not industry or industry in seen_industries or not available:
            continue
        created = normalize_explicit_timestamp(row.get("created_at"))
        updated = normalize_explicit_timestamp(row.get("updated_at"))
        if not (
            is_available_at(available, cutoff, require_explicit_offset=True)
            and created
            and updated
            and is_available_at(created, cutoff, require_explicit_offset=True)
            and is_available_at(updated, cutoff, require_explicit_offset=True)
        ):
            continue
        seen_industries.add(industry)
        state_rows.append(row)
    return {
        "ok": True,
        "dates": calendar_dates,
        "aggregates": aggregates,
        "latest_companies": latest_companies,
        "state_rows": state_rows,
        "point_in_time": _theme_pit_marker(cutoff, rows=len(visible_rows), sessions=calendar_dates),
    }


def _theme_query_cutoff(value: str | None, *, default_now: bool = False) -> str:
    """Normalize a theme query boundary; date-only input means end of day."""
    text = str(value or "").strip()
    if not text and default_now:
        return normalize_explicit_timestamp(datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds"))
    normalized = normalize_explicit_timestamp(text)
    if normalized:
        return normalized
    compact = text.replace("-", "")[:8]
    if len(compact) == 8 and compact.isdigit():
        return normalize_explicit_timestamp(
            f"{compact[:4]}-{compact[4:6]}-{compact[6:]}T23:59:59+08:00"
        )
    return ""


def _latest_complete_theme_trade_date(c: Any, *, cutoff_date: str) -> str:
    """Return the newest market session with sufficient normalized coverage.

    A current-time materialization may run before the next trading session has
    a complete market snapshot (including weekends).  It must derive from the
    latest complete observed session instead of treating that absence as a
    reason to hide every previously valid current theme.
    """
    stock_count_row = c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()
    stock_count = int(dict(stock_count_row).get("cnt") or 0) if stock_count_row else 0
    minimum_rows = max(1, int(ceil(stock_count * .80))) if stock_count else 1
    row = c.execute(
        """SELECT trade_date
             FROM dc_stock_daily_snapshot
             WHERE trade_date<=?
             GROUP BY trade_date
             HAVING COUNT(DISTINCT ts_code)>=?
             ORDER BY trade_date DESC LIMIT 1""",
        (cutoff_date, minimum_rows),
    ).fetchone()
    return str(dict(row).get("trade_date") or "") if row else ""



def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _lifecycle(
    *,
    score: float,
    base_score: float,
    momentum: float,
    breadth: float,
    crowding: float,
    price_confirmation: float,
    persistence: float = 0.0,
    breadth_slope: float = 0.0,
    score_slope: float = 0.0,
) -> str:
    """Compatibility wrapper around the v3 observable label contract.

    ``crowding`` is passed only as an additional maturity observation for old
    callers. It is never a required class-defining condition when real
    persistence/breadth evidence is available.
    """
    return _candidate_lifecycle_state(
        base_score=base_score,
        momentum=momentum,
        breadth=breadth,
        persistence=max(float(persistence), 1.0 if float(crowding) >= .60 else 0.0),
        price_confirmation=price_confirmation,
        breadth_slope=breadth_slope,
        score_slope=score_slope,
    )


def materialize_theme_snapshots(*, as_of_time: str | None = None, lookback: int = 20) -> dict[str, Any]:
    cutoff = _theme_query_cutoff(as_of_time, default_now=True)
    cutoff_date = cutoff[:10].replace("-", "")
    with _conn() as c:
        # Historical callers deliberately pin their own cutoff.  Only a
        # current refresh may fall back from a non-session/incomplete day to
        # the newest complete observed market session.
        if not as_of_time:
            complete_date = _latest_complete_theme_trade_date(c, cutoff_date=cutoff_date)
            if not complete_date:
                return {
                    "ok": False, "status": "insufficient_data",
                    "reason": "no_complete_market_snapshot_for_current_theme",
                    "contract": _THEME_PIT_CONTRACT,
                }
            cutoff_date = complete_date
        pit = _pit_theme_inputs(
            c, cutoff=cutoff, cutoff_date=cutoff_date, lookback=lookback,
        )
        if not pit.get("ok"):
            return pit
        dates = list(pit["dates"])
        aggregates = list(pit["aggregates"])
        state_rows = list(pit["state_rows"])
        latest_companies = list(pit["latest_companies"])
        point_in_time = dict(pit["point_in_time"])

        by_industry: dict[str, list[dict[str, Any]]] = {}
        for row in aggregates:
            by_industry.setdefault(str(row.get("industry") or ""), []).append(row)
        companies_by_industry: dict[str, list[dict[str, Any]]] = {}
        for row in latest_companies:
            companies_by_industry.setdefault(str(row.get("industry") or ""), []).append(row)
        states: dict[str, dict[str, Any]] = {}
        for row in state_rows:
            try:
                row["state"] = json.loads(row.get("state_json") or "{}")
                row["evidence"] = json.loads(row.get("evidence_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["state"], row["evidence"] = {}, {}
            states[str(row.get("industry_name") or "")] = row

        candidates: list[dict[str, Any]] = []
        raw_by_name: dict[str, dict[str, Any]] = {}
        for industry, sessions in by_industry.items():
            companies = companies_by_industry.get(industry) or []
            if len(sessions) < 3 or len(companies) < 5:
                continue
            changes = [float(item.get("average_change_pct") or 0) / 100.0 for item in sessions]
            breadth_values = [float(item.get("breadth") or 0) for item in sessions]
            turnovers = [float(item.get("turnover") or 0) for item in sessions]
            recent_n = min(5, len(changes))
            momentum = sum(changes[-recent_n:])
            prior = sum(changes[:-recent_n]) / max(1, len(changes) - recent_n)
            price_confirmation = _clip(.5 + (momentum - prior * recent_n) * 5)
            breadth = _clip(sum(breadth_values[-recent_n:]) / recent_n)
            persistence = _clip(sum(1 for value in changes[-recent_n:] if value > 0) / recent_n)
            average_turnover = sum(turnovers[:-recent_n]) / max(1, len(turnovers) - recent_n) if len(turnovers) > recent_n else sum(turnovers) / len(turnovers)
            liquidity = _clip((sum(turnovers[-recent_n:]) / recent_n) / max(average_turnover, 1e-9) / 2)
            company_turnover = sorted((float(item.get("turnover") or 0) for item in companies), reverse=True)
            crowding = _clip(sum(company_turnover[:max(1, len(company_turnover) // 10)]) / max(sum(company_turnover), 1e-9))
            state = (states.get(industry) or {}).get("state") or {}
            fundamental = _clip(float(state.get("score") or 0) / 100.0) if state.get("status") == "ready" else 0.0
            # A tradable short-term narrative is independently confirmed by
            # price, breadth and capital persistence. Fundamentals are useful
            # corroboration, but are not a prerequisite for a new story.
            narrative_strength = _clip((price_confirmation + breadth + persistence + liquidity) / 4)
            source_types = int(price_confirmation >= .55) + int(breadth >= .5) + int((persistence + liquidity) / 2 >= .5) + int(fundamental > 0)
            item = {
                "theme": f"{industry}景气",
                "narrative_strength": narrative_strength,
                "price_confirmation": price_confirmation,
                "breadth": breadth,
                "capital_persistence": (persistence + liquidity) / 2,
                "fundamental_verifiability": fundamental,
                "catalyst_duration": persistence,
                "source_policy_credibility": 1.0 if source_types >= 3 else .75 if source_types >= 2 else .5,
                "crowding": crowding,
                "source_types": source_types,
                "related_instruments": len(companies),
                "sessions": len(sessions),
                "point_in_time": point_in_time,
            }
            candidates.append(item)
            raw_by_name[item["theme"]] = {
                "industry": industry,
                "companies": companies,
                "momentum": momentum,
                "breadth": breadth,
                "crowding": crowding,
                "persistence": (persistence + liquidity) / 2,
                "state": state,
            }

        scored = score_theme_narratives(candidates)
        now = datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")
        written = mappings = 0
        for item in scored:
            raw = raw_by_name[item["theme"]]
            candidate_lifecycle = _lifecycle(
                score=float(item["score"]),
                base_score=float(item.get("base_score") or 0.0),
                momentum=float(raw["momentum"]),
                breadth=float(raw["breadth"]),
                crowding=float(raw["crowding"]),
                price_confirmation=float(
                    (item.get("components") or {}).get("price_confirmation")
                    or 0.0
                ),
                persistence=float(raw.get("persistence") or 0.0),
            )
            theme_code = f"industry:{raw['industry']}"
            prior_rows = c.execute(
                """SELECT lifecycle,payload_json FROM dc_theme_snapshot
                   WHERE theme_code=? AND as_of_date<? ORDER BY as_of_date DESC LIMIT 12""",
                (theme_code, dates[-1]),
            ).fetchall()
            prior = [dict(row) for row in prior_rows]
            previous_lifecycle = str(prior[0].get("lifecycle") or "") if prior else None
            previous_age = 0
            for row in prior:
                if str(row.get("lifecycle") or "") != previous_lifecycle:
                    break
                previous_age += 1
            candidate_streak = 1
            for row in prior:
                if str(row.get("lifecycle") or "") != candidate_lifecycle:
                    break
                candidate_streak += 1
            lifecycle, transition_meta = _transition_lifecycle_state(
                previous_lifecycle,
                candidate_lifecycle,
                previous_age=previous_age,
                candidate_streak=candidate_streak,
            )
            item["lifecycle"] = lifecycle
            crowding_observation = _crowding_risk(
                top_decile_concentration=float(raw["crowding"]),
                company_count=len(raw["companies"]),
            )
            item["lifecycle_contract"] = {
                **_lifecycle_contract_payload(),
                "observations": {
                    "score_after_crowding_penalty": round(float(item["score"]), 8),
                    "base_score_before_crowding_penalty": round(float(item.get("base_score") or 0.0), 8),
                    "momentum_5_session": round(float(raw["momentum"]), 8),
                    "breadth_5_session": round(float(raw["breadth"]), 8),
                    "persistence_5_session": round(float(raw.get("persistence") or 0.0), 8),
                    "turnover_top_decile_concentration": round(float(raw["crowding"]), 8),
                    "crowding_risk": crowding_observation,
                    "candidate_lifecycle": candidate_lifecycle,
                    "state_age": previous_age + 1 if lifecycle == previous_lifecycle else 1,
                    "transition": transition_meta,
                },
            }
            # A confirmed score is still not buyable once price has rolled
            # over; this explicitly retires old stories such as a faded CPO.
            item["promotable"] = bool(
                item["promotable"] and item["lifecycle"] != "衰退"
            )
            item["state"] = (
                "confirmed"
                if item["promotable"]
                else "fading"
                if item["lifecycle"] == "衰退"
                else "watch"
            )
            item["knowledge_graph"] = {"theme": item["theme"], "industry": raw["industry"], "company_count": len(raw["companies"]), "evidence_types": ["price_volume"] + (["company_financial_aggregate"] if raw["state"].get("status") == "ready" else [])}
            c.execute(
                """INSERT INTO dc_theme_snapshot
                   (as_of_date,theme_code,theme_name,lifecycle,score,promotable,payload_json,model_version,available_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code) DO UPDATE SET
                   theme_name=excluded.theme_name,lifecycle=excluded.lifecycle,score=excluded.score,
                   promotable=excluded.promotable,payload_json=excluded.payload_json,
                   model_version=excluded.model_version,available_time=excluded.available_time,updated_at=excluded.updated_at""",
                (dates[-1], theme_code, item["theme"], item["lifecycle"], item["score"], 1 if item["promotable"] else 0,
                 json.dumps(item, ensure_ascii=False), "industry_price_fundamental_theme_v3", cutoff or now, now, now),
            )
            written += 1
            max_turnover = max((float(company.get("turnover") or 0) for company in raw["companies"]), default=1.0)
            for company in sorted(raw["companies"], key=lambda value: float(value.get("turnover") or 0), reverse=True)[:50]:
                leadership = _clip(float(company.get("turnover") or 0) / max(max_turnover, 1e-9))
                exposure = round((.6 + .4 * leadership) * 100, 4)
                evidence = {"industry_membership": raw["industry"], "price_leadership": leadership, "trade_date": dates[-1], "source": "dc_stock_daily_snapshot_version", "point_in_time": point_in_time}
                c.execute(
                    """INSERT INTO dc_theme_company_mapping(as_of_date,theme_code,ts_code,exposure_score,evidence_json,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(as_of_date,theme_code,ts_code) DO UPDATE SET
                       exposure_score=excluded.exposure_score,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (dates[-1], theme_code, company.get("ts_code"), exposure, json.dumps(evidence, ensure_ascii=False), now),
                )
                mappings += 1
    return {"ok": True, "as_of_date": dates[-1], "themes_written": written, "company_mappings_written": mappings, "promotable": sum(1 for item in scored if item["promotable"]), "model_version": "industry_price_fundamental_theme_v3"}


def materialize_theme_snapshot_history(*, end_date: str = "", target_sessions: int = 250, limit_dates: int = 20) -> dict[str, Any]:
    target = max(1, min(int(target_sessions or 250), 2400))
    limit = max(1, min(int(limit_dates or 20), 250))
    cutoff = str(end_date or "").replace("-", "")[:8]
    with _conn() as c:
        if not cutoff:
            row = c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
            cutoff = str(dict(row).get("value") or "") if row else ""
        usable_row = c.execute(
            """
            SELECT trade_date value
            FROM dc_stock_daily_snapshot
            WHERE trade_date<=?
            GROUP BY trade_date
            HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) > 0
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        usable_cutoff = str(dict(usable_row).get("value") or cutoff) if usable_row else cutoff
        rows = c.execute(
            """
            SELECT cal_date FROM dc_trade_calendar
            WHERE is_open=1 AND cal_date<=?
            ORDER BY cal_date DESC LIMIT ?
            """,
            (usable_cutoff, target),
        ).fetchall()
        target_dates = [str(dict(row).get("cal_date") or "") for row in rows if dict(row).get("cal_date")]
        placeholders = ",".join("?" for _ in target_dates)
        existing_rows = c.execute(
            f"""SELECT as_of_date,payload_json,model_version
                FROM dc_theme_snapshot
                WHERE as_of_date IN ({placeholders})
                  AND LEFT(theme_code,9)='industry:'""",
            tuple(target_dates),
        ).fetchall() if target_dates else []
        existing = {
            str(dict(row).get("as_of_date") or "")
            for row in existing_rows
        }
        quality_by_date: dict[str, list[bool]] = {}
        for row in existing_rows:
            item = dict(row)
            payload = item.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            components = (
                payload.get("components")
                if isinstance(payload, dict)
                and isinstance(payload.get("components"), dict)
                else {}
            )
            evidence = (
                payload.get("evidence")
                if isinstance(payload, dict)
                and isinstance(payload.get("evidence"), dict)
                else {}
            )
            degenerate = (
                float(components.get("breadth") or 0.0) == 0.0
                and float(components.get("capital_persistence") or 0.0) == 0.0
                and int(evidence.get("source_types") or 0) == 0
            )
            contract_version = str(
                (payload.get("lifecycle_contract") or {}).get("schema_version")
                if isinstance(payload, dict)
                else ""
            )
            quality_by_date.setdefault(str(item.get("as_of_date") or ""), []).append(
                degenerate or contract_version != NARRATIVE_LIFECYCLE_SCHEMA_VERSION
            )
    missing = sorted(
        trade_date for trade_date in target_dates if trade_date not in existing
    )
    invalid = sorted(
        trade_date
        for trade_date, flags in quality_by_date.items()
        if flags and sum(flags) / len(flags) >= .80
    )
    pending = sorted(set(missing) | set(invalid))
    batch = pending[:limit]
    results = []
    for trade_date in batch:
        try:
            results.append(materialize_theme_snapshots(as_of_time=f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T23:59:59", lookback=60))
        except Exception as exc:
            results.append({"ok": False, "as_of_date": trade_date, "error": str(exc)[:300]})
    written_dates = [str(item.get("as_of_date") or "") for item in results if item.get("ok")]
    return {
        "ok": all(bool(item.get("ok")) for item in results) if results else True,
        "target_sessions": target,
        "requested_end_date": cutoff,
        "effective_end_date": usable_cutoff,
        "existing_sessions": len(existing),
        "missing_sessions": len(missing),
        "invalid_sessions": len(invalid),
        "invalid_dates": invalid[:20],
        "processed_sessions": len(batch),
        "written_sessions": len(written_dates),
        "written_dates": written_dates,
        "remaining_sessions": max(0, len(pending) - len(written_dates)),
        "results": results[:10],
    }


def list_themes(
    *,
    as_of_time: str | None = None,
    promotable_only: bool = False,
    limit: int = 100,
    include_companies: bool = True,
    compact: bool = False,
    include_company_counts: bool | None = None,
) -> dict[str, Any]:
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    visibility_cutoff = _theme_query_cutoff(as_of_time, default_now=not bool(as_of_time))
    if as_of_time and not visibility_cutoff:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "invalid_theme_observation_cutoff",
            "as_of_time": as_of_time,
            "items": [],
            "count": 0,
        }
    if include_company_counts is None:
        include_company_counts = bool(compact)
    mapping_cutoff = visibility_cutoff if as_of_time else ""
    with _conn() as c:
        clauses = []
        params: list[Any] = []
        if cutoff:
            clauses.append("as_of_date<=?"); params.append(cutoff)
        if promotable_only:
            clauses.append("promotable=1")
        # The portfolio allocator only needs the newest visible snapshot per
        # theme.  Reading every historic JSON payload (tens of thousands of
        # rows in production) made an otherwise small plan calculation exceed
        # the statement timeout.  Filter the row clocks in SQL and choose one
        # current candidate per theme; Python still validates the embedded PIT
        # source marker below, so this is a bounded transport optimisation and
        # not a relaxation of the theme point-in-time contract.
        if visibility_cutoff:
            clauses.extend([
                "available_time<>''", "created_at<>''", "updated_at<>''",
                "available_time<=?", "created_at<=?", "updated_at<=?",
            ])
            params.extend([visibility_cutoff, visibility_cutoff, visibility_cutoff])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        if bool(getattr(c, "_is_postgres", False)):
            candidate_sql = f"""SELECT DISTINCT ON (theme_code) *
                                  FROM dc_theme_snapshot s {where}
                                 ORDER BY theme_code,as_of_date DESC,
                                          promotable DESC,score DESC"""
        else:
            candidate_sql = f"""WITH ranked AS (
                                  SELECT s.*,ROW_NUMBER() OVER (
                                      PARTITION BY theme_code
                                      ORDER BY as_of_date DESC,promotable DESC,score DESC
                                  ) AS row_rank
                                    FROM dc_theme_snapshot s {where}
                                ) SELECT * FROM ranked WHERE row_rank=1"""
        candidates = [dict(row) for row in c.execute(candidate_sql, tuple(params)).fetchall()]
        visible_by_theme: dict[str, dict[str, Any]] = {}
        for row in candidates:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not _theme_snapshot_visible_at(payload, visibility_cutoff, row=row):
                continue
            theme_code = str(row.get("theme_code") or "")
            if not theme_code or theme_code in visible_by_theme:
                continue
            row["_decoded_payload"] = payload
            visible_by_theme[theme_code] = row
        rows = sorted(
            visible_by_theme.values(),
            key=lambda item: (
                -int(item.get("promotable") or 0),
                -float(item.get("score") or 0),
                str(item.get("as_of_date") or ""),
            ),
        )[:max(1, min(int(limit), 500))]
        if as_of_time and not rows:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "no_theme_snapshot_visible_at_cutoff",
                "as_of_time": as_of_time,
                "items": [],
                "count": 0,
                "candidate_rows": len(candidates),
                "point_in_time_contract": _THEME_PIT_CONTRACT,
            }
        company_counts: dict[tuple[str, str], int] = {}
        if rows and include_company_counts:
            count_where = " OR ".join("(as_of_date=? AND theme_code=?)" for _ in rows)
            count_params = tuple(value for row in rows for value in (row.get("as_of_date"), row.get("theme_code")))
            mapping_rows = [dict(item) for item in c.execute(
                f"SELECT as_of_date,theme_code,updated_at FROM dc_theme_company_mapping WHERE {count_where}",
                count_params,
            ).fetchall()]
            for mapping in mapping_rows:
                if not _theme_mapping_visible_at(mapping, mapping_cutoff):
                    continue
                key = (str(mapping.get("as_of_date") or ""), str(mapping.get("theme_code") or ""))
                company_counts[key] = company_counts.get(key, 0) + 1
        for row in rows:
            row["payload"] = row.pop("_decoded_payload", None) or json.loads(row.pop("payload_json") or "{}")
            row["company_count"] = company_counts.get(
                (str(row.get("as_of_date") or ""), str(row.get("theme_code") or "")),
                0,
            )
            if compact:
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                row["payload"] = {"knowledge_graph": payload.get("knowledge_graph") or {}}
            mappings = []
            if include_companies:
                mapping_rows = [dict(item) for item in c.execute(
                    """SELECT ts_code,exposure_score,evidence_json,updated_at FROM dc_theme_company_mapping
                       WHERE as_of_date=? AND theme_code=? ORDER BY exposure_score DESC LIMIT 50""",
                    (row["as_of_date"], row["theme_code"]),
                ).fetchall()]
                mappings = [item for item in mapping_rows if _theme_mapping_visible_at(item, mapping_cutoff)]
                for mapping in mappings:
                    mapping.pop("updated_at", None)
                    mapping["evidence"] = json.loads(mapping.pop("evidence_json") or "{}")
            if include_companies:
                row["companies"] = mappings
    return {"ok": True, "items": rows, "count": len(rows), "as_of_time": as_of_time}


def get_theme(theme_code: str, *, as_of_date: str = "") -> dict[str, Any] | None:
    """Return one theme snapshot with its full company evidence on demand."""
    code = str(theme_code or "").strip()
    cutoff = str(as_of_date or "")[:10].replace("-", "")
    visibility_cutoff = _theme_query_cutoff(as_of_date, default_now=not bool(as_of_date))
    with _conn() as c:
        if cutoff:
            candidates = c.execute(
                "SELECT * FROM dc_theme_snapshot WHERE theme_code=? AND as_of_date<=? ORDER BY as_of_date DESC",
                (code, cutoff),
            ).fetchall()
        else:
            candidates = c.execute(
                "SELECT * FROM dc_theme_snapshot WHERE theme_code=? ORDER BY as_of_date DESC",
                (code,),
            ).fetchall()
        row = None
        for candidate in candidates:
            candidate_dict = dict(candidate)
            try:
                payload = json.loads(candidate_dict.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if _theme_snapshot_visible_at(payload, visibility_cutoff, row=candidate_dict):
                row = candidate_dict
                row["_decoded_payload"] = payload
                break
        if not row:
            return None
        item = dict(row)
        item["payload"] = item.pop("_decoded_payload", None) or json.loads(item.pop("payload_json") or "{}")
        mapping_rows = [dict(mapping) for mapping in c.execute(
            "SELECT ts_code,exposure_score,evidence_json,updated_at FROM dc_theme_company_mapping WHERE as_of_date=? AND theme_code=? ORDER BY exposure_score DESC LIMIT 200",
            (item.get("as_of_date"), code),
        ).fetchall()]
        mapping_cutoff = visibility_cutoff if as_of_date else ""
        mappings = [mapping for mapping in mapping_rows if _theme_mapping_visible_at(mapping, mapping_cutoff)]
    for mapping in mappings:
        mapping.pop("updated_at", None)
        mapping["evidence"] = json.loads(mapping.pop("evidence_json") or "{}")
    item["company_count"] = len(mappings)
    item["companies"] = mappings
    return item


def best_theme_for_stock(ts_code: str, *, as_of_time: str | None = None) -> dict[str, Any] | None:
    code = str(ts_code or "").strip().upper()
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    visibility_cutoff = _theme_query_cutoff(as_of_time, default_now=not bool(as_of_time))
    with _conn() as c:
        clauses = ["m.ts_code=?", "s.promotable=1"]
        params: list[Any] = [code]
        if cutoff:
            clauses.append("m.as_of_date<=?"); params.append(cutoff)
        candidates = c.execute(
            f"""SELECT s.*,m.exposure_score,m.evidence_json,m.updated_at AS mapping_updated_at FROM dc_theme_company_mapping m
                JOIN dc_theme_snapshot s ON s.as_of_date=m.as_of_date AND s.theme_code=m.theme_code
                WHERE {' AND '.join(clauses)} ORDER BY m.as_of_date DESC,s.score DESC""", tuple(params),
        ).fetchall()
        row = None
        for candidate in candidates:
            candidate_dict = dict(candidate)
            try:
                payload = json.loads(candidate_dict.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if (
                _theme_snapshot_visible_at(payload, visibility_cutoff, row=candidate_dict)
                and _theme_mapping_visible_at(
                    {"updated_at": candidate_dict.get("mapping_updated_at")},
                    visibility_cutoff if as_of_time else "",
                )
            ):
                row = candidate_dict
                row["_decoded_payload"] = payload
                break
    if not row:
        return None
    item = dict(row)
    item["payload"] = item.pop("_decoded_payload", None) or json.loads(item.pop("payload_json") or "{}")
    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
    item.pop("mapping_updated_at", None)
    return item

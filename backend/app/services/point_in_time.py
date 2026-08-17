from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo


PIT_SCHEMA_VERSION = "point_in_time_contract_v2_observation_clock"
SIGNAL_CUTOFF_TIME = "15:00:00"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def normalize_timestamp(
    value: Any,
    *,
    fallback: str = "",
    require_explicit_offset: bool = False,
) -> str:
    """Return an explicit Asia/Shanghai timestamp or an empty fail-closed value.

    Market-data writers persist an actual observation timestamp rather than a
    market-date placeholder.  Consumers must compare instants, not ISO string
    prefixes: a timezone-qualified ``15:00:00+08:00`` used to compare as later
    than the old timezone-naive ``15:00:00`` cutoff.
    """

    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return str(fallback or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return str(fallback or "")
    if parsed.tzinfo is None:
        if require_explicit_offset:
            return str(fallback or "")
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(BUSINESS_TZ).isoformat(timespec="seconds")


def normalize_explicit_timestamp(value: Any, *, fallback: str = "") -> str:
    """Normalize only a timestamp that carries an explicit timezone offset.

    ``normalize_timestamp`` remains intentionally permissive for legacy
    research fixtures and display-only compatibility paths.  Formal decision
    evidence must use this stricter variant: interpreting a naive provider
    string as Asia/Shanghai could backdate a late response into a frozen PIT
    contract.
    """

    normalized = normalize_timestamp(
        value,
        fallback="",
        require_explicit_offset=True,
    )
    if normalized:
        return normalized
    fallback_text = str(fallback or "").strip()
    if not fallback_text:
        return ""
    return normalize_timestamp(fallback_text, require_explicit_offset=True)


def _timestamp(value: Any) -> datetime | None:
    normalized = normalize_timestamp(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:  # defensive: normalize_timestamp already validated it
        return None


def signal_available_time(trade_date: str) -> str:
    value = str(trade_date or "").strip()[:10]
    compact = value.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"无效交易日: {trade_date}")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}T{SIGNAL_CUTOFF_TIME}+08:00"


def is_available_at(
    value: Any,
    cutoff: str,
    *,
    require_explicit_offset: bool = False,
) -> bool:
    if require_explicit_offset:
        observed_text = normalize_explicit_timestamp(value)
        boundary_text = normalize_explicit_timestamp(cutoff)
        if not observed_text or not boundary_text:
            return False
        observed = datetime.fromisoformat(observed_text)
        boundary = datetime.fromisoformat(boundary_text)
    else:
        observed = _timestamp(value)
        boundary = _timestamp(cutoff)
    return observed is not None and boundary is not None and observed <= boundary


def observed_at(record: dict[str, Any]) -> str:
    """Return actual observation time; legacy `available_time` is compatibility only."""

    return str(record.get("observed_at") or record.get("available_time") or "").strip()


def filter_available_records(
    records: Iterable[dict[str, Any]], *, cutoff: str, source: str,
    strict_missing: bool = True, require_explicit_offset: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter evidence under a cutoff, optionally requiring explicit offsets."""
    accepted: list[dict[str, Any]] = []
    future = 0
    missing = 0
    max_available_at: datetime | None = None
    for item in records:
        available_time = observed_at(item)
        if not available_time:
            missing += 1
            if not strict_missing:
                accepted.append(item)
            continue
        if require_explicit_offset:
            explicit_available = normalize_explicit_timestamp(available_time)
            parsed_available = datetime.fromisoformat(explicit_available) if explicit_available else None
        else:
            parsed_available = _timestamp(available_time)
        if parsed_available is None:
            missing += 1
            if not strict_missing:
                accepted.append(item)
            continue
        if max_available_at is None or parsed_available > max_available_at:
            max_available_at = parsed_available
        if is_available_at(
            available_time,
            cutoff,
            require_explicit_offset=require_explicit_offset,
        ):
            accepted.append(item)
        else:
            future += 1
    return accepted, {
        "source": source,
        "cutoff": cutoff,
        "read_count": len(accepted),
        "future_excluded_count": future,
        "missing_available_time_count": missing,
        "max_seen_available_time": (
            max_available_at.astimezone(BUSINESS_TZ).isoformat(timespec="seconds")
            if max_available_at is not None else None
        ),
        "strict_missing": strict_missing,
        # An empty required source is not PIT proof.  Required consumers may
        # distinguish a separately classified valid-empty response, but they
        # must never display this read as a successful data-bearing source.
        "passed": bool(accepted) and future == 0 and (missing == 0 or not strict_missing),
    }


def point_in_time_contract(*, cutoff: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_cutoff = normalize_explicit_timestamp(cutoff)
    violations = [item for item in sources if not item.get("passed", True)]
    if not normalized_cutoff:
        violations.append({
            "source": "decision_cutoff",
            "passed": False,
            "reason": "invalid_or_naive_observation_cutoff",
        })
    return {
        "schema_version": PIT_SCHEMA_VERSION,
        "decision_cutoff": normalized_cutoff,
        "decision_clock": "Asia/Shanghai",
        "signal_rule": "T日15:00及以前可得数据",
        "execution_observation_rule": "T+1开盘价仅用于模拟成交，不回流为T日信号特征",
        "sources": sources,
        "passed": not violations,
        "violation_count": len(violations),
        "cutoff_valid": bool(normalized_cutoff),
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
    }

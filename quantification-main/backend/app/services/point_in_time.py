from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


PIT_SCHEMA_VERSION = "point_in_time_contract_v1"
SIGNAL_CUTOFF_TIME = "15:00:00"


def signal_available_time(trade_date: str) -> str:
    value = str(trade_date or "").strip()[:10]
    compact = value.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"无效交易日: {trade_date}")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}T{SIGNAL_CUTOFF_TIME}"


def is_available_at(value: Any, cutoff: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and text <= str(cutoff)


def filter_available_records(
    records: Iterable[dict[str, Any]], *, cutoff: str, source: str, strict_missing: bool = True
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    future = 0
    missing = 0
    max_available_time = ""
    for item in records:
        available_time = str(item.get("available_time") or "").strip()
        if not available_time:
            missing += 1
            if not strict_missing:
                accepted.append(item)
            continue
        max_available_time = max(max_available_time, available_time)
        if available_time <= cutoff:
            accepted.append(item)
        else:
            future += 1
    return accepted, {
        "source": source,
        "cutoff": cutoff,
        "read_count": len(accepted),
        "future_excluded_count": future,
        "missing_available_time_count": missing,
        "max_seen_available_time": max_available_time or None,
        "strict_missing": strict_missing,
        "passed": future == 0 and (missing == 0 or not strict_missing),
    }


def point_in_time_contract(*, cutoff: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    violations = [item for item in sources if not item.get("passed", True)]
    return {
        "schema_version": PIT_SCHEMA_VERSION,
        "decision_cutoff": cutoff,
        "decision_clock": "Asia/Shanghai",
        "signal_rule": "T日15:00及以前可得数据",
        "execution_observation_rule": "T+1开盘价仅用于模拟成交，不回流为T日信号特征",
        "sources": sources,
        "passed": not violations,
        "violation_count": len(violations),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

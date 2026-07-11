from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE, compute_factor_coverage


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _backend_cache_root() -> Path:
    return _repo_root() / "backend" / ".cache"


def _data_root() -> Path:
    return _repo_root() / ".cache" / "data"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _date_files(directory: Path, pattern: str = "*.json", prefix: str = "", suffix: str = "") -> list[str]:
    if not directory.exists():
        return []
    out: list[str] = []
    for path in directory.glob(pattern):
        stem = path.stem
        if prefix and stem.startswith(prefix):
            stem = stem[len(prefix) :]
        if suffix and stem.endswith(suffix):
            stem = stem[: -len(suffix)]
        if len(stem) == 8 and stem.isdigit():
            out.append(stem)
    return sorted(set(out))


def _summarize_dates(dates: list[str]) -> dict[str, Any]:
    return {
        "count": len(dates),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }


def _load_all_in_one_day(path: Path) -> dict[str, Any] | None:
    payload = _load_json(path)
    return payload if isinstance(payload, dict) else None


def _summarize_all_in_one(limit_days: int) -> dict[str, Any]:
    base = _backend_cache_root() / "all_in_one"
    dates = _date_files(base)
    recent = dates[-limit_days:] if limit_days > 0 else dates
    day_summaries: list[dict[str, Any]] = []
    weak_days: list[dict[str, Any]] = []

    for ymd in recent:
        payload = _load_all_in_one_day(base / f"{ymd}.json")
        if not payload:
            continue
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        rows = int((counts or {}).get("symbol_rows") or 0)
        coverage = payload.get("factor_coverage")
        if not isinstance(coverage, dict) or ymd not in coverage:
            data = payload.get("data")
            if isinstance(data, dict):
                coverage = compute_factor_coverage(data)
            else:
                coverage = {}
        day_cov = coverage.get(ymd, {}) if isinstance(coverage, dict) else {}
        if not isinstance(day_cov, dict):
            day_cov = {}
        low_core = {
            key: float(day_cov.get(key, 0) or 0)
            for key in ("close", "volume", "turnover", "float_mv", "total_mv", "pe_ttm", "pb")
            if float(day_cov.get(key, 0) or 0) < 0.8
        }
        item = {
            "date": ymd,
            "symbol_rows": rows,
            "source_counts": counts,
            "core_factor_coverage": {
                key: round(float(day_cov.get(key, 0) or 0), 4)
                for key in ("close", "volume", "turnover", "float_mv", "total_mv", "pe_ttm", "pb")
            },
        }
        day_summaries.append(item)
        if rows <= 0 or low_core:
            weak_days.append({"date": ymd, "symbol_rows": rows, "low_core_factors": low_core})

    return {
        **_summarize_dates(dates),
        "recent_days": day_summaries,
        "weak_days": weak_days,
    }


def build_data_health_report(limit_days: int = 30) -> dict[str, Any]:
    """Return a local, read-only data coverage report."""

    limit_days = max(1, min(int(limit_days or 30), 250))
    backend_cache = _backend_cache_root()
    data_root = _data_root()

    all_in_one = _summarize_all_in_one(limit_days)
    snapshot_dates = _date_files(backend_cache / "tushare_snapshot")
    backtest_snapshot_dates = _date_files(backend_cache / "backtest", pattern="snapshot_*.json", prefix="snapshot_")
    backtest_daily_basic_dates = _date_files(backend_cache / "backtest", pattern="daily_basic_*.json", prefix="daily_basic_")
    local_daily_basic_json = _date_files(data_root / "daily_basic")
    local_daily_basic_csv = _date_files(data_root / "daily_basic", pattern="*.csv")
    local_snapshot_csv = _date_files(data_root / "daily_basic" / "2026", pattern="*.csv")

    available_trade_dates = sorted(
        set(all_in_one.get("recent_days", []) and [x["date"] for x in all_in_one["recent_days"]] or [])
        | set(snapshot_dates)
        | set(backtest_snapshot_dates)
        | set(local_snapshot_csv)
    )

    issues: list[dict[str, Any]] = []
    if all_in_one["count"] <= 0:
        issues.append(
            {
                "level": "error",
                "code": "all_in_one_missing",
                "message": "No all-in-one cache files found. Run POST /api/init before trusting selection/backtest outputs.",
            }
        )
    if not snapshot_dates and not backtest_snapshot_dates and not local_snapshot_csv:
        has_all_in_one = all_in_one["count"] > 0 and any(
            int((day or {}).get("symbol_rows") or 0) > 0 for day in all_in_one.get("recent_days", [])
        )
        if has_all_in_one:
            issues.append(
                {
                    "level": "info",
                    "code": "snapshot_source_cleaned",
                    "message": "Source snapshot files were cleaned after all-in-one merge. Using consolidated cache only.",
                }
            )
        else:
            issues.append(
                {
                    "level": "error",
                    "code": "snapshot_missing",
                    "message": "No local snapshot coverage found. Signal generation and backtest universe coverage are not trustworthy.",
                }
            )
    if not backtest_daily_basic_dates and not local_daily_basic_json and not local_daily_basic_csv:
        has_mv_in_all_in_one = any(
            float(((day or {}).get("core_factor_coverage") or {}).get("total_mv", 0) or 0) >= 0.8
            for day in all_in_one.get("recent_days", [])
        )
        if not has_mv_in_all_in_one:
            issues.append(
                {
                    "level": "warning",
                    "code": "daily_basic_missing",
                    "message": "No local daily_basic cache found. Market-cap and valuation filters may fall back or be skipped.",
                }
            )
    for weak in all_in_one.get("weak_days", [])[:10]:
        issues.append(
            {
                "level": "warning",
                "code": "weak_factor_coverage",
                "message": "Recent all-in-one day has weak row count or core factor coverage.",
                "detail": weak,
            }
        )

    return {
        "ok": not any(x.get("level") == "error" for x in issues),
        "limit_days": limit_days,
        "cache_roots": {
            "backend_cache": str(backend_cache),
            "data_root": str(data_root),
        },
        "trade_dates": _summarize_dates(available_trade_dates),
        "all_in_one": all_in_one,
        "snapshots": {
            "tushare_snapshot": _summarize_dates(snapshot_dates),
            "backtest_snapshot": _summarize_dates(backtest_snapshot_dates),
            "local_snapshot_csv": _summarize_dates(local_snapshot_csv),
        },
        "daily_basic": {
            "backtest_cache": _summarize_dates(backtest_daily_basic_dates),
            "local_json": _summarize_dates(local_daily_basic_json),
            "local_csv": _summarize_dates(local_daily_basic_csv),
        },
        "factor_keys": list(FACTOR_KEYS_FOR_COVERAGE),
        "issues": issues,
    }

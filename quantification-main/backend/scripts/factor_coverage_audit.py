#!/usr/bin/env python
from __future__ import annotations

"""
对 backend/.cache/all_in_one 根目录下的按日 JSON 计算因子覆盖率并输出报告。

用法（在 backend 目录下）：
  python scripts/factor_coverage_audit.py
"""

import json
import sys
from pathlib import Path
from typing import Dict, Tuple


def _ensure_backend_on_path() -> Path:
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    return backend_dir


def _load_per_day_data() -> Dict[str, Dict[str, Dict[str, object]]]:
    _ensure_backend_on_path()
    from app.services.cache_consolidation import _bundle_dir, _normalize_ymd

    bundle_dir = _bundle_dir()
    out: Dict[str, Dict[str, Dict[str, object]]] = {}
    if not bundle_dir.exists():
        return out
    for p in bundle_dir.glob("*.json"):
        # 仅统计根目录下的按日文件：YYYYMMDD.json
        if not (len(p.stem) == 8 and p.stem.isdigit()):
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        trade_date = ""
        if isinstance(payload.get("trade_date"), str):
            trade_date = _normalize_ymd(payload["trade_date"])
        if not trade_date:
            trade_date = _normalize_ymd(p.stem)
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        per_symbol = data.get(trade_date)
        if not isinstance(per_symbol, dict):
            # 有些旧文件可能是 {date: {symbol: row}} 结构，直接取第一个键
            for k, v in data.items():
                if isinstance(v, dict):
                    trade_date = _normalize_ymd(k)
                    per_symbol = v
                    break
        if not trade_date or not isinstance(per_symbol, dict):
            continue
        out[trade_date] = per_symbol
    return out


def _compute_coverage() -> Tuple[Dict[str, Dict[str, float]], Tuple[float, float]]:
    _ensure_backend_on_path()
    from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE, compute_factor_coverage

    per_day = _load_per_day_data()
    coverage = compute_factor_coverage({d: per_symbol for d, per_symbol in per_day.items()})
    # 统计整体最小/平均覆盖率（按所有因子 & 日期）
    total_vals = []
    min_val = 1.0
    for d, cov in coverage.items():
        for k in FACTOR_KEYS_FOR_COVERAGE:
            v = float(cov.get(k, 0.0) or 0.0)
            total_vals.append(v)
            if v < min_val:
                min_val = v
    avg_val = sum(total_vals) / len(total_vals) if total_vals else 1.0
    return coverage, (min_val, avg_val)


def main() -> int:
    coverage, (min_val, avg_val) = _compute_coverage()
    dates = sorted(coverage.keys())
    print(f"[1] 共有交易日 {len(dates)} 天；所有因子覆盖率最小值={min_val:.4f}，平均值={avg_val:.4f}")

    backend_dir = _ensure_backend_on_path()
    report_path = backend_dir / "factor_coverage_report.md"

    lines = [
        "# 因子覆盖率报告",
        "",
        f"交易日总数: {len(dates)}",
        f"全局最小覆盖率: {min_val:.4f}",
        f"全局平均覆盖率: {avg_val:.4f}",
        "",
        "| 日期 | 最小覆盖率 | 覆盖率<1.0 的因子 |",
        "|------|------------|--------------------|",
    ]

    from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE

    for d in dates:
        cov = coverage.get(d, {})
        if not cov:
            lines.append(f"| {d} | 0.0000 | 所有因子缺失 |")
            continue
        min_cov = 1.0
        missing_factors = []
        for k in FACTOR_KEYS_FOR_COVERAGE:
            v = float(cov.get(k, 0.0) or 0.0)
            if v < min_cov:
                min_cov = v
            if v < 1.0:
                missing_factors.append(k)
        if missing_factors:
            mf = ", ".join(missing_factors)
        else:
            mf = ""
        lines.append(f"| {d} | {min_cov:.4f} | {mf} |")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[2] 已生成覆盖率报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


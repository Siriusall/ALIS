#!/usr/bin/env python
from __future__ import annotations

"""
使用 TuShare 为因子覆盖率不足的日期补齐市值/估值类因子，并重建对应 all_in_one/dates/<date>.json。

默认仅分析哪些日期/因子覆盖率不足（dry run），不会真正打 TuShare。
要实际补齐，需要显式传入 --apply 标志。

用法（在 backend 目录下）：
  # 仅查看哪些日期需要补齐（不调用 TuShare）
  python scripts/factor_backfill_tushare.py

  # 对最近 10 个覆盖率不足的交易日尝试补齐并重建合并文件
  python scripts/factor_backfill_tushare.py --apply --max-dates 10

  # 仅处理指定日期
  python scripts/factor_backfill_tushare.py --apply --date 20260226
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple


TARGET_FACTORS = ("float_mv", "total_mv", "pe_ttm", "pb", "turnover_rate")


def _ensure_backend_on_path() -> Path:
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    return backend_dir


def _load_per_day_data() -> Dict[str, Dict[str, Dict[str, object]]]:
    _ensure_backend_on_path()
    from app.services.cache_consolidation import _bundle_dir, _normalize_ymd

    dates_dir = _bundle_dir()
    out: Dict[str, Dict[str, Dict[str, object]]] = {}
    if not dates_dir.exists():
        return out
    for p in dates_dir.glob("*.json"):
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
            for k, v in data.items():
                if isinstance(v, dict):
                    trade_date = _normalize_ymd(k)
                    per_symbol = v
                    break
        if not trade_date or not isinstance(per_symbol, dict):
            continue
        out[trade_date] = per_symbol
    return out


def _compute_target_coverage() -> Dict[str, Dict[str, float]]:
    _ensure_backend_on_path()
    from app.services.cache_consolidation import compute_factor_coverage

    per_day = _load_per_day_data()
    cov = compute_factor_coverage({d: per_symbol for d, per_symbol in per_day.items()})
    # 只保留我们关心的 TARGET_FACTORS
    filtered: Dict[str, Dict[str, float]] = {}
    for d, m in cov.items():
        fm = {}
        for f in TARGET_FACTORS:
            fm[f] = float(m.get(f, 0.0) or 0.0)
        filtered[d] = fm
    return filtered


def _pick_dates_with_low_coverage(
    coverage: Dict[str, Dict[str, float]],
    threshold: float = 1.0,
) -> List[Tuple[str, Dict[str, float]]]:
    items: List[Tuple[str, Dict[str, float]]] = []
    for d, cov in coverage.items():
        if any(v < threshold for v in cov.values()):
            items.append((d, cov))
    items.sort(key=lambda kv: kv[0])
    return items


def _backfill_one_date(trade_date: str) -> None:
    """
    对单个交易日：
    - 调用 TuShare daily_basic（通过 BacktestDataSource），补齐市值/估值类因子缓存；
    - 调用 get_daily_snapshot(trade_date, force_refresh=True)，补齐快照与 daily_basic；
    - 调用 build_consolidated_cache_bundle(trade_date=...) 重建当日合并文件。
    """
    _ensure_backend_on_path()
    from app.services.backtest_data_source import BacktestDataSource
    from app.services.cache_consolidation import build_consolidated_cache_bundle
    from app.services.tushare_snapshot import get_daily_snapshot

    print(f"  [Backfill] {trade_date}: TuShare daily_basic …")
    ds = BacktestDataSource()
    db_map = ds.get_daily_basic_map(trade_date)
    print(f"    daily_basic rows={len(db_map)}")

    print(f"  [Backfill] {trade_date}: TuShare snapshot …")
    snap = get_daily_snapshot(trade_date=trade_date, force_refresh=True, cache_only=False)
    print(f"    snapshot rows={len(snap)}")

    print(f"  [Backfill] {trade_date}: rebuild all_in_one day file …")
    build_consolidated_cache_bundle(trade_date=trade_date)


def main() -> int:
    parser = argparse.ArgumentParser(description="为因子覆盖率不足的日期使用 TuShare 做 backfill。")
    parser.add_argument("--date", help="仅处理指定交易日 YYYYMMDD", default=None)
    parser.add_argument("--max-dates", type=int, default=20, help="最大处理日期数（未指定 date 时生效）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际调用 TuShare 并重建合并文件（默认只打印需要补齐的日期，不做任何修改）",
    )
    args = parser.parse_args()

    coverage = _compute_target_coverage()
    low_items = _pick_dates_with_low_coverage(coverage, threshold=1.0)

    if args.date:
        target_dates = [d for (d, _) in low_items if d == args.date]
    else:
        target_dates = [d for (d, _) in low_items][: args.max_dates]

    print(f"[1] 共有覆盖率不足的交易日 {len(low_items)} 天；本次目标处理 {len(target_dates)} 天。")
    for d, cov in low_items[: min(len(low_items), 10)]:
        print(f"    示例 {d}: " + ", ".join(f"{k}={v:.3f}" for k, v in cov.items()))

    if not target_dates:
        print("[2] 没有需要处理的日期，直接退出。")
        return 0

    if not args.apply:
        print("[2] dry run 模式，仅打印需要补齐的日期（不调用 TuShare）：")
        print("    " + ", ".join(target_dates))
        print("    若要实际 backfill，请增加 --apply 参数。")
        return 0

    print("[2] 开始对以下日期执行 backfill:")
    print("    " + ", ".join(target_dates))
    for d in target_dates:
        try:
            _backfill_one_date(d)
        except Exception as e:  # noqa: BLE001
            print(f"  [Backfill] {d}: 失败 {e}")

    try:
        from app.services.cache_consolidation import cleanup_source_cache_dirs

        cleanup = cleanup_source_cache_dirs()
        print(
            "[3] backfill 完成后已清理源目录："
            f" removed_dirs={cleanup.get('removed_dirs', 0)} "
            f"removed_files={cleanup.get('removed_files', 0)} "
            f"failed={cleanup.get('failed', 0)}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[3] backfill 后清理源目录失败: {e}")
    print("[4] 可根据需要重新运行 factor_coverage_audit.py 查看效果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


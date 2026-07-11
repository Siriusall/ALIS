#!/usr/bin/env python
from __future__ import annotations

"""
审计 .cache 四类原始目录是否已完整合并到 all_in_one/dates。

用法（在 backend 目录下）：
  python scripts/verify_cache_merge.py
  python scripts/verify_cache_merge.py --sample-check
  python scripts/verify_cache_merge.py --delete-sources --force

仅当：
- 四类源目录与 all_in_one/dates 的日期集合一致，且
- 各日期 symbol 行数大致一致
时，才会在 --delete-sources --force 下执行删除源目录操作。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple


def _ensure_backend_on_path() -> Path:
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    return backend_dir


def _load_sources() -> Tuple[Dict[str, Dict[str, Dict[str, object]]], Dict[str, int]]:
    """
    返回：
    - per_date_symbols: {date: {symbol: True}}
    - row_counts: {date: symbol_count}
    """
    _ensure_backend_on_path()
    from app.services.cache_consolidation import (
        _load_backtest_daily_basic_by_date,
        _load_daily_and_compute_factors,
        _load_snapshot_by_date,
        _load_spot_by_date,
    )

    spot = _load_spot_by_date()
    snap = _load_snapshot_by_date()
    back = _load_backtest_daily_basic_by_date()
    daily = _load_daily_and_compute_factors()

    per_date_symbols: Dict[str, Dict[str, object]] = {}
    for d, m in daily.items():
        per_date_symbols.setdefault(d, {}).update({s: True for s in m.keys()})
    for d, m in snap.items():
        per_date_symbols.setdefault(d, {}).update({s: True for s in m.keys()})
    for d, m in back.items():
        per_date_symbols.setdefault(d, {}).update({s: True for s in m.keys()})
    for d, m in spot.items():
        per_date_symbols.setdefault(d, {}).update({s: True for s in m.keys()})

    row_counts = {d: len(sym_map) for d, sym_map in per_date_symbols.items()}
    return per_date_symbols, row_counts


def _load_all_in_one() -> Tuple[Dict[str, int], Dict[str, Path]]:
    """
    返回：
    - row_counts: {date: symbol_rows}
    - files: {date: path}
    """
    _ensure_backend_on_path()
    from app.services.cache_consolidation import _bundle_dir, _normalize_ymd

    dates_dir = _bundle_dir()
    row_counts: Dict[str, int] = {}
    files: Dict[str, Path] = {}
    if not dates_dir.exists():
        return row_counts, files
    for p in dates_dir.glob("*.json"):
        if not (len(p.stem) == 8 and p.stem.isdigit()):
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        trade_date = ""
        if isinstance(payload, dict):
            if isinstance(payload.get("trade_date"), str):
                trade_date = _normalize_ymd(payload["trade_date"])
            if not trade_date:
                trade_date = _normalize_ymd(p.stem)
            data = payload.get("data") or {}
            counts = payload.get("counts") or {}
            symbol_rows = int(counts.get("symbol_rows") or 0)
            if not symbol_rows and isinstance(data, dict) and trade_date in data and isinstance(data[trade_date], dict):
                symbol_rows = len(data[trade_date])
        else:
            continue
        if not trade_date:
            continue
        row_counts[trade_date] = symbol_rows
        files[trade_date] = p
    return row_counts, files


def _print_set_diff(title: str, left: Iterable[str], right: Iterable[str]) -> Tuple[set, set]:
    left_set = set(left)
    right_set = set(right)
    missing = sorted(left_set - right_set)
    extra = sorted(right_set - left_set)
    print(f"\n[{title}]")
    print(f"  left={len(left_set)} right={len(right_set)}")
    if missing:
        print(f"  在源数据有但 all_in_one 缺失的日期数: {len(missing)}，示例: {', '.join(missing[:10])}")
    else:
        print("  没有在源数据中出现但 all_in_one 缺失的日期。")
    if extra:
        print(f"  在 all_in_one 有但源数据中没有的日期数: {len(extra)}，示例: {', '.join(extra[:10])}")
    else:
        print("  没有只存在于 all_in_one 的额外日期。")
    return set(missing), set(extra)


def _sample_check(per_date_symbols: Dict[str, Dict[str, object]], allin_rows: Dict[str, int], samples: int = 5) -> None:
    import random

    common_dates = sorted(set(per_date_symbols.keys()) & set(allin_rows.keys()))
    if not common_dates:
        print("\n[SampleCheck] 没有交集日期，跳过示例检查。")
        return
    pick_dates = random.sample(common_dates, min(samples, len(common_dates)))
    print(f"\n[SampleCheck] 抽查 {len(pick_dates)} 个日期的 symbol 行数：")
    for d in pick_dates:
        src_rows = len(per_date_symbols.get(d, {}))
        all_rows = allin_rows.get(d, 0)
        print(f"  {d}: sources={src_rows} all_in_one={all_rows} diff={all_rows - src_rows}")


def _delete_source_dirs() -> None:
    _ensure_backend_on_path()
    from app.services.cache_consolidation import _cache_root

    root = _cache_root()
    targets = [
        root / "tushare_spot",
        root / "tushare_snapshot",
        root / "backtest",
        root / "daily",
    ]
    print("\n[DeleteSources] 将删除以下目录及其全部内容：")
    for t in targets:
        print(f"  - {t}")
    for t in targets:
        if not t.exists():
            print(f"  跳过（不存在）：{t}")
            continue
        # 保险起见，仅删除 .cache 下这几个精确目录
        if root not in t.parents:
            print(f"  跳过（不在 .cache 下）：{t}")
            continue
        for path in sorted(t.rglob("*"), reverse=True):
            if path.is_file():
                try:
                    path.unlink()
                except OSError as e:
                    print(f"    删除文件失败 {path}: {e}")
        try:
            t.rmdir()
            print(f"  已删除目录：{t}")
        except OSError as e:
            print(f"  删除目录失败 {t}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 .cache/* 与 all_in_one/dates 是否一致。")
    parser.add_argument("--sample-check", action="store_true", help="随机抽查若干日期的行数对比")
    parser.add_argument("--delete-sources", action="store_true", help="在校验通过时删除 .cache/tushare_spot 等四个目录")
    parser.add_argument("--force", action="store_true", help="与 --delete-sources 一同使用，确认执行删除操作")
    args = parser.parse_args()

    print("[1] 加载源数据日期与 symbol 行数 …")
    per_date_symbols, src_rows = _load_sources()
    print(f"    源数据共有日期数: {len(src_rows)}")

    print("[2] 加载 all_in_one/dates …")
    allin_rows, _ = _load_all_in_one()
    print(f"    all_in_one/dates 共有日期数: {len(allin_rows)}")

    missing, extra = _print_set_diff("日期集合对比", src_rows.keys(), allin_rows.keys())

    print("\n[3] 按日期比较 symbol 行数差异（仅展示差异明显的前 20 个）")
    diffs = []
    for d in sorted(set(src_rows.keys()) & set(allin_rows.keys())):
        s = src_rows[d]
        a = allin_rows.get(d, 0)
        if s != a:
            diffs.append((d, s, a, a - s))
    if not diffs:
        print("  所有共有日期的 symbol 行数完全一致。")
    else:
        print(f"  有 {len(diffs)} 个日期 symbol 行数不一致，示例：")
        for d, s, a, diff in diffs[:20]:
            print(f"  {d}: sources={s} all_in_one={a} diff={diff}")

    if args.sample_check:
        _sample_check(per_date_symbols, allin_rows)

    ok = not missing and not extra and not diffs
    print(f"\n[Summary] 日期集合一致: {not missing and not extra}，行数完全一致: {not diffs}，总体结果: {'OK' if ok else 'NOT-OK'}")

    if args.delete_sources:
        if not ok:
            print("\n[DeleteSources] 校验未通过（存在缺失/多余日期或行数差异），不会执行删除。")
        elif not args.force:
            print("\n[DeleteSources] 需要同时指定 --force 才会真正删除源目录。")
        else:
            _delete_source_dirs()

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


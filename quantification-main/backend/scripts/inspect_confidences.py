#!/usr/bin/env python
from __future__ import annotations

"""
调试脚本：直接调用后端同一套选股逻辑，打印前 50 名的信心值分布。

用法（在 backend 目录下，已激活 .venv）:
    python scripts/inspect_confidences.py

按策略/预算复现（建议用这个来对齐前端）:
    python scripts/inspect_confidences.py --list-strategies
    python scripts/inspect_confidences.py --strategy-name "市值20-30亿" --budget 100000 --top 30
    python scripts/inspect_confidences.py --strategy-id 3 --budget 100000 --top 30
"""

from collections import Counter
from pathlib import Path
import sys
import argparse

# 确保可以 import app.*
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.db.store import get_default_strategy_id, get_strategy, list_strategies
from app.services.factor_engine import score_factors_with_strategy
from app.services.stock_selector import select_affordable_stocks


def _pick_strategy(strategy_id: int | None, strategy_name: str | None):
    if strategy_id is not None:
        s = get_strategy(int(strategy_id))
        return s
    if strategy_name:
        name = str(strategy_name).strip()
        items = list_strategies()
        for s in items:
            if name in str(s.get("name", "")):
                return get_strategy(int(s["id"]))
        return None
    default_id = get_default_strategy_id()
    return get_strategy(default_id) if default_id is not None else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-strategies", action="store_true", help="列出策略 ID 与名称")
    ap.add_argument("--strategy-id", type=int, default=None, help="使用指定策略 ID")
    ap.add_argument("--strategy-name", type=str, default=None, help="按名称包含匹配（如 市值20-30亿）")
    ap.add_argument("--budget", type=float, default=None, help="传入预算（用于 1 手可买过滤）")
    ap.add_argument("--top", type=int, default=50, help="打印前 N 名")
    args = ap.parse_args()

    if args.list_strategies:
        items = list_strategies()
        print(f"策略数量: {len(items)}")
        for s in items:
            print(f"- id={s.get('id')}  name={s.get('name')}  type={s.get('type')}")
        return

    strategy = _pick_strategy(args.strategy_id, args.strategy_name)
    print(f"使用策略: id={strategy.get('id') if strategy else None} name={strategy.get('name') if strategy else None}")

    quotes = select_affordable_stocks(limit=None, max_one_lot_cost=args.budget if args.budget and args.budget > 0 else None)
    scores = score_factors_with_strategy(quotes, strategy)
    print(f"总共打分标的数: {len(scores)}")
    if not scores:
        return

    confs = [s.confidence for s in scores]
    print(f"信心值最小: {min(confs):.4f}, 最大: {max(confs):.4f}")
    uniq = sorted(set(confs))
    print(f"不同信心值个数: {len(uniq)}，示例前 20 个: {uniq[:20]}")

    top_n = max(1, int(args.top))
    print(f"\n前 {top_n} 名候选的信心值（保留 4 位小数）：")
    for i, s in enumerate(scores[:top_n], start=1):
        print(f"{i:02d}. {s.symbol} {s.name}  confidence={s.confidence:.4f}")

    # 模拟前端展示：toFixed(2) 后统计（看是不是都变成 100.00）
    shown = [float(f"{s.confidence:.2f}") for s in scores[:top_n]]
    print(f"\n前 {top_n} 名（按前端两位小数展示）的分布:")
    c = Counter(shown)
    for k in sorted(c.keys(), reverse=True)[:20]:
        print(f"- {k:.2f}: {c[k]} 只")


if __name__ == "__main__":
    main()


"""
自动投研管道：候选池填充 → 投资逻辑生成 → 持仓状态同步
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.research_engine import build_stock_research, materialize_pool_research_snapshots
from app.services.thesis_store import generate_watch_pool_theses, generate_thesis_for_stock
from app.services.decision_store import list_pool_members, transition_position
from app.db.store import get_positions


def run_daily_maintenance(*, max_candidates: int = 50) -> dict[str, Any]:
    """Run full daily pipeline: research → pool → theses → position states."""
    now = datetime.now().isoformat(timespec="seconds")
    errors = []

    # Step 1: Research on current positions
    positions = get_positions()
    research_count = 0
    for p in positions:
        code = str(p.get("symbol", "")).strip().upper()
        if not code.endswith(".SH") and not code.endswith(".SZ"):
            for suffix in (".SH", ".SZ"):
                try:
                    r = build_stock_research(f"{code}{suffix}", persist=True)
                    if r.get("ok"):
                        research_count += 1
                    break
                except Exception as e:
                    errors.append(f"research_{code}{suffix}: {str(e)[:60]}")
                    continue
        else:
            try:
                r = build_stock_research(code, persist=True)
                if r.get("ok"):
                    research_count += 1
            except Exception as e:
                errors.append(f"research_{code}: {str(e)[:60]}")

    # Step 2: Generate watch pool theses (this also fills the pool)
    try:
        pool_result = generate_watch_pool_theses(limit=max_candidates)
        pool_added = pool_result.get("theses_created", 0)
    except Exception as e:
        errors.append(f"pool_theses: {str(e)[:100]}")
        pool_added = 0

    # Step 3: Generate theses for current positions
    thesis_count = 0
    for p in positions:
        code = str(p.get("symbol", "")).strip().upper()
        if not code.endswith(".SH") and not code.endswith(".SZ"):
            code = f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"
        try:
            r = generate_thesis_for_stock(code, force=True)
            if r.get("ok") or r.get("thesis_id"):
                thesis_count += 1
        except Exception:
            pass

    # Step 4: Sync position states
    state_count = 0
    for p in positions:
        sym = str(p.get("symbol", "")).strip().upper()
        try:
            transition_position(
                ts_code=sym,
                to_state="hold",
                reason="auto_daily_maintenance",
                rule_version="daily_pipeline_v1",
                automatic_decision=True,
            )
            state_count += 1
        except Exception:
            pass

    return {
        "ok": True,
        "results": {
            "research": research_count,
            "pool_theses": pool_added,
            "position_theses": thesis_count,
            "position_states": state_count,
        },
        "errors": errors[:10],
        "timestamp": now,
        "model_version": "daily_maintenance_v1",
    }

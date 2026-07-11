#!/usr/bin/env python
from __future__ import annotations

"""
Tushare-only 全因子缺失补全（提速版）。

改进点：
- 自动注入 backend 路径，无需手动设置 PYTHONPATH
- 默认只处理“覆盖率有缺失”的日期（missing-only）
- 支持并发预取（--workers），处理阶段仍按日期顺序，保证衍生因子一致
- 进度条每 5 秒刷新
"""

import argparse
import json
import math
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


BACKEND_ROOT = _backend_root()
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _cache_root() -> Path:
    return BACKEND_ROOT / ".cache"


def _all_in_one_dates_dir() -> Path:
    d = _cache_root() / "all_in_one"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundle_dir() -> Path:
    d = _cache_root() / "all_in_one"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm_symbol(ts_code_or_symbol: str) -> str:
    s = str(ts_code_or_symbol or "").strip()
    if "." in s:
        s = s.split(".", 1)[0]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _load_day_dates() -> List[str]:
    out: List[str] = []
    for p in _all_in_one_dates_dir().glob("*.json"):
        stem = p.stem
        if len(stem) == 8 and stem.isdigit():
            out.append(stem)
    return sorted(set(out))


def _load_missing_dates_only(dates: List[str]) -> List[str]:
    from app.services.cache_consolidation import FACTOR_KEYS_FOR_COVERAGE

    out: List[str] = []
    dd = _all_in_one_dates_dir()
    for d in dates:
        p = dd / f"{d}.json"
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            out.append(d)
            continue
        cov = payload.get("factor_coverage")
        day_cov = {}
        if isinstance(cov, dict):
            day_cov = cov.get(d) if isinstance(cov.get(d), dict) else {}
        # 没有覆盖率或任意因子 < 1，则认为需补
        if not day_cov:
            out.append(d)
            continue
        need = False
        for k in FACTOR_KEYS_FOR_COVERAGE:
            v = float(day_cov.get(k, 0.0) or 0.0)
            if v < 1.0:
                need = True
                break
        if need:
            out.append(d)
    return out


@dataclass
class ProgressState:
    total: int
    done: int = 0
    success: int = 0
    failed: int = 0
    current_date: str = ""
    current_stage: str = "idle"


def _render_bar(done: int, total: int, success: int, failed: int, current_date: str, stage: str) -> str:
    width = 40
    ratio = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    pct = f"{ratio * 100:5.1f}%"
    return (
        f"[{bar}] {pct}  {done}/{total}  success={success} failed={failed}  "
        f"date={current_date or '-'} stage={stage}"
    )


def _progress_loop(state: ProgressState, stop_event: threading.Event) -> None:
    last_len = 0
    while not stop_event.is_set():
        line = _render_bar(state.done, state.total, state.success, state.failed, state.current_date, state.current_stage)
        pad = " " * max(0, last_len - len(line))
        print("\r" + line + pad, end="", flush=True)
        last_len = len(line)
        stop_event.wait(5.0)
    line = _render_bar(state.done, state.total, state.success, state.failed, state.current_date, state.current_stage)
    pad = " " * max(0, last_len - len(line))
    print("\r" + line + pad)


def _df_to_rows(df) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    return [dict(r) for _, r in df.iterrows()]


def _compute_series_factors(history: List[Dict[str, float]]) -> Dict[str, float]:
    if not history:
        return {"momentum_5": 0.0, "momentum_20": 0.0, "volatility_20": 0.0, "volume_ratio_5": 0.0}
    closes = [float(x.get("close", 0.0) or 0.0) for x in history]
    volumes = [float(x.get("volume", 0.0) or 0.0) for x in history]
    i = len(closes) - 1
    close = closes[i]
    momentum_5 = (close / closes[i - 4] - 1.0) if i >= 4 and closes[i - 4] > 0 else 0.0
    momentum_20 = (close / closes[i - 19] - 1.0) if i >= 19 and closes[i - 19] > 0 else 0.0
    start = max(0, i - 19)
    win = closes[start : i + 1]
    rets: List[float] = []
    for j in range(1, len(win)):
        c0 = win[j - 1]
        c1 = win[j]
        if c0 > 0:
            rets.append(c1 / c0 - 1.0)
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        volatility_20 = math.sqrt(var) if var >= 0 else 0.0
    else:
        volatility_20 = 0.0
    if i >= 4:
        v_win = volumes[i - 4 : i + 1]
        v_avg = sum(v_win) / len(v_win) if v_win else 0.0
        volume_ratio_5 = volumes[i] / v_avg if v_avg > 0 else 0.0
    else:
        volume_ratio_5 = 0.0
    return {
        "momentum_5": momentum_5,
        "momentum_20": momentum_20,
        "volatility_20": volatility_20,
        "volume_ratio_5": volume_ratio_5,
    }


def _build_day_payload(
    trade_date: str,
    name_map: Dict[str, str],
    fetched: Dict[str, List[Dict[str, Any]]],
    symbol_history: Dict[str, List[Dict[str, float]]],
) -> Dict[str, Any]:
    daily_rows = fetched["daily"]
    basic_rows = fetched["basic"]
    suspend_rows = fetched["suspend"]
    limit_rows = fetched["limit"]

    per_symbol: Dict[str, Dict[str, Any]] = {}
    daily_map = {_norm_symbol(str(r.get("ts_code", ""))): r for r in daily_rows if _norm_symbol(str(r.get("ts_code", "")))}
    basic_map = {_norm_symbol(str(r.get("ts_code", ""))): r for r in basic_rows if _norm_symbol(str(r.get("ts_code", "")))}
    suspend_set = {_norm_symbol(str(r.get("ts_code", ""))) for r in suspend_rows if _norm_symbol(str(r.get("ts_code", "")))}
    limit_map = {_norm_symbol(str(r.get("ts_code", ""))): r for r in limit_rows if _norm_symbol(str(r.get("ts_code", "")))}

    all_symbols = set(daily_map.keys()) | set(basic_map.keys()) | set(limit_map.keys()) | suspend_set
    for sym in sorted(all_symbols):
        row: Dict[str, Any] = {"trade_date": trade_date, "symbol": sym, "name": name_map.get(sym, "")}
        d = daily_map.get(sym)
        if d:
            open_ = float(d.get("open", 0) or 0)
            high = float(d.get("high", 0) or 0)
            low = float(d.get("low", 0) or 0)
            close = float(d.get("close", 0) or 0)
            prev_close = float(d.get("pre_close", 0) or 0)
            volume = float(d.get("vol", 0) or 0)
            turnover = float(d.get("amount", 0) or 0)
            change_pct = (close / prev_close - 1.0) if prev_close > 0 else 0.0
            row.update(
                {
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "price": close,
                    "volume": volume,
                    "turnover": turnover,
                    "change_pct": change_pct,
                }
            )
            h = symbol_history.setdefault(sym, [])
            h.append({"close": close, "volume": volume})
            row.update(_compute_series_factors(h))

        b = basic_map.get(sym)
        if b:
            row.update(
                {
                    "float_mv": float(b.get("circ_mv", 0) or 0),
                    "total_mv": float(b.get("total_mv", 0) or 0),
                    "pe_ttm": float(b.get("pe_ttm", 0) or 0),
                    "pb": float(b.get("pb", 0) or 0),
                    "turnover_rate": float(b.get("turnover_rate", 0) or 0),
                }
            )

        lm = limit_map.get(sym)
        if lm and "close" in row:
            c = float(row.get("close", 0) or 0)
            up = float(lm.get("up_limit", 0) or 0)
            down = float(lm.get("down_limit", 0) or 0)
            row["is_limit_up"] = 1.0 if up > 0 and c >= (up - 1e-6) else 0.0
            row["is_limit_down"] = 1.0 if down > 0 and c <= (down + 1e-6) else 0.0
        else:
            row.setdefault("is_limit_up", 0.0)
            row.setdefault("is_limit_down", 0.0)

        row["is_suspended"] = 1.0 if sym in suspend_set else 0.0
        row["is_st"] = 1.0 if "ST" in str(row.get("name", "")).upper() else 0.0
        per_symbol[sym] = row

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "schema_version": "2.0",
        "generated_at": generated_at,
        "trade_date": trade_date,
        "counts": {"dates": 1, "symbol_rows": len(per_symbol)},
        "data": {trade_date: per_symbol},
    }


def _write_day_and_update_latest(day_payload: Dict[str, Any]) -> None:
    trade_date = str(day_payload.get("trade_date", "") or "")
    if not trade_date:
        return
    (_all_in_one_dates_dir() / f"{trade_date}.json").write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")
    bundle = _bundle_dir()
    (bundle / f"{trade_date}.json").write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")
    latest_file = bundle / "latest.json"
    try:
        old_date = ""
        if latest_file.exists():
            old = json.loads(latest_file.read_text(encoding="utf-8"))
            old_date = str(old.get("trade_date", "") or "")
        if not old_date or trade_date >= old_date:
            latest_file.write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        latest_file.write_text(json.dumps(day_payload, ensure_ascii=False), encoding="utf-8")


def _fetch_name_map(client) -> Dict[str, str]:
    rows = _df_to_rows(client.stock_basic(list_status="L", fields="ts_code,name"))
    out: Dict[str, str] = {}
    for r in rows:
        sym = _norm_symbol(str(r.get("ts_code", "")))
        if sym:
            out[sym] = str(r.get("name", "") or "")
    return out


def _fetch_one_date(client, trade_date: str) -> Dict[str, List[Dict[str, Any]]]:
    daily_rows = _df_to_rows(
        client.daily(trade_date=trade_date, fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_chg")
    )
    basic_rows = _df_to_rows(
        client.daily_basic(
            trade_date=trade_date,
            ts_code="",
            fields="ts_code,trade_date,total_mv,circ_mv,pe_ttm,pb,turnover_rate",
        )
    )
    suspend_rows = _df_to_rows(client.suspend_d(trade_date=trade_date))
    limit_rows = _df_to_rows(client.stk_limit(trade_date=trade_date, ts_code="", fields="ts_code,trade_date,up_limit,down_limit"))
    return {"daily": daily_rows, "basic": basic_rows, "suspend": suspend_rows, "limit": limit_rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Tushare-only 全因子补全（提速版）。")
    parser.add_argument("--apply", action="store_true", help="实际执行补全")
    parser.add_argument("--dry-run", action="store_true", help="仅预览")
    parser.add_argument("--date", default=None, help="仅处理指定日期 YYYYMMDD")
    parser.add_argument("--workers", type=int, default=4, help="并发预取线程数（建议 2~6）")
    parser.add_argument("--all-dates", action="store_true", help="处理 all_in_one 中全部日期（默认只处理缺失日期）")
    parser.add_argument("--max-dates", type=int, default=0, help="最多处理多少个日期（0=不限制）")
    args = parser.parse_args()

    from app.integrations.tushare_client import get_tushare_client
    from app.services.cache_consolidation import cleanup_source_cache_dirs

    all_dates = _load_day_dates()
    dates = all_dates
    if not args.all_dates:
        dates = _load_missing_dates_only(all_dates)
    if args.date:
        dates = [d for d in dates if d == args.date]
    if args.max_dates and args.max_dates > 0:
        dates = dates[: int(args.max_dates)]

    print(f"all_in_one 日期总数: {len(all_dates)}；本次处理日期: {len(dates)}")
    if dates:
        print("前 15 个日期: " + ", ".join(dates[:15]))
    if not dates:
        print("没有需要处理的日期。")
        return 0

    if args.dry_run or not args.apply:
        print("dry-run: 未执行回源补全。加 --apply 后执行。")
        return 0

    client = get_tushare_client()
    name_map = _fetch_name_map(client)

    state = ProgressState(total=len(dates), current_stage="init")
    stop_event = threading.Event()
    t = threading.Thread(target=_progress_loop, args=(state, stop_event), daemon=True)
    t.start()

    symbol_history: Dict[str, List[Dict[str, float]]] = {}
    report_rows: List[Dict[str, Any]] = []

    try:
        workers = max(1, int(args.workers))
        state.current_stage = "prefetch"
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: Dict[int, Future] = {}
            next_submit = 0
            # 先灌满窗口
            while next_submit < len(dates) and len(futures) < workers:
                d = dates[next_submit]
                futures[next_submit] = pool.submit(_fetch_one_date, client, d)
                next_submit += 1

            for i, d in enumerate(dates):
                state.current_date = d
                state.current_stage = "fetch"
                fut = futures.pop(i)
                try:
                    fetched = fut.result()
                    daily_rows = fetched["daily"]
                    basic_rows = fetched["basic"]
                    if not daily_rows or not basic_rows:
                        raise RuntimeError(
                            f"strict 模式失败：daily_rows={len(daily_rows)} basic_rows={len(basic_rows)}，当日数据不完整"
                        )
                    state.current_stage = "build"
                    day_payload = _build_day_payload(d, name_map, fetched, symbol_history)
                    state.current_stage = "write"
                    _write_day_and_update_latest(day_payload)
                    state.success += 1
                    report_rows.append(
                        {
                            "date": d,
                            "ok": True,
                            "daily_rows": len(daily_rows),
                            "basic_rows": len(basic_rows),
                            "symbol_rows": int(day_payload.get("counts", {}).get("symbol_rows", 0)),
                            "error": "",
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    state.failed += 1
                    report_rows.append(
                        {"date": d, "ok": False, "daily_rows": 0, "basic_rows": 0, "symbol_rows": 0, "error": str(e)[:300]}
                    )
                finally:
                    state.done += 1
                    state.current_stage = "next"

                # 持续补充预取窗口
                if next_submit < len(dates):
                    d2 = dates[next_submit]
                    futures[next_submit] = pool.submit(_fetch_one_date, client, d2)
                    next_submit += 1
    finally:
        state.current_stage = "finalize"
        stop_event.set()
        t.join(timeout=2)

    ok_cnt = sum(1 for r in report_rows if r["ok"])
    fail_cnt = len(report_rows) - ok_cnt
    report_path = BACKEND_ROOT / "factor_backfill_report.md"
    lines = [
        "# Tushare 全因子补全报告",
        "",
        f"总日期: {len(report_rows)}",
        f"成功: {ok_cnt}",
        f"失败: {fail_cnt}",
        "",
        "| 日期 | 结果 | daily_rows | basic_rows | symbol_rows | 错误 |",
        "|------|------|------------|------------|-------------|------|",
    ]
    for r in report_rows:
        lines.append(
            f"| {r['date']} | {'成功' if r['ok'] else '失败'} | {r['daily_rows']} | {r['basic_rows']} | {r['symbol_rows']} | {r['error']} |"
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入报告: {report_path}")

    cleanup = cleanup_source_cache_dirs()
    print(
        "已清理源目录: "
        f"removed_dirs={cleanup.get('removed_dirs', 0)} "
        f"removed_files={cleanup.get('removed_files', 0)} "
        f"failed={cleanup.get('failed', 0)}"
    )
    print(f"完成：成功 {ok_cnt}/{len(report_rows)}，失败 {fail_cnt}/{len(report_rows)}")
    return 0 if fail_cnt == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


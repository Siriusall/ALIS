from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.state import (
    QuantRunConfig,
    append_quant_run_record,
    ensure_manual_trade_allowed,
    get_quant_run_records,
    get_quant_run_state,
    get_quant_run_state_dict,
    start_quant_run,
    stop_quant_run,
    update_quant_run_progress,
)
from app.core.exchange_groups import normalize_exchange_group
from app.core.a_share_fees import is_exchange_etf, sell_cost_breakdown
from app.core.read_only_diagnostics import degraded_read
from app.data_platform.db import _conn as data_conn
from app.db.store import (
    append_operations_to_execution,
    create_execution,
    create_quant_run_task,
    delete_quant_run_task,
    get_account_balance,
    get_account_baseline,
    get_execution,
    get_default_strategy_id,
    get_quant_run_task,
    get_positions,
    get_strategy,
    list_operations,
    list_operations_for_execution_ids,
    list_recent_quant_run_task_records,
    list_quant_run_task_records,
    list_quant_run_tasks,
    save_performance_reports,
    list_performance_reports,
    upsert_quant_run_task_meta,
    update_quant_run_task,
    sell_position,
)
from app.services.backtest_data_source import BacktestDataSource
from app.services.cache_consolidation import trigger_backfill_merge_meta_async
from app.services.defensive_assets import defensive_asset_bar
from app.services.operations_overview import build_live_portfolio_risk
from app.services.quant_run_engine import (
    HistoricalDataMissingError,
    ReplayRiskGateError,
    analyze_market_plan,
    execute_analyze_buy_plan,
    execute_analyze_rebalance_plan,
    preview_analyze_rebalance_plan,
    list_consolidated_trade_dates,
    require_consolidated_trade_dates,
    run_for_trade_date,
)
from app.services.quant_task_runner import run_quant_task_async
from app.services.quant_task_state_machine import can_transition, normalize_status
from app.services.decision_store import build_review_outcome_suggestion, list_decision_snapshots, list_position_state_history, list_reviews
from app.services.performance_reports import enrich_performance_period, load_market_state_history
from app.services.simulation_ledger import build_simulation_ledger

router = APIRouter()


class QuantRunStartRequest(BaseModel):
    strategy_id: int = Field(..., ge=1)
    top_n: int = Field(10, ge=1, le=100)
    budget_ratio: float = Field(1.0, ge=0.0, le=1.0)
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


class QuantRunCatchUpRequest(BaseModel):
    start_date: str | None = Field(None, description="可选，YYYY-MM-DD；不传则从 last_signal_date 后开始")
    max_steps: int = Field(5, ge=1, le=60, description="一次最多补跑多少个信号日")


class QuantRiskRebalanceRequest(BaseModel):
    strategy_id: int | None = Field(None, ge=1)
    top_n: int = Field(10, ge=1, le=100)
    budget_ratio: float = Field(1.0, ge=0.0, le=1.0)
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


class QuantTaskCreateRequest(BaseModel):
    strategy_id: int = Field(..., ge=1)
    top_n: int = Field(10, ge=1, le=100)
    budget_ratio: float = Field(1.0, ge=0.0, le=1.0)
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")
    start_date: str | None = Field(None, description="可选，YYYY-MM-DD；不传则从最近可执行信号日开始")
    max_steps: int = Field(5, ge=1, le=120)
    replay_mode: str = Field("pragmatic_recover", description="pragmatic_recover|strict_replay")


class QuantAnalyzeRequest(BaseModel):
    signal_date: str | None = Field(None, description="可选，YYYY-MM-DD；默认最近交易日")
    top_n: int = Field(10, ge=1, le=50)
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


class QuantExecuteBuyRequest(BaseModel):
    signal_date: str = Field(..., description="分析日 YYYY-MM-DD")
    strategy_id: int | None = Field(None, description="策略 id；省略则用默认")
    symbols: list[str] = Field(default_factory=list, description="建议买入标的；完整调仓仅卖出时可留空")
    target_position_pct: float = Field(1.0, ge=0.0, le=1.0, description="目标总仓位占权益比例")
    max_single_position_pct: float | None = Field(None, ge=0.0, le=1.0, description="单票上限占权益比例")
    mode: str = Field("full_rebalance", description="buy_only|full_rebalance")
    target_symbols: list[str] | None = Field(None, description="完整目标池，用于完整调仓卖出判断")
    selection_scores: dict[str, float] | None = Field(None, description="选股得分，用于资金二次分配")


class QuantRebalancePreviewRequest(BaseModel):
    signal_date: str = Field(..., description="分析日 YYYY-MM-DD")
    strategy_id: int | None = None
    symbols: list[str] = Field(default_factory=list)
    target_position_pct: float = Field(1.0, ge=0.0, le=1.0)
    max_single_position_pct: float | None = Field(None, ge=0.0, le=1.0)
    mode: str = Field("full_rebalance", description="buy_only|full_rebalance")
    target_symbols: list[str] | None = None
    selection_scores: dict[str, float] | None = None


def _safe_float(v: object) -> float:
    try:
        x = float(v or 0)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _safe_int(v: object) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _normalize_index_ts_code(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if "." in s:
        return s
    code = s.zfill(6)
    # 常见指数：399xxx 为深市，其余默认沪市
    suffix = "SZ" if code.startswith("399") else "SH"
    return f"{code}.{suffix}"


def _canonical_market_ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value
    code = value.split(".", 1)[0].zfill(6)
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _raw_position_close_maps(symbols: set[str], dates: list[str]) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """Load unadjusted stock and fund closes for account-value replay.

    ``BacktestDataSource`` intentionally exposes adjusted bars for research
    returns.  Account equity must instead use the quoted close, otherwise a
    corporate-action factor is incorrectly treated as an increase in wealth.
    """
    if not symbols or not dates:
        return {}, {}
    stock_code_by_symbol = {
        symbol: _canonical_market_ts_code(symbol)
        for symbol in symbols if not is_exchange_etf(symbol)
    }
    etf_code_by_symbol = {
        symbol: _canonical_market_ts_code(symbol)
        for symbol in symbols if is_exchange_etf(symbol)
    }
    start, end = min(dates).replace("-", ""), max(dates).replace("-", "")
    stock_prices: dict[tuple[str, str], float] = {}
    etf_prices: dict[tuple[str, str], float] = {}
    try:
        with data_conn() as conn:
            if stock_code_by_symbol:
                codes = list(stock_code_by_symbol.values())
                placeholders = ",".join("?" for _ in codes)
                rows = conn.execute(
                    f"""SELECT ts_code,trade_date,close FROM dc_stock_daily_snapshot
                         WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
                           AND close IS NOT NULL""",
                    (*codes, start, end),
                ).fetchall()
                symbol_by_code = {code: symbol for symbol, code in stock_code_by_symbol.items()}
                for row in rows:
                    item = dict(row)
                    symbol = symbol_by_code.get(str(item.get("ts_code") or ""))
                    price = _safe_float(item.get("close"))
                    if symbol and price > 0:
                        stock_prices[(symbol, str(item.get("trade_date") or ""))] = price
            if etf_code_by_symbol:
                codes = list(etf_code_by_symbol.values())
                placeholders = ",".join("?" for _ in codes)
                rows = conn.execute(
                    f"""SELECT ts_code,trade_date,payload_json FROM dc_tushare_raw_records
                         WHERE api_name='fund_daily' AND ts_code IN ({placeholders})
                           AND trade_date BETWEEN ? AND ?""",
                    (*codes, start, end),
                ).fetchall()
                symbol_by_code = {code: symbol for symbol, code in etf_code_by_symbol.items()}
                for row in rows:
                    item = dict(row)
                    try:
                        payload = json.loads(item.get("payload_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    symbol = symbol_by_code.get(str(item.get("ts_code") or ""))
                    price = _safe_float(payload.get("close"))
                    if symbol and price > 0:
                        etf_prices[(symbol, str(item.get("trade_date") or ""))] = price
    except Exception:
        # A missing source-traced close is a valuation gap.  Callers must
        # surface it instead of extending a curve with a prior quote.
        return {}, {}
    return stock_prices, etf_prices


def _load_index_close_map(benchmark: str, dates: list[str]) -> dict[str, float]:
    """Read benchmark closes from the source-traced data platform only.

    Performance reporting is read-only.  It must surface a missing benchmark
    instead of making an untracked, point-in-time-ambiguous network request.
    """
    if not dates:
        return {}
    ts_code = _normalize_index_ts_code(benchmark)
    start = min(dates).replace("-", "")
    end = max(dates).replace("-", "")
    try:
        with data_conn() as conn:
            rows = conn.execute(
                """SELECT trade_date,payload_json
                   FROM dc_tushare_raw_records
                   WHERE api_name='index_daily' AND ts_code=?
                     AND trade_date BETWEEN ? AND ?
                   ORDER BY trade_date""",
                (ts_code, start, end),
            ).fetchall()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for raw in rows:
        row = dict(raw)
        td = str(row.get("trade_date") or "").strip()
        if len(td) == 8 and td.isdigit():
            d = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
            try:
                payload = json.loads(str(row.get("payload_json") or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            px = _safe_float(payload.get("close"))
            if px > 0:
                out[d] = px
    return out


def _compute_drawdown(equity: list[float]) -> list[float]:
    out: list[float] = []
    peak = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak <= 0:
            out.append(0.0)
        else:
            out.append(round((v / peak - 1.0) * 100.0, 2))
    return out


def _utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _default_task_risk_config() -> dict:
    result = {
        "enforce_trading_session_gate": str(os.getenv("QUANT_ENFORCE_TRADING_SESSION_GATE", "0")).strip().lower() in {"1", "true", "yes", "on"},
        "enable_data_health_gate": str(os.getenv("QUANT_ENABLE_DATA_HEALTH_GATE", "1")).strip().lower() in {"1", "true", "yes", "on"},
        "scoring_timeout_sec": max(10, min(int(os.getenv("QUANT_SCORING_TIMEOUT_SEC", "90")), 1800)),
        "min_account_balance": max(0.0, float(os.getenv("QUANT_MIN_ACCOUNT_BALANCE", "0") or 0)),
        "max_consecutive_losses": max(0, min(int(os.getenv("QUANT_MAX_CONSECUTIVE_LOSSES", "3")), 30)),
    }


def _attach_risk_decision(task: dict) -> dict:
    out = dict(task or {})
    raw = out.get("error")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and "recovery_condition" in payload:
                out["risk_decision"] = payload
        except Exception:
            out["risk_decision"] = None
    else:
        out["risk_decision"] = None
    return out


def _replay_cost_state_before(execution_at: str, symbols: set[str]) -> dict[str, dict]:
    """
    回放 execution_at 之前的成交，得到指定 symbols 的持仓成本状态。
    成本口径：简单加权成本（不含费用），用于展示“个股收益”。
    """
    state: dict[str, dict] = {s: {"qty": 0, "cost": 0.0} for s in symbols}
    if not symbols:
        return state
    ops = list_operations(execution_id=None, limit=200000)
    if not ops:
        return state
    valid = []
    for o in ops:
        side = str(o.get("side") or "").upper()
        sym = str(o.get("symbol") or "").strip()
        if side not in {"BUY", "SELL"} or sym not in symbols:
            continue
        valid.append(o)
    valid.sort(key=lambda x: (str(x.get("at") or ""), int(x.get("id") or 0)))
    for o in valid:
        at = str(o.get("at") or "")
        if at and at >= execution_at:
            break
        sym = str(o.get("symbol") or "").strip()
        qty = _safe_int(o.get("quantity"))
        px = _safe_float(o.get("price"))
        if qty <= 0 or px <= 0:
            continue
        side = str(o.get("side") or "").upper()
        st = state.setdefault(sym, {"qty": 0, "cost": 0.0})
        if side == "BUY":
            st["qty"] = int(st["qty"]) + qty
            st["cost"] = _safe_float(st["cost"]) + px * qty
        elif side == "SELL":
            cur_qty = int(st["qty"])
            cur_cost = _safe_float(st["cost"])
            if cur_qty <= 0:
                continue
            sell_qty = min(cur_qty, qty)
            avg_cost = (cur_cost / cur_qty) if cur_qty > 0 else 0.0
            st["qty"] = cur_qty - sell_qty
            st["cost"] = max(0.0, cur_cost - avg_cost * sell_qty)
    return state


@router.get("/quant-runs/curve")
def quant_run_curve(limit: int = 250, benchmark: str = "000300", task_id: int | None = Query(None, ge=1)) -> dict:
    """
    量化执行曲线（专业看板用）：
    - strategy_equity: 组合市值指数（归一化到 100）
    - benchmark_equity: 基准指数（归一化到 100）
    - drawdown_pct: 组合回撤（%）
    """
    limit = max(30, min(int(limit or 250), 1000))
    def _build_flat_series_from_task_runs(task_runs: list[dict]) -> dict:
        dates = sorted({str(x.get("execute_date") or "") for x in task_runs if x.get("execute_date")})
        dates = [d for d in dates if d]
        if not dates:
            return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}
        if len(dates) > limit:
            dates = dates[-limit:]
        ds = BacktestDataSource()
        index_close_map = _load_index_close_map(benchmark, dates)
        benchmark_raw = []
        for d in dates:
            bm0 = float(ds.get_price_on_date(benchmark, d, field="close") or 0.0)
            if bm0 <= 0:
                bm0 = _safe_float(index_close_map.get(d))
            benchmark_raw.append(bm0)
        base_benchmark = next((x for x in benchmark_raw if x > 0), 0.0)
        benchmark_eq = [
            round((x / base_benchmark * 100.0), 4) if base_benchmark > 0 and x > 0 else None
            for x in benchmark_raw
        ]
        strategy_eq = [100.0 for _ in dates]
        series = [
            {
                "date": d,
                "strategy_equity": strategy_eq[i],
                "benchmark_equity": benchmark_eq[i],
                "drawdown_pct": 0.0,
                "day_return_pct": 0.0,
                "cum_return_pct": 0.0,
            }
            for i, d in enumerate(dates)
        ]
        return {
            "series": series,
            "summary": {
                "period_return": 0.0,
                "max_drawdown": 0.0,
                "points": len(series),
                "benchmark": benchmark,
                "task_id": task_id,
            },
        }

    ops: list[dict] = []
    task_runs: list[dict] = []
    if task_id is not None:
        task_runs = list_quant_run_task_records(task_id, limit=5000)
        exec_ids = [int(x["execution_id"]) for x in task_runs if x.get("execution_id") is not None]
        ops = list_operations_for_execution_ids(exec_ids, limit_per_execution=5000)
    else:
        ops = list_operations(execution_id=None, limit=5000)
    if not ops:
        if task_id is not None and task_runs:
            return _build_flat_series_from_task_runs(task_runs)
        return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}

    # 仅统计交易层操作，按时间升序重放持仓
    valid_ops = [
        x for x in ops
        if str(x.get("side", "")).upper() in {"BUY", "SELL"} and str(x.get("symbol", "")).strip()
    ]
    if not valid_ops:
        if task_id is not None and task_runs:
            return _build_flat_series_from_task_runs(task_runs)
        return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}
    valid_ops.sort(key=lambda x: str(x.get("at", "")))

    trade_dates = list_consolidated_trade_dates()
    if not trade_dates:
        if task_id is not None and task_runs:
            return _build_flat_series_from_task_runs(task_runs)
        return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}

    first_op_date = str(valid_ops[0].get("at", ""))[:10]
    dates = [d for d in trade_dates if d >= first_op_date]
    if len(dates) > limit:
        dates = dates[-limit:]
    if not dates:
        if task_id is not None and task_runs:
            return _build_flat_series_from_task_runs(task_runs)
        return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}

    ds = BacktestDataSource()
    index_close_map = _load_index_close_map(benchmark, dates)
    ops_by_date: dict[str, list[dict]] = defaultdict(list)
    symbols: set[str] = set()
    for o in valid_ops:
        d = str(o.get("at", ""))[:10]
        ops_by_date[d].append(o)
        sym = str(o.get("symbol", "")).strip()
        if sym:
            symbols.add(sym)
    stock_close_by_date, etf_close_by_date = _raw_position_close_maps(symbols, dates)

    holdings: dict[str, int] = defaultdict(int)
    # Replay cash from the fixed simulation baseline instead of valuing only
    # the currently held shares. This keeps idle cash and partial allocations
    # in the performance curve and prevents false 100% losses when positions
    # are small relative to the account.
    replay_cash = float(get_account_baseline() or 200000.0)
    benchmark_raw: list[float] = []
    strategy_raw: list[float] = []
    series: list[dict] = []
    missing_close_by_date: dict[str, list[str]] = {}

    for d in dates:
        snapshot_rows = ds.load_snapshot(d.replace("-", ""))
        snapshot_close_map = {str(r.symbol).strip(): _safe_float(getattr(r, "close", 0)) for r in snapshot_rows}
        # 先应用当日交易，再按收盘价估值
        for o in ops_by_date.get(d, []):
            sym = str(o.get("symbol", "")).strip()
            qty = _safe_int(o.get("quantity"))
            if not sym or qty <= 0:
                continue
            side = str(o.get("side", "")).upper()
            trade_price = _safe_float(o.get("price"))
            trade_fees = sum(_safe_float(o.get(key)) for key in ("commission", "transfer_fee", "stamp_tax"))
            if side == "BUY":
                holdings[sym] += qty
                replay_cash -= trade_price * qty + trade_fees
            elif side == "SELL":
                holdings[sym] = max(0, holdings[sym] - qty)
                replay_cash += trade_price * qty - trade_fees

        stock_value = 0.0
        compact_date = d.replace("-", "")
        missing_symbols: list[str] = []
        for sym, qty in holdings.items():
            if qty <= 0:
                continue
            px = (
                etf_close_by_date.get((sym, compact_date), 0.0)
                if is_exchange_etf(sym)
                else stock_close_by_date.get((sym, compact_date), 0.0)
            )
            if px <= 0:
                # Normalized snapshot is a raw-close fallback. Never use the
                # adjusted research bars for account market value.
                px_snap = _safe_float(snapshot_close_map.get(sym))
                if px_snap > 0:
                    px = px_snap
                else:
                    # An account curve may not relabel a prior close, order
                    # price, or cost basis as this session's market value.
                    missing_symbols.append(sym)
            stock_value += px * qty
        if missing_symbols:
            missing_close_by_date[d] = sorted(set(missing_symbols))

        bm_raw = ds.get_price_on_date(benchmark, d, field="close")
        bm = _safe_float(bm_raw) if bm_raw is not None else 0.0
        if bm <= 0:
            bm = _safe_float(snapshot_close_map.get(str(benchmark).strip().zfill(6)))
        if bm <= 0:
            bm = _safe_float(index_close_map.get(d))
        strategy_raw.append(max(0.0, replay_cash) + stock_value)
        benchmark_raw.append(_safe_float(bm))

    if missing_close_by_date:
        missing_dates = sorted(missing_close_by_date)
        return {
            "ok": False,
            "status": "blocked",
            "reason": "exact_account_close_missing",
            "series": [],
            "summary": {
                "period_return": None,
                "max_drawdown": None,
                "points": 0,
                "benchmark": benchmark,
                "task_id": task_id,
                "missing_close_by_date": missing_close_by_date,
                "missing_close_dates": missing_dates,
                "message": "账户曲线缺少持仓的精确交易日收盘价；禁止用前收、成交价或成本价补点。",
            },
        }

    # 归一化为指数
    base_strategy = next((x for x in strategy_raw if x > 0), 0.0)
    base_benchmark = next((x for x in benchmark_raw if x > 0), 0.0)
    if base_strategy <= 0:
        if task_id is not None and task_runs:
            return _build_flat_series_from_task_runs(task_runs)
        return {"series": [], "summary": {"max_drawdown": 0.0, "period_return": 0.0}}

    strategy_eq = [round(x / base_strategy * 100.0, 4) for x in strategy_raw]
    benchmark_eq = [
        round((x / base_benchmark * 100.0), 4) if base_benchmark > 0 and x > 0 else None
        for x in benchmark_raw
    ]
    drawdown = _compute_drawdown(strategy_eq)
    day_ret_pct: list[float] = []
    cum_ret_pct: list[float] = []
    for i, v in enumerate(strategy_eq):
        prev = strategy_eq[i - 1] if i > 0 else None
        dr = ((v / prev - 1.0) * 100.0) if prev and prev > 0 else 0.0
        cr = ((v / strategy_eq[0] - 1.0) * 100.0) if strategy_eq and strategy_eq[0] > 0 else 0.0
        day_ret_pct.append(round(dr, 4))
        cum_ret_pct.append(round(cr, 4))

    for i, d in enumerate(dates):
        series.append(
            {
                "date": d,
                "strategy_equity": strategy_eq[i],
                "strategy_equity_cny": round(strategy_raw[i], 2),
                "benchmark_equity": benchmark_eq[i],
                "drawdown_pct": drawdown[i],
                "day_return_pct": day_ret_pct[i],
                "cum_return_pct": cum_ret_pct[i],
            }
        )

    period_return = round(strategy_eq[-1] / strategy_eq[0] - 1.0, 4) if len(strategy_eq) >= 2 else 0.0
    max_drawdown = round(min(drawdown), 2) if drawdown else 0.0
    return {
        "ok": True,
        "status": "ready",
        "series": series,
        "summary": {
            "period_return": period_return,
            "max_drawdown": max_drawdown,
            "points": len(series),
            "benchmark": benchmark,
            "task_id": task_id,
        },
    }


@router.post("/quant-runs/analyze")
def quant_run_analyze(req: QuantAnalyzeRequest | None = None) -> dict:
    """一键分析：趋势理解 + 策略模板 + 选股与调仓建议（不执行买卖）。"""
    body = req or QuantAnalyzeRequest()
    exchange_group = normalize_exchange_group(body.exchange_group)
    try:
        return analyze_market_plan(
            signal_date=body.signal_date,
            top_n=int(body.top_n),
            exchange_group=exchange_group,
        )
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_analyze_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )
    except ReplayRiskGateError as e:
        raise HTTPException(status_code=400, detail={"code": getattr(e, "code", "replay_risk_gate"), "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/quant-runs/execute-preview")
def quant_run_execute_preview(req: QuantRebalancePreviewRequest) -> dict:
    """调仓预览：展示预计买卖、成本、仓位占比与拦截原因。"""
    symbols = [str(s).strip() for s in (req.symbols or []) if str(s).strip()]
    mode = str(req.mode or "full_rebalance").strip().lower()
    if not symbols and mode != "full_rebalance":
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    try:
        return preview_analyze_rebalance_plan(
            signal_date=str(req.signal_date).strip(),
            symbols=symbols,
            strategy_id=req.strategy_id,
            mode=str(req.mode or "full_rebalance"),
            target_position_pct=float(req.target_position_pct),
            max_single_position_pct=req.max_single_position_pct,
            target_symbols=req.target_symbols,
            selection_scores=req.selection_scores,
        )
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_execute_preview_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )
    except ReplayRiskGateError as e:
        raise HTTPException(status_code=400, detail={"code": getattr(e, "code", "replay_risk_gate"), "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/quant-runs/execute-buy")
def quant_run_execute_buy(req: QuantExecuteBuyRequest) -> dict:
    """Retired shared-account write path; use the autonomous frozen-plan worker."""
    raise HTTPException(
        status_code=410,
        detail="旧直接成交入口已关闭；成交只能由自治任务领取已授权冻结调仓计划",
    )
    # Kept below for source compatibility with old deployments; unreachable by
    # design so request payloads can never mutate the shared account.
    try:
        ensure_manual_trade_allowed("开仓")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    symbols = [str(s).strip() for s in (req.symbols or []) if str(s).strip()]
    mode = str(req.mode or "full_rebalance").strip().lower()
    if not symbols and mode != "full_rebalance":
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    try:
        return execute_analyze_rebalance_plan(
            signal_date=str(req.signal_date).strip(),
            symbols=symbols,
            strategy_id=req.strategy_id,
            mode=str(req.mode or "full_rebalance"),
            target_position_pct=float(req.target_position_pct),
            max_single_position_pct=req.max_single_position_pct,
            target_symbols=req.target_symbols,
            selection_scores=req.selection_scores,
        )
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_execute_buy_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )
    except ReplayRiskGateError as e:
        raise HTTPException(status_code=400, detail={"code": getattr(e, "code", "replay_risk_gate"), "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/quant-runs/tasks")
def quant_run_tasks(limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"items": [_attach_risk_decision(x) for x in list_quant_run_tasks(limit=limit)]}


@router.get("/quant-runs/tasks/{task_id}")
def quant_run_task_detail(task_id: int) -> dict:
    task = get_quant_run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = _attach_risk_decision(task)
    task["runs"] = list_quant_run_task_records(task_id, limit=5000)
    return task


@router.get("/quant-runs/tasks/{task_id}/run-detail")
def quant_run_task_run_detail(task_id: int, execute_date: str = Query(..., description="YYYY-MM-DD")) -> dict:
    task = get_quant_run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = list_quant_run_task_records(task_id, limit=5000)
    target = None
    for r in reversed(rows):
        if str(r.get("execute_date") or "") == str(execute_date):
            target = r
            break
    if target is None:
        raise HTTPException(status_code=404, detail="该日期无任务执行记录")
    execution_id = target.get("execution_id")
    detail = target.get("detail") if isinstance(target.get("detail"), dict) else {}
    if execution_id is None:
        return {
            "task_id": task_id,
            "execute_date": execute_date,
            "signal_date": target.get("signal_date"),
            "execution_id": None,
            "executed_at": None,
            "message": target.get("message"),
            "operations": [],
            "stock_summary": [],
            "trend_analysis": detail.get("trend_analysis") or {},
            "strategy_template": detail.get("strategy_template") or {},
            "selected_factors": detail.get("selected_factors") or [],
            "risk_budget": detail.get("risk_budget") or {},
            "target_positions": detail.get("target_positions") or [],
            "rebalance_actions": detail.get("rebalance_actions") or {},
            "current_positions": detail.get("current_positions") or [],
            "summary": detail.get("summary") or "",
            "selections": detail.get("selections") or [],
            "sell_triggers": detail.get("sell_triggers") or [],
            "blocked_events": detail.get("blocked_events") or [],
            "filter_summary": detail.get("filter_summary") or {},
            "buy_summary": detail.get("buy_summary") or {},
        }

    execution = get_execution(int(execution_id))
    executed_at = str((execution or {}).get("executed_at") or f"{execute_date}T09:30:00")
    ops = list_operations(execution_id=int(execution_id), limit=5000)
    ops = [
        x for x in ops
        if str(x.get("side") or "").upper() in {"BUY", "SELL"} and str(x.get("symbol") or "").strip()
    ]
    ops.sort(key=lambda x: int(x.get("id") or 0))
    symbols = {str(x.get("symbol") or "").strip() for x in ops}
    cost_state = _replay_cost_state_before(executed_at, symbols)
    ds = BacktestDataSource()
    snap = ds.load_snapshot(str(execute_date).replace("-", ""))
    close_map = {str(x.symbol).strip(): _safe_float(getattr(x, "close", 0)) for x in snap}

    op_details: list[dict] = []
    stock_summary_map: dict[str, dict] = {}
    for o in ops:
        sym = str(o.get("symbol") or "").strip()
        side = str(o.get("side") or "").upper()
        qty = _safe_int(o.get("quantity"))
        px = _safe_float(o.get("price"))
        close_px = _safe_float(close_map.get(sym))
        pnl = 0.0
        pnl_pct: float | None = None
        cost_price: float | None = None
        st = cost_state.setdefault(sym, {"qty": 0, "cost": 0.0})
        if side == "SELL":
            cur_qty = int(st["qty"])
            cur_cost = _safe_float(st["cost"])
            sell_qty = min(cur_qty, qty) if cur_qty > 0 else qty
            avg_cost = (cur_cost / cur_qty) if cur_qty > 0 else 0.0
            cost_price = round(avg_cost, 4) if avg_cost > 0 else None
            pnl = round((px - avg_cost) * sell_qty, 2) if avg_cost > 0 and sell_qty > 0 else 0.0
            pnl_pct = round((px / avg_cost - 1.0) * 100.0, 4) if avg_cost > 0 else None
            if cur_qty > 0 and sell_qty > 0:
                st["qty"] = cur_qty - sell_qty
                st["cost"] = max(0.0, cur_cost - avg_cost * sell_qty)
        else:
            if close_px > 0 and px > 0 and qty > 0:
                pnl = round((close_px - px) * qty, 2)
                pnl_pct = round((close_px / px - 1.0) * 100.0, 4)
            st["qty"] = int(st["qty"]) + qty
            st["cost"] = _safe_float(st["cost"]) + px * qty

        op_details.append(
            {
                "symbol": sym,
                "side": side,
                "quantity": qty,
                "trade_price": round(px, 4),
                "close_price": round(close_px, 4) if close_px > 0 else None,
                "cost_price": cost_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
        sm = stock_summary_map.setdefault(
            sym,
            {"symbol": sym, "buy_qty": 0, "sell_qty": 0, "turnover": 0.0, "pnl": 0.0},
        )
        if side == "BUY":
            sm["buy_qty"] += qty
        else:
            sm["sell_qty"] += qty
        sm["turnover"] = round(_safe_float(sm["turnover"]) + px * qty, 2)
        sm["pnl"] = round(_safe_float(sm["pnl"]) + pnl, 2)

    stock_summary = sorted(stock_summary_map.values(), key=lambda x: str(x.get("symbol") or ""))
    sold_cnt = _safe_int(target.get("sold_count"))
    has_sell_ops = any(str(x.get("side") or "").upper() == "SELL" for x in op_details)
    data_warning = None
    if sold_cnt > 0 and not has_sell_ops:
        data_warning = "该记录来自旧版本，卖出操作未关联 execution_id；节点收益仅覆盖已关联成交。"
    result = {
        "task_id": task_id,
        "execute_date": execute_date,
        "signal_date": target.get("signal_date"),
        "execution_id": int(execution_id),
        "executed_at": executed_at,
        "message": target.get("message"),
        "data_warning": data_warning,
        "operations": op_details,
        "stock_summary": stock_summary,
        "trend_analysis": detail.get("trend_analysis") or {},
        "strategy_template": detail.get("strategy_template") or {},
        "selected_factors": detail.get("selected_factors") or [],
        "risk_budget": detail.get("risk_budget") or {},
        "target_positions": detail.get("target_positions") or [],
        "rebalance_actions": detail.get("rebalance_actions") or {},
        "current_positions": detail.get("current_positions") or [],
        "summary": detail.get("summary") or "",
        "selections": detail.get("selections") or [],
        "sell_triggers": detail.get("sell_triggers") or [],
        "blocked_events": detail.get("blocked_events") or [],
        "filter_summary": detail.get("filter_summary") or {},
        "buy_summary": detail.get("buy_summary") or {},
    }


@router.post("/quant-runs/tasks")
def quant_run_task_create(req: QuantTaskCreateRequest) -> dict:
    exchange_group = normalize_exchange_group(req.exchange_group)
    replay_mode = str(req.replay_mode or "pragmatic_recover").strip().lower()
    if replay_mode not in {"pragmatic_recover", "strict_replay"}:
        replay_mode = "pragmatic_recover"
    task_id = create_quant_run_task(
        strategy_id=req.strategy_id,
        top_n=req.top_n,
        budget_ratio=req.budget_ratio,
        exchange_group=exchange_group,
        start_date=req.start_date,
        max_steps=req.max_steps,
        replay_mode=replay_mode,
    )
    strategy = get_strategy(req.strategy_id) or {}
    strategy_snapshot = {
        "id": int(strategy.get("id") or req.strategy_id),
        "name": str(strategy.get("name") or ""),
        "type": str(strategy.get("type") or ""),
        "content": strategy.get("content"),
        "captured_at": _utc_now_iso(),
    }
    risk_cfg = _default_task_risk_config()
    upsert_quant_run_task_meta(task_id, strategy_snapshot=strategy_snapshot, risk_config=risk_cfg)
    started = run_quant_task_async(task_id)
    task = get_quant_run_task(task_id)
    # Task creation changes the shared operations console.  The next read
    # serves its last durable snapshot and refreshes it outside the request.
    from app.services.console_snapshots import invalidate
    invalidate(("overview", "operations", "portfolio"), reason="quant_task_created")
    return {"ok": True, "task_id": task_id, "started": started, "task": _attach_risk_decision(task or {})}


@router.get("/quant-runs/ledger-summary")
def quant_run_ledger_summary() -> dict:
    try:
        from app.data_platform.services.market_calendar import target_trade_date
        from app.services.position_valuation import (
            PositionMarketDataNotReadyError,
            build_position_market_series_cache,
            missing_exact_position_valuations,
        )

        cash_balance = float(get_account_balance() or 0)
        baseline = float(get_account_baseline() or 0)
        positions = get_positions()
        with data_conn() as conn:
            latest_date = target_trade_date(conn)
        valuation_cache = build_position_market_series_cache(
            positions,
            as_of=latest_date,
        )
        missing = missing_exact_position_valuations(positions, valuation_cache, as_of=latest_date)
        if missing:
            raise PositionMarketDataNotReadyError(missing, latest_date, missing)
        market_value = 0.0
        for p in positions:
            sym = str(p.get("symbol") or "").strip()
            qty = int(p.get("quantity") or 0)
            if not sym or qty <= 0:
                continue
            valuation = valuation_cache.get(sym) or {}
            px = _safe_float(valuation.get("price"))
            market_value += px * qty
        market_value = round(market_value, 2)
        total_equity = round(cash_balance + market_value, 2)
        equity_delta = round(total_equity - baseline, 2)
        return {
            "ok": True,
            "status": "ready",
            "cash_balance": round(cash_balance, 2),
            "position_market_value": market_value,
            "total_equity": total_equity,
            "equity_delta": equity_delta,
            "position_count": len(positions),
            "valuation_date": latest_date,
        }
    except Exception as exc:  # noqa: BLE001 - read-only summary must not become a 500
        return degraded_read("ledger_summary", exc, items=[], count=0)


@router.get("/quant-runs/simulation-ledger")
def quant_run_simulation_ledger(
    execution_id: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    try:
        return build_simulation_ledger(limit=limit, execution_id=execution_id)
    except Exception as exc:  # noqa: BLE001 - optional ledger detail must degrade safely
        return degraded_read("simulation_ledger", exc, items=[], count=0)


def build_quant_run_performance(
    task_id: int | None = None,
    benchmark: str = "000300",
    *,
    persist_reports: bool = False,
) -> dict:
    """Return weekly/monthly/yearly performance aggregates for the simulator."""
    curve = quant_run_curve(limit=1000, benchmark=benchmark, task_id=task_id)
    if str(curve.get("status") or "") == "blocked":
        return {
            "ok": False,
            "status": "blocked",
            "reason": str(curve.get("reason") or "exact_account_close_missing"),
            "task_id": task_id,
            "benchmark": benchmark,
            "daily": [],
            "weekly": [],
            "monthly": [],
            "yearly": [],
            "source_summary": curve.get("summary") or {},
            "persisted_reports": None,
            "read_only": not persist_reports,
        }
    series = [x for x in (curve.get("series") or []) if x.get("date") and x.get("strategy_equity") is not None]
    previous_point: dict | None = None
    for point in series:
        previous_strategy = _safe_float((previous_point or {}).get("strategy_equity"))
        previous_strategy_cny = _safe_float((previous_point or {}).get("strategy_equity_cny"))
        previous_benchmark = _safe_float((previous_point or {}).get("benchmark_equity"))
        current_strategy = _safe_float(point.get("strategy_equity"))
        current_benchmark = _safe_float(point.get("benchmark_equity"))
        point["daily_return_pct"] = (
            round((current_strategy / previous_strategy - 1.0) * 100.0, 4)
            if previous_strategy > 0 and current_strategy > 0 else 0.0
        )
        point["daily_absolute_return"] = round(
            _safe_float(point.get("strategy_equity_cny")) - previous_strategy_cny, 4
        ) if previous_strategy_cny else 0.0
        point["daily_benchmark_return_pct"] = (
            round((current_benchmark / previous_benchmark - 1.0) * 100.0, 4)
            if previous_benchmark > 0 and current_benchmark > 0 else None
        )
        previous_point = point
    task_runs = list_quant_run_task_records(task_id, limit=2000) if task_id is not None else []
    if task_id is not None:
        operations = list_operations_for_execution_ids(
            {int(x["execution_id"]) for x in task_runs if x.get("execution_id") is not None},
            limit_per_execution=5000,
        )
    else:
        operations = list_operations(execution_id=None, limit=5000)
    operations.sort(key=lambda item: (str(item.get("at") or ""), int(item.get("id") or 0)))
    decisions: list[dict] = []
    try:
        review_records = list_reviews(limit=2000)
        intervention_records = list_position_state_history(limit=2000)
    except Exception:
        review_records, intervention_records = [], []

    attribution_by_date: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    seen_attribution_lots: set[tuple[int, int, int]] = set()
    try:
        decisions = list_decision_snapshots(limit=2000)
        for decision in decisions:
            if not decision.get("operation_id"):
                continue
            suggestion = build_review_outcome_suggestion(int(decision["id"]))
            attribution = (suggestion or {}).get("attribution") or {}
            if attribution.get("method") != "brinson_fifo_execution_v1":
                continue
            added_lot = False
            for lot in attribution.get("lots") or []:
                key = (int(lot.get("entry_operation_id") or 0), int(lot.get("exit_operation_id") or 0), int(lot.get("quantity") or 0))
                if key in seen_attribution_lots:
                    continue
                seen_attribution_lots.add(key)
                added_lot = True
                exit_date = str(lot.get("exit_at") or "")[:10]
                bucket = attribution_by_date[exit_date]
                bucket["market"] += float(lot.get("market_attribution") or 0)
                bucket["industry"] += float(lot.get("industry_attribution") or 0)
                bucket["selection"] += float(lot.get("selection_attribution") or 0)
            if added_lot:
                exit_date = str((attribution.get("lots") or [{}])[-1].get("exit_at") or "")[:10]
                attribution_by_date[exit_date]["execution"] += float(attribution.get("execution_attribution") or 0)
    except Exception:
        attribution_by_date = defaultdict(lambda: defaultdict(float))
    market_states = load_market_state_history()
    try:
        from app.services.decision_version_manifest import build_decision_version_manifest
        version_manifest = build_decision_version_manifest(
            trade_date=str((series[-1] if series else {}).get("date") or datetime.now().date().isoformat())
        )
    except Exception as exc:
        version_manifest = {
            "schema_version": "alis_decision_version_manifest_unavailable",
            "error": str(exc)[:300],
        }

    realized_by_date: dict[str, float] = defaultdict(float)
    lots_by_symbol: dict[str, list[dict[str, float]]] = defaultdict(list)
    for operation in operations:
        symbol = str(operation.get("symbol") or "")
        side = str(operation.get("side") or "").upper()
        qty = int(operation.get("quantity") or 0)
        price = _safe_float(operation.get("price"))
        fees = sum(_safe_float(operation.get(key)) for key in ("commission", "transfer_fee", "stamp_tax"))
        if not symbol or qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            lots_by_symbol[symbol].append({"remaining": float(qty), "unit_cost": price + fees / qty})
            continue
        if side != "SELL":
            continue
        remaining = qty
        net_proceeds = qty * price - fees
        matched_cost = 0.0
        matched = 0
        for lot in lots_by_symbol[symbol]:
            if remaining <= 0:
                break
            used = min(remaining, int(lot["remaining"]))
            if used <= 0:
                continue
            lot["remaining"] -= used
            remaining -= used
            matched += used
            matched_cost += used * lot["unit_cost"]
        if matched:
            matched_proceeds = net_proceeds * matched / qty
            realized_by_date[str(operation.get("at") or "")[:10]] += matched_proceeds - matched_cost

    def aggregate(key_fn):
        buckets: dict[str, list[dict]] = defaultdict(list)
        for point in series:
            buckets[key_fn(str(point["date"]))].append(point)
        out = []
        for key, points in sorted(buckets.items()):
            first, last = points[0], points[-1]
            start_index = _safe_float(first.get("strategy_equity")) or 100.0
            end_index = _safe_float(last.get("strategy_equity")) or start_index
            start = _safe_float(first.get("strategy_equity_cny")) or start_index
            end = _safe_float(last.get("strategy_equity_cny")) or end_index
            benchmark_points = [
                item for item in points if _safe_float(item.get("benchmark_equity")) > 0
            ]
            strategy_factor = 1.0
            for item in points:
                strategy_factor *= 1.0 + float(item.get("daily_return_pct") or 0.0) / 100.0
            benchmark_daily_returns = [
                float(item["daily_benchmark_return_pct"])
                for item in points if item.get("daily_benchmark_return_pct") is not None
            ]
            benchmark_complete = len(benchmark_daily_returns) == len(points)
            benchmark_factor = 1.0
            for daily_return in benchmark_daily_returns:
                benchmark_factor *= 1.0 + daily_return / 100.0
            drawdowns = [_safe_float(x.get("drawdown_pct")) for x in points]
            start_date, end_date = str(first["date"]), str(last["date"])
            period_ops = [x for x in operations if start_date <= str(x.get("at") or "")[:10] <= end_date]
            traded_amount = sum(_safe_float(x.get("price")) * int(x.get("quantity") or 0) for x in period_ops)
            transaction_cost = sum(
                sum(_safe_float(x.get(fee)) for fee in ("commission", "transfer_fee", "stamp_tax"))
                for x in period_ops
            )
            rejected = 0
            market_gate_blocks = 0
            period_blocked_events: list[dict] = []
            for run in task_runs:
                if not (start_date <= str(run.get("execute_date") or "") <= end_date):
                    continue
                detail = run.get("detail") or {}
                blocked = detail.get("blocked_events") or []
                period_blocked_events.extend(item for item in blocked if isinstance(item, dict))
                rejected += len(blocked)
                market_gate_blocks += sum(1 for item in blocked if "market" in str(item.get("code") or "").lower())
            model_error_count = sum(
                1 for review in review_records
                if start_date <= str(review.get("review_time") or "")[:10] <= end_date
                and str(review.get("error_type") or "") not in {"", "运气因素"}
            )
            human_interventions = [
                item for item in intervention_records
                if start_date <= str(item.get("changed_at") or "")[:10] <= end_date
                and str(item.get("intervention_type") or "none") != "none"
            ]
            realized_selection = sum(value for date, value in realized_by_date.items() if start_date <= date <= end_date)
            formal_attribution = {key: sum(value.get(key, 0.0) for date, value in attribution_by_date.items() if start_date <= date <= end_date) for key in ("market", "industry", "selection", "execution")}
            has_formal_attribution = any(start_date <= date <= end_date for date in attribution_by_date)
            absolute_return = end - start
            report = {
                "period": key,
                "start_date": start_date,
                "end_date": end_date,
                "return_pct": (
                    float(last.get("daily_return_pct") or 0)
                    if len(points) == 1 else
                    round((strategy_factor - 1.0) * 100.0, 4)
                ),
                "absolute_return": (
                    float(last.get("daily_absolute_return") or 0)
                    if len(points) == 1 else round(sum(float(item.get("daily_absolute_return") or 0.0) for item in points), 4)
                ),
                "benchmark_return_pct": (
                    last.get("daily_benchmark_return_pct")
                    if len(points) == 1 else
                    round((benchmark_factor - 1.0) * 100.0, 4) if benchmark_complete else None
                ),
                "benchmark_coverage": {
                    "start_date": (benchmark_points[0].get("date") if benchmark_points else None),
                    "end_date": (benchmark_points[-1].get("date") if benchmark_points else None),
                    "points": len(benchmark_points),
                    "return_points": len(benchmark_daily_returns),
                    "required_return_points": len(points),
                    "complete": benchmark_complete,
                },
                "max_drawdown_pct": round(min(drawdowns), 4) if drawdowns else 0.0,
                "turnover_rate": round(traded_amount / ((start + end) / 2.0), 6) if (start + end) > 0 else 0.0,
                "traded_amount": round(traded_amount, 4),
                "transaction_cost": round(transaction_cost, 4),
                "cash_defensive_contribution": 0.0,
                "stock_selection_contribution": round(formal_attribution["selection"] if has_formal_attribution else realized_selection, 4),
                "industry_allocation_contribution": round(formal_attribution["industry"], 4) if has_formal_attribution else None,
                "market_contribution": round(formal_attribution["market"], 4) if has_formal_attribution else None,
                "execution_contribution": round(formal_attribution["execution"], 4) if has_formal_attribution else round(-transaction_cost, 4),
                "market_state_gate": {"blocked_signals": market_gate_blocks},
                "model_error_count": model_error_count,
                "human_unexecuted_difference": {"count": len(human_interventions), "amount": None, "interventions": [{"ts_code": item.get("ts_code"), "type": item.get("intervention_type"), "actor": item.get("intervention_actor")} for item in human_interventions]},
                "rejected_signal_count": rejected,
                "attribution_status": "complete" if has_formal_attribution else "partial" if period_ops else "no_trades",
                "attribution_limitations": [] if has_formal_attribution else ["尚无已实现且绑定决策快照的FIFO批次", "人工未执行差异需存在用户否决记录"],
                "points": len(points),
            }
            out.append(enrich_performance_period(
                report,
                points=points,
                operations=period_ops,
                decisions=decisions,
                reviews=review_records,
                market_states=market_states,
                blocked_events=period_blocked_events,
                version_manifest=version_manifest,
            ))
        return out

    result = {
        "task_id": task_id,
        "benchmark": benchmark,
        # A daily row is the durable counterpart of the per-run
        # ``daily_rebalance_report``.  Keep it in the same aggregation path
        # as the longer horizons so all reports use identical equity, cost,
        # attribution and gate definitions.
        "daily": aggregate(lambda d: d),
        "weekly": aggregate(lambda d: f"{d[:4]}-W{datetime.fromisoformat(d).isocalendar().week:02d}"),
        "monthly": aggregate(lambda d: d[:7]),
        "yearly": aggregate(lambda d: d[:4]),
        "source_summary": curve.get("summary") or {},
    }
    if persist_reports:
        result["persisted_reports"] = save_performance_reports(
            task_id, {k: result[k] for k in ("daily", "weekly", "monthly", "yearly")}
        )
    else:
        result["persisted_reports"] = None
        result["read_only"] = True
    return result


@router.get("/quant-runs/performance")
def quant_run_performance(task_id: int | None = Query(None, ge=1), benchmark: str = "000300") -> dict:
    """Read performance without materialising or updating durable reports."""
    return build_quant_run_performance(task_id=task_id, benchmark=benchmark, persist_reports=False)


@router.get("/quant-runs/performance/reports")
def quant_run_performance_reports(task_id: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500)) -> dict:
    return {"items": list_performance_reports(task_id=task_id, limit=limit)}


@router.post("/quant-runs/tasks/{task_id}/stop")
def quant_run_task_stop(task_id: int) -> dict:
    task = get_quant_run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    status = normalize_status(task.get("status"))
    if status in {"failed", "stopped"}:
        return {"ok": True, "task_id": task_id, "message": f"任务已是 {status}"}
    if status in {"pending", "paused"} and (not can_transition(status, "stopped", event="manual_stop_finalize")):
        return {"ok": False, "task_id": task_id, "message": f"当前状态 {status} 不允许停止"}
    if status in {"running", "stopping"} and (not can_transition(status, "stopping", event="manual_stop_request")):
        return {"ok": False, "task_id": task_id, "message": f"当前状态 {status} 不允许停止"}
    if status in {"pending", "paused"}:
        update_quant_run_task(
            task_id,
            stop_requested=1,
            status="stopped",
            finished_at=_utc_now_iso(),
            last_message="任务已停止",
        )
        return {"ok": True, "task_id": task_id, "message": "任务已停止"}
    update_quant_run_task(task_id, stop_requested=1, status="stopping", last_message="停止请求已提交")
    return {"ok": True, "task_id": task_id, "message": "停止请求已提交"}


@router.post("/quant-runs/tasks/{task_id}/resume")
def quant_run_task_resume(task_id: int) -> dict:
    task = get_quant_run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    status = normalize_status(task.get("status"))
    if status in {"running", "stopping"}:
        return {"ok": True, "task_id": task_id, "started": False, "message": "任务正在运行中，无需继续"}
    if not can_transition(status, "pending", event="manual_resume"):
        return {"ok": False, "task_id": task_id, "started": False, "message": f"当前状态 {status} 不允许继续"}
    update_quant_run_task(
        task_id,
        status="pending",
        stop_requested=0,
        finished_at=None,
        error=None,
        last_message="任务已恢复，正在检查缺口并继续执行",
    )
    started = run_quant_task_async(task_id)
    return {"ok": True, "task_id": task_id, "started": started, "message": "任务已恢复"}


@router.delete("/quant-runs/tasks/{task_id}")
def quant_run_task_delete(task_id: int) -> dict:
    task = get_quant_run_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    status = str(task.get("status") or "").lower()
    if status in {"running", "stopping"}:
        raise HTTPException(status_code=400, detail="任务运行中，不能删除。请先停止后重试。")
    ok = delete_quant_run_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "task_id": task_id}


@router.get("/quant-runs/status")
def quant_run_status() -> dict:
    return get_quant_run_state_dict()


@router.get("/quant-runs/recent")
def quant_run_recent(limit: int = Query(10, ge=1, le=100)) -> dict:
    try:
        task_rows = list_recent_quant_run_task_records(limit=limit)
        if task_rows:
            return {
                "items": [
                    {
                        "at": str(x.get("created_at") or ""),
                        "signal_date": str(x.get("signal_date") or ""),
                        "execute_date": str(x.get("execute_date") or ""),
                        "sold_count": int(x.get("sold_count") or 0),
                        "bought_count": int(x.get("bought_count") or 0),
                        "total_buy_cost": float(x.get("total_buy_cost") or 0),
                        "message": str(x.get("message") or ""),
                    }
                    for x in task_rows
                ]
            }
        return {"items": get_quant_run_records(limit=limit)}
    except Exception as exc:  # noqa: BLE001 - optional quant evidence must degrade safely
        return degraded_read("quant_recent", exc, items=[])


@router.post("/quant-runs/start")
def quant_run_start(req: QuantRunStartRequest) -> dict:
    exchange_group = normalize_exchange_group(req.exchange_group)
    state = start_quant_run(
        QuantRunConfig(
            strategy_id=req.strategy_id,
            top_n=req.top_n,
            budget_ratio=req.budget_ratio,
            exchange_group=exchange_group,
        )
    )
    return {
        "running": state.running,
        "strategy_id": state.strategy_id,
        "top_n": state.top_n,
        "budget_ratio": state.budget_ratio,
        "exchange_group": state.exchange_group,
        "started_at": state.started_at,
        "message": state.last_message,
    }


@router.post("/quant-runs/stop")
def quant_run_stop() -> dict:
    state = stop_quant_run()
    return {
        "running": state.running,
        "stopped_at": state.stopped_at,
        "message": state.last_message,
    }


@router.post("/quant-runs/run-once")
def quant_run_once() -> dict:
    """Retired legacy runner; autonomous_daily is the only production writer."""
    raise HTTPException(
        status_code=410,
        detail="旧 run-once 入口已关闭；请使用 autonomous_daily 冻结计划执行器",
    )
    state = get_quant_run_state()
    if not state.running:
        raise HTTPException(status_code=400, detail="量化任务未启动，请先调用 /api/quant-runs/start")
    if not state.strategy_id:
        raise HTTPException(status_code=400, detail="缺少 strategy_id")
    try:
        dates = require_consolidated_trade_dates(min_count=2)
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_run_once_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )
    signal_date = dates[-2]
    execute_date = dates[-1]
    result = run_for_trade_date(
        strategy_id=state.strategy_id,
        signal_date=signal_date,
        execute_date=execute_date,
        top_n=state.top_n,
        budget_ratio=state.budget_ratio,
        exchange_group=state.exchange_group,
    )
    update_quant_run_progress(
        last_signal_date=result.signal_date,
        last_execute_date=result.execute_date,
        message=result.message,
    )
    append_quant_run_record(
        signal_date=result.signal_date,
        execute_date=result.execute_date,
        sold_count=result.sold_count,
        bought_count=result.bought_count,
        total_buy_cost=result.total_buy_cost,
        message=result.message,
    )
    return {
        "ok": True,
        "signal_date": result.signal_date,
        "execute_date": result.execute_date,
        "sold_count": result.sold_count,
        "bought_count": result.bought_count,
        "total_buy_cost": result.total_buy_cost,
        "message": result.message,
    }


@router.post("/quant-runs/execute-risk-rebalance")
def quant_run_execute_risk_rebalance(req: QuantRiskRebalanceRequest | None = None) -> dict:
    """Retired direct risk writer; risk reductions must be frozen in a plan."""
    raise HTTPException(
        status_code=410,
        detail="旧风险再平衡入口已关闭；减仓也必须绑定冻结调仓计划",
    )
    body = req or QuantRiskRebalanceRequest()
    try:
        ensure_manual_trade_allowed("风险再平衡")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        dates = require_consolidated_trade_dates(min_count=2)
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_risk_rebalance_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )
    strategy_id = int(body.strategy_id or get_default_strategy_id() or 1)
    signal_date = dates[-2]
    execute_date = dates[-1]
    portfolio_risk = build_live_portfolio_risk()
    actions = [
        item for item in (portfolio_risk.get("deleveraging_actions") or [])
        if str(item.get("action") or "") == "reduce" and item.get("symbol")
    ]
    exposures = {str(item.get("symbol") or ""): item for item in (portfolio_risk.get("exposures") or [])}
    positions = {str(item.get("symbol") or ""): item for item in get_positions()}
    equity = float(((portfolio_risk.get("account") or {}).get("equity")) or 0)
    if equity <= 0:
        raise HTTPException(status_code=400, detail="账户权益不足，无法执行风险再平衡")
    ds = BacktestDataSource()
    executed_at = f"{execute_date}T09:30:00"
    sell_orders: list[dict] = []
    blocked: list[dict] = []

    for action in actions:
        symbol = str(action.get("symbol") or "")
        pos = positions.get(symbol)
        if not pos:
            continue
        quantity = int(pos.get("quantity") or 0)
        if quantity <= 0:
            continue
        target_weight = float(action.get("target_weight") or 0)
        if target_weight <= 0:
            continue
        px = float(ds.get_price_on_date(symbol, execute_date, field="open") or 0)
        if px <= 0:
            bar = defensive_asset_bar(f"{symbol}.SH", execute_date) or defensive_asset_bar(f"{symbol}.SZ", execute_date)
            px = float((bar or {}).get("open") or (bar or {}).get("close") or 0)
        if px <= 0:
            blocked.append({"symbol": symbol, "side": "SELL", "code": "price_missing", "message": "执行日卖出价格缺失"})
            continue
        target_value = equity * target_weight
        current_value = float((exposures.get(symbol) or {}).get("market_value") or 0) or px * quantity
        excess_value = current_value - target_value
        sell_qty = min(quantity, int(excess_value // (px * 100)) * 100)
        if sell_qty <= 0 and current_value > target_value and quantity >= 100:
            sell_qty = 100
        if sell_qty <= 0:
            continue
        try:
            fee = sell_cost_breakdown(px, sell_qty, symbol)
            exec_price = float(fee["executed_price"])
            sell_position(int(pos["id"]), quantity=sell_qty, price=exec_price, trade_date=execute_date, record_operation=False)
        except ValueError as exc:
            blocked.append({"symbol": symbol, "side": "SELL", "code": "sell_constraint", "message": str(exc)})
            continue
        sell_orders.append({
            "symbol": symbol,
            "side": "SELL",
            "price": exec_price,
            "quantity": sell_qty,
            "at": executed_at,
            "remark": "风险再平衡卖出（单票超限降权）",
            "name": str(pos.get("name") or ""),
            **{k: fee[k] for k in ("commission", "transfer_fee", "stamp_tax", "slippage_bps", "fee_schedule_version")},
        })

    execution_id = None
    if sell_orders:
        execution_id = create_execution(strategy_id=strategy_id, executed_at=executed_at, budget=0.0, top_n=int(body.top_n), orders=[])
        append_operations_to_execution(execution_id, executed_at, sell_orders)
    message = f"risk-only execute={execute_date} 卖出{len(sell_orders)} 笔，拦截{len(blocked)} 笔"
    append_quant_run_record(
        signal_date=signal_date,
        execute_date=execute_date,
        sold_count=len(sell_orders),
        bought_count=0,
        total_buy_cost=0.0,
        message=message,
    )
    update_quant_run_progress(
        last_signal_date=signal_date,
        last_execute_date=execute_date,
        message=message,
    )
    return {
        "ok": True,
        "mode": "risk_rebalance",
        "strategy_id": strategy_id,
        "signal_date": signal_date,
        "execute_date": execute_date,
        "sold_count": len(sell_orders),
        "bought_count": 0,
        "total_buy_cost": 0.0,
        "execution_id": execution_id,
        "orders": sell_orders,
        "blocked_events": blocked,
        "message": message,
    }


@router.post("/quant-runs/catch-up")
def quant_run_catch_up(req: QuantRunCatchUpRequest) -> dict:
    """Retired catch-up writer; historical work must use isolated replay."""
    raise HTTPException(
        status_code=410,
        detail="旧 catch-up 入口已关闭；历史回放只能使用隔离账本 replay",
    )
    state = get_quant_run_state()
    if not state.running:
        raise HTTPException(status_code=400, detail="量化任务未启动，请先调用 /api/quant-runs/start")
    if not state.strategy_id:
        raise HTTPException(status_code=400, detail="缺少 strategy_id")
    try:
        dates = require_consolidated_trade_dates(min_count=2)
    except HistoricalDataMissingError as e:
        trigger_backfill_merge_meta_async(reason="quant_runs_catch_up_508")
        raise HTTPException(
            status_code=508,
            detail={
                "code": "historical_data_missing_508",
                "message": str(e),
                "action": "backfill_triggered",
            },
        )

    start_date = req.start_date or state.last_signal_date
    if start_date and start_date not in dates:
        raise HTTPException(status_code=400, detail=f"start_date 不在可用交易日列表: {start_date}")

    if start_date:
        start_idx = dates.index(start_date) + 1
    else:
        start_idx = max(0, len(dates) - 2)

    # signal_date 必须有 next execute_date，所以只到 len-2
    end_idx = len(dates) - 2
    if start_idx > end_idx:
        return {"ok": True, "steps": 0, "message": "没有需要补跑的交易日"}

    runs = []
    steps = 0
    for i in range(start_idx, end_idx + 1):
        if steps >= req.max_steps:
            break
        signal_date = dates[i]
        execute_date = dates[i + 1]
        result = run_for_trade_date(
            strategy_id=state.strategy_id,
            signal_date=signal_date,
            execute_date=execute_date,
            top_n=state.top_n,
            budget_ratio=state.budget_ratio,
            exchange_group=state.exchange_group,
        )
        update_quant_run_progress(
            last_signal_date=result.signal_date,
            last_execute_date=result.execute_date,
            message=result.message,
        )
        append_quant_run_record(
            signal_date=result.signal_date,
            execute_date=result.execute_date,
            sold_count=result.sold_count,
            bought_count=result.bought_count,
            total_buy_cost=result.total_buy_cost,
            message=result.message,
        )
        runs.append(
            {
                "signal_date": result.signal_date,
                "execute_date": result.execute_date,
                "sold_count": result.sold_count,
                "bought_count": result.bought_count,
                "total_buy_cost": result.total_buy_cost,
                "message": result.message,
            }
        )
        steps += 1

    return {"ok": True, "steps": steps, "runs": runs}

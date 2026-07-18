from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from app.db.store import get_account_balance, get_account_baseline, get_positions, list_operations, list_quant_run_task_records
try:
    from app.db.store import get_account_high_water_mark, update_account_high_water_mark
except ImportError:  # lightweight contract-test store shim
    get_account_high_water_mark = lambda: get_account_baseline()
    update_account_high_water_mark = lambda equity: equity
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import build_data_health_report

REQUIRED_MARKET_DAY_APIS = ("daily", "daily_basic", "stk_limit")


def historical_cvar(returns: list[float], confidence: float = 0.95) -> dict[str, float]:
    """Compute empirical left-tail VaR/CVaR for portfolio daily returns.

    This is the deterministic CVaR component used until the portfolio model
    supplies a larger simulated return distribution; it never silently falls
    back to a volatility-only threshold.
    """
    values = sorted(float(x) for x in returns if isinstance(x, (int, float)))
    if not values:
        return {"confidence": confidence, "var": 0.0, "cvar": 0.0, "observations": 0.0}
    level = min(0.999, max(0.5, float(confidence)))
    tail_n = max(1, int(len(values) * (1.0 - level)))
    tail = values[:tail_n]
    var = tail[-1]
    return {
        "confidence": level,
        "var": round(var, 8),
        "cvar": round(sum(tail) / len(tail), 8),
        "observations": float(len(values)),
    }


def garch11_forecast(returns: list[float], omega: float = 1e-6, alpha: float = 0.08, beta: float = 0.9) -> dict[str, float]:
    """Deterministic GARCH(1,1) one-step variance forecast."""
    values = [float(x) for x in returns if isinstance(x, (int, float))]
    if not values:
        return {"variance": 0.0, "volatility": 0.0, "observations": 0.0}
    a = min(0.99, max(0.0, float(alpha)))
    b = min(0.99 - a, max(0.0, float(beta)))
    variance = sum(x * x for x in values) / len(values)
    for value in values:
        variance = max(0.0, float(omega) + a * value * value + b * variance)
    return {"variance": round(variance, 10), "volatility": round(variance ** 0.5, 8), "observations": float(len(values))}


def kalman_level(values: list[float], process_variance: float = 1e-5, measurement_variance: float = 1e-3) -> dict[str, float]:
    """Scalar local-level Kalman filter for a dynamic factor observation."""
    obs = [float(x) for x in values if isinstance(x, (int, float))]
    if not obs:
        return {"state": 0.0, "state_variance": 0.0, "observations": 0.0}
    state = obs[0]
    variance = max(1e-12, float(measurement_variance))
    q = max(1e-12, float(process_variance))
    r = max(1e-12, float(measurement_variance))
    for measurement in obs[1:]:
        prior = variance + q
        gain = prior / (prior + r)
        state = state + gain * (measurement - state)
        variance = (1.0 - gain) * prior
    return {"state": round(state, 8), "state_variance": round(variance, 10), "observations": float(len(obs))}


def bocpd_change_probability(values: list[float], window: int = 20) -> dict[str, float]:
    """Lightweight BOCPD-style predictive surprise for regime monitoring."""
    obs = [float(x) for x in values if isinstance(x, (int, float))]
    if len(obs) < 3:
        return {"change_probability": 0.0, "regime": 0.0, "observations": float(len(obs))}
    w = max(2, min(int(window), len(obs) - 1))
    history = obs[-w - 1:-1]
    mean_v = sum(history) / len(history)
    var_v = sum((x - mean_v) ** 2 for x in history) / max(1, len(history) - 1)
    scale = max(var_v ** 0.5, 1e-6)
    surprise = abs(obs[-1] - mean_v) / scale
    probability = min(1.0, max(0.0, 1.0 - 2.718281828 ** (-0.5 * surprise * surprise)))
    regime = 1.0 if obs[-1] >= mean_v else -1.0
    return {"change_probability": round(probability, 8), "regime": regime, "observations": float(len(obs))}


def hsmm_regime(values: list[float], min_duration: int = 3) -> dict[str, Any]:
    """Compact semi-Markov regime label with duration persistence."""
    obs = [float(x) for x in values if isinstance(x, (int, float))]
    if not obs:
        return {"state": "unknown", "duration": 0, "transition_probability": 0.0}
    d = max(1, int(min_duration))
    recent = obs[-d:]
    mean_recent = sum(recent) / len(recent)
    if mean_recent > 0.002:
        state = "expansion"
    elif mean_recent < -0.002:
        state = "contraction"
    else:
        state = "neutral"
    duration = 1
    for x in reversed(obs[:-d]):
        if (x > 0.002 and state == "expansion") or (x < -0.002 and state == "contraction") or (abs(x) <= 0.002 and state == "neutral"):
            duration += 1
        else:
            break
    return {"state": state, "duration": duration, "transition_probability": round(1.0 / max(duration, 1), 8)}


@dataclass(frozen=True)
class RiskPauseDecision:
    should_pause: bool
    code: str
    message: str
    recovery_condition: str
    exit_condition: str
    severity: str = "warning"
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = self.evidence or {}
        return payload


def no_pause() -> RiskPauseDecision:
    return RiskPauseDecision(
        should_pause=False,
        code="ok",
        message="risk checks passed",
        recovery_condition="",
        exit_condition="",
        severity="info",
        evidence={},
    )


def pause_decision(
    *,
    code: str,
    message: str,
    recovery_condition: str,
    exit_condition: str,
    severity: str = "warning",
    evidence: dict[str, Any] | None = None,
) -> RiskPauseDecision:
    return RiskPauseDecision(
        should_pause=True,
        code=code,
        message=message,
        recovery_condition=recovery_condition,
        exit_condition=exit_condition,
        severity=severity,
        evidence=evidence or {},
    )


def evaluate_data_health_gate(limit_days: int = 5) -> RiskPauseDecision:
    report = build_data_health_report(limit_days=limit_days)
    if report.get("ok"):
        return no_pause()
    issue_codes = [str(x.get("code") or "") for x in report.get("issues", []) if x.get("level") == "error"]
    return pause_decision(
        code="paused_data_health_failed",
        message="数据健康检查未通过，暂停任务执行。",
        recovery_condition="执行 POST /api/init 或补齐本地缓存后，确认 GET /api/check/data-health 返回 ok=true，再点击继续。",
        exit_condition="若连续两次初始化后仍缺 all-in-one/snapshot/daily_basic，停止该策略任务并人工核对数据源。",
        severity="error",
        evidence={
            "issue_codes": issue_codes,
            "trade_dates": report.get("trade_dates", {}),
            "snapshots": report.get("snapshots", {}),
            "daily_basic": report.get("daily_basic", {}),
        },
    )


def evaluate_latest_market_day_gate() -> RiskPauseDecision:
    from app.data_platform.db import _conn

    with _conn() as conn:
        latest = conn.execute("SELECT MAX(trade_date) AS latest_trade_date FROM dc_available_trade_dates").fetchone()
        latest_trade_date = str(dict(latest).get("latest_trade_date") or "")
        if not latest_trade_date:
            latest = conn.execute("SELECT MAX(trade_date) AS latest_trade_date FROM dc_stock_daily_snapshot").fetchone()
            latest_trade_date = str(dict(latest).get("latest_trade_date") or "")
        if not latest_trade_date:
            return pause_decision(
                code="paused_latest_market_day_not_ready",
                message="最新交易日行情尚未物化，暂停任务执行。",
                recovery_condition="先完成收盘后的 daily、daily_basic、stk_limit 入库与快照构建，再继续任务。",
                exit_condition="若连续两次日更后仍缺最新交易日切片，停止自动任务并人工核对数据源。",
                severity="error",
                evidence={"latest_trade_date": "", "missing_apis": list(REQUIRED_MARKET_DAY_APIS), "snapshot_ready": False},
            )
        rows = conn.execute(
            f"""SELECT api_name,COUNT(*) AS rows FROM dc_tushare_raw_records
                WHERE trade_date=? AND api_name IN ({",".join("?" for _ in REQUIRED_MARKET_DAY_APIS)})
                GROUP BY api_name""",
            (latest_trade_date, *REQUIRED_MARKET_DAY_APIS),
        ).fetchall()
        present = {
            str(dict(row).get("api_name") or "")
            for row in rows
            if int(dict(row).get("rows") or 0) > 0
        }
        snapshot = conn.execute("SELECT COUNT(*) AS count FROM dc_stock_daily_snapshot WHERE trade_date=?", (latest_trade_date,)).fetchone()
    missing = [api for api in REQUIRED_MARKET_DAY_APIS if api not in present]
    snapshot_ready = int(dict(snapshot).get("count") or 0) > 0
    if not missing and snapshot_ready:
        return no_pause()
    return pause_decision(
        code="paused_latest_market_day_not_ready",
        message="最新交易日行情尚未物化，暂停任务执行。",
        recovery_condition="先完成收盘后的 daily、daily_basic、stk_limit 入库与快照构建，再继续任务。",
        exit_condition="若连续两次日更后仍缺最新交易日切片，停止自动任务并人工核对数据源。",
        severity="error",
        evidence={
            "latest_trade_date": latest_trade_date,
            "missing_apis": missing,
            "snapshot_ready": snapshot_ready,
            "required_apis": list(REQUIRED_MARKET_DAY_APIS),
        },
    )


def evaluate_model_readiness_gate(model_status: dict[str, Any] | None = None) -> RiskPauseDecision:
    """Use one production-model contract for UI decisions and task execution."""
    if model_status is None:
        from app.services.model_registry import get_model_status

        model_status = get_model_status()
    active = dict((model_status or {}).get("active_version") or {})
    if not active:
        return pause_decision(
            code="paused_model_not_ready",
            message="当前没有已激活的生产模型，暂停自动调仓。",
            recovery_condition="完成至少1512个交易日的训练与验证，并晋升一个production_eligible模型。",
            exit_condition="若暂不启用模型，只允许人工分析，不得由后台规则回退自动成交。",
            severity="error",
            evidence={"has_active_model": False},
        )
    validation = dict(active.get("validation") or {})
    eligibility = dict(validation.get("production_eligibility") or {})
    production_eligible = bool(validation.get("production_eligible")) and bool(eligibility.get("eligible", True))
    if not bool(active.get("is_active")) or str(validation.get("status") or "") != "passed" or not production_eligible:
        return pause_decision(
            code="paused_model_not_production_eligible",
            message="当前模型尚未同时满足激活、样本外验证和生产历史门槛，暂停自动调仓。",
            recovery_condition="补齐至少1512个交易日并通过holdout与walk-forward后重新晋升模型。",
            exit_condition="保持模拟任务暂停，禁止静默退回规则模型自动成交。",
            severity="error",
            evidence={
                "model_version_id": active.get("id"),
                "model_version": active.get("version"),
                "validation_status": validation.get("status"),
                "production_eligible": production_eligible,
                "production_eligibility": eligibility,
            },
        )
    return no_pause()


def _safe_float(v: object) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _estimate_run_pnl(record: dict[str, Any], ds: BacktestDataSource) -> float:
    execution_id = record.get("execution_id")
    if execution_id is None:
        return 0.0
    execute_date = str(record.get("execute_date") or "")
    if not execute_date:
        return 0.0
    pnl = 0.0
    for op in list_operations(execution_id=int(execution_id), limit=5000):
        side = str(op.get("side") or "").upper()
        qty = int(op.get("quantity") or 0)
        px = _safe_float(op.get("price"))
        if qty <= 0 or px <= 0:
            continue
        sym = str(op.get("symbol") or "").strip()
        close_px = _safe_float(ds.get_price_on_date(sym, execute_date, field="close"))
        if side == "BUY" and close_px > 0:
            pnl += (close_px - px) * qty
    return round(pnl, 2)


def evaluate_consecutive_loss_gate(
    *,
    task_id: int,
    max_consecutive_losses: int,
) -> RiskPauseDecision:
    max_losses = max(0, int(max_consecutive_losses or 0))
    if max_losses <= 0:
        return no_pause()
    records = list_quant_run_task_records(task_id, limit=max_losses + 10)
    if len(records) < max_losses:
        return no_pause()
    ds = BacktestDataSource()
    recent = list(reversed(records))
    loss_streak: list[dict[str, Any]] = []
    for rec in recent:
        pnl = _estimate_run_pnl(rec, ds)
        if pnl < 0:
            loss_streak.append(
                {
                    "execute_date": rec.get("execute_date"),
                    "signal_date": rec.get("signal_date"),
                    "execution_id": rec.get("execution_id"),
                    "estimated_intraday_pnl": pnl,
                }
            )
            if len(loss_streak) >= max_losses:
                break
            continue
        break
    if len(loss_streak) < max_losses:
        return no_pause()
    return pause_decision(
        code="paused_consecutive_losses",
        message=f"连续 {len(loss_streak)} 个执行节点估算亏损，暂停任务执行。",
        recovery_condition="人工复盘最近亏损节点，确认策略仍有效后手动点击继续；建议先降低 budget_ratio 或减少 top_n。",
        exit_condition="若复盘发现亏损来自策略逻辑失效、数据异常或连续滑点扩大，停止该策略并回到模拟/回测阶段。",
        severity="error",
        evidence={"loss_streak": loss_streak},
    )


def evaluate_account_drawdown_gate(*, max_drawdown: float = 0.10) -> RiskPauseDecision:
    """Hard floor for the independent simulation account.

    Uses a persisted account high-water mark, never a transient process value.
    """
    baseline = max(0.0, _safe_float(get_account_baseline()))
    cash = max(0.0, _safe_float(get_account_balance()))
    position_value = 0.0
    try:
        for pos in get_positions():
            position_value += max(0.0, _safe_float(pos.get("quantity"))) * max(0.0, _safe_float(pos.get("price")))
    except Exception:
        position_value = 0.0
    equity = cash + position_value
    peak = max(baseline, get_account_high_water_mark(), equity)
    update_account_high_water_mark(equity)
    threshold = peak * (1.0 - max(0.0, min(float(max_drawdown), 0.95)))
    drawdown = (equity / peak - 1.0) if peak > 0 else 0.0
    if peak > 0 and equity < threshold:
        return pause_decision(
            code="paused_max_drawdown",
            message=f"模拟账户权益 {equity:.2f} 已低于本金回撤 {max_drawdown:.0%} 门槛，暂停自动调仓。",
            recovery_condition="人工复核持仓和模型；确认风险后再恢复任务。",
            exit_condition="若数据或策略逻辑失效，停止任务并重新回测。",
            severity="error",
            evidence={"baseline": baseline, "high_water_mark": round(peak, 2), "equity": round(equity, 2), "drawdown": round(drawdown, 6), "threshold": round(threshold, 2)},
        )
    return no_pause()


def model_risk_multiplier(returns: list[float]) -> dict[str, float | str]:
    """Fixed model-constitution gate for cross-sectional scores.

    This is deliberately a bounded overlay, not a replacement for the
    learned ranker: regime/change-point and tail-risk evidence may reduce
    aggressiveness, while never turning a valid score into a buy signal by
    itself.
    """
    vals = [float(x) for x in returns if x is not None]
    regime = hsmm_regime(vals)
    change = bocpd_change_probability(vals)
    vol = garch11_forecast(vals)
    cvar = historical_cvar(vals)
    multiplier = 1.0
    state = str(regime.get("state") or "neutral")
    if state == "contraction":
        multiplier *= 0.75
    if float(change.get("p_change_1d") or 0.0) >= 0.65:
        multiplier *= 0.85
    if float(vol.get("forecast_volatility") or 0.0) > 0.04:
        multiplier *= 0.80
    if float(cvar.get("cvar") or 0.0) < -0.05:
        multiplier *= 0.80
    return {"multiplier": round(max(0.35, min(1.0, multiplier)), 6), "state": state}


def cvar_position_multiplier(returns: list[float], *, limit: float = 0.05) -> float:
    """Bounded CVaR multiplier for the portfolio allocator."""
    cvar = abs(float(historical_cvar(returns).get("cvar") or 0.0))
    if cvar <= max(0.0, float(limit)):
        return 1.0
    return round(max(0.35, min(1.0, float(limit) / cvar)), 6)


def ledoit_wolf_covariance(returns_by_symbol: dict[str, list[float]]) -> dict[str, Any]:
    """Small deterministic Ledoit-Wolf-style diagonal shrinkage estimator."""
    symbols = [s for s, vals in returns_by_symbol.items() if len(vals) >= 2]
    if not symbols:
        return {"symbols": [], "covariance": [], "shrinkage": 1.0}
    n = max(len(returns_by_symbol[s]) for s in symbols)
    matrix = []
    for s in symbols:
        vals = [float(x) for x in returns_by_symbol[s]]
        mean = sum(vals) / len(vals)
        matrix.append(vals + [mean] * (n - len(vals)))
    means = [sum(col) / len(col) for col in matrix]
    cov: list[list[float]] = []
    for i, left in enumerate(matrix):
        row = []
        for j, right in enumerate(matrix):
            row.append(sum((left[k] - means[i]) * (right[k] - means[j]) for k in range(n)) / max(1, n - 1))
        cov.append(row)
    avg_var = sum(cov[i][i] for i in range(len(symbols))) / len(symbols)
    shrink = 0.20
    shrunk = [[(1 - shrink) * cov[i][j] + (shrink * avg_var if i == j else 0.0) for j in range(len(symbols))] for i in range(len(symbols))]
    return {"symbols": symbols, "covariance": shrunk, "shrinkage": shrink}


def constrained_cvar_weights(returns_by_symbol: dict[str, list[float]], *, max_single: float = 0.25) -> dict[str, float]:
    """Risk-budget weights with CVaR-aware scaling and hard single-name cap."""
    if not returns_by_symbol:
        return {}
    raw: dict[str, float] = {}
    for symbol, values in returns_by_symbol.items():
        vals = [float(x) for x in values]
        variance = sum(x * x for x in vals) / max(1, len(vals)) if vals else 1.0
        raw[symbol] = cvar_position_multiplier(vals) / max(math.sqrt(variance), 1e-6)
    total = sum(raw.values()) or 1.0
    weights = {s: min(float(max_single), v / total) for s, v in raw.items()}
    remainder = max(0.0, 1.0 - sum(weights.values()))
    eligible = [s for s in weights if weights[s] < float(max_single) - 1e-9]
    while remainder > 1e-8 and eligible:
        add = remainder / len(eligible)
        next_eligible = []
        for s in eligible:
            room = float(max_single) - weights[s]
            inc = min(room, add)
            weights[s] += inc
            remainder -= inc
            if room - inc > 1e-9:
                next_eligible.append(s)
        if len(next_eligible) == len(eligible):
            break
        eligible = next_eligible
    return {s: round(v, 8) for s, v in weights.items()}


def enforce_marginal_cvar_limit(
    weights: dict[str, float], returns_by_symbol: dict[str, list[float]], *, max_contribution: float = .12,
) -> dict[str, Any]:
    """Enforce the B11.2 Euler-style standalone tail-risk budget ceiling."""
    symbols = [symbol for symbol in weights if returns_by_symbol.get(symbol)]
    limit = max(0.01, min(1.0, float(max_contribution)))
    minimum_names = math.ceil(1.0 / limit)
    if len(symbols) < minimum_names:
        return {"status": "infeasible", "weights": {}, "contributions": {}, "minimum_names": minimum_names, "reason": f"正风险贡献标的少于{minimum_names}只，无法同时满足单股边际CVaR贡献≤{limit:.0%}"}
    tail_risk = {
        symbol: max(1e-8, abs(float(historical_cvar(returns_by_symbol[symbol]).get("cvar") or 0)))
        for symbol in symbols
    }
    inverse = {symbol: 1.0 / risk for symbol, risk in tail_risk.items()}
    total_inverse = sum(inverse.values()) or 1.0
    risk_parity_weights = {symbol: inverse[symbol] / total_inverse for symbol in symbols}
    total_risk = sum(risk_parity_weights[symbol] * tail_risk[symbol] for symbol in symbols) or 1.0
    contributions = {symbol: risk_parity_weights[symbol] * tail_risk[symbol] / total_risk for symbol in symbols}
    breached = {symbol: value for symbol, value in contributions.items() if value > limit + 1e-8}
    return {
        "status": "ready" if not breached else "infeasible",
        "weights": {symbol: round(value, 8) for symbol, value in risk_parity_weights.items()} if not breached else {},
        "contributions": {symbol: round(value, 8) for symbol, value in contributions.items()},
        "minimum_names": minimum_names,
        "limit": limit,
        "breaches": breached,
    }


def hrp_fallback_weights(returns_by_symbol: dict[str, list[float]], *, max_single: float = 0.25) -> dict[str, float]:
    """Deterministic inverse-volatility HRP-style fallback.

    It is intentionally conservative and only used when the primary CVaR
    optimizer cannot produce usable observations.
    """
    valid = {s: [float(x) for x in v] for s, v in returns_by_symbol.items() if v}
    if not valid:
        return {}
    raw = {s: 1.0 / max(math.sqrt(sum(x * x for x in vals) / len(vals)), 1e-6) for s, vals in valid.items()}
    total = sum(raw.values()) or 1.0
    weights = {s: min(float(max_single), v / total) for s, v in raw.items()}
    norm = sum(weights.values()) or 1.0
    return {s: round(v / norm, 8) for s, v in weights.items()}

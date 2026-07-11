from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.db.store import list_operations, list_quant_run_task_records
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import build_data_health_report


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

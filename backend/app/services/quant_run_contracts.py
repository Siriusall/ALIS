"""Stable execution contracts shared by quant APIs, workers, and engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


class HistoricalDataMissingError(Exception):
    """Historical data is insufficient for a replay or execution request."""

    def __init__(self, message: str = "历史交易数据不足，无法执行任务", code: str = "historical_data_missing_508"):
        self.status_code = 508
        self.code = code
        super().__init__(message)


class ReplayRiskGateError(Exception):
    """A deterministic data, account, or execution risk gate blocked mutation."""

    def __init__(self, message: str, code: str = "replay_risk_gate"):
        self.code = code
        super().__init__(message)


def require_consistent_simulation_ledger() -> None:
    """Stop new mutations when the account projection differs from its ledger."""
    from app.services.simulation_ledger_audit import audit_simulation_ledger

    audit = audit_simulation_ledger(persist=False)
    if audit.get("ok"):
        return
    raise ReplayRiskGateError(
        "模拟账户独立账本校验未通过，已阻断新成交：" + "；".join(
            str(item.get("message") or "") for item in (audit.get("issues") or [])
        ),
        code="paused_simulation_ledger_drift",
    )


@dataclass
class QuantRunExecResult:
    signal_date: str
    execute_date: str
    sold_count: int
    bought_count: int
    total_buy_cost: float
    execution_id: int | None
    message: str
    planned_buy_count: int = 0
    executed_buy_count: int = 0
    fill_rate_pct: float = 0.0
    avg_slippage_bps: float = 0.0
    exec_latency_ms: int = 0
    detail: Dict[str, Any] | None = None

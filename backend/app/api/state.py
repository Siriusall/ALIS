from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
import threading
from typing import List

from app.core.config import settings
from app.schemas.log import LogEntryResponse


_logs: List[LogEntryResponse] = []


def add_log(level: str, message: str) -> None:
    entry = LogEntryResponse(
        timestamp=datetime.now().isoformat(),
        level=level,
        message=message,
    )
    _logs.append(entry)
    if len(_logs) > settings.log_limit:
        _logs.pop(0)


def get_logs() -> List[LogEntryResponse]:
    return list(_logs)


@dataclass
class QuantRunConfig:
    strategy_id: int
    top_n: int = 10
    budget_ratio: float = 1.0
    exchange_group: str = "HS"


@dataclass
class QuantRunState:
    running: bool = False
    started_at: str | None = None
    stopped_at: str | None = None
    strategy_id: int | None = None
    top_n: int = 10
    budget_ratio: float = 1.0
    exchange_group: str = "HS"
    last_signal_date: str | None = None
    last_execute_date: str | None = None
    last_message: str | None = None


@dataclass
class QuantRunRecord:
    at: str
    signal_date: str
    execute_date: str
    sold_count: int
    bought_count: int
    total_buy_cost: float
    message: str


_quant_lock = threading.Lock()
_quant_state = QuantRunState()
_quant_records: list[QuantRunRecord] = []


def _now_iso() -> str:
    return datetime.now().isoformat()


def get_quant_run_state() -> QuantRunState:
    with _quant_lock:
        return QuantRunState(**asdict(_quant_state))


def get_quant_run_state_dict() -> dict:
    with _quant_lock:
        return asdict(_quant_state)


def start_quant_run(config: QuantRunConfig) -> QuantRunState:
    with _quant_lock:
        _quant_state.running = True
        _quant_state.started_at = _now_iso()
        _quant_state.stopped_at = None
        _quant_state.strategy_id = int(config.strategy_id)
        _quant_state.top_n = int(config.top_n)
        _quant_state.budget_ratio = float(config.budget_ratio)
        _quant_state.exchange_group = str(config.exchange_group or "HS")
        _quant_state.last_message = "量化任务已启动"
        return QuantRunState(**asdict(_quant_state))


def stop_quant_run(message: str = "量化任务已停止") -> QuantRunState:
    with _quant_lock:
        _quant_state.running = False
        _quant_state.stopped_at = _now_iso()
        _quant_state.last_message = message
        return QuantRunState(**asdict(_quant_state))


def update_quant_run_progress(
    *,
    last_signal_date: str | None = None,
    last_execute_date: str | None = None,
    message: str | None = None,
) -> QuantRunState:
    with _quant_lock:
        if last_signal_date:
            _quant_state.last_signal_date = last_signal_date
        if last_execute_date:
            _quant_state.last_execute_date = last_execute_date
        if message is not None:
            _quant_state.last_message = message
        return QuantRunState(**asdict(_quant_state))


def ensure_manual_trade_allowed(action: str) -> None:
    with _quant_lock:
        if _quant_state.running:
            raise RuntimeError(f"量化任务运行中，禁止手动{action}；仅允许手动平仓")


def append_quant_run_record(
    *,
    signal_date: str,
    execute_date: str,
    sold_count: int,
    bought_count: int,
    total_buy_cost: float,
    message: str,
) -> None:
    with _quant_lock:
        _quant_records.append(
            QuantRunRecord(
                at=_now_iso(),
                signal_date=signal_date,
                execute_date=execute_date,
                sold_count=int(sold_count),
                bought_count=int(bought_count),
                total_buy_cost=round(float(total_buy_cost), 2),
                message=message,
            )
        )
        # 只保留最近 500 条，避免内存无限增长
        if len(_quant_records) > 500:
            del _quant_records[:-500]


def get_quant_run_records(limit: int = 20) -> list[dict]:
    n = max(1, min(int(limit or 20), 200))
    with _quant_lock:
        rows = list(reversed(_quant_records[-n:]))
    return [asdict(x) for x in rows]

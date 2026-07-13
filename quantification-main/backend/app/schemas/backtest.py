from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class BacktestRunRequest(BaseModel):
    strategy_id: Optional[int] = Field(None, ge=1)
    model_version_id: Optional[int] = Field(None, ge=1)
    start_date: str = Field(..., description="YYYY-MM-DD 或 YYYYMMDD")
    end_date: str = Field(..., description="YYYY-MM-DD 或 YYYYMMDD")
    initial_cash: float = Field(200_000, ge=1000)
    rebalance_freq: Optional[str] = Field(None, description="daily|weekly|monthly")
    benchmark_symbol: str = Field("000300")
    exchange_group: str = Field("HS", description="ALL|HS|CYB|BJ")


class BacktestMetrics(BaseModel):
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    turnover_rate: float
    benchmark_return: float
    excess_return: float


class BacktestRunResponse(BaseModel):
    backtest_id: int
    strategy_id: int
    model_version_id: Optional[int] = None
    model_version: Optional[str] = None
    strategy_name: str
    start_date: str
    end_date: str
    initial_cash: float
    rebalance_freq: str
    top_n: int
    benchmark_symbol: str
    metrics: BacktestMetrics
    warnings: List[str] = []
    data_quality: dict[str, Any] = {}
    trade_constraints: dict[str, Any] = {}


class BacktestRunAsyncResponse(BaseModel):
    job_id: str
    status: str
    total_days: int = 0


class BacktestDailyProgress(BaseModel):
    date: str
    total_value: float
    benchmark_value: float
    trades_count: int


class BacktestJobResponse(BaseModel):
    job_id: str
    status: str
    processed_days: int
    total_days: int
    phase: Optional[str] = None
    universe_processed: int = 0
    universe_total: int = 0
    last_date: Optional[str] = None
    backtest_id: Optional[int] = None
    error: Optional[str] = None
    daily_records: List[BacktestDailyProgress] = []


class BacktestEquityResponse(BaseModel):
    backtest_id: int
    equity_series: list[dict]
    benchmark_series: list[dict]


class BacktestTradesResponse(BaseModel):
    backtest_id: int
    trades: list[dict]


class BacktestListItem(BaseModel):
    id: int
    strategy_id: int
    strategy_name: str
    started_at: str
    start_date: str
    end_date: str
    rebalance_freq: str

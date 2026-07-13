import { api } from "./client";

export interface BacktestMetrics {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  turnover_rate: number;
  benchmark_return: number;
  excess_return: number;
}

export interface BacktestRunResponse {
  backtest_id: number;
  strategy_id: number;
  model_version_id?: number | null;
  model_version?: string | null;
  strategy_name: string;
  start_date: string;
  end_date: string;
  initial_cash: number;
  rebalance_freq: string;
  top_n: number;
  benchmark_symbol: string;
  metrics: BacktestMetrics;
  warnings: string[];
  data_quality?: {
    used_fallback?: boolean;
    fallback_events?: {
      type: string;
      target_date?: string;
      fallback_date?: string;
      rows?: string;
    }[];
    warnings?: string[];
  };
  trade_constraints?: {
    policy?: Record<string, unknown>;
    blocked_count?: number;
    blocked_events?: {
      date: string;
      symbol: string;
      side: string;
      code: string;
      message: string;
    }[];
  };
}

export interface BacktestListItem {
  id: number;
  strategy_id: number;
  strategy_name: string;
  started_at: string;
  start_date: string;
  end_date: string;
  rebalance_freq: string;
}

export interface BacktestEquityResponse {
  backtest_id: number;
  equity_series: { date: string; total_value: number }[];
  benchmark_series: { date: string; total_value: number }[];
}

export interface BacktestTradesResponse {
  backtest_id: number;
  trades: {
    date: string;
    symbol: string;
    side: string;
    price: number;
    quantity: number;
    gross_amount: number;
    fees: number;
    pnl?: number | null;
    reason?: string;
  }[];
}

export interface BacktestRunAsyncResponse {
  job_id: string;
  status: string;
  total_days: number;
}

export interface BacktestDailyProgress {
  date: string;
  total_value: number;
  benchmark_value: number;
  trades_count: number;
}

export interface BacktestJobResponse {
  job_id: string;
  status: "running" | "completed" | "failed";
  processed_days: number;
  total_days: number;
  phase?: string | null;
  universe_processed?: number;
  universe_total?: number;
  last_date?: string | null;
  backtest_id?: number | null;
  error?: string | null;
  daily_records: BacktestDailyProgress[];
}

const DETAIL_TIMEOUT_MS = 30_000;

export async function runBacktest(body: {
  strategy_id?: number | null;
  model_version_id?: number | null;
  start_date: string;
  end_date: string;
  initial_cash: number;
  rebalance_freq?: string;
  benchmark_symbol?: string;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
}): Promise<BacktestRunResponse> {
  const { data } = await api.post<BacktestRunResponse>("/api/backtest/run", body, {
    timeout: 10 * 60 * 1000,
  });
  return data;
}

export async function runBacktestAsync(body: {
  strategy_id?: number | null;
  model_version_id?: number | null;
  start_date: string;
  end_date: string;
  initial_cash: number;
  rebalance_freq?: string;
  benchmark_symbol?: string;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
}): Promise<BacktestRunAsyncResponse> {
  const { data } = await api.post<BacktestRunAsyncResponse>("/api/backtest/run-async", body, {
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function fetchBacktestList(limit = 30): Promise<BacktestListItem[]> {
  const { data } = await api.get<BacktestListItem[]>("/api/backtest/list", {
    params: { limit },
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function fetchBacktest(backtestId: number): Promise<BacktestRunResponse> {
  const { data } = await api.get<BacktestRunResponse>(`/api/backtest/${backtestId}`, {
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function fetchBacktestEquity(backtestId: number): Promise<BacktestEquityResponse> {
  const { data } = await api.get<BacktestEquityResponse>(`/api/backtest/${backtestId}/equity`, {
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function fetchBacktestTrades(backtestId: number): Promise<BacktestTradesResponse> {
  const { data } = await api.get<BacktestTradesResponse>(`/api/backtest/${backtestId}/trades`, {
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function fetchBacktestJob(jobId: string): Promise<BacktestJobResponse> {
  const { data } = await api.get<BacktestJobResponse>(`/api/backtest/job/${jobId}`, {
    timeout: DETAIL_TIMEOUT_MS,
  });
  return data;
}

export async function deleteBacktest(backtestId: number): Promise<void> {
  await api.delete(`/api/backtest/${backtestId}`, {
    timeout: DETAIL_TIMEOUT_MS,
  });
}

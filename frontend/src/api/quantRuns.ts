import { api } from "./client";
import type {
  QuantAnalyzeResponse,
  QuantExecuteBuyResponse,
  QuantRebalancePreviewResponse,
  QuantRunCurveResponse,
  QuantRunLedgerSummary,
  QuantRunRecentRecord,
  QuantRunStatus,
  QuantTaskRunDetailResponse,
  QuantTaskItem,
  SimulationLedgerResponse,
} from "../types";

export interface QuantRunStartPayload {
  strategy_id: number;
  top_n: number;
  budget_ratio: number;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
}

export interface QuantRunCatchUpPayload {
  start_date?: string;
  max_steps: number;
}

export interface QuantTaskCreatePayload {
  strategy_id: number;
  top_n: number;
  budget_ratio: number;
  exchange_group: "ALL" | "HS" | "CYB" | "BJ";
  start_date?: string;
  max_steps: number;
  replay_mode?: "pragmatic_recover" | "strict_replay";
}

export interface PerformancePeriod {
  period: string;
  start_date: string;
  end_date: string;
  return_pct: number;
  benchmark_return_pct: number | null;
  max_drawdown_pct: number;
  transaction_cost: number;
  stock_selection_contribution: number;
  industry_allocation_contribution: number | null;
  market_contribution: number | null;
  execution_contribution: number;
  attribution_status: string;
  learning_status?: "healthy" | "attention";
  learning_signals?: Array<{ code: string; severity: string; target: string; value: number }>;
  market_state_breakdown?: Record<string, {
    sessions: number;
    return_pct: number;
    benchmark_return_pct: number;
    average_confidence: number;
    market_model_versions: string[];
  }>;
  model_version_breakdown?: Record<string, {
    decision_count: number;
    buy_count: number;
    sell_count: number;
    reviewed_count: number;
    error_count: number;
    error_types: Record<string, number>;
    symbols: string[];
  }>;
  error_breakdown?: {
    review_error_types: Record<string, number>;
    decision_error_types?: Record<string, number>;
    review_activity_error_types?: Record<string, number>;
    blocked_signal_codes: Record<string, number>;
  };
}

export interface QuantPerformanceOverview {
  daily: PerformancePeriod[];
  weekly: PerformancePeriod[];
  monthly: PerformancePeriod[];
  yearly: PerformancePeriod[];
}

export async function analyzeQuantMarket(payload?: {
  signal_date?: string;
  top_n?: number;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
}): Promise<QuantAnalyzeResponse> {
  const { data } = await api.post<QuantAnalyzeResponse>("/api/quant-runs/analyze", payload ?? {});
  return data;
}

export async function previewQuantAnalyzeRebalance(payload: {
  signal_date: string;
  strategy_id?: number;
  symbols: string[];
  target_position_pct?: number;
  max_single_position_pct?: number;
  mode?: "buy_only" | "full_rebalance";
  target_symbols?: string[];
  selection_scores?: Record<string, number>;
}): Promise<QuantRebalancePreviewResponse> {
  const { data } = await api.post<QuantRebalancePreviewResponse>("/api/quant-runs/execute-preview", payload);
  return data;
}

export async function executeQuantAnalyzeBuy(payload: {
  signal_date: string;
  strategy_id?: number;
  symbols: string[];
  target_position_pct?: number;
  max_single_position_pct?: number;
  mode?: "buy_only" | "full_rebalance";
  target_symbols?: string[];
  selection_scores?: Record<string, number>;
}): Promise<QuantExecuteBuyResponse> {
  const { data } = await api.post<QuantExecuteBuyResponse>("/api/quant-runs/execute-buy", payload);
  return data;
}

export async function getQuantRunStatus(): Promise<QuantRunStatus> {
  const { data } = await api.get<QuantRunStatus>("/api/quant-runs/status");
  return data;
}

export async function startQuantRun(payload: QuantRunStartPayload): Promise<QuantRunStatus> {
  const { data } = await api.post<QuantRunStatus>("/api/quant-runs/start", payload);
  return data;
}

export async function stopQuantRun(): Promise<QuantRunStatus> {
  const { data } = await api.post<QuantRunStatus>("/api/quant-runs/stop");
  return data;
}

export async function runQuantRunOnce(): Promise<{
  ok: boolean;
  signal_date: string;
  execute_date: string;
  sold_count: number;
  bought_count: number;
  total_buy_cost: number;
  message: string;
}> {
  const { data } = await api.post("/api/quant-runs/run-once");
  return data;
}

export async function catchUpQuantRun(payload: QuantRunCatchUpPayload): Promise<{
  ok: boolean;
  steps: number;
  message?: string;
  runs?: Array<{
    signal_date: string;
    execute_date: string;
    sold_count: number;
    bought_count: number;
    total_buy_cost: number;
    message: string;
  }>;
}> {
  const { data } = await api.post("/api/quant-runs/catch-up", payload);
  return data;
}

export async function getQuantRunCurve(
  limit = 250,
  benchmark = "000300",
  taskId?: number | null
): Promise<QuantRunCurveResponse> {
  const { data } = await api.get<QuantRunCurveResponse>("/api/quant-runs/curve", {
    params: { limit, benchmark, task_id: taskId ?? undefined },
  });
  return data;
}

export async function getQuantRunRecent(limit = 10): Promise<QuantRunRecentRecord[]> {
  const { data } = await api.get<{ items: QuantRunRecentRecord[] }>("/api/quant-runs/recent", {
    params: { limit },
  });
  return data?.items ?? [];
}

export async function listQuantTasks(limit = 50): Promise<QuantTaskItem[]> {
  const { data } = await api.get<{ items: QuantTaskItem[] }>("/api/quant-runs/tasks", {
    params: { limit },
  });
  return data?.items ?? [];
}

export async function createQuantTask(payload: QuantTaskCreatePayload): Promise<{
  ok: boolean;
  task_id: number;
  started: boolean;
  task?: QuantTaskItem;
}> {
  const { data } = await api.post("/api/quant-runs/tasks", payload);
  return data;
}

export async function stopQuantTask(taskId: number): Promise<{ ok: boolean; task_id: number; message?: string }> {
  const { data } = await api.post(`/api/quant-runs/tasks/${taskId}/stop`);
  return data;
}

export async function resumeQuantTask(taskId: number): Promise<{ ok: boolean; task_id: number; started?: boolean; message?: string }> {
  const { data } = await api.post(`/api/quant-runs/tasks/${taskId}/resume`);
  return data;
}

export async function deleteQuantTask(taskId: number): Promise<{ ok: boolean; task_id: number }> {
  const { data } = await api.delete(`/api/quant-runs/tasks/${taskId}`);
  return data;
}

export async function getQuantTaskRunDetail(taskId: number, executeDate: string): Promise<QuantTaskRunDetailResponse> {
  const { data } = await api.get<QuantTaskRunDetailResponse>(`/api/quant-runs/tasks/${taskId}/run-detail`, {
    params: { execute_date: executeDate },
  });
  return data;
}

export async function getQuantRunLedgerSummary(): Promise<QuantRunLedgerSummary> {
  const { data } = await api.get<QuantRunLedgerSummary>("/api/quant-runs/ledger-summary");
  return data;
}

export async function getQuantPerformance(): Promise<QuantPerformanceOverview> {
  const { data } = await api.get<QuantPerformanceOverview>("/api/quant-runs/performance");
  return data;
}

export async function getSimulationLedger(params?: {
  execution_id?: number;
  limit?: number;
}): Promise<SimulationLedgerResponse> {
  const { data } = await api.get<SimulationLedgerResponse>("/api/quant-runs/simulation-ledger", {
    params,
  });
  return data;
}

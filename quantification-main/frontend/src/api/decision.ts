import { api } from "./client";
import type { DataHealthReport } from "../types";
import type { ModelStrategyStatus } from "./modelStrategy";

export interface DecisionGate {
  tone: "go" | "hold" | "stop";
  title: string;
  detail: string;
  reason_code: string;
}

export interface DecisionLedger {
  cash_balance: number;
  position_market_value: number;
  total_equity: number;
  equity_delta: number;
  position_count: number;
  valuation_date: string;
  cash_pct: number;
  max_position_pct: number;
  max_position_symbol: string;
  initial_cash: number;
  high_water_mark: number;
  drawdown: number;
  account_risks: Array<{ code: string; level: string; message: string }>;
}

export interface DecisionMarketOverview {
  ok: boolean;
  status: string;
  reason?: string;
  trade_date?: string;
  available_time?: string;
  market_state: null | {
    state: string;
    label: string;
    confidence: number;
    model: string;
    reason: string;
    risk_budget_multiplier: number;
  };
  sentiment: null | {
    score: number;
    state: string;
    crowding_penalty: number;
    components: Record<string, number>;
  };
  observations?: Record<string, number>;
  evidence?: { latest_date?: string; previous_date?: string; symbols?: number; amount_dates?: number };
  portfolio_risk?: {
    ok?: boolean;
    status?: string;
    breaches?: Array<{ code?: string; level?: string; message?: string }>;
    limitations?: string[];
  };
  position_gate?: {
    allow_new_exposure: boolean;
    market_multiplier: number;
    portfolio_multiplier: number;
    combined_risk_budget_multiplier: number;
    reason: string;
  };
  themes: { status: string; items: Array<{ theme: string; score: number; state: string }>; reason?: string };
  limitations: string[];
}

export interface DecisionExecutionOperation {
  symbol: string;
  name?: string;
  side: string;
  price: number;
  quantity: number;
  at: string;
  remark?: string;
}

export interface DecisionRecentExecution {
  execution_id: number;
  executed_at: string;
  strategy_id: number;
  budget: number;
  top_n: number;
  buy_count: number;
  sell_count: number;
  total_buy_cost: number;
  operations: DecisionExecutionOperation[];
}

export interface DecisionModelValidation {
  backtest_id: number;
  model_version_id: number;
  model_version: string;
  start_date: string;
  end_date: string;
  metrics: Record<string, number>;
}

export interface DecisionSummary {
  data_health: DataHealthReport;
  ledger: DecisionLedger;
  model_status: ModelStrategyStatus;
  market_overview: DecisionMarketOverview;
  gate_decision: DecisionGate;
  recent_execution: DecisionRecentExecution | null;
  latest_model_validation: DecisionModelValidation | null;
  latest_analyze: null;
}

export async function fetchDecisionSummary(limitDays = 30): Promise<DecisionSummary> {
  const { data } = await api.get<DecisionSummary>("/api/decision/summary", {
    params: { limit_days: limitDays },
  });
  return data;
}

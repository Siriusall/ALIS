import axios from "axios";
import type { DataHealthReport } from "../types";
import type { ModelStrategyStatus } from "./modelStrategy";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
});

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
  account_risks: Array<{ code: string; level: string; message: string }>;
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

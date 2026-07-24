import { api } from "./client";
import type { DataHealthReport } from "../types";
import type { ModelStrategyStatus } from "./modelStrategy";

export interface DecisionGate {
  tone: "go" | "hold" | "stop";
  title: string;
  detail: string;
  reason_code: string;
}

export interface RiskGateExplanation {
  allow_new_exposure: boolean;
  allow_sell?: boolean;
  allow_reduce?: boolean;
  allow_defensive?: boolean;
  requires_rebalance?: boolean;
  risk_budget_multiplier: number;
  reason_code: string;
  headline: string;
  reasons: Array<{ level: "ok" | "warn" | "block" | string; title: string; detail: string }>;
  constraints: string[];
  today_plan: Array<{ step: string; detail: string }>;
  release_conditions: string[];
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

export interface DecisionEnhancement {
  schema_version: string;
  generated_at: string;
  revenue_dashboard: {
    today_action: string;
    expected_return_source: string;
    max_risk: string;
    why_not_move: string;
    risk_budget?: number;
    next_step: string;
    position_ceiling: {
      state: string;
      label: string;
      max_equity: number;
      current_equity: number;
      cash_target: number;
      risk_budget: number;
      max_single_position: number;
      concentration_limit: number;
      holding_period_bias: string;
      explanation: string;
    };
  };
  market_position_policy: DecisionEnhancement["revenue_dashboard"]["position_ceiling"];
  model_role_contract: {
    summary: string;
    items: Array<{
      model_key: string;
      name: string;
      family: string;
      plan_role?: string;
      decision_role: string;
      owns: string[];
      must_not_own: string[];
      output: string;
    }>;
  };
  candidate_shadow_ab: {
    ok: boolean;
    policy?: string;
    reason?: string;
    items: Array<{
      model_key: string;
      name: string;
      champion_version?: string;
      challenger_version?: string;
      observation_sessions: number;
      minimum_sessions: number;
      challenger_walk_forward_lift: number;
      champion_walk_forward_lift: number;
      delta: number;
      recommendation: string;
      status: string;
    }>;
  };
  extreme_market_protection: {
    active: boolean;
    severity: string;
    triggers: Array<{ code: string; label: string; detail: string }>;
    auto_actions: string[];
    release_conditions: string[];
  };
}

export interface DecisionExecutionOperation {
  symbol: string;
  name?: string;
  side: string;
  price: number;
  quantity: number;
  at: string;
  remark?: string;
  amount?: number;
}

export interface DecisionRecentExecution {
  execution_id: number;
  execution_ids?: number[];
  trade_date?: string;
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
  risk_gate_explanation?: RiskGateExplanation;
  decision_enhancement?: DecisionEnhancement;
  recent_execution: DecisionRecentExecution | null;
  latest_model_validation: DecisionModelValidation | null;
  latest_analyze: null;
}

export async function fetchDecisionSummary(limitDays = 30): Promise<DecisionSummary> {
  const { data } = await api.get<DecisionSummary>("/api/decision/summary", {
    params: { limit_days: limitDays },
    // 决策摘要需要聚合数据健康、账户、市场状态与模型信息；冷启动时明显慢于普通接口。
    timeout: 60_000,
  });
  return data;
}

export interface DailyReportSection {
  title: string;
  [key: string]: unknown;
}

export interface DailyRebalanceReport {
  ok: boolean;
  generated_at: string;
  trade_date: string;
  sections: DailyReportSection[];
  summary: {
    decision: string;
    tone: string;
  };
}

export async function fetchDailyReport(): Promise<DailyRebalanceReport> {
  const { data } = await api.get<DailyRebalanceReport>("/api/decision/daily-report");
  return data;
}

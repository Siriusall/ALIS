export interface Overview {
  total_candidates: number;
  avg_confidence: number;
  top_signal: string;
  last_run: string;
}

export interface Candidate {
  symbol: string;
  name: string;
  price: number;
  change_pct: number;
  volume: number;
  turnover: number;
  confidence: number;
}

export interface SignalDetail {
  symbol: string;
  name: string;
  factors: Record<string, unknown>;
  confidence: number;
}

export interface ExecutionOrder {
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  status: string;
  message: string;
  commission?: number;
  transfer_fee?: number;
  block_code?: string;
}

export interface ExecutionResult {
  execution_id?: number | null;
  executed_at: string;
  orders: ExecutionOrder[];
}

export interface Strategy {
  id: number;
  name: string;
  type: string;
  content: unknown;
  created_at: string;
  updated_at: string;
}

export interface ExecutionListItem {
  id: number;
  strategy_id: number;
  executed_at: string;
  budget: number;
  top_n: number;
  orders: { symbol: string; side: string; price: number; quantity: number }[];
}

export interface Position {
  id: number;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  name?: string;
  current_price?: number | null;
  current_value?: number | null;
  profit_loss?: number | null;
  profit_loss_pct?: number | null;
  strategy_next?: string | null;
}

export interface PositionsListResponse {
  positions: Position[];
  data_source_time?: string | null;
}

export interface Operation {
  id: number;
  execution_id?: number | null;
  symbol: string;
  side: string;
  price: number;
  quantity: number;
  at: string;
  remark: string | null;
  name?: string;
}

export interface EquityCurvePoint {
  date: string;
  total_value: number;
  by_symbol: Record<string, number>;
}

export interface EquityCurveResponse {
  execution_id: number | null;
  strategy_id?: number;
  series: EquityCurvePoint[];
}

export interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
}

export interface QuantRunStatus {
  running: boolean;
  started_at?: string | null;
  stopped_at?: string | null;
  strategy_id?: number | null;
  top_n?: number;
  budget_ratio?: number;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
  last_signal_date?: string | null;
  last_execute_date?: string | null;
  last_message?: string | null;
}

export interface QuantRunCurvePoint {
  date: string;
  strategy_equity: number;
  benchmark_equity: number | null;
  drawdown_pct: number;
  day_return_pct?: number;
  cum_return_pct?: number;
}

export interface QuantRunCurveResponse {
  series: QuantRunCurvePoint[];
  summary: {
    period_return: number;
    max_drawdown: number;
    points: number;
    benchmark: string;
    task_id?: number | null;
  };
}

export interface QuantTaskRunRecord {
  id: number;
  task_id: number;
  signal_date: string;
  execute_date: string;
  sold_count: number;
  bought_count: number;
  total_buy_cost: number;
  execution_id?: number | null;
  message: string;
  created_at: string;
}

export interface QuantTaskItem {
  id: number;
  status: string;
  strategy_id: number;
  top_n: number;
  budget_ratio: number;
  exchange_group: "ALL" | "HS" | "CYB" | "BJ" | "SH" | "SZ";
  start_date?: string | null;
  max_steps: number;
  stop_requested: boolean;
  processed_steps: number;
  total_steps: number;
  last_signal_date?: string | null;
  last_execute_date?: string | null;
  last_message?: string | null;
  error?: string | null;
  replay_mode?: "pragmatic_recover" | "strict_replay" | string;
  replay_status?: string | null;
  replay_gap_days?: number;
  replay_current_execute_date?: string | null;
  replay_target_execute_date?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  runs?: QuantTaskRunRecord[];
  strategy_snapshot?: {
    id?: number;
    name?: string;
    type?: string;
    content?: unknown;
    captured_at?: string;
  };
  risk_config?: {
    enforce_trading_session_gate?: boolean;
    enable_data_health_gate?: boolean;
    scoring_timeout_sec?: number;
    min_account_balance?: number;
    max_consecutive_losses?: number;
  };
  risk_decision?: {
    should_pause?: boolean;
    code?: string;
    message?: string;
    recovery_condition?: string;
    exit_condition?: string;
    severity?: string;
    evidence?: Record<string, unknown>;
  } | null;
  heartbeat_at?: string | null;
  last_step_latency_ms?: number;
  avg_step_latency_ms?: number;
  step_count?: number;
  risk_hit_count?: number;
  consecutive_pause_count?: number;
  last_risk_code?: string;
}

export interface QuantRunRecentRecord {
  at: string;
  signal_date: string;
  execute_date: string;
  sold_count: number;
  bought_count: number;
  total_buy_cost: number;
  message: string;
}

export interface QuantTaskRunDetailOperation {
  symbol: string;
  side: "BUY" | "SELL" | string;
  quantity: number;
  trade_price: number;
  close_price?: number | null;
  cost_price?: number | null;
  pnl: number;
  pnl_pct?: number | null;
}

export interface QuantTaskRunDetailStockSummary {
  symbol: string;
  buy_qty: number;
  sell_qty: number;
  turnover: number;
  pnl: number;
}

export interface QuantTaskRunDetailResponse {
  task_id: number;
  execute_date: string;
  signal_date?: string | null;
  execution_id?: number | null;
  executed_at?: string | null;
  message?: string | null;
  data_warning?: string | null;
  operations: QuantTaskRunDetailOperation[];
  stock_summary: QuantTaskRunDetailStockSummary[];
  trend_analysis?: {
    trade_date?: string;
    trend?: string;
    risk_level?: string;
    reason?: string;
    features?: Record<string, unknown>;
  };
  strategy_template?: {
    id?: string;
    name?: string;
    description?: string;
    factors?: string[];
    reason?: string;
    risk_budget?: Record<string, unknown>;
    top_n?: number;
  };
  selected_factors?: string[];
  risk_budget?: Record<string, unknown>;
  target_positions?: Array<{
    symbol: string;
    name?: string;
    score?: number;
  }>;
  rebalance_actions?: {
    buy?: Array<{ symbol: string; name?: string; score?: number; reason?: string; quantity?: number; price?: number; close?: number; change_pct?: number; volume?: number }>;
    sell?: Array<{ symbol: string; name?: string; quantity?: number; reason?: string; price?: number }>;
    hold?: string[];
    blocked?: Array<Record<string, unknown>>;
  };
  strategy_controller?: {
    schema_version?: string;
    summary?: string;
    action_counts?: Record<string, number>;
    trace?: Array<{ step?: string; status?: string; detail?: string }>;
    contracts?: StrategyActionContract[];
  };
  strategy_action_contracts?: StrategyActionContract[];
  rebalance_decision_trace?: Array<{ step?: string; status?: string; detail?: string }>;
  current_positions?: Array<{
    symbol: string;
    name?: string;
    quantity?: number;
    cost_price?: number;
  }>;
  summary?: string;
  selections?: Array<{
    symbol: string;
    name?: string;
    score?: number;
    factor_contributions?: Array<{
      factor: string;
      weight?: number;
      raw_value?: number;
      contribution?: number;
    }>;
  }>;
  sell_triggers?: Array<{
    symbol: string;
    reason?: string;
    detail?: string;
    quantity?: number;
    price?: number;
  }>;
  blocked_events?: Array<{
    date?: string;
    symbol: string;
    side?: string;
    code?: string;
    message?: string;
  }>;
  filter_summary?: Record<string, unknown>;
  buy_summary?: Record<string, unknown>;
}

export interface StrategyActionContract {
  symbol: string;
  name?: string;
  action: string;
  side: string;
  priority?: number;
  score?: number | null;
  trigger_models?: string[];
  expected_return_source?: string;
  holding_period?: string;
  narrative_lifecycle?: string;
  capital_confirmation?: string;
  risk_budget?: Record<string, unknown>;
  exit_conditions?: string[];
  forbidden_conditions?: string[];
  data_status?: string;
  reason?: string;
  source?: string;
}

export interface QuantAnalyzeResponse {
  ok: boolean;
  signal_date: string;
  execute_date?: string;
  execution_note?: string;
  same_day_fallback?: boolean;
  analyzed_at?: string;
  strategy_id?: number;
  exchange_group?: string;
  summary?: string;
  trend_analysis?: QuantTaskRunDetailResponse["trend_analysis"];
  strategy_template?: QuantTaskRunDetailResponse["strategy_template"];
  selected_factors?: string[];
  risk_budget?: Record<string, unknown>;
  target_symbols?: string[];
  selections?: QuantTaskRunDetailResponse["selections"];
  rebalance_actions?: QuantTaskRunDetailResponse["rebalance_actions"];
  strategy_controller?: QuantTaskRunDetailResponse["strategy_controller"];
  strategy_action_contracts?: QuantTaskRunDetailResponse["strategy_action_contracts"];
  rebalance_decision_trace?: QuantTaskRunDetailResponse["rebalance_decision_trace"];
  current_positions?: QuantTaskRunDetailResponse["current_positions"];
  filter_summary?: Record<string, unknown>;
}

export interface QuantRebalanceOrderPreview {
  symbol: string;
  name?: string;
  quantity?: number;
  request_price?: number;
  exec_price?: number;
  price?: number;
  amount?: number;
  commission?: number;
  transfer_fee?: number;
  total_cost?: number;
  position_pct?: number;
  score?: number;
  status?: string;
  message?: string;
  block_code?: string;
}

export interface QuantRebalancePreviewResponse {
  ok: boolean;
  dry_run: boolean;
  mode: string;
  signal_date: string;
  execute_date: string;
  execution_note?: string;
  same_day_fallback?: boolean;
  total_equity: number;
  cash_balance: number;
  position_market_value: number;
  buy_budget: number;
  target_position_pct: number;
  max_single_position_pct: number;
  total_buy_cost: number;
  estimated_cash_after: number;
  planned_buy_count: number;
  bought_count: number;
  sold_count: number;
  blocked_count: number;
  pruned_symbols?: string[];
  fill_rate_pct?: number;
  message?: string;
  sell_orders: QuantRebalanceOrderPreview[];
  buy_orders: QuantRebalanceOrderPreview[];
  blocked_events?: QuantRebalanceOrderPreview[];
  strategy_controller?: QuantTaskRunDetailResponse["strategy_controller"];
  strategy_action_contracts?: QuantTaskRunDetailResponse["strategy_action_contracts"];
  rebalance_decision_trace?: QuantTaskRunDetailResponse["rebalance_decision_trace"];
}

export interface QuantExecuteBuyResponse {
  ok: boolean;
  dry_run?: boolean;
  mode?: string;
  signal_date: string;
  execute_date?: string;
  execution_note?: string;
  executed_at?: string | null;
  execution_id?: number | null;
  strategy_id?: number;
  total_equity?: number;
  total_buy_cost: number;
  planned_buy_count: number;
  bought_count: number;
  sold_count?: number;
  blocked_count: number;
  pruned_symbols?: string[];
  fill_rate_pct?: number;
  account_balance: number;
  buy_budget?: number;
  estimated_cash_after?: number;
  available_budget?: number;
  target_position_pct?: number;
  max_single_position_pct?: number;
  message?: string;
  sell_orders?: QuantRebalanceOrderPreview[];
  buy_orders?: QuantRebalanceOrderPreview[];
  orders?: ExecutionOrder[];
}

export interface QuantRunLedgerSummary {
  cash_balance: number;
  position_market_value: number;
  total_equity: number;
  equity_delta: number;
  position_count: number;
  valuation_date: string;
}

export interface SimulationLedgerOperation {
  operation_id: number;
  operation_uid: string;
  execution_id: number;
  symbol: string;
  name?: string;
  side: "BUY" | "SELL" | string;
  quantity: number;
  simulated_price: number;
  trade_time: string;
  assumption: {
    price_rule: string;
    slippage_bps: number;
    fee_schedule_version: string;
    remark?: string;
  };
  fees: {
    commission: number;
    transfer_fee: number;
    stamp_tax: number;
    total: number;
  };
  close_price?: number | null;
  mark_to_market_pnl?: number | null;
}

export interface SimulationLedgerItem {
  execution_id: number;
  strategy_id?: number;
  executed_at: string;
  signal_date?: string | null;
  execute_date?: string | null;
  source: "quant_task" | "manual_or_legacy" | string;
  message?: string;
  strategy_version?: string;
  model_version?: string;
  target_positions: Array<Record<string, unknown>>;
  current_positions_before: Array<Record<string, unknown>>;
  orders: Array<Record<string, unknown>>;
  operations: SimulationLedgerOperation[];
  blocked_signals: Array<Record<string, unknown>>;
  summary: {
    operation_count: number;
    buy_count: number;
    sell_count: number;
    gross_turnover: number;
    total_fees: number;
    unfilled_count: number;
  };
  links: {
    task_run_detail?: string | null;
    operations?: string | null;
    equity_curve?: string | null;
  };
}

export interface SimulationLedgerResponse {
  ok: boolean;
  schema_version: "simulation_ledger_v1" | string;
  generated_at: string;
  items: SimulationLedgerItem[];
  limitations: string[];
}

export interface DataHealthDateSummary {
  count: number;
  first?: string | null;
  last?: string | null;
}

export interface DataHealthIssue {
  level: "error" | "warning" | string;
  code: string;
  message: string;
  detail?: unknown;
}

export interface DataHealthReport {
  ok: boolean;
  limit_days: number;
  storage: "postgresql" | string;
  trade_dates: DataHealthDateSummary;
  market_snapshots: DataHealthDateSummary & {
    recent_days?: Array<{
      date: string;
      symbol_rows: number;
      source_counts?: Record<string, unknown>;
      core_factor_coverage?: Record<string, number>;
    }>;
    weak_days?: unknown[];
  };
  snapshots: {
    postgresql_snapshot: DataHealthDateSummary;
  };
  daily_basic: {
    postgresql: DataHealthDateSummary;
  };
  factor_keys?: string[];
  issues: DataHealthIssue[];
}

/** 统一缓存单行：trade_date + symbol + 各因子字段 */
export type ConsolidatedCacheRow = Record<string, unknown> & {
  trade_date?: string;
  symbol?: string;
};

/** GET /api/cache/all 响应：按 date 必传、可选 symbols 过滤，带因子覆盖率摘要 */
export interface ConsolidatedCacheResponse {
  schema_version?: string;
  generated_at?: string;
  trade_date?: string;
  counts?: { dates?: number; symbol_rows?: number };
  factor_coverage?: Record<string, Record<string, number>>;
  factor_coverage_summary?: Record<string, number>;
  data: Record<string, Record<string, ConsolidatedCacheRow>>;
}

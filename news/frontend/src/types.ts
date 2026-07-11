export interface Sector {
  id: number;
  code: string;
  name: string;
  stock_count: number;
}

export interface StockFactor {
  code: string;
  name: string;
  market: string;
  sector: string | null;
  revenue_yoy: number | null;
  deduct_profit_yoy: number | null;
  cagr_3y: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  roe: number | null;
  roic: number | null;
  ocf_to_profit: number | null;
  free_cashflow: number | null;
  debt_ratio: number | null;
  interest_debt: number | null;
  cash_coverage: number | null;
  dividend_yield: number | null;
  payout_ratio: number | null;
  buyback_amount: number | null;
  profit_volatility_5y: number | null;
  cyclicality: string | null;
  pe: number | null;
  pb: number | null;
  ps: number | null;
  valuation_percentile: number | null;
  fund_holding: number | null;
  northbound: number | null;
  etf_holding: number | null;
  research_count: number | null;
  crawled_at: string | null;
}

export interface StockListResponse {
  total: number;
  page: number;
  size: number;
  items: StockFactor[];
}

export interface CrawlRun {
  id: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  total: number;
  success: number;
  failed: number;
  message: string | null;
}

export interface CrawlStatusResponse {
  running: boolean;
  processRunning?: boolean;
  latest: CrawlRun | null;
}

export interface CrawlTriggerResponse {
  message: string;
  running: boolean;
  processRunning?: boolean;
  pid?: number;
  runId?: number | null;
}

export interface CrawlCoverageResponse {
  runId: number | null;
  total: number;
  fields: Record<string, { nonNull: number; coverage: number }>;
}

export interface CrawlErrorItem {
  code: string;
  market: string | null;
  error: string | null;
  failed_at: string | null;
}

export interface CrawlErrorsResponse {
  runId: number | null;
  total: number;
  items: CrawlErrorItem[];
}

export interface CrawlLogItem {
  timestamp: string;
  level: "info" | "stdout" | "stderr" | "error" | string;
  message: string;
}

export interface CrawlLogsResponse {
  total: number;
  items: CrawlLogItem[];
}

// ── Verify / Data Quality ──

export interface VerifyRun {
  id: number;
  type: "sample" | "correct";
  field: string | null;
  sample_size: number;
  total: number;
  processed: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
}

export interface VerifyStatusResponse {
  running: boolean;
  latest_run: VerifyRun | null;
}

export interface FieldAccuracy {
  field: string;
  total: number;
  correct: number;
  wrong: number;
  accuracy: number;
}

export interface VerifyReportResponse {
  run_id: number | null;
  total_samples: number;
  fields: FieldAccuracy[];
  generated_at: string;
}

export interface SampleDetailItem {
  code: string;
  name: string;
  field: string;
  db_value: number | null;
  real_value: number | null;
  deviation: number | null;
  status: string;
}

export interface SampleDetailResponse {
  total: number;
  page: number;
  size: number;
  items: SampleDetailItem[];
}

export interface VerifyLogItem {
  timestamp: string;
  level: string;
  message: string;
}

export interface VerifyLogsResponse {
  total: number;
  items: VerifyLogItem[];
}

import { api } from "./client";

export interface DataSourceItem {
  code: string;
  name: string;
  source_type: string;
  priority: number;
  status: string;
  description?: string;
  created_at: string;
  updated_at: string;
}

export interface TushareCatalogItem {
  api_name: string;
  category: string;
  display_name: string;
  recommended_frequency: string;
  enabled: number;
  last_probe_status: "ok" | "empty" | "failed" | "unknown" | string;
  last_probe_rows: number;
  last_probe_error?: string;
  last_probe_at?: string | null;
}

export interface DataIngestionRun {
  id: number;
  job_name: string;
  source_code: string;
  status: string;
  started_at: string;
  finished_at?: string | null;
  rows_read: number;
  rows_written: number;
  error?: string;
  meta_json?: string;
}

export interface DataCenterCoverage {
  stock_master_rows: number;
  trade_calendar_rows: number;
  raw_record_rows: number;
  stock_daily_snapshot_rows: number;
  data_quality_check_rows: number;
  announcement_raw_rows: number;
  announcement_event_rows: number;
  industry_metric_definitions: number;
  industry_metric_values: number;
  web_factor_rows: number;
}

export interface DataCenterStatus {
  ok: boolean;
  sources: DataSourceItem[];
  catalog: TushareCatalogItem[];
  coverage: DataCenterCoverage;
  recent_runs: DataIngestionRun[];
}

export interface VipBackfillContractStatus {
  completed_slices: number;
  expected_slices: number;
  missing_slices: number;
  coverage: number;
  complete: boolean;
}

export interface VipBackfillStatus {
  ok: boolean;
  capital: VipBackfillContractStatus;
  etf_price_nav: VipBackfillContractStatus;
  major_news: VipBackfillContractStatus;
  scope: {
    capital_start: string;
    news_start: string;
    end_date: string;
    trade_dates: number;
    etfs: number;
    news_sources: number;
  };
}

export interface DataCenterEstimate {
  ok: boolean;
  assumptions: Record<string, unknown>;
  initial_full_load: Record<string, unknown>;
  daily_incremental: Record<string, unknown>;
  next_optimization: string[];
}

export interface SyncResponse {
  ok: boolean;
  [key: string]: unknown;
}

export interface SimpleDataHealth {
  ok: boolean;
  level: "healthy" | "stale" | "needs_setup" | string;
  summary: string;
  latest_trade_date: string;
  stock_count: number;
  snapshot_count: number;
  latest_announcement?: string | null;
  failed_runs: number;
}

export interface DailyUpdateStatus extends SyncResponse {
  running: boolean;
  run_id?: number;
  phase: string;
  message: string;
  progress?: number;
  trade_date?: string;
  error?: string;
}

export interface DataObservability {
  ok: boolean;
  latest_trade_date: string;
  quality: {
    sample_rows: number;
    quality_pass_rows: number;
    quality_pass_rate: number;
    average_quality_score: number;
    definition: string;
    checks: Array<{ check_name: string; scope: string; status: string; severity: string; message: string; affected_count: number; checked_at: string }>;
  };
  missingness: Array<{ field: string; label: string; category: string; total: number; missing: number; missing_rate: number; coverage_rate: number }>;
  interface_summary: { total: number; healthy: number; warning: number; failed: number };
  interfaces: Array<{ api_name: string; category: string; display_name: string; recommended_frequency: string; last_probe_status: string; last_probe_rows: number; last_probe_error?: string; last_probe_at?: string; health: string }>;
  source_stats: Array<{ source_code: string; total_runs: number; successful_runs: number; failed_runs: number; last_run_at?: string }>;
  logs: DataIngestionRun[];
  runtime_logs: Array<{ id:number; level:string; logger:string; message:string; context:Record<string,unknown>; created_at:string }>;
  redis: { available:boolean; url:string };
  quality_audit?: {
    status: "ok" | "warning" | "error" | string;
    summary: { errors:number; warnings:number; info:number; raw_records:number };
    issues: Array<{
      code:string; title:string; severity:string; status:string; count:number; message:string;
      sample?: Array<Record<string,unknown>>;
    }>;
  };
}

export type RuntimeLogItem = DataObservability["runtime_logs"][number];

export interface ReadinessRequirement {
  key: string; label: string; threshold: number; policy: "block" | "degrade";
  status: "ready" | "degraded" | "blocked"; coverage: number;
  available: number; expected: number; evidence: string;
}

export interface ConsumerReadiness {
  key: string; name: string; description: string;
  status: "ready" | "degraded" | "blocked" | "unknown";
  blocked_count: number; degraded_count: number; requirements: ReadinessRequirement[];
}

export interface ReadinessOverview {
  ok: boolean;
  scope: { latest_trade_date: string; stock_count: number; known_open_dates: number; stored_history_dates: number };
  consumers: ConsumerReadiness[];
  status_definition: Record<string, string>;
}

export interface DataAssetItem {
  key:string; name:string; domain:string; consumer:string; weight:number; target_scope:string;
  frequency:string; source:string; actual_records:number; expected_records:number|null;
  coverage:number|null; coverage_basis?:number; coverage_expected?:number|null;
  symbols:number; dates:number; min_date?:string|null; max_date?:string|null;
  window_start?:string; window_end?:string; window_depth?:string; rationale?:string;
  periods_actual?:number|null; periods_expected?:number|null;
  status:"complete"|"partial"|"available"|"missing"; measurement:string;
}
export interface DataAssetsResponse {
  ok:boolean;
  scope:{
    market:string; start_date:string; end_date:string; stocks:number; open_days:number; financial:string;
    open_days_in_calendar?:number; calendar_total_open_days?:number;
    financial_periods?:string[]; financial_start?:string; financial_end?:string;
    policy?:{daily_lookback_days:number; financial_quarters:number; financial_annual_years:number; fallback_open_days:number};
  };
  summary:{
    datasets:number; actual_records:number; complete:number; partial:number; missing:number;
    available?:number; average_coverage?:number;
  };
  items:DataAssetItem[];
}

export interface ColdArchiveItem {
  archive_id: string;
  dataset: string;
  api_names: string[];
  date_start: string;
  date_end: string;
  row_count: number;
  file_path: string;
  file_sha256: string;
  status: string;
  created_at: string;
  file_exists: boolean;
  file_bytes: number;
}

export interface ColdArchiveStatus {
  ok: boolean;
  storage: "local_filesystem" | string;
  root: string;
  exists: boolean;
  contract: string;
  disk: { total_bytes: number; free_bytes: number; free_pct: number };
  items: ColdArchiveItem[];
}

export interface WebFactorItem {
  id: number;
  symbol: string;
  ts_code: string;
  name: string;
  industry: string;
  source_run_key: string;
  crawled_at: string;
  factors: Record<string, string | number | null>;
}

export interface WebFactorResponse {
  total: number;
  items: WebFactorItem[];
  limit: number;
  offset: number;
}

export async function fetchDataCenterStatus(limitRuns = 20): Promise<DataCenterStatus> {
  const { data } = await api.get<DataCenterStatus>("/api/data-center/status", {
    params: { limit_runs: limitRuns },
  });
  return data;
}

export async function fetchSimpleDataHealth(): Promise<SimpleDataHealth> {
  const { data } = await api.get<SimpleDataHealth>("/api/data-center/simple-health");
  return data;
}

export async function fetchColdArchiveStatus(limit = 30): Promise<ColdArchiveStatus> {
  const { data } = await api.get<ColdArchiveStatus>("/api/data-center/cold-archive", { params: { limit } });
  return data;
}

export async function startColdArchive(payload: {
  apiNames: string[];
  startDate: string;
  endDate: string;
  maxRows?: number;
}): Promise<ColdArchiveItem & { ok: boolean }> {
  const { data } = await api.post<ColdArchiveItem & { ok: boolean }>("/api/data-center/cold-archive/raw", {
    api_names: payload.apiNames,
    start_date: payload.startDate,
    end_date: payload.endDate,
    max_rows: payload.maxRows ?? 250000,
  }, { timeout: 5 * 60 * 1000 });
  return data;
}

export async function startDailyUpdate(): Promise<DailyUpdateStatus> {
  const { data } = await api.post<DailyUpdateStatus>("/api/data-center/daily-update");
  return data;
}

export async function fetchDailyUpdateStatus(): Promise<DailyUpdateStatus> {
  const { data } = await api.get<DailyUpdateStatus>("/api/data-center/daily-update/status");
  return data;
}

export async function fetchDataObservability(limitLogs = 50): Promise<DataObservability> {
  const { data } = await api.get<DataObservability>("/api/data-center/observability", { params: { limit_logs: limitLogs } });
  return data;
}

export async function fetchRuntimeLogsAfter(afterId = 0, limit = 100, waitSeconds = 0): Promise<{ ok:boolean; after_id:number; total:number; items:RuntimeLogItem[] }> {
  const { data } = await api.get<{ ok:boolean; after_id:number; total:number; items:RuntimeLogItem[] }>("/api/data-center/runtime-logs", {
    params: { after_id: afterId, limit, wait_seconds: waitSeconds },
    // 实时日志使用长轮询；必须比服务端等待时间多留出网络和序列化余量，不能沿用全局15秒超时。
    timeout: waitSeconds > 0 ? Math.ceil(waitSeconds * 1000) + 5000 : undefined,
  });
  return data;
}

export async function fetchReadiness(): Promise<ReadinessOverview> {
  const { data } = await api.get<ReadinessOverview>("/api/data-center/readiness");
  return data;
}

export interface ReadinessRefreshStatus { ok:boolean; running:boolean; started?:boolean; last_started_at:string; last_finished_at:string; last_error:string; has_cached_snapshot:boolean; cached_consumers:number }
export async function fetchReadinessRefreshStatus(): Promise<ReadinessRefreshStatus> {
  const { data } = await api.get<ReadinessRefreshStatus>("/api/data-center/readiness/refresh");
  return data;
}
export async function startReadinessRefresh(): Promise<ReadinessRefreshStatus> {
  const { data } = await api.post<ReadinessRefreshStatus>("/api/data-center/readiness/refresh");
  return data;
}

export async function fetchDataAssets(): Promise<DataAssetsResponse> {
  // 资产目录会汇总各数据域的实际行数，查询可能超过通用15秒请求窗口。
  const { data } = await api.get<DataAssetsResponse>("/api/data-center/assets", { timeout: 60 * 1000 }); return data;
}

export interface BackfillStep {
  id: string;
  priority: number;
  key: string;
  title: string;
  why: string;
  action: string;
  params: Record<string, unknown>;
  estimated_calls: number;
  estimated_minutes: number;
  status: "needed" | "blocked" | "done" | string;
  gap?: Record<string, unknown>;
}

export interface BackfillPlan {
  ok: boolean;
  generated_at: string;
  scope: Record<string, unknown>;
  summary: {
    steps_total: number;
    steps_needed: number;
    steps_blocked: number;
    steps_done: number;
    estimated_minutes: number;
    estimated_hours: number;
    asset_gaps: number;
    next_action: string;
  };
  asset_gaps: Array<Record<string, unknown>>;
  steps: BackfillStep[];
  execution?: Record<string, unknown>;
}

export interface BackfillStatus {
  ok: boolean;
  running?: boolean;
  run_id?: number;
  phase?: string;
  message?: string;
  progress?: number;
  started_at?: string;
  finished_at?: string;
  error?: string;
  can_resume?: boolean;
  resume_action?: string;
  auto_resume?: boolean;
  auto_resume_attempt?: number;
  auto_resume_max_attempts?: number;
  auto_resume_delay_seconds?: number;
  auto_resume_at?: string;
  auto_resume_reason?: string;
  current_step?: BackfillStep;
  executed?: Array<Record<string, unknown>>;
  failure_detail?: {
    step?: Partial<BackfillStep>;
    summary?: {
      error?: string;
      failed?: number;
      total?: number;
      success?: number;
      failed_items?: Array<{ ts_code?: string; api?: string; error?: string }>;
      [key: string]: unknown;
    };
    resume_hint?: string;
  };
  plan_summary?: BackfillPlan["summary"];
  plan?: BackfillPlan;
  max_steps?: number;
  activity?: {
    event: "request" | "retry" | "failed" | "response" | "saved" | string;
    api: string;
    params: Record<string, unknown>;
    at: string;
    attempt?: number;
    rows?: number;
    written?: number;
    category?: string;
    error?: string;
    text: string;
  };
}

export interface StreamMessage {
  id: string;
  consumer?: string;
  idle_ms?: number;
  deliveries?: number;
  fields: {
    task_type?: string;
    payload_json?: string;
    payload?: Record<string, unknown>;
    enqueued_at?: string;
    attempt?: string;
    [key: string]: unknown;
  };
}

export interface StreamAdminStatus {
  ok: boolean;
  available: boolean;
  health?: "healthy" | "needs_attention" | string;
  summary?: string;
  stream: string;
  group: string;
  consumer: string;
  length: number;
  pending: number;
  last_delivered_id: string;
  lag: number;
  consumers: Array<{ name: string; pending: number; idle_ms: number }>;
  pending_messages: StreamMessage[];
  recent_messages: StreamMessage[];
  dead_letter: { stream: string; length: number; recent_messages: StreamMessage[] };
  auto_cleanup?: Record<string, unknown>;
}

export async function fetchBackfillPlan(marketBatchDays = 30, financialBatchSize = 100): Promise<BackfillPlan> {
  const { data } = await api.get<BackfillPlan>("/api/data-center/backfill/plan", {
    params: { market_batch_days: marketBatchDays, financial_batch_size: financialBatchSize },
  });
  return data;
}

export async function startBackfill(maxSteps = 0, marketBatchDays = 30, financialBatchSize = 100): Promise<BackfillStatus & { plan?: BackfillPlan }> {
  const { data } = await api.post<BackfillStatus & { plan?: BackfillPlan }>("/api/data-center/backfill/start", null, {
    params: { max_steps: maxSteps, market_batch_days: marketBatchDays, financial_batch_size: financialBatchSize },
    timeout: 60 * 1000,
  });
  return data;
}

export async function fetchBackfillStatus(): Promise<BackfillStatus> {
  const { data } = await api.get<BackfillStatus>("/api/data-center/backfill/status");
  return data;
}

export async function fetchStreamAdminStatus(limit = 50, pendingLimit = 50): Promise<StreamAdminStatus> {
  const { data } = await api.get<StreamAdminStatus>("/api/data-center/streams", {
    params: { limit, pending_limit: pendingLimit },
  });
  return data;
}

export async function handleStreamMessage(messageId: string, action: "ack" | "delete" | "dead_letter" | "retry"): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>(`/api/data-center/streams/messages/${encodeURIComponent(messageId)}`, { action });
  return data;
}

export async function bulkHandleStreamMessages(action: "ack" | "delete" | "dead_letter", taskType = "data_platform.backfill", limit = 200): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/streams/bulk", { action, task_type: taskType, limit });
  return data;
}

export async function fetchVipBackfillStatus(endDate: string): Promise<VipBackfillStatus> {
  const { data } = await api.get<VipBackfillStatus>("/api/data-center/backfill/vip-status", {
    params: { end_date: endDate },
  });
  return data;
}

export async function fetchDataCenterEstimate(years = 5): Promise<DataCenterEstimate> {
  const { data } = await api.get<DataCenterEstimate>("/api/data-center/estimate-runtime", {
    params: { years },
  });
  return data;
}

export async function seedDataCenter(): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/seed");
  return data;
}

export async function importLegacyNews(path = "../../news/data/news.db"): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/web-fallback/import-legacy", null, {
    params: { path }, timeout: 10 * 60 * 1000,
  });
  return data;
}

export async function fetchWebFactors(q = "", limit = 20): Promise<WebFactorResponse> {
  const { data } = await api.get<WebFactorResponse>("/api/data-center/web-fallback/factors", {
    params: { q, limit },
  });
  return data;
}

export async function startWebFallbackCrawl(codes: string, limit = 20): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/web-fallback/crawl", null, {
    params: { codes, limit },
  });
  return data;
}

export async function syncMarket(tradeDate: string): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/sync/market", null, {
    params: { trade_date: tradeDate },
    timeout: 5 * 60 * 1000,
  });
  return data;
}

export async function syncFinancialBatch(payload: {
  period: string;
  codes?: string;
  limit?: number;
}): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/sync/financial-batch", null, {
    params: {
      period: payload.period,
      codes: payload.codes || "",
      limit: payload.limit ?? 20,
    },
    timeout: 10 * 60 * 1000,
  });
  return data;
}

export async function syncAnnouncements(payload: {
  startDate: string;
  endDate: string;
  tsCode?: string;
  pageSize?: number;
  maxPages?: number;
}): Promise<SyncResponse> {
  const { data } = await api.post<SyncResponse>("/api/data-center/sync/announcements", null, {
    params: {
      start_date: payload.startDate,
      end_date: payload.endDate,
      ts_code: payload.tsCode || undefined,
      page_size: payload.pageSize ?? 10,
      max_pages: payload.maxPages ?? 1,
    },
    timeout: 3 * 60 * 1000,
  });
  return data;
}

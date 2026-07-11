import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
});

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
}

export interface DataCenterStatus {
  ok: boolean;
  sources: DataSourceItem[];
  catalog: TushareCatalogItem[];
  coverage: DataCenterCoverage;
  recent_runs: DataIngestionRun[];
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

export async function fetchDataCenterStatus(limitRuns = 20): Promise<DataCenterStatus> {
  const { data } = await api.get<DataCenterStatus>("/api/data-center/status", {
    params: { limit_runs: limitRuns },
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

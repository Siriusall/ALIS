import { api } from "./client";

export interface ModelVersion {
  id: number;
  model_key: string;
  version: string;
  status: string;
  artifact_path: string;
  config: Record<string, unknown>;
  metrics: Record<string, number | string | unknown>;
  validation: Record<string, number | string | unknown>;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelTrainRun {
  id: number;
  status: string;
  phase: string;
  progress: number;
  message: string;
  model_version_id?: number | null;
  metrics: Record<string, number | string | unknown>;
  error?: string;
  started_at?: string;
  finished_at?: string;
  created_at: string;
  updated_at: string;
}

export interface ModelStrategyStatus {
  model_key: string;
  active_version?: ModelVersion | null;
  active_artifact?: Record<string, unknown>;
  invalid_active_version?: Record<string, unknown> | null;
  latest_train_run?: ModelTrainRun | null;
  versions: ModelVersion[];
  has_active_model: boolean;
}

export interface ModelConstitution {
  model_key: string;
  components: Record<string, { model: string; status: string }>;
}

export async function fetchModelConstitution(): Promise<ModelConstitution> {
  const { data } = await api.get<ModelConstitution>("/api/model-strategy/constitution");
  return data;
}

export async function fetchModelStrategyStatus(): Promise<ModelStrategyStatus> {
  const { data } = await api.get<ModelStrategyStatus>("/api/model-strategy/status");
  return data;
}

export interface StrategyWorkbenchVersionCard {
  id?: number;
  version?: string;
  status?: string;
  is_active?: boolean;
  metrics?: Record<string, unknown>;
  validation?: Record<string, unknown>;
  production_eligible?: boolean;
  blocked_reasons?: string[];
  created_at?: string;
  updated_at?: string;
}

export interface StrategyWorkbenchResponse {
  ok: boolean;
  schema_version: "strategy_workbench_v1" | string;
  generated_at: string;
  status: {
    overall: string;
    model: string;
    backend_acceptance: string;
    market_regime: string;
    operations: string;
  };
  current_analysis: {
    mode: string;
    scoring_source: string;
    execution_rule: string;
    links: Record<string, string>;
  };
  model: {
    constitution: Record<string, { model: string; status: string }>;
    active?: StrategyWorkbenchVersionCard | null;
    active_artifact?: Record<string, unknown>;
    invalid_active_version?: Record<string, unknown> | null;
    challengers: StrategyWorkbenchVersionCard[];
    latest_train_run?: ModelTrainRun | null;
    experiments: ModelTrainRun[];
    failed_experiments: ModelTrainRun[];
    links: Record<string, string>;
  };
  market: {
    governance: Record<string, unknown>;
    links: Record<string, string>;
  };
  backend_acceptance: {
    status: string;
    counts: Record<string, number>;
    checks: Array<Record<string, unknown>>;
    include_details: boolean;
    links: Record<string, string>;
  };
  operations: Record<string, unknown>;
  pending_user_decisions: Array<{
    key: string;
    status: string;
    message: string;
    evidence?: Record<string, unknown>;
  }>;
}

export async function fetchStrategyWorkbench(params?: {
  limit?: number;
  include_details?: boolean;
}): Promise<StrategyWorkbenchResponse> {
  const { data } = await api.get<StrategyWorkbenchResponse>("/api/model-strategy/workbench", {
    params,
  });
  return data;
}

export async function startModelTraining(payload?: {
  start_date?: string;
  end_date?: string;
  exchange_group?: "ALL" | "HS" | "CYB" | "BJ";
}): Promise<{ ok: boolean; run_id: number; status: string }> {
  const { data } = await api.post("/api/model-strategy/train", payload ?? {});
  return data;
}

export interface HsmmShadowTrainingResult {
  ok: boolean;
  status?: string;
  version?: string;
  model_version_id?: number;
  latest_shadow_state?: string;
  validation?: Record<string, unknown>;
  api_refresh?: Record<string, unknown>;
}

/**
 * Explicitly starts the only long-history data pull in the normal product.
 * The backend limits it to three compact index series; it never backfills a
 * decade of the full stock market.
 */
export async function startHsmmShadowTraining(): Promise<HsmmShadowTrainingResult> {
  const { data } = await api.post<HsmmShadowTrainingResult>("/api/model-strategy/market-context/hsmm/train-shadow");
  return data;
}

export async function fetchModelTrainRun(runId: number): Promise<ModelTrainRun> {
  const { data } = await api.get<ModelTrainRun>(`/api/model-strategy/train-runs/${runId}`);
  return data;
}

export async function autoTuneModels(): Promise<{ decision: string; evaluated: number; winner?: Record<string, unknown> }> {
  const { data } = await api.post("/api/model-strategy/auto-tune");
  return data;
}

export async function rollbackModelVersion(modelVersionId: number): Promise<{ ok: boolean; status: string; previous_model_version_id?: number | null; model_version_id: number }> {
  const { data } = await api.post(`/api/model-strategy/rollback/${modelVersionId}`);
  return data;
}

export interface ModelComparison { champion_id: number; challenger_id: number; champion_version?: string; challenger_version?: string; deltas: Record<string, number>; config_diff: Record<string, { champion: unknown; challenger: unknown }>; gates: Record<string, boolean>; decision: string; blocked_reasons: string[] }
export async function compareModelVersions(championId: number, challengerId: number): Promise<ModelComparison> { const { data } = await api.post<ModelComparison>(`/api/model-strategy/compare?champion_id=${championId}&challenger_id=${challengerId}`); return data; }

export interface PerformanceReport {
  id?: number;
  task_id?: number | null;
  period_type: "daily" | "weekly" | "monthly" | "yearly" | string;
  period: string;
  payload: Record<string, unknown>;
  created_at?: string;
}

export async function fetchPerformanceReports(taskId?: number): Promise<{ items: PerformanceReport[] }> {
  const query = taskId ? `?task_id=${taskId}` : "";
  const { data } = await api.get<{ items: PerformanceReport[] }>(`/api/quant-runs/performance/reports${query}`);
  return data;
}

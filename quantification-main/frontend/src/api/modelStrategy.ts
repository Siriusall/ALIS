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
  metrics: Record<string, number | string | unknown> & {
    timeline?: Array<{ at?: string; phase?: string; progress?: number; message?: string }>;
  };
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
  constitution?: Record<string, { model: string; status: string }>;
}

export interface PlanModelItem {
  model_key: string;
  name: string;
  family: string;
  role: string;
  status: "production" | "candidate" | "trainable" | "proxy" | string;
  required_data: string[];
  coverage: Record<string, number | boolean | string>;
  active_version?: ModelVersion | null;
  latest_version?: ModelVersion | null;
  latest_train_run?: ModelTrainRun | null;
  blocked_reasons?: string[];
  data_actions?: Array<{ label: string; api: string; reason: string }>;
  training_contract?: Record<string, unknown>;
  active_artifact?: Record<string, unknown>;
  latest_artifact?: Record<string, unknown>;
  versions?: ModelVersion[];
}

export interface PlanModelControl {
  ok: boolean;
  schema_version: string;
  items: PlanModelItem[];
  counts: Record<string, number>;
  lightgbm_params: Record<string, unknown>;
}

export interface PlanModelAutoOptimizationItem {
  model_key: string;
  name: string;
  role: string;
  status: string;
  trigger: string;
  reason: string;
  requires_human: boolean;
  latest_version_id?: number | null;
  latest_version?: string | null;
  active_version?: string | null;
  candidate_metrics: Record<string, unknown>;
  production_metrics: Record<string, unknown>;
  blocked_reasons: string[];
  running: boolean;
  shadow_ab?: {
    champion_version?: string | null;
    challenger_version?: string | null;
    has_challenger?: boolean;
    observation_sessions: number;
    minimum_sessions: number;
    challenger_walk_forward_lift: number;
    champion_walk_forward_lift: number;
    delta: number;
    status: string;
    recommendation: string;
  };
}

export interface PlanModelAutoOptimizationStatus {
  ok: boolean;
  schema_version: string;
  policy: {
    mode: string;
    summary: string;
    auto_allowed: string[];
    human_required: string[];
  };
  auto_rejected?: Array<{ model_key?: string; version?: string; reason?: string; result?: Record<string, unknown> }>;
  review_queue: PlanModelAutoOptimizationItem[];
  items: PlanModelAutoOptimizationItem[];
  generated_at: string;
}

export async function fetchPlanModels(): Promise<PlanModelControl> {
  const { data } = await api.get<PlanModelControl>("/api/model-strategy/plan-models", { timeout: 60_000 });
  return data;
}

export async function fetchPlanModelAutoOptimization(): Promise<PlanModelAutoOptimizationStatus> {
  const { data } = await api.get<PlanModelAutoOptimizationStatus>("/api/model-strategy/auto-optimization", { timeout: 60_000 });
  return data;
}

export async function runPlanModelAutoOptimization(force = false): Promise<{ ok: boolean; auto_rejected_count?: number; auto_rejected?: unknown[]; started_count: number; skipped_count: number; status: PlanModelAutoOptimizationStatus }> {
  const { data } = await api.post("/api/model-strategy/auto-optimization/run", { force }, { timeout: 60_000 });
  return data;
}

export async function startPlanModelTraining(modelKey: string): Promise<{ ok: boolean; started: boolean; run_id?: number; model_key: string; status?: string; reason?: string }> {
  const { data } = await api.post("/api/model-strategy/plan-models/train", { model_key: modelKey });
  return data;
}

export async function startAllTrainablePlanModels(): Promise<{ ok: boolean; started_count: number; skipped_count: number }> {
  const { data } = await api.post("/api/model-strategy/plan-models/train-all");
  return data;
}

export async function reviewPlanModelVersion(
  modelVersionId: number,
  payload: { decision: "approved" | "rejected"; reviewer?: string; comment?: string; waive_live_observation?: boolean },
): Promise<{ ok: boolean; model_version_id: number; production_eligible: boolean; production_eligibility: Record<string, unknown> }> {
  const { data } = await api.post(`/api/model-strategy/plan-models/review/${modelVersionId}`, {
    reviewer: payload.reviewer || "operator",
    decision: payload.decision,
    comment: payload.comment || "",
    waive_live_observation: payload.waive_live_observation ?? true,
  });
  return data;
}

export async function reviewCandidatePlanModels(): Promise<{ ok: boolean; reviewed_count: number; skipped_count: number }> {
  const { data } = await api.post("/api/model-strategy/plan-models/review-candidates", {
    reviewer: "operator",
    decision: "approved",
    comment: "批量上线评审通过；模拟系统豁免真实20日观察",
    waive_live_observation: true,
  });
  return data;
}

export async function materializeFormalPerformanceReports(): Promise<{ ok: boolean; updated: number; total: number }> {
  const { data } = await api.post("/api/model-strategy/performance-reports/materialize-formal");
  return data;
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
    // This endpoint aggregates model governance, plan acceptance and
    // operations evidence across the PostgreSQL store.
    timeout: 60_000,
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

/** Builds HSMM observations from persisted full-market daily aggregates and
 * refreshes three broad-index anchors before fitting the shadow model. */
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

export async function promoteModelVersion(modelVersionId: number): Promise<{ ok: boolean; status: string; model_version_id: number }> {
  const { data } = await api.post(`/api/model-strategy/promote/${modelVersionId}`);
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

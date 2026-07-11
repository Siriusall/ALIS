import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
});

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
  latest_train_run?: ModelTrainRun | null;
  versions: ModelVersion[];
  has_active_model: boolean;
}

export async function fetchModelStrategyStatus(): Promise<ModelStrategyStatus> {
  const { data } = await api.get<ModelStrategyStatus>("/api/model-strategy/status");
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

export async function fetchModelTrainRun(runId: number): Promise<ModelTrainRun> {
  const { data } = await api.get<ModelTrainRun>(`/api/model-strategy/train-runs/${runId}`);
  return data;
}

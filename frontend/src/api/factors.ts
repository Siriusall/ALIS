import { api } from "./client";

export interface FactorItem {
  key: string;
  name_cn: string;
  category: string;
  description: string;
  formula_hint: string;
  in_factor_engine: boolean;
  in_backtest: boolean;
  in_model_training: boolean;
  in_cache_coverage: boolean;
  needs_history: boolean;
  snapshot_only: boolean;
  definition_id?: number | null;
  version?: string;
  status?: string;
  available_time_rule?: string;
  missing_policy?: string;
  leakage_status?: string;
}

export interface FactorLibraryResponse {
  total: number;
  categories: Record<string, number>;
  model_training_features: string[];
  factors: FactorItem[];
  versioned_registry: {
    schema_version: string;
    active_count: number;
    candidate_count: number;
    latest_materialized_date: string;
    value_count: number;
    evaluation_count: number;
    definitions: Array<{
      id: number;
      factor_key: string;
      version: string;
      status: string;
      name_cn: string;
      category: string;
      formula: { expression?: string; engine?: string };
      inputs: string[];
      direction: number;
      window_sessions: number;
      available_time_rule: string;
      missing_policy: string;
      leakage_status: string;
    }>;
    consumer_contract: Record<string, string>;
  };
}

export async function fetchFactorLibrary(): Promise<FactorLibraryResponse> {
  const { data } = await api.get<FactorLibraryResponse>("/api/factors");
  return data;
}

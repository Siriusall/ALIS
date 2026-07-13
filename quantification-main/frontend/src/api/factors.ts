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
}

export interface FactorLibraryResponse {
  total: number;
  categories: Record<string, number>;
  model_training_features: string[];
  factors: FactorItem[];
}

export async function fetchFactorLibrary(): Promise<FactorLibraryResponse> {
  const { data } = await api.get<FactorLibraryResponse>("/api/factors");
  return data;
}

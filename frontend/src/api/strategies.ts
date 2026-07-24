import { api } from "./client";
import type { Strategy } from "../types";

export async function fetchStrategies(): Promise<Strategy[]> {
  const { data } = await api.get<Strategy[]>("/api/strategies");
  return data;
}

export async function createStrategy(
  name: string,
  type: string,
  content: unknown
): Promise<Strategy> {
  const { data } = await api.post<Strategy>("/api/strategies", {
    name,
    type,
    content
  });
  return data;
}

export async function updateStrategy(
  id: number,
  body: { name?: string; type?: string; content?: unknown }
): Promise<Strategy> {
  const { data } = await api.put<Strategy>(`/api/strategies/${id}`, body);
  return data;
}

export async function deleteStrategy(id: number): Promise<void> {
  await api.delete(`/api/strategies/${id}`);
}

export interface EquityCurveResponse {
  execution_id: number | null;
  strategy_id?: number;
  series: { date: string; total_value: number; by_symbol: Record<string, number> }[];
}

export async function fetchEquityCurve(executionId: number): Promise<EquityCurveResponse> {
  const { data } = await api.get<EquityCurveResponse>("/api/equity-curve", {
    params: { execution_id: executionId }
  });
  return data;
}

/** 按当前持仓计算跟盘折线，不按某次执行 */
export async function fetchPositionsEquityCurve(): Promise<EquityCurveResponse> {
  const { data } = await api.get<EquityCurveResponse>("/api/positions-equity-curve");
  return data;
}

export async function fetchStrategyEquityCurve(
  strategyId?: number | null,
  executionId?: number | null
): Promise<EquityCurveResponse> {
  const params: { strategy_id?: number; execution_id?: number } = {};
  if (strategyId != null) params.strategy_id = strategyId;
  if (executionId != null) params.execution_id = executionId;
  const { data } = await api.get<EquityCurveResponse>("/api/strategy-equity-curve", {
    params
  });
  return data;
}

import { api } from "./client";
import type { ExecutionListItem, Operation, Position, PositionsListResponse } from "../types";

export async function fetchPositions(): Promise<PositionsListResponse> {
  const { data } = await api.get<PositionsListResponse>("/api/positions");
  return data;
}

/** 仅取持仓列表，兼容旧用法 */
export async function fetchPositionsList(): Promise<Position[]> {
  const res = await fetchPositions();
  return res.positions ?? [];
}

export async function addPosition(symbol: string, quantity: number, price: number): Promise<Position> {
  const { data } = await api.post<Position>("/api/positions", { symbol, quantity, price });
  return data;
}

export async function updatePosition(
  id: number,
  body: { quantity?: number; price?: number }
): Promise<Position> {
  const { data } = await api.put<Position>(`/api/positions/${id}`, body);
  return data;
}

export async function deletePosition(id: number): Promise<void> {
  await api.delete(`/api/positions/${id}`);
}

/** 手动卖出持仓。quantity 不传或为 0 表示全部卖出，price 不传则用当前价。返回 204 表示全部卖出，200 返回更新后持仓。 */
export async function sellPosition(
  positionId: number,
  body: { quantity?: number; price?: number } = {}
): Promise<Position | null> {
  const { data } = await api.request<Position | undefined>({
    method: "POST",
    url: `/api/positions/${positionId}/sell`,
    data: body,
    validateStatus: (s) => s === 200 || s === 204,
  });
  return data ?? null;
}

export async function fetchOperations(executionId?: number | null): Promise<Operation[]> {
  const params = executionId != null ? { execution_id: executionId } : {};
  const { data } = await api.get<Operation[]>("/api/operations", { params });
  return data;
}

export async function fetchExecutions(): Promise<ExecutionListItem[]> {
  const { data } = await api.get<ExecutionListItem[]>("/api/executions");
  return data;
}

/** 批量查询股票名称，用于列表展示「名称 代码」。返回 { 代码: 名称 } */
export async function fetchStockNames(symbols: string[]): Promise<Record<string, string>> {
  if (symbols.length === 0) return {};
  const params = new URLSearchParams({ symbols: [...new Set(symbols)].join(",") });
  const { data } = await api.get<Record<string, string>>(`/api/stock_names?${params}`);
  return data ?? {};
}

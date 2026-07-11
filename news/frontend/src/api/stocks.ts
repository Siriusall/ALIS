import axios from "axios";
import type { Sector, StockFactor, StockListResponse } from "../types";

const client = axios.create({ baseURL: "/api" });

export async function fetchSectors(): Promise<Sector[]> {
  const { data } = await client.get<Sector[]>("/stocks/sectors");
  return data;
}

export async function fetchStocks(params: {
  page?: number;
  size?: number;
  q?: string;
  sector?: string;
}): Promise<StockListResponse> {
  const { data } = await client.get<StockListResponse>("/stocks", { params });
  return data;
}

export async function fetchStockDetail(code: string): Promise<StockFactor> {
  const { data } = await client.get<StockFactor>(`/stocks/${code}`);
  return data;
}

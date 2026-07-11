import axios from "axios";
import {
  Candidate,
  ConsolidatedCacheResponse,
  ExecutionResult,
  LogEntry,
  Overview,
  QuantRunStatus,
  SignalDetail
} from "../types";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || ""
});

export async function fetchOverview(): Promise<Overview> {
  const { data } = await api.get<Overview>("/api/overview");
  return data;
}

/**
 * 统一缓存：按日期（必填）+ 可选标的列表拉取 all-in-one 合并数据。
 * 候选/信号/持仓等业务接口后端已优先走该缓存；前端需直接读合并截面时使用此 API。
 * @param date YYYYMMDD
 * @param symbols 可选，逗号分隔或数组，不传则返回当日全市场（若超过服务端上限会 400，需拆分 symbols）
 */
export async function fetchConsolidatedCache(
  date: string,
  symbols?: string[] | string | null
): Promise<ConsolidatedCacheResponse> {
  const dateYmd = String(date).replace(/-/g, "").trim();
  if (!dateYmd || dateYmd.length !== 8) {
    throw new Error("fetchConsolidatedCache 需要 date 为 YYYYMMDD");
  }
  const params: { date: string; symbols?: string } = { date: dateYmd };
  if (symbols != null && symbols !== "") {
    params.symbols = Array.isArray(symbols) ? symbols.join(",") : String(symbols).trim();
    if (!params.symbols) delete params.symbols;
  }
  const { data } = await api.get<ConsolidatedCacheResponse>("/api/cache/all", { params });
  return data;
}

export interface CandidatesListResponse {
  candidates: Candidate[];
  warnings: string[];
}

/** 选股接口可能拉取日线数据，带市值等过滤的策略会先预筛再拉日线，仍可能需 1～3 分钟。 */
const CANDIDATES_TIMEOUT_MS = 5 * 60 * 1000;

/** limit=0 表示拉取全部；budget 传入时仅返回 1 手价格<=预算 的股票。返回带 warnings 时需在选股页展示提示。 */
export async function fetchCandidates(
  limit = 0,
  strategyId?: number | null,
  budget?: number | null,
  exchange?: "ALL" | "HS" | "CYB" | "BJ" | null
): Promise<CandidatesListResponse> {
  const params: { limit: number; strategy_id?: number; budget?: number; exchange?: string } = { limit };
  if (strategyId != null) params.strategy_id = strategyId;
  if (budget != null && budget > 0) params.budget = budget;
  if (exchange != null && exchange !== "ALL") params.exchange = exchange;
  const { data } = await api.get<CandidatesListResponse>("/api/candidates", {
    params,
    timeout: CANDIDATES_TIMEOUT_MS,
  });
  if (Array.isArray(data)) return { candidates: data, warnings: [] };
  return { candidates: data?.candidates ?? [], warnings: data?.warnings ?? [] };
}

export async function fetchSignal(symbol: string): Promise<SignalDetail> {
  const { data } = await api.get<SignalDetail>(`/api/signals/${symbol}`);
  return data;
}

/** 使用当前账户余额执行模拟买入；传入 candidates 时后端直接按列表下单，不再重跑选股（避免卡住） */
export async function executeOpen(
  top_n: number,
  strategyId?: number | null,
  candidates?: { symbol: string; name: string; price: number; change_pct?: number; volume?: number; close?: number }[] | null
): Promise<ExecutionResult> {
  const body: {
    top_n: number;
    strategy_id?: number;
    candidates?: { symbol: string; name?: string; price: number; change_pct?: number; volume?: number; close?: number }[];
  } = { top_n };
  if (strategyId != null) body.strategy_id = strategyId;
  if (candidates != null && candidates.length > 0) {
    body.candidates = candidates.slice(0, top_n).map((c) => ({
      symbol: c.symbol,
      name: c.name,
      price: c.price,
      change_pct: c.change_pct,
      volume: c.volume,
      close: c.close,
    }));
  }
  const { data } = await api.post<ExecutionResult>("/api/execute/open", body);
  return data;
}

export async function fetchLogs(): Promise<LogEntry[]> {
  const { data } = await api.get<LogEntry[]>("/api/logs");
  return data;
}

export async function fetchQuantRunStatus(): Promise<QuantRunStatus> {
  const { data } = await api.get<QuantRunStatus>("/api/quant-runs/status");
  return data;
}

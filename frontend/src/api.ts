export type Json = Record<string, any>;

const API_BASE = String(import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
const API_READ_TIMEOUT_MS = Number(import.meta.env.VITE_API_READ_TIMEOUT_MS || 60_000);
const TOKEN_KEY = "alis_access_token";
let sessionProbe: Promise<void> | null = null;

export function getToken() {
  return window.sessionStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string | null) {
  if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
  else window.sessionStorage.removeItem(TOKEN_KEY);
  // Remove tokens written by pre-session-storage builds.
  window.localStorage.removeItem(TOKEN_KEY);
}

async function confirmSessionExpired(token: string | null) {
  if (!token || sessionProbe) return sessionProbe;
  sessionProbe = fetch(`${API_BASE}/api/auth/me`, {
    headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
  }).then((response) => {
    if (response.status === 401) window.dispatchEvent(new CustomEvent("alis:session-expired"));
  }).catch(() => {
    // A network failure is not evidence that the seven-day local session expired.
  }).finally(() => { sessionProbe = null; });
  return sessionProbe;
}

export async function readApi(path: string, signal?: AbortSignal, timeoutMs = API_READ_TIMEOUT_MS): Promise<Json> {
  const timeout = new AbortController();
  const timer = timeoutMs > 0 ? window.setTimeout(() => timeout.abort(), timeoutMs) : undefined;
  const abort = () => timeout.abort();
  if (signal?.aborted) timeout.abort();
  else signal?.addEventListener("abort", abort, { once: true });
  try {
    const token = getToken();
    const response = await fetch(`${API_BASE}${path}`, {
      signal: timeout.signal,
      cache: "no-store",
      headers: { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    });
    if (response.status === 401) {
      if (path === "/api/auth/me") window.dispatchEvent(new CustomEvent("alis:session-expired"));
      else void confirmSessionExpired(token);
    }
    if (!response.ok) {
      // /api/health deliberately uses HTTP 503 for readiness failures, but
      // its diagnostic payload is still useful to the read-only admin page.
      if ((path.split("?", 1)[0] === "/api/health" || path.split("?", 1)[0] === "/api/trading-ready") && response.status === 503) return response.json();
      const error = new Error(`HTTP ${response.status}`) as Error & { status?: number };
      error.status = response.status;
      throw error;
    }
    return response.json();
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

const AUTH_WRITE_PATHS = new Set([
  "/api/auth/login",
  "/api/auth/register",
]);

export async function authWriteApi(path: string, body?: unknown): Promise<Json> {
  if (!AUTH_WRITE_PATHS.has(path)) {
    throw new Error("前端只允许认证写入，业务写接口已被拒绝");
  }
  const token = getToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(String(payload?.detail || `HTTP ${response.status}`));
  }
  return response.json();
}

export const endpoints = {
  health: "/api/health",
  tradingReady: "/api/trading-ready?detail=compact",
  systemVersionStatus: "/api/system-versions/status",
  systemReleases: "/api/system-versions/releases?limit=30",
  systemContracts: "/api/system-versions/contracts",
  owner: "/api/model-strategy/evolution/owner-summary",
  decision: "/api/decision/summary",
  latestPlan: "/api/decision/latest-plan?compact=true",
  report: "/api/decision/daily-report",
  reportCompact: "/api/decision/daily-report?compact=true",
  ledger: "/api/quant-runs/ledger-summary",
  ledgerDetail: "/api/quant-runs/simulation-ledger?limit=20",
  positions: "/api/positions",
  performance: "/api/quant-runs/performance",
  portfolio: "/api/research/portfolio-candles",
  risk: "/api/research/portfolio-risk/live",
  strategy: "/api/model-strategy/strategy-lab",
  candidateObservations: "/api/model-strategy/candidate-observations?limit=200",
  tasks: "/api/model-strategy/task-center?compact=true&limit=16&log_limit=40",
  adminTasksLive: "/api/model-strategy/task-center?compact=true&limit=30&log_limit=20",
  modelTasksLive: "/api/model-strategy/task-center?compact=true&limit=24&log_limit=40",
  evolutionTasks: "/api/model-strategy/evolution/tasks?limit=30",
  evolutionSchedule: "/api/model-strategy/evolution/schedule",
  ledgerAudit: "/api/model-strategy/evolution/ledger-audit",
  nodeActivities: "/api/model-strategy/evolution/node-activities",
  quantTasks: "/api/quant-runs/tasks?limit=20",
  quantStatus: "/api/quant-runs/status",
  quantRecent: "/api/quant-runs/recent?limit=10",
  operationsPage: "/api/operations/page?limit=20&offset=0",
  executionsPage: "/api/executions/page?limit=20&offset=0",
  data: "/api/data-center/readiness",
  oos: "/api/data-center/oos-hard-data",
  sealed: "/api/data-center/sealed-strategy-holdout",
  research: "/api/research/profit-loop?compact=true",
  researchContracts: "/api/research/contracts",
  researchGovernance: "/api/research/governance/overview",
  researchPositionState: "/api/research/position-state",
  researchDataAssets: "/api/model-strategy/evolution/research-data-assets",
  analystConsensus: "/api/model-strategy/analyst-consensus/batch?limit=100",
  theses: "/api/research/theses",
  industries: "/api/research/industries?compact=true",
  industryAllocation: "/api/research/industries/allocation?compact=true",
  themes: "/api/research/themes?limit=100&compact=true",
  events: "/api/research/events",
  pools: "/api/research/pools",
  factors: "/api/factors/registry",
  factorStatus: "/api/factors",
  factorResearch: "/api/factors/research/status",
  factorResearchConsole: "/api/factors/research/status?console=true",
  factorResearchCompact: "/api/factors/research/status?compact=true",
  researchAnalytics: "/api/factors/research/analytics",
  researchAnalyticsConsole: "/api/factors/research/analytics/console",
  researchFactorAnalytics: "/api/factors/research/analytics/factors?limit=600",
  researchTimeseries: "/api/factors/research/analytics/timeseries?limit=1000",
  researchExpertHistory: "/api/factors/research/analytics/experts/history?limit=100",
  researchEnvironmentSimilarity: "/api/factors/research/analytics/environment-similarity",
  researchArchive: "/api/factors/research/analytics/archive",
  factorLineage: "/api/factors/strategy-lineage",
  models: "/api/model-strategy/status",
  planModels: "/api/model-strategy/plan-models?console=true",
  modelOperations: "/api/model-strategy/plan-models/operations",
  modelWorkbench: "/api/model-strategy/workbench",
  modelGovernance: "/api/model-strategy/market-context/governance",
  modelReports: "/api/model-strategy/performance-reports",
  modelProduction: "/api/model-strategy/production-strategy",
  modelController: "/api/model-strategy/strategy-controller",
  modelOutputContracts: "/api/model-strategy/model-output-contracts",
  modelEvidenceBindings: "/api/model-strategy/evidence-bindings",
  modelValidation: "/api/model-strategy/validation/multi-period",
  modelMarketLive: "/api/model-strategy/market-context/live",
  modelMarketHistory: "/api/model-strategy/market-context/history?limit=120",
  watermarks: "/api/data-center/assets/watermarks",
  failedRequests: "/api/data-center/tushare/failed-requests",
  dataOperations: "/api/data-center/llm-data-operations",
  runtimeLogs: "/api/data-center/runtime-logs?limit=100",
  dataObservability: "/api/data-center/observability?compact=true",
  dataStreams: "/api/data-center/streams",
  dailyUpdateStatus: "/api/data-center/daily-update/status?compact=true",
  backfillStatus: "/api/data-center/backfill/status",
  backfillPlan: "/api/data-center/backfill/plan",
  decisionAudit: "/api/research/decision-audit?limit=20&compact=true",
  decisions: "/api/research/decisions?limit=20&compact=true",
  reviews: "/api/research/reviews?limit=20",
  reviewLearning: "/api/research/reviews/learning-overview",
  llmBudget: "/api/research/llm/token-budget",
  llmProposals: "/api/research/llm/proposals?limit=20",
  performanceReports: "/api/quant-runs/performance/reports?limit=20",
  backtests: "/api/backtest/list?limit=20",
} as const;

export function asArray(value: unknown): Json[] {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : [];
}

export function firstArray(source: Json | Json[] | undefined, keys: string[]): Json[] {
  if (Array.isArray(source)) return asArray(source);
  for (const key of keys) {
    const value = key.split(".").reduce<any>((current, part) => current?.[part], source);
    if (Array.isArray(value)) return asArray(value);
  }
  return [];
}

export function pick(source: Json | undefined, keys: string[], fallback: any = null): any {
  for (const key of keys) {
    const value = key.split(".").reduce<any>((current, part) => current?.[part], source);
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return fallback;
}

export function number(source: Json | undefined, keys: string[], fallback = 0): number {
  const value = Number(pick(source, keys, fallback));
  return Number.isFinite(value) ? value : fallback;
}

export function unwrapOwner(payload?: Json): Json | undefined {
  const summary = payload?.summary;
  return summary && typeof summary === "object" ? summary : payload;
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { endpoints, firstArray, Json, pick, readApi } from "./api";

export type ConsoleData = Partial<Record<keyof typeof endpoints, Json>> & {
  researchStockEvidence?: Json;
  researchIndustryEvidence?: Json;
  evidenceVerification?: Json;
  decisionDetail?: Json;
};

const routeKeys: Record<string, Array<keyof typeof endpoints>> = {
  "/overview": ["owner", "ledger", "portfolio", "tradingReady", "latestPlan", "health", "adminTasksLive", "dailyUpdateStatus", "watermarks", "ledgerAudit", "oos", "sealed"],
  "/system": ["health", "tradingReady", "adminTasksLive"],
  "/decision": ["owner", "decision", "tradingReady", "latestPlan"],
  "/portfolio": ["owner", "ledger", "positions", "portfolio", "risk", "research"],
  "/evolution": ["factors", "factorStatus", "factorResearchConsole", "factorLineage", "evolutionTasks"],
  "/evolution/factors": ["factors", "researchAnalytics", "researchFactorAnalytics"],
  "/evolution/metrics": ["researchAnalytics", "researchTimeseries", "evolutionTasks"],
  "/evolution/waves": ["researchAnalyticsConsole", "researchExpertHistory", "researchEnvironmentSimilarity", "researchArchive"],
  "/admin": ["health", "tradingReady", "tasks", "adminTasksLive", "dailyUpdateStatus", "watermarks", "ledgerAudit", "oos", "sealed"],
  "/admin/operations": ["health", "owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executionsPage", "operationsPage", "ledgerAudit", "oos", "sealed", "apiCatalog"],
  "/factors": ["owner", "strategy", "sealed", "factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/admin/models": ["planModels", "modelOperations", "models", "modelMarketLive", "modelGovernance", "modelReports", "modelProduction", "modelController", "modelOutputContracts", "modelEvidenceBindings", "modelValidation", "modelMarketHistory", "factorResearchCompact", "evolutionTasks", "modelTasksLive"],
  "/admin/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "tasks", "oos", "sealed"],
  "/admin/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "reportCompact", "backtests", "ledgerAudit"],
  "/operations": ["health", "owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executionsPage", "operationsPage", "ledgerAudit", "oos", "sealed", "apiCatalog"],
  "/models": ["planModels", "modelOperations", "models", "modelMarketLive", "modelGovernance", "modelReports", "modelProduction", "modelController", "modelOutputContracts", "modelEvidenceBindings", "modelValidation", "modelMarketHistory", "factorResearchCompact", "evolutionTasks", "modelTasksLive"],
  "/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "tasks", "oos", "sealed"],
  "/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "reportCompact", "backtests", "ledgerAudit"],
  "/research": ["researchContracts", "researchGovernance", "researchPositionState", "researchDataAssets", "analystConsensus", "theses", "industries", "industryAllocation", "themes", "events", "pools", "modelMarketLive"],
};

const cache = new Map<string, { value: Json; readAt: number }>();
type PendingRequest = {
  key: string;
  promise: Promise<Json>;
  controller: AbortController;
  consumers: number;
  keepAlive: boolean;
  settled: boolean;
};

const inFlight = new Map<string, PendingRequest>();
const CACHE_TTL_MS = 60_000;

const initialRequestLabels: Record<string, string> = {
  owner: "系统摘要",
  ledger: "账本摘要",
  tradingReady: "交易就绪检查",
  decision: "今日决策",
  researchDataAssets: "研究数据资产",
};

function initialRequestLabel(key: string) {
  return initialRequestLabels[key] || key;
}

function terminalReadFailure(component: string, error: any): Json | null {
  const statusCode = Number(error?.status || 0);
  // Session expiry is handled centrally by readApi/App. Do not turn it into
  // a false degraded data result that could mask the login boundary.
  if (statusCode === 401 || String(error?.message || "").includes("HTTP 401")) return null;
  const timedOut = error?.name === "AbortError";
  const code = timedOut
    ? "request_timeout"
    : statusCode > 0
      ? `http_${statusCode}`
      : "request_failed";
  return {
    ok: false,
    status: "degraded",
    data_available: timedOut ? null : false,
    component,
    error: {
      code,
      kind: timedOut ? "timeout" : "read_failure",
      message: timedOut
        ? "只读查询超时，不能据此判断数据缺失；未使用推测值填充。"
        : "只读接口读取失败，未使用推测值填充。",
    },
    items: [],
  };
}

function responseIsTerminal(value: unknown): value is Json {
  if (Array.isArray(value)) return true;
  if (!value || typeof value !== "object") return false;
  const payload = value as Json;
  return Object.keys(payload).length > 0;
}

function responseIsDegraded(value: unknown): boolean {
  if (!responseIsTerminal(value) || Array.isArray(value)) return false;
  const payload = value as Json;
  return payload.data_available === false
    || payload.available === false
    || (payload.ok === false && payload.status === "degraded");
}

function diagnosticMessage(key: string, value: unknown) {
  if (!responseIsDegraded(value)) return "";
  const payload = value as Json;
  const component = String(payload.component || key);
  const code = String(payload.error?.code || payload.status || "data_unavailable");
  if (code === "request_timeout") return `${component}（查询超时）未完成，不能判断为无数据`;
  return `${component}（${code}）已返回终态，当前没有可用证据`;
}

function payloadIsComplete(key: string, value: unknown): value is Json {
  if (!responseIsTerminal(value)) return false;
  if (Array.isArray(value)) return true;
  const payload = value as Json;
  // A degraded/empty response is still a completed request. Render the
  // page with an explicit warning instead of retrying forever behind a
  // spinner while waiting for data that the backend has already ruled out.
  if (responseIsDegraded(payload)) return true;
  if (Object.keys(payload).length === 0) return false;
  if (key === "owner") return Boolean(payload.summary || payload.questions || payload.account || payload.acceptance_progress);
  if (key === "ledger") return ["total_equity", "cash_balance", "position_market_value"].every((field) => Object.prototype.hasOwnProperty.call(payload, field));
  if (key === "tradingReady") return typeof payload.status === "string" && Boolean(payload.summary || payload.hard_blockers || payload.gates);
  if (key === "decision") return Boolean(
    payload.status || payload.summary || payload.actions || payload.rebalance || payload.items
      || payload.gate_decision || payload.latest_data_contract || payload.data_health || payload.market_overview,
  );
  // Dynamic detail groups are complete once their child requests have settled.
  // A child data domain may legitimately be unavailable/partial; that is an
  // evidence warning, not a reason to keep the whole page behind a spinner.
  if (key === "researchStockEvidence" || key === "researchIndustryEvidence" || key === "evidenceVerification" || key === "decisionDetail") return true;
  return true;
}

function incompleteRouteData(pathname: string, data: ConsoleData): string[] {
  const keys = routeKeys[pathname] || routeKeys["/overview"];
  const missing = keys.filter((key) => !payloadIsComplete(String(key), data[key])).map(String);
  const dynamicMissing = initialDynamicRequests(pathname, data)
    .filter((request) => !payloadIsComplete(request.dataKey, data[request.dataKey]))
    .map((request) => request.dataKey);
  return [...missing, ...dynamicMissing];
}

function abortError() {
  const error = new Error("读取已取消");
  error.name = "AbortError";
  return error;
}

function releasePending(pending: PendingRequest) {
  pending.consumers = Math.max(0, pending.consumers - 1);
  if (!pending.settled && pending.consumers === 0 && !pending.keepAlive) {
    pending.controller.abort();
    if (inFlight.get(pending.key) === pending) inFlight.delete(pending.key);
  }
}

function subscribePending(pending: PendingRequest, signal?: AbortSignal): Promise<Json> {
  if (!signal) {
    pending.keepAlive = true;
    return pending.promise;
  }
  if (signal.aborted) return Promise.reject(abortError());
  pending.consumers += 1;
  return new Promise<Json>((resolve, reject) => {
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      signal.removeEventListener("abort", onAbort);
      releasePending(pending);
    };
    const onAbort = () => {
      release();
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    pending.promise.then(
      (value) => {
        release();
        resolve(value);
      },
      (error) => {
        release();
        reject(error);
      },
    );
  });
}

async function loadPath(cacheKey: string, path: string, force = false, signal?: AbortSignal): Promise<Json> {
  let cached = cache.get(cacheKey);
  if (cached && !responseIsTerminal(cached.value)) {
    cache.delete(cacheKey);
    cached = undefined;
  }
  if (!force && cached && Date.now() - cached.readAt < CACHE_TTL_MS) return cached.value;
  const pending = inFlight.get(cacheKey);
  if (pending) return subscribePending(pending, signal);
  if (signal?.aborted) return Promise.reject(abortError());
  const controller = new AbortController();
  let requestEntry: PendingRequest;
  const request = readApi(path, controller.signal).then((value) => {
    if (responseIsTerminal(value)) cache.set(cacheKey, { value, readAt: Date.now() });
    return value;
  });
  requestEntry = {
    key: cacheKey,
    promise: request,
    controller,
    consumers: 0,
    keepAlive: !signal,
    settled: false,
  };
  inFlight.set(cacheKey, requestEntry);
  request.then(
    () => {
      requestEntry.settled = true;
      if (inFlight.get(cacheKey) === requestEntry) inFlight.delete(cacheKey);
    },
    () => {
      requestEntry.settled = true;
      if (inFlight.get(cacheKey) === requestEntry) inFlight.delete(cacheKey);
    },
  );
  return subscribePending(requestEntry, signal);
}

async function loadEndpoint(key: keyof typeof endpoints, force = false, signal?: AbortSignal): Promise<Json> {
  return loadPath(String(key), endpoints[key], force, signal);
}

function retryableReadFailure(error: any) {
  return error?.name !== "AbortError" && Number(error?.status || 0) === 0;
}

function waitForReadRetry(signal?: AbortSignal) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, 250);
    const onAbort = () => {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function loadEndpointWithRetry(key: keyof typeof endpoints, force = false, signal?: AbortSignal): Promise<Json> {
  try {
    return await loadEndpoint(key, force, signal);
  } catch (error) {
    if (!retryableReadFailure(error)) throw error;
    await waitForReadRetry(signal);
    return loadEndpoint(key, true, signal);
  }
}

type InitialDynamicRequest = {
  dataKey: "researchStockEvidence" | "researchIndustryEvidence" | "evidenceVerification" | "decisionDetail";
  cacheKey: string;
  paths: string[];
};

function initialDynamicRequests(pathname: string, data: ConsoleData): InitialDynamicRequest[] {
  if (pathname === "/research") {
    const pools = firstArray(data.pools, ["items", "members", "pools"]);
    const industries = firstArray(data.industries, ["items", "industries"]);
    const stock = String(pick(pools[0], ["ts_code", "symbol"], ""));
    const industry = String(pick(industries.find((row) => {
      const value = String(pick(row, ["industry", "industry_name", "industry_code"], "")).trim().toLowerCase();
      return Boolean(value) && value !== "nan";
    }), ["industry_code", "code", "name"], ""));
    const requests: InitialDynamicRequest[] = [];
    if (stock) {
      requests.push({
        dataKey: "researchStockEvidence",
        cacheKey: `research-stock:${stock}`,
        paths: [`/api/research/stocks/${encodeURIComponent(stock)}/console-evidence`],
      });
    }
    if (industry) requests.push({
      dataKey: "researchIndustryEvidence",
      cacheKey: `research-industry:${industry}`,
      paths: [`/api/research/industries/${encodeURIComponent(industry)}`],
    });
    return requests;
  }
  if (pathname === "/admin/evidence" || pathname === "/evidence") {
    const decisions = firstArray(data.decisions, ["items", "decisions"]);
    const id = Number(decisions[0]?.id || 0);
    return id ? [
      { dataKey: "decisionDetail", cacheKey: `decision-detail:${id}`, paths: [`/api/research/decisions/${id}`] },
      { dataKey: "evidenceVerification", cacheKey: `evidence-verification:${id}`, paths: [`/api/research/evidence/verify/${id}`] },
    ] : [];
  }
  return [];
}

async function loadDynamicRequest(request: InitialDynamicRequest, force: boolean, signal: AbortSignal): Promise<Json> {
  const cached = cache.get(request.cacheKey);
  if (!force && cached && Date.now() - cached.readAt < CACHE_TTL_MS) return cached.value;
  const results = await Promise.allSettled(request.paths.map((path) => loadPath(path, path, force, signal)));
  const values = request.paths.map((path, index) => {
    const result = results[index];
    return result.status === "fulfilled" ? result.value : { status: "unavailable", reason: String(result.reason) };
  });
  const value = request.paths.length === 1 ? values[0] : Object.fromEntries(["company", "expectations", "valuation", "eligibility", "financialCompleteness"].map((key, index) => [key, values[index]]));
  const failed = results.filter((result) => result.status === "rejected");
  if (failed.length) {
    const error = new Error(`${failed.length}/${request.paths.length} 首屏详情请求失败`);
    (error as Error & { value?: Json }).value = value;
    throw error;
  }
  cache.set(request.cacheKey, { value, readAt: Date.now() });
  return value;
}

export async function preloadConsoleRoutes(paths: string[], concurrency = 3) {
  const keys = [...new Set(paths.flatMap((path) => routeKeys[path] || []))];
  const results: PromiseSettledResult<Json>[] = [];
  let cursor = 0;
  const worker = async () => {
    while (cursor < keys.length) {
      const key = keys[cursor++];
      try {
        results.push({ status: "fulfilled", value: await loadEndpoint(key, false) });
      } catch (reason) {
        results.push({ status: "rejected", reason });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(Math.max(1, concurrency), keys.length) }, worker));
  return results;
}

export function useConsoleData(pathname: string) {
  const keys = useMemo(() => routeKeys[pathname] || routeKeys["/overview"], [pathname]);
  const cachedData = Object.fromEntries(keys.flatMap((key) => {
    const item = cache.get(key);
    return item ? [[key, item.value]] : [];
  })) as ConsoleData;
  for (const request of initialDynamicRequests(pathname, cachedData)) {
    const item = cache.get(request.cacheKey);
    if (item) cachedData[request.dataKey] = item.value;
  }
  const initialDynamic = initialDynamicRequests(pathname, cachedData);
  const initialIncomplete = incompleteRouteData(pathname, cachedData);
  const [data, setData] = useState<ConsoleData>(cachedData);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(initialIncomplete.length > 0 || keys.some((key) => !cache.has(String(key))) || initialDynamic.some((request) => !cache.has(request.cacheKey)));
  const [loadingMessage, setLoadingMessage] = useState("正在汇总决策、账户和运行证据");
  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const controller = useRef<AbortController | null>(null);
  const retryTimer = useRef<number | null>(null);
  const retryAttempt = useRef(0);

  const refresh = useCallback(async (force = true) => {
    const startedAt = Date.now();
    if (retryTimer.current !== null) {
      window.clearTimeout(retryTimer.current);
      retryTimer.current = null;
    }
    if (force) retryAttempt.current = 0;
    controller.current?.abort();
    controller.current = new AbortController();
    const hasRouteCache = incompleteRouteData(pathname, cachedData).length === 0 && keys.every((key) => cache.has(String(key))) && initialDynamicRequests(pathname, cachedData).every((request) => cache.has(request.cacheKey));
    setLoading(!hasRouteCache);
    setLoadingMessage("正在汇总决策、账户和运行证据");
    setRefreshing(force);
    const entries = keys;
    const activeController = controller.current;
    setProgress({ completed: 0, total: entries.length });
    setErrors({});
    const releaseRefreshTimer = window.setTimeout(() => {
      if (controller.current === activeController) setRefreshing(false);
    }, 600);
    let cursor = 0;
    // Keep admin reads bounded: the API process and database are also serving
    // scheduled work, and a larger fan-out increases contention on the same
    // point-in-time diagnostics instead of reducing the page's total span.
    const requestConcurrency = pathname === "/models"
      ? 3
      : (pathname.startsWith("/admin") || ["/operations", "/data", "/evidence"].includes(pathname)) ? 2 : 3;
    const loadedData: ConsoleData = { ...cachedData };
    const loadWorker = async () => {
      while (cursor < entries.length) {
        const key = entries[cursor++];
        try {
          const value = await loadEndpointWithRetry(key, force, activeController.signal);
          if (controller.current !== activeController) return;
          loadedData[key] = value;
          setData((current) => ({ ...current, [key]: value }));
          const warning = diagnosticMessage(String(key), value);
          if (warning) setErrors((current) => ({ ...current, [key]: warning }));
        } catch (error: any) {
          if (activeController.signal.aborted || controller.current !== activeController) return;
          const terminal = terminalReadFailure(String(key), error);
          if (terminal) {
            loadedData[key] = terminal;
            setData((current) => ({ ...current, [key]: terminal }));
            setErrors((current) => ({ ...current, [key]: diagnosticMessage(String(key), terminal) }));
          }
          const message = error?.name === "AbortError" ? "读取超时" : String(error?.message || "读取失败");
          if (!terminal) setErrors((current) => ({ ...current, [key]: message }));
        } finally {
          if (controller.current === activeController) {
            setProgress((current) => ({ ...current, completed: Math.min(current.total, current.completed + 1) }));
          }
        }
      }
    };
    const requests = Array.from(
      { length: Math.min(requestConcurrency, entries.length) },
      () => loadWorker(),
    );
    const minimumVisibleMs = 300;
    const remainingMs = Math.max(0, minimumVisibleMs - (Date.now() - startedAt));
    if (remainingMs) await new Promise((resolve) => window.setTimeout(resolve, remainingMs));
    await Promise.allSettled(requests);
    const dynamicRequests = initialDynamicRequests(pathname, loadedData);
    if (controller.current === activeController) {
      const dynamicUnits = dynamicRequests.reduce((total, request) => total + request.paths.length, 0);
      setProgress((current) => ({ ...current, total: current.total + dynamicUnits }));
    }
    let dynamicCursor = 0;
    const loadDynamicWorker = async () => {
      while (dynamicCursor < dynamicRequests.length) {
        const request = dynamicRequests[dynamicCursor++];
        try {
          const value = await loadDynamicRequest(request, force, activeController.signal);
          if (controller.current !== activeController) return;
          loadedData[request.dataKey] = value;
          setData((current) => ({ ...current, [request.dataKey]: value }));
          const warning = diagnosticMessage(request.dataKey, value);
          if (warning) setErrors((current) => ({ ...current, [request.dataKey]: warning }));
        } catch (error: any) {
          if (activeController.signal.aborted || controller.current !== activeController) return;
          const terminal = terminalReadFailure(request.dataKey, error);
          if (terminal) {
            const value = error?.value && typeof error.value === "object" && !Array.isArray(error.value)
              ? { ...error.value, ...terminal, component: request.dataKey }
              : terminal;
            loadedData[request.dataKey] = value;
            setData((current) => ({ ...current, [request.dataKey]: value }));
            setErrors((current) => ({ ...current, [request.dataKey]: diagnosticMessage(request.dataKey, value) }));
          }
          const message = error?.name === "AbortError" ? "读取超时" : String(error?.message || "读取失败");
          if (!terminal) setErrors((current) => ({ ...current, [request.dataKey]: message }));
        } finally {
          if (controller.current === activeController) {
            setProgress((current) => ({ ...current, completed: Math.min(current.total, current.completed + request.paths.length) }));
          }
        }
      }
    };
    const dynamicWorkers = Array.from(
      { length: Math.min(requestConcurrency, dynamicRequests.length) },
      () => loadDynamicWorker(),
    );
    await Promise.allSettled(dynamicWorkers);
    const incomplete = incompleteRouteData(pathname, loadedData);
    if (incomplete.length > 0) {
      const dynamicUnits = dynamicRequests.reduce((total, request) => total + request.paths.length, 0);
      const completed = entries.filter((key) => payloadIsComplete(String(key), loadedData[key])).length
        + dynamicRequests.filter((request) => payloadIsComplete(request.dataKey, loadedData[request.dataKey])).reduce((total, request) => total + request.paths.length, 0);
      retryAttempt.current += 1;
      const retryInMs = Math.min(15_000, 1_000 * 2 ** Math.min(retryAttempt.current - 1, 4));
      window.clearTimeout(releaseRefreshTimer);
      if (controller.current === activeController) {
        setProgress({ completed, total: entries.length + dynamicUnits });
        setLoading(true);
        setRefreshing(false);
        const pendingLabels = incomplete.map(initialRequestLabel).join("、");
        setLoadingMessage(`等待首屏接口：${pendingLabels}；${Math.ceil(retryInMs / 1000)} 秒后自动重试`);
        retryTimer.current = window.setTimeout(() => {
          retryTimer.current = null;
          if (controller.current === activeController) void refresh(false);
        }, retryInMs);
      }
      return;
    }
    retryAttempt.current = 0;
    window.clearTimeout(releaseRefreshTimer);
    if (controller.current === activeController) { setLoading(false); setRefreshing(false); setLoadingMessage("正在汇总决策、账户和运行证据"); }
  }, [keys]);

  useEffect(() => {
    refresh(false);
    return () => {
      controller.current?.abort();
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
    };
  }, [refresh]);

  return { data, errors, loading, loadingMessage, refreshing, progress, refresh: () => refresh(true) };
}

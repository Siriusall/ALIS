import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { endpoints, firstArray, Json, pick, readApi } from "./api";

export type ConsoleData = Partial<Record<keyof typeof endpoints, Json>> & {
  researchStockEvidence?: Json;
  researchIndustryEvidence?: Json;
  evidenceVerification?: Json;
  decisionDetail?: Json;
};

const routeKeys: Record<string, Array<keyof typeof endpoints>> = {
  "/overview": ["owner", "ledger", "portfolio", "tradingReady", "latestPlan", "health", "adminTasksLive", "dailyUpdateStatus", "watermarks", "ledgerAudit"],
  "/system": ["health", "tradingReady", "adminTasksLive"],
  "/system/versions": ["systemVersionStatus", "systemReleases", "systemContracts"],
  "/decision": ["owner", "decision", "tradingReady", "latestPlan"],
  "/portfolio": ["owner", "ledger", "positions", "portfolio", "risk", "research", "latestPlan"],
  "/evolution": ["factors", "factorStatus", "factorResearchConsole", "factorLineage", "evolutionTasks", "candidateObservations", "ledger"],
  "/evolution/factors": ["factors", "researchAnalytics", "researchFactorAnalytics"],
  "/evolution/metrics": ["researchAnalytics", "researchTimeseries", "evolutionTasks"],
  "/evolution/waves": ["researchAnalyticsConsole", "researchExpertHistory", "researchEnvironmentSimilarity", "researchArchive"],
  "/admin": ["health", "tradingReady", "tasks", "adminTasksLive", "dailyUpdateStatus", "watermarks", "ledgerAudit"],
  "/admin/operations": ["health", "owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executionsPage", "operationsPage", "ledgerAudit"],
  "/factors": ["owner", "strategy", "factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/admin/models": ["planModels", "modelOperations", "models", "modelMarketLive", "modelGovernance", "modelReports", "modelProduction", "modelController", "modelOutputContracts", "modelEvidenceBindings", "modelValidation", "modelMarketHistory", "factorResearchCompact", "evolutionTasks", "modelTasksLive"],
  "/admin/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "tasks"],
  "/admin/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "reportCompact", "backtests", "ledgerAudit"],
  "/operations": ["health", "owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executionsPage", "operationsPage", "ledgerAudit"],
  "/models": ["planModels", "modelOperations", "models", "modelMarketLive", "modelGovernance", "modelReports", "modelProduction", "modelController", "modelOutputContracts", "modelEvidenceBindings", "modelValidation", "modelMarketHistory", "factorResearchCompact", "evolutionTasks", "modelTasksLive"],
  "/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "tasks"],
  "/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "reportCompact", "backtests", "ledgerAudit"],
  "/research": ["researchContracts", "researchGovernance", "researchPositionState", "researchDataAssets", "analystConsensus", "theses", "industries", "industryAllocation", "themes", "events", "pools", "modelMarketLive"],
};

// Shared-account pages are materialized on the server.  A browser therefore
// reads one coherent page document instead of fanning out into the same large
// PostgreSQL reads as every other open tab.
const snapshotPages: Record<string, string> = {
  "/overview": "overview",
  "/decision": "decision",
  "/portfolio": "portfolio",
  "/operations": "operations",
  "/system": "operations",
  "/system/versions": "versions",
  "/data": "data",
  "/models": "models",
  "/research": "research",
  "/factors": "factors",
  "/evolution/factors": "factors",
  "/evolution/metrics": "factors",
  "/evolution": "factors",
  "/evolution/waves": "waves",
  "/evidence": "evidence",
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
const snapshotRequestGeneration = new Map<string, number>();
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

function snapshotData(payload: unknown): ConsoleData | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const data = (payload as Json).data;
  return data && typeof data === "object" && !Array.isArray(data) ? data as ConsoleData : null;
}

function snapshotHasData(payload: unknown) {
  const data = snapshotData(payload);
  return Boolean(data && Object.keys(data).length > 0);
}

function snapshotStatus(payload: unknown) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "unavailable";
  return String((payload as Json).status || "unavailable").trim().toLowerCase();
}

function snapshotIsHealthy(payload: unknown): payload is Json {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const snapshot = payload as Json;
  const status = snapshotStatus(snapshot);
  // A completed document may be degraded because one named section is
  // unavailable. Its other sections are still authoritative and must render;
  // only transient/no-document states keep the page behind loading.
  return status !== "warming_up"
    && !Boolean(snapshot.stale)
    && !Boolean(snapshot.refreshing)
    && snapshotHasData(snapshot);
}

function cachedHealthySnapshot(page: string): Json | null {
  const cacheKey = `snapshot:${page}`;
  const cached = cache.get(cacheKey);
  if (!cached) return null;
  if (!snapshotIsHealthy(cached.value)) {
    cache.delete(cacheKey);
    return null;
  }
  return cached.value;
}

function snapshotErrorCode(section: Json) {
  const error = section.error;
  const value = error && typeof error === "object" && !Array.isArray(error)
    ? error.code || error.kind || error.status
    : section.error_code || section.code;
  return String(value || "").trim().toLowerCase();
}

function snapshotSectionMessage(key: string, section: Json) {
  const label = initialRequestLabel(key);
  const status = String(section.status || "error").trim().toLowerCase();
  const code = snapshotErrorCode(section);
  if (/timeout|deadline/.test(code)) return `${label} 查询超时，不能据此判断为无数据`;
  if (/connection|database|unavailable/.test(code)) return `${label} 暂不可读，正在等待下一个完整快照`;
  if (status === "degraded") return `${label} 仅返回部分已验证证据`;
  return `${label} 暂不可读，正在等待下一个完整快照`;
}

function snapshotSectionErrors(payload: Json) {
  const sections = payload.sections;
  if (!sections || typeof sections !== "object" || Array.isArray(sections)) return {};
  const readableResearchSections = new Set(["researchGovernance", "theses", "industryAllocation", "modelMarketLive"]);
  return Object.entries(sections).reduce<Record<string, string>>((result, [key, value]) => {
    const section = value && typeof value === "object" && !Array.isArray(value) ? value as Json : {};
    const status = String(section.status || "").trim().toLowerCase();
    // These research routes return structured business conclusions even when
    // one sub-check is blocked or historical evidence is partial.  Do not
    // label the whole section as unavailable; the section body shows its
    // exact governance/data state.
    if (payload.page === "research" && readableResearchSections.has(key)) return result;
    if (status === "error" || status === "degraded") result[key] = snapshotSectionMessage(key, section);
    return result;
  }, {});
}

function snapshotRetryMessage(payload: Json, hasVisibleData: boolean) {
  if (snapshotStatus(payload) === "warming_up") {
    return hasVisibleData
      ? "服务端页面快照正在生成，继续展示最近一次可验证数据"
      : "服务端页面快照正在生成，尚无可验证的页面数据";
  }
  if (payload.stale) {
    return hasVisibleData
      ? "服务端页面快照已失效，继续展示最近一次可验证数据"
      : "服务端页面快照已失效，正在等待首个可验证版本";
  }
  if (payload.refreshing) return "服务端正在生成新的页面快照";
  if (snapshotStatus(payload) === "degraded") return "新页面快照包含部分可用证据，正在等待完整版本";
  return "页面快照尚未达到完整状态，正在等待新版本";
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
  const isSnapshot = cacheKey.startsWith("snapshot:");
  if (cached && (!responseIsTerminal(cached.value) || (isSnapshot && !snapshotIsHealthy(cached.value)))) {
    cache.delete(cacheKey);
    cached = undefined;
  }
  // The server-side snapshot layer already provides the cheap Redis/PostgreSQL
  // read path. Re-read it on each navigation so a browser cache cannot hide a
  // durable invalidation or a completed worker rebuild.
  if (!force && !isSnapshot && cached && Date.now() - cached.readAt < CACHE_TTL_MS) return cached.value;
  const pending = inFlight.get(cacheKey);
  // A user-initiated snapshot refresh must still issue its durable
  // invalidation when a hover prefetch is in flight.
  if (pending && !(isSnapshot && force)) return subscribePending(pending, signal);
  if (signal?.aborted) return Promise.reject(abortError());
  const controller = new AbortController();
  const generation = isSnapshot ? (snapshotRequestGeneration.get(cacheKey) || 0) + 1 : 0;
  if (isSnapshot) snapshotRequestGeneration.set(cacheKey, generation);
  let requestEntry: PendingRequest;
  const request = readApi(path, controller.signal).then((value) => {
    if (responseIsTerminal(value) && (!isSnapshot || snapshotIsHealthy(value))
      && (!isSnapshot || snapshotRequestGeneration.get(cacheKey) === generation)) {
      cache.set(cacheKey, { value, readAt: Date.now() });
    }
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
  // Snapshot-backed routes must never fan out into live detail reads. The
  // control worker materializes the bounded detail sections with the page.
  if (snapshotPages[pathname]) return [];
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
  const snapshotPaths = [...new Set(paths.map((path) => snapshotPages[path]).filter(Boolean))];
  if (snapshotPaths.length) {
    return Promise.allSettled(snapshotPaths.map(async (page) => {
      const payload = await loadPath(`snapshot:${page}`, `/api/console/snapshots/${page}`);
      // Hover prefetch must not make a warming or stale response look like a
      // usable document to the next route transition.
      if (!snapshotIsHealthy(payload)) throw new Error("snapshot_not_ready");
      return payload;
    }));
  }
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
  const snapshotPage = snapshotPages[pathname];
  const initialSnapshot = snapshotPage ? cachedHealthySnapshot(snapshotPage) : null;
  const cachedData = (snapshotData(initialSnapshot) || Object.fromEntries(keys.flatMap((key) => {
    const item = cache.get(key);
    return item ? [[key, item.value]] : [];
  }))) as ConsoleData;
  for (const request of initialDynamicRequests(pathname, cachedData)) {
    const item = cache.get(request.cacheKey);
    if (item) cachedData[request.dataKey] = item.value;
  }
  const initialDynamic = initialDynamicRequests(pathname, cachedData);
  const initialIncomplete = incompleteRouteData(pathname, cachedData);
  const [data, setData] = useState<ConsoleData>(cachedData);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(
    snapshotPage
      ? !initialSnapshot
      : initialIncomplete.length > 0 || keys.some((key) => !cache.has(String(key))) || initialDynamic.some((request) => !cache.has(request.cacheKey)),
  );
  const [loadingMessage, setLoadingMessage] = useState("正在汇总决策、账户和运行证据");
  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const routeGeneration = useRef({ pathname, value: 0 });
  if (routeGeneration.current.pathname !== pathname) {
    routeGeneration.current = { pathname, value: routeGeneration.current.value + 1 };
  }
  const activeRouteGeneration = routeGeneration.current.value;
  const [loadedRoute, setLoadedRoute] = useState({
    pathname: initialSnapshot ? pathname : "",
    generation: 0,
  });
  const controller = useRef<AbortController | null>(null);
  const retryTimer = useRef<number | null>(null);
  const retryAttempt = useRef(0);
  const lastGoodSnapshot = useRef<{ page: string; payload: Json } | null>(
    snapshotPage && initialSnapshot ? { page: snapshotPage, payload: initialSnapshot } : null,
  );
  const lastVisibleSnapshot = useRef<{ page: string; data: ConsoleData } | null>(
    snapshotPage && snapshotData(initialSnapshot) ? { page: snapshotPage, data: snapshotData(initialSnapshot)! } : null,
  );

  const refresh = useCallback(async (force = true) => {
    const startedAt = Date.now();
    if (retryTimer.current !== null) {
      window.clearTimeout(retryTimer.current);
      retryTimer.current = null;
    }
    if (force) retryAttempt.current = 0;
    controller.current?.abort();
    controller.current = new AbortController();
    const snapshotPage = snapshotPages[pathname];
    const activeController = controller.current;
    if (snapshotPage) {
      const snapshotCacheKey = `snapshot:${snapshotPage}`;
      const cachedSnapshot = cachedHealthySnapshot(snapshotPage);
      if (cachedSnapshot) lastGoodSnapshot.current = { page: snapshotPage, payload: cachedSnapshot };
      const priorGood = lastGoodSnapshot.current?.page === snapshotPage
        ? snapshotData(lastGoodSnapshot.current.payload)
        : null;
      const priorVisible = lastVisibleSnapshot.current?.page === snapshotPage
        ? lastVisibleSnapshot.current.data
        : null;
      const visibleBeforeRead = priorGood || priorVisible;
      if (force) cache.delete(snapshotCacheKey);

      const showSnapshotData = (snapshot: ConsoleData) => {
        lastVisibleSnapshot.current = { page: snapshotPage, data: snapshot };
        setData(snapshot);
        setLoadedRoute({ pathname, generation: activeRouteGeneration });
      };
      const scheduleSnapshotRetry = (message: string, hasVisibleData: boolean) => {
        retryAttempt.current += 1;
        const retryInMs = Math.min(15_000, 1_000 * 2 ** Math.min(retryAttempt.current - 1, 4));
        if (controller.current !== activeController) return;
        setProgress({ completed: 0, total: 1 });
        setLoading(!hasVisibleData);
        setRefreshing(true);
        setLoadingMessage(`${message}；${Math.ceil(retryInMs / 1000)} 秒后自动重试`);
        retryTimer.current = window.setTimeout(() => {
          retryTimer.current = null;
          if (controller.current === activeController) void refresh(false);
        }, retryInMs);
      };

      if (visibleBeforeRead) showSnapshotData(visibleBeforeRead);
      setLoading(!visibleBeforeRead);
      setLoadingMessage("正在读取服务端页面快照");
      setRefreshing(Boolean(visibleBeforeRead) || force);
      setProgress({ completed: 0, total: 1 });
      setErrors({});
      try {
        const payload = await loadPath(
          `snapshot:${snapshotPage}`,
          `/api/console/snapshots/${snapshotPage}${force ? "?refresh=true" : ""}`,
          force,
          activeController.signal,
        );
        if (controller.current !== activeController) return;
        const nextSnapshot = snapshotData(payload);
        if (snapshotIsHealthy(payload) && nextSnapshot) {
          lastGoodSnapshot.current = { page: snapshotPage, payload };
          showSnapshotData(nextSnapshot);
          retryAttempt.current = 0;
          setProgress({ completed: 1, total: 1 });
          setLoading(false);
          setRefreshing(false);
          setLoadingMessage("正在汇总决策、账户和运行证据");
          setErrors(snapshotSectionErrors(payload));
          return;
        }

        // A stale durable document is useful only when this tab has no newer
        // visible data. It remains in component state, never in browser cache.
        const fallbackData = priorGood || priorVisible
          || (snapshotStatus(payload) === "warming_up" ? null : nextSnapshot);
        if (fallbackData) showSnapshotData(fallbackData);
        const sectionErrors = snapshotSectionErrors(payload);
        sectionErrors.snapshot = snapshotRetryMessage(payload, Boolean(fallbackData));
        setErrors(sectionErrors);
        scheduleSnapshotRetry(sectionErrors.snapshot, Boolean(fallbackData));
      } catch (error: any) {
        if (activeController.signal.aborted || controller.current !== activeController) return;
        const message = error?.name === "AbortError"
          ? "页面快照读取超时，不能据此判断为无数据"
          : "页面快照暂不可读，正在等待下一个完整版本";
        if (visibleBeforeRead) showSnapshotData(visibleBeforeRead);
        setErrors({ snapshot: message });
        scheduleSnapshotRetry(message, Boolean(visibleBeforeRead));
      }
      return;
    }
    const hasRouteCache = incompleteRouteData(pathname, cachedData).length === 0 && keys.every((key) => cache.has(String(key))) && initialDynamicRequests(pathname, cachedData).every((request) => cache.has(request.cacheKey));
    setLoading(!hasRouteCache);
    setLoadingMessage("正在汇总决策、账户和运行证据");
    setRefreshing(force);
    const entries = keys;
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
  }, [keys, pathname, activeRouteGeneration]);

  useEffect(() => {
    refresh(false);
    return () => {
      controller.current?.abort();
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
    };
  }, [refresh]);

  const cachedSnapshot = snapshotPage ? cachedHealthySnapshot(snapshotPage) : null;
  const cachedSnapshotData = snapshotData(cachedSnapshot);
  const snapshotRouteReady = Boolean(cachedSnapshotData)
    || (loadedRoute.pathname === pathname && loadedRoute.generation === activeRouteGeneration);
  const stateMatchesSnapshotRoute = loadedRoute.pathname === pathname
    && loadedRoute.generation === activeRouteGeneration;
  const visibleData = cachedSnapshotData || data;
  const visibleLoading = snapshotPage ? loading || !snapshotRouteReady : loading;
  const cachedSnapshotErrors = cachedSnapshot ? snapshotSectionErrors(cachedSnapshot) : {};
  const visibleErrors = snapshotPage
    ? stateMatchesSnapshotRoute
      ? { ...cachedSnapshotErrors, ...errors }
      : cachedSnapshotErrors
    : errors;

  return {
    data: visibleData,
    errors: visibleErrors,
    loading: visibleLoading,
    loadingMessage,
    refreshing,
    progress,
    refresh: () => refresh(true),
  };
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { endpoints, firstArray, Json, pick, readApi } from "./api";
import {
  consoleDataIsRenderable,
  initialRequestLabel,
  payloadIsComplete,
  responseIsDegraded,
  responseIsTerminal,
  snapshotData,
  snapshotIsHealthy,
  snapshotIsRenderable,
  snapshotPresentationStatus,
  snapshotRefreshUnavailable,
  snapshotRetryDelay,
  snapshotRetryMessage,
  snapshotSectionErrors,
  snapshotStatus,
} from "./snapshotUtils";

export type ConsoleData = Partial<Record<keyof typeof endpoints, Json>> & {
  researchStockEvidence?: Json;
  researchIndustryEvidence?: Json;
  evidenceVerification?: Json;
  decisionDetail?: Json;
  __snapshot?: Json;
};

// Snapshot-backed console routes are the source of truth. Legacy /admin and
// /factors aliases only redirect in App, so they do not keep a separate fan-out map.
const routeKeys: Record<string, Array<keyof typeof endpoints>> = {
  "/overview": ["owner", "ledger", "portfolio", "tradingReady", "latestPlan", "health", "adminTasksLive", "dailyUpdateStatus", "watermarks", "ledgerAudit", "nodeActivities"],
  "/system": ["health", "tradingReady", "adminTasksLive"],
  "/system/versions": ["systemVersionStatus", "systemReleases", "systemContracts"],
  "/decision": ["owner", "decision", "tradingReady", "latestPlan"],
  "/portfolio": ["owner", "ledger", "positions", "portfolio", "risk", "research", "latestPlan"],
  "/evolution": ["factors", "factorStatus", "factorResearchConsole", "factorLineage", "evolutionTasks", "candidateObservations", "ledger"],
  "/evolution/factors": ["factors", "researchAnalytics", "researchFactorAnalytics"],
  "/evolution/metrics": ["researchAnalytics", "researchTimeseries", "evolutionTasks"],
  "/evolution/waves": ["researchAnalyticsConsole", "researchExpertHistory", "researchEnvironmentSimilarity", "researchArchive"],
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
const SNAPSHOT_POLL_INTERVAL_MS: Record<string, number> = {
  "/overview": 600_000,
  "/decision": 600_000,
  "/portfolio": 600_000,
  "/operations": 30_000,
  "/system": 30_000,
  "/system/versions": 600_000,
  "/data": 600_000,
  "/models": 600_000,
  "/research": 600_000,
  "/factors": 600_000,
  "/evolution": 600_000,
  "/evolution/factors": 600_000,
  "/evolution/metrics": 600_000,
  "/evolution/waves": 600_000,
  "/evidence": 600_000,
};

function snapshotPollInterval(pathname: string) {
  return SNAPSHOT_POLL_INTERVAL_MS[pathname] || 600_000;
}

function snapshotViewData(
  page: string,
  snapshot: ConsoleData,
  payload?: Json,
  readHealth?: "healthy" | "partial" | "degraded",
  refreshPayload?: Json,
): ConsoleData {
  const refreshState = refreshPayload || payload;
  const rawSourceStatus = payload ? snapshotStatus(payload) : "unavailable";
  const sourceStatus = payload ? snapshotPresentationStatus(payload) : rawSourceStatus;
  const health = readHealth || (
    refreshState?.stale || refreshState?.refreshing
      ? "degraded"
      : sourceStatus === "degraded"
        ? "partial"
        : "healthy"
  );
  return {
    ...snapshot,
    __snapshot: {
      page,
      version: String(payload?.version || snapshot.__snapshot?.version || ""),
      generatedAt: String(payload?.generated_at || snapshot.__snapshot?.generatedAt || ""),
      checkedAt: payload ? new Date().toISOString() : String(snapshot.__snapshot?.checkedAt || ""),
      attemptedAt: new Date().toISOString(),
      sourceStatus,
      documentStatus: String(payload?.document_status || rawSourceStatus),
      businessStatus: String(payload?.business_status || "unknown"),
      dataAsOf: String(payload?.data_as_of || ""),
      staleReason: String(refreshState?.stale_reason || ""),
      refreshAvailable: !snapshotRefreshUnavailable(refreshState),
      refreshWorkerStatus: String(
        refreshState?.snapshot_worker_status
          || refreshState?.refresh_worker_status
          || refreshState?.worker_status
          || "",
      ),
      refreshErrorCode: String(
        refreshState?.refresh_error_code
          || refreshState?.error_code
          || refreshState?.error?.code
          || "",
      ),
      health,
      stale: Boolean(refreshState?.stale),
      refreshing: Boolean(refreshState?.refreshing)
        || Boolean(refreshState && !snapshotIsRenderable(refreshState)),
    },
  };
}

function cachedRenderableSnapshot(page: string): Json | null {
  const cacheKey = `snapshot:${page}`;
  const cached = cache.get(cacheKey);
  if (!cached) return null;
  if (!snapshotIsRenderable(cached.value)) {
    cache.delete(cacheKey);
    return null;
  }
  return cached.value;
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

function diagnosticMessage(key: string, value: unknown) {
  if (!responseIsDegraded(value)) return "";
  const payload = value as Json;
  const component = String(payload.component || key);
  const code = String(payload.error?.code || payload.status || "data_unavailable");
  if (code === "request_timeout") return `${component}（查询超时）未完成，不能判断为无数据`;
  return `${component}（${code}）已返回终态，当前没有可用证据`;
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
  if (cached && (!responseIsTerminal(cached.value) || (isSnapshot && !snapshotIsRenderable(cached.value)))) {
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
    if (responseIsTerminal(value) && (!isSnapshot || snapshotIsRenderable(value))
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
      // Hover prefetch may retain a stale last-known-good document, but never
      // makes a cold warming/error envelope look like page content.
      if (!snapshotIsRenderable(payload)) throw new Error("snapshot_not_ready");
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
  const initialSnapshot = snapshotPage ? cachedRenderableSnapshot(snapshotPage) : null;
  const initialSnapshotData = snapshotData(initialSnapshot);
  const cachedData = (initialSnapshotData && snapshotPage
    ? snapshotViewData(snapshotPage, initialSnapshotData as ConsoleData, initialSnapshot || undefined)
    : Object.fromEntries(keys.flatMap((key) => {
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
  const snapshotRetryState = useRef("");
  const snapshotNextPollDelay = useRef<number | null>(null);
  const lastGoodSnapshot = useRef<{ page: string; payload: Json } | null>(
    snapshotPage && initialSnapshot ? { page: snapshotPage, payload: initialSnapshot } : null,
  );
  const lastVisibleSnapshot = useRef<{ page: string; data: ConsoleData } | null>(
    snapshotPage && initialSnapshotData ? { page: snapshotPage, data: cachedData } : null,
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
      const cachedSnapshot = cachedRenderableSnapshot(snapshotPage);
      if (cachedSnapshot) lastGoodSnapshot.current = { page: snapshotPage, payload: cachedSnapshot };
      const priorGood = lastGoodSnapshot.current?.page === snapshotPage
        ? snapshotData(lastGoodSnapshot.current.payload)
        : null;
      const priorVisible = lastVisibleSnapshot.current?.page === snapshotPage
        ? lastVisibleSnapshot.current.data
        : null;
      const visibleBeforeRead = priorGood || priorVisible;
      // Keep the last-known-good browser copy while a forced request is in
      // flight. If the API or worker is unavailable, route changes must still
      // be able to render that evidence instead of turning a transient read
      // failure into a cold page.

      const showSnapshotData = (
        snapshot: ConsoleData,
        payload?: Json,
        readHealth?: "healthy" | "partial" | "degraded",
        refreshPayload?: Json,
      ) => {
        const visibleSnapshot = snapshotViewData(
          snapshotPage,
          snapshot,
          payload,
          readHealth,
          refreshPayload,
        );
        lastVisibleSnapshot.current = { page: snapshotPage, data: visibleSnapshot };
        setData(visibleSnapshot);
        setLoadedRoute({ pathname, generation: activeRouteGeneration });
      };
      const scheduleSnapshotRetry = (
        message: string,
        hasVisibleData: boolean,
        payload?: Json,
      ) => {
        const retryInMs = snapshotRetryDelay(
          payload,
          retryAttempt.current,
          snapshotPollInterval(pathname),
        );
        retryAttempt.current += 1;
        snapshotNextPollDelay.current = retryInMs;
        if (controller.current !== activeController) return;
        setProgress({ completed: 0, total: 1 });
        setLoading(!hasVisibleData);
        // The server status bar still reports a pending refresh. Keep the
        // manual action available when it explicitly reports no worker.
        setRefreshing(!snapshotRefreshUnavailable(payload));
        setLoadingMessage(`${message}；${Math.ceil(retryInMs / 1000)} 秒后自动重试`);
      };
      const resetRetryOnSnapshotProgress = (payload: Json) => {
        const retryState = [
          snapshotPage,
          payload.version,
          payload.refresh_generation,
          payload.generated_at,
          payload.last_attempt_generation,
          payload.last_attempt_at,
          payload.refresh_available,
          payload.refresh_error_code,
          payload.snapshot_worker_status,
          payload.refresh_worker_status,
          payload.worker_status,
        ].map((value) => String(value || "")).join("|");
        if (retryState && retryState !== snapshotRetryState.current) {
          retryAttempt.current = 0;
          snapshotRetryState.current = retryState;
        }
      };

      if (visibleBeforeRead) {
        const priorPayload = lastGoodSnapshot.current?.page === snapshotPage
          ? lastGoodSnapshot.current.payload
          : undefined;
        showSnapshotData(visibleBeforeRead, priorPayload);
      }
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
          showSnapshotData(nextSnapshot, payload);
          if (snapshotRefreshUnavailable(payload)) {
            resetRetryOnSnapshotProgress(payload);
            const sectionErrors = snapshotSectionErrors(payload);
            const retryMessage = snapshotRetryMessage(payload, true);
            sectionErrors.snapshot = retryMessage;
            setErrors(sectionErrors);
            scheduleSnapshotRetry(retryMessage, true, payload);
            return;
          }
          retryAttempt.current = 0;
          snapshotRetryState.current = "";
          snapshotNextPollDelay.current = null;
          setProgress({ completed: 1, total: 1 });
          setLoading(false);
          setRefreshing(false);
          setLoadingMessage("正在汇总决策、账户和运行证据");
          setErrors(snapshotSectionErrors(payload));
          return;
        }

        if (snapshotIsRenderable(payload) && nextSnapshot) {
          lastGoodSnapshot.current = { page: snapshotPage, payload };
          showSnapshotData(nextSnapshot, payload, "degraded");
          resetRetryOnSnapshotProgress(payload);
          const sectionErrors = snapshotSectionErrors(payload);
          const retryMessage = snapshotRetryMessage(payload, true);
          // A healthy last-known-good page that is merely awaiting the worker
          // belongs in the status bar, not in the fatal-looking error strip.
          if (snapshotRefreshUnavailable(payload)) sectionErrors.snapshot = retryMessage;
          setErrors(sectionErrors);
          scheduleSnapshotRetry(retryMessage, true, payload);
          return;
        }

        // Keep content and metadata from the same server document. Reusing
        // priorGood here displayed old evidence under a new generation time.
        const fallbackData = priorGood || priorVisible;
        const fallbackPayload = lastGoodSnapshot.current?.page === snapshotPage
          ? lastGoodSnapshot.current.payload
          : undefined;
        if (fallbackData) showSnapshotData(fallbackData, fallbackPayload, "degraded", payload);
        const sectionErrors = snapshotSectionErrors(payload);
        const retryMessage = snapshotRetryMessage(payload, Boolean(fallbackData));
        sectionErrors.snapshot = retryMessage;
        setErrors(sectionErrors);
        scheduleSnapshotRetry(retryMessage, Boolean(fallbackData), payload);
      } catch (error: any) {
        if (activeController.signal.aborted || controller.current !== activeController) return;
        const hasVisibleData = Boolean(visibleBeforeRead);
        const message = error?.name === "AbortError"
          ? hasVisibleData
            ? "页面快照读取超时，继续展示最近一次可验证数据，正在等待下一个完整快照"
            : "页面快照读取超时，不能据此判断为无数据"
          : hasVisibleData
            ? "页面快照暂不可读，继续展示最近一次可验证数据，正在等待下一个完整快照"
            : "页面快照暂不可读，正在等待下一个完整版本";
        const fallbackPayload = lastGoodSnapshot.current?.page === snapshotPage
          ? lastGoodSnapshot.current.payload
          : undefined;
        const readFailurePayload: Json = {
          status: "unavailable",
          stale: true,
          refreshing: true,
          refresh_available: false,
          refresh_error_code: error?.name === "AbortError" ? "snapshot_read_timeout" : "snapshot_read_unavailable",
        };
        if (visibleBeforeRead) showSnapshotData(visibleBeforeRead, fallbackPayload, "degraded", readFailurePayload);
        setErrors({ snapshot: message });
        scheduleSnapshotRetry(message, hasVisibleData, readFailurePayload);
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
      : ["/operations", "/data", "/evidence"].includes(pathname) ? 2 : 3;
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
    let stopped = false;
    let pollTimer: number | null = null;
    const clearPoll = () => {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    };
    const scheduleNextPoll = () => {
      clearPoll();
      if (stopped || !snapshotPage || document.hidden) return;
      pollTimer = window.setTimeout(async () => {
        if (stopped || document.hidden) return;
        await refresh(false);
        if (!stopped && !document.hidden) scheduleNextPoll();
      }, snapshotNextPollDelay.current ?? snapshotPollInterval(pathname));
    };
    const onVisibilityChange = () => {
      if (document.hidden) {
        clearPoll();
        return;
      }
      // Resume immediately when the tab becomes visible again.
      void refresh(false).then(() => {
        if (!stopped && !document.hidden) scheduleNextPoll();
      });
    };
    void refresh(false).then(() => {
      if (!stopped && !document.hidden) scheduleNextPoll();
    });
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      clearPoll();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      controller.current?.abort();
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
    };
  }, [pathname, refresh, snapshotPage]);

  const cachedSnapshot = snapshotPage ? cachedRenderableSnapshot(snapshotPage) : null;
  const cachedSnapshotData = snapshotData(cachedSnapshot);
  const cachedSnapshotView = snapshotPage && cachedSnapshotData
    ? snapshotViewData(snapshotPage, cachedSnapshotData as ConsoleData, cachedSnapshot || undefined)
    : null;
  const snapshotRouteReady = Boolean(cachedSnapshotData)
    || (loadedRoute.pathname === pathname && loadedRoute.generation === activeRouteGeneration);
  const stateMatchesSnapshotRoute = loadedRoute.pathname === pathname
    && loadedRoute.generation === activeRouteGeneration;
  const visibleData = snapshotPage
    ? stateMatchesSnapshotRoute ? data : cachedSnapshotView || data
    : data;
  const visibleLoading = snapshotPage ? loading || !snapshotRouteReady : loading;
  const cachedSnapshotErrors = cachedSnapshot ? snapshotSectionErrors(cachedSnapshot) : {};
  const visibleErrors = snapshotPage
    ? stateMatchesSnapshotRoute
      ? { ...cachedSnapshotErrors, ...errors }
      : cachedSnapshotErrors
    : errors;

  const hasVisibleData = consoleDataIsRenderable(visibleData);

  return {
    data: visibleData,
    errors: visibleErrors,
    loading: visibleLoading,
    bootstrapping: visibleLoading && !hasVisibleData,
    hasVisibleData,
    loadingMessage,
    refreshing,
    progress,
    refresh: () => refresh(true),
  };
}

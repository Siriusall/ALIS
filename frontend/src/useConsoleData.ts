import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { endpoints, Json, readApi } from "./api";

export type ConsoleData = Partial<Record<keyof typeof endpoints, Json>>;

const routeKeys: Record<string, Array<keyof typeof endpoints>> = {
  "/overview": ["owner", "ledger"],
  "/decision": ["owner", "decision"],
  "/portfolio": ["owner", "ledger", "positions", "portfolio", "performance", "risk", "research"],
  "/evolution": ["factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/evolution/factors": ["factors", "researchAnalytics", "researchFactorAnalytics"],
  "/evolution/metrics": ["researchAnalytics", "researchTimeseries", "evolutionTasks"],
  "/evolution/waves": ["researchAnalytics", "factorResearch", "researchExpertHistory", "researchEnvironmentSimilarity", "researchArchive"],
  "/admin": ["health", "tasks", "dailyUpdateStatus", "watermarks", "ledgerAudit", "oos", "sealed"],
  "/admin/operations": ["health", "owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executions", "operations", "ledgerAudit", "oos", "sealed", "apiCatalog"],
  "/factors": ["owner", "strategy", "sealed", "factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/admin/models": ["modelOperations"],
  "/admin/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "tasks", "oos", "sealed"],
  "/admin/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "report", "backtests", "ledgerAudit"],
  "/research": ["researchContracts", "researchGovernance", "researchPositionState", "researchDataAssets", "analystConsensus", "theses", "industries", "industryAllocation", "themes", "events", "pools", "modelMarketLive"],
};

const cache = new Map<keyof typeof endpoints, { value: Json; readAt: number }>();
const inFlight = new Map<keyof typeof endpoints, Promise<Json>>();
const CACHE_TTL_MS = 60_000;
const ownerRoutes = [
  "/overview", "/decision", "/portfolio", "/evolution", "/evolution/factors",
  "/evolution/metrics", "/evolution/waves", "/research",
];
const adminRoutes = ["/admin", "/admin/operations", "/admin/data", "/admin/models", "/admin/evidence"];

async function loadEndpoint(key: keyof typeof endpoints, force = false): Promise<Json> {
  const cached = cache.get(key);
  if (!force && cached && Date.now() - cached.readAt < CACHE_TTL_MS) return cached.value;
  const pending = inFlight.get(key);
  if (pending) return pending;
  const request = readApi(endpoints[key]).then((value) => {
    cache.set(key, { value, readAt: Date.now() });
    return value;
  }).finally(() => {
    if (inFlight.get(key) === request) inFlight.delete(key);
  });
  inFlight.set(key, request);
  return request;
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
  const [data, setData] = useState<ConsoleData>(cachedData);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(keys.some((key) => !cache.has(key)));
  const [refreshing, setRefreshing] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const controller = useRef<AbortController | null>(null);

  const refresh = useCallback(async (force = true) => {
    const startedAt = Date.now();
    controller.current?.abort();
    controller.current = new AbortController();
    const hasRouteCache = keys.every((key) => cache.has(key));
    setLoading(!hasRouteCache);
    setRefreshing(force);
    const entries = keys;
    const activeController = controller.current;
    setProgress({ completed: 0, total: entries.length });
    setErrors({});
    let routeRevealed = hasRouteCache;
    const revealRoute = () => {
      if (routeRevealed || controller.current !== activeController) return;
      routeRevealed = true;
      setLoading(false);
    };
    const revealTimer = window.setTimeout(revealRoute, 1_500);
    const releaseRefreshTimer = window.setTimeout(() => {
      if (controller.current === activeController) setRefreshing(false);
    }, 600);
    const requests = entries.map(async (key) => {
      try {
        const value = await loadEndpoint(key, force);
        if (controller.current !== activeController) return;
        setData((current) => ({ ...current, [key]: value }));
        revealRoute();
      } catch (error: any) {
        if (activeController.signal.aborted || controller.current !== activeController) return;
        const message = error?.name === "AbortError" ? "读取超时" : String(error?.message || "读取失败");
        setErrors((current) => ({ ...current, [key]: message }));
      } finally {
        if (controller.current === activeController) {
          setProgress((current) => ({ ...current, completed: Math.min(current.total, current.completed + 1) }));
        }
      }
    });
    const minimumVisibleMs = 300;
    const remainingMs = Math.max(0, minimumVisibleMs - (Date.now() - startedAt));
    if (remainingMs) await new Promise((resolve) => window.setTimeout(resolve, remainingMs));
    await Promise.allSettled(requests);
    window.clearTimeout(revealTimer);
    window.clearTimeout(releaseRefreshTimer);
    if (controller.current === activeController) { setLoading(false); setRefreshing(false); }
  }, [keys]);

  useEffect(() => {
    refresh(false);
    return () => controller.current?.abort();
  }, [refresh]);

  useEffect(() => {
    if (loading) return;
    const routeGroup = pathname.startsWith("/admin") ? adminRoutes : ownerRoutes;
    const deferredGroup = pathname.startsWith("/admin") ? ownerRoutes : adminRoutes;
    const preload = async () => {
      await preloadConsoleRoutes(routeGroup.filter((path) => path !== pathname), 3);
      await new Promise((resolve) => window.setTimeout(resolve, 250));
      await preloadConsoleRoutes(deferredGroup, 2);
    };
    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: IdleRequestCallback, options?: IdleRequestOptions) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    if (idleWindow.requestIdleCallback) {
      const idleId = idleWindow.requestIdleCallback(() => { void preload(); }, { timeout: 2_000 });
      return () => idleWindow.cancelIdleCallback?.(idleId);
    }
    const timer = window.setTimeout(() => { void preload(); }, 500);
    return () => window.clearTimeout(timer);
  }, [loading, pathname]);

  return { data, errors, loading, refreshing, progress, refresh: () => refresh(true) };
}

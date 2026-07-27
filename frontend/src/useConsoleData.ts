import { useCallback, useEffect, useRef, useState } from "react";
import { endpoints, Json, readApi } from "./api";

export type ConsoleData = Partial<Record<keyof typeof endpoints, Json>>;

const routeKeys: Record<string, Array<keyof typeof endpoints>> = {
  "/overview": ["owner", "ledger"],
  "/decision": ["owner", "decision"],
  "/portfolio": ["owner", "ledger", "positions", "portfolio", "performance", "risk", "research"],
  "/evolution": ["owner", "strategy", "sealed", "factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/operations": ["owner", "tasks", "evolutionTasks", "evolutionSchedule", "quantTasks", "quantStatus", "quantRecent", "executions", "operations", "ledgerAudit", "oos", "sealed", "apiCatalog"],
  "/factors": ["owner", "strategy", "sealed", "factors", "factorStatus", "factorResearch", "factorLineage", "evolutionTasks"],
  "/models": ["models", "planModels", "modelWorkbench", "modelGovernance", "modelReports", "modelProduction", "modelController", "modelOutputContracts", "modelEvidenceBindings", "modelValidation", "modelMarketLive", "modelMarketHistory", "owner"],
  "/data": ["watermarks", "failedRequests", "dataOperations", "runtimeLogs", "dataObservability", "dataStreams", "dailyUpdateStatus", "backfillStatus", "backfillPlan", "oos", "sealed"],
  "/evidence": ["decisionAudit", "decisions", "reviews", "reviewLearning", "llmBudget", "llmProposals", "performanceReports", "ledgerDetail", "report", "backtests", "ledgerAudit"],
  "/research": ["researchContracts", "researchGovernance", "researchPositionState", "researchDataAssets", "analystConsensus", "theses", "industries", "industryAllocation", "themes", "events", "pools", "modelMarketLive"],
};

export function useConsoleData(pathname: string) {
  const [data, setData] = useState<ConsoleData>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const controller = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    const startedAt = Date.now();
    controller.current?.abort();
    controller.current = new AbortController();
    setLoading(true);
    const keys = routeKeys[pathname] || routeKeys["/overview"];
    const entries = keys.map((key) => [key, endpoints[key]] as [keyof typeof endpoints, string]);
    const activeController = controller.current;
    setProgress({ completed: 0, total: entries.length });
    setErrors((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !keys.includes(key as keyof typeof endpoints))));
    const requests = entries.map(async ([key, path]) => {
      try {
        const value = await readApi(path, activeController.signal);
        if (controller.current !== activeController) return;
        setData((current) => ({ ...current, [key]: value }));
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
    if (controller.current === activeController) setLoading(false);
  }, [pathname]);

  useEffect(() => {
    refresh();
    return () => controller.current?.abort();
  }, [refresh]);

  return { data, errors, loading, progress, refresh };
}

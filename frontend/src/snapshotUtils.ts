import type { Json } from "./api";

export type ConsoleSnapshotData = Record<string, Json | undefined> & {
  __snapshot?: Json;
};

const initialRequestLabels: Record<string, string> = {
  owner: "系统摘要",
  ledger: "账本摘要",
  tradingReady: "交易就绪检查",
  decision: "今日决策",
  researchDataAssets: "研究数据资产",
};

export function initialRequestLabel(key: string) {
  return initialRequestLabels[key] || key;
}

export function snapshotData(payload: unknown): ConsoleSnapshotData | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const data = (payload as Json).data;
  return data && typeof data === "object" && !Array.isArray(data)
    ? data as ConsoleSnapshotData
    : null;
}

export function snapshotHasData(payload: unknown) {
  const data = snapshotData(payload);
  return Boolean(data && Object.keys(data).length > 0);
}

export function snapshotStatus(payload: unknown) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "unavailable";
  return String((payload as Json).status || "unavailable").trim().toLowerCase();
}

export function snapshotIsHealthy(payload: unknown): payload is Json {
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

export function snapshotErrorCode(section: Json) {
  const error = section.error;
  const value = error && typeof error === "object" && !Array.isArray(error)
    ? error.code || error.kind || error.status
    : section.error_code || section.code;
  return String(value || "").trim().toLowerCase();
}

export function snapshotSectionMessage(key: string, section: Json) {
  const label = initialRequestLabel(key);
  const status = String(section.status || "error").trim().toLowerCase();
  const code = snapshotErrorCode(section);
  if (/timeout|deadline/.test(code)) return `${label} 查询超时，不能据此判断为无数据`;
  if (/connection|database|unavailable/.test(code)) return `${label} 暂不可读，正在等待下一个完整快照`;
  if (status === "degraded") return `${label} 仅返回部分已验证证据`;
  return `${label} 暂不可读，正在等待下一个完整快照`;
}

export function snapshotSectionErrors(payload: Json) {
  const sections = payload.sections;
  if (!sections || typeof sections !== "object" || Array.isArray(sections)) return {};
  const readableResearchSections = new Set([
    "researchGovernance",
    "theses",
    "industryAllocation",
    "modelMarketLive",
  ]);
  return Object.entries(sections).reduce<Record<string, string>>((result, [key, value]) => {
    const section = value && typeof value === "object" && !Array.isArray(value) ? value as Json : {};
    const status = String(section.status || "").trim().toLowerCase();
    // These research routes return structured business conclusions even when
    // one sub-check is blocked or historical evidence is partial.  Do not
    // label the whole section as unavailable; the section body shows its
    // exact governance/data state.
    if (payload.page === "research" && readableResearchSections.has(key)) return result;
    if (status === "error" || status === "degraded") {
      result[key] = snapshotSectionMessage(key, section);
    }
    return result;
  }, {});
}

export function snapshotRetryMessage(payload: Json, hasVisibleData: boolean) {
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
  if (snapshotStatus(payload) === "degraded") {
    return "新页面快照包含部分可用证据，正在等待完整版本";
  }
  return "页面快照尚未达到完整状态，正在等待新版本";
}

export function responseIsTerminal(value: unknown): value is Json {
  if (Array.isArray(value)) return true;
  if (!value || typeof value !== "object") return false;
  const payload = value as Json;
  return Object.keys(payload).length > 0;
}

export function responseIsDegraded(value: unknown): boolean {
  if (!responseIsTerminal(value) || Array.isArray(value)) return false;
  const payload = value as Json;
  return payload.data_available === false
    || payload.available === false
    || (payload.ok === false && payload.status === "degraded");
}

export function payloadIsComplete(key: string, value: unknown): value is Json {
  if (!responseIsTerminal(value)) return false;
  if (Array.isArray(value)) return true;
  const payload = value as Json;
  // A degraded/empty response is still a completed request. Render the
  // page with an explicit warning instead of retrying forever behind a
  // spinner while waiting for data that the backend has already ruled out.
  if (responseIsDegraded(payload)) return true;
  if (Object.keys(payload).length === 0) return false;
  if (key === "owner") {
    return Boolean(payload.summary || payload.questions || payload.account || payload.acceptance_progress);
  }
  if (key === "ledger") {
    return ["total_equity", "cash_balance", "position_market_value"].every(
      (field) => Object.prototype.hasOwnProperty.call(payload, field),
    );
  }
  if (key === "tradingReady") {
    return typeof payload.status === "string"
      && Boolean(payload.summary || payload.hard_blockers || payload.gates);
  }
  if (key === "decision") {
    return Boolean(
      payload.status || payload.summary || payload.actions || payload.rebalance || payload.items
        || payload.gate_decision || payload.latest_data_contract || payload.data_health
        || payload.market_overview,
    );
  }
  // Dynamic detail groups are complete once their child requests have settled.
  if (
    key === "researchStockEvidence"
    || key === "researchIndustryEvidence"
    || key === "evidenceVerification"
    || key === "decisionDetail"
  ) {
    return true;
  }
  return true;
}

export function consoleDataIsRenderable(data: ConsoleSnapshotData | null | undefined) {
  if (!data) return false;
  return Object.keys(data).some((key) => key !== "__snapshot");
}

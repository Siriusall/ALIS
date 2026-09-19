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

const unavailableSectionStatuses = new Set(["error", "unavailable", "warming_up"]);
const sectionEnvelopeKeys = new Set([
  "status", "ok", "data_available", "available", "component", "error",
  "retry_after_seconds", "reason", "message",
]);

function hasMeaningfulSectionValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value !== "object") return true;
  return Object.entries(value as Json).some(([key, item]) => {
    if (sectionEnvelopeKeys.has(key)) return false;
    return hasMeaningfulSectionValue(item);
  });
}

function snapshotHasReadableSection(payload: Json) {
  const data = snapshotData(payload);
  if (!data) return false;
  return Object.entries(data).some(([, section]) => {
    if (!section || typeof section !== "object" || Array.isArray(section)) {
      return hasMeaningfulSectionValue(section);
    }
    const status = String((section as Json).status || "").trim().toLowerCase();
    return !unavailableSectionStatuses.has(status) || hasMeaningfulSectionValue(section);
  });
}

/**
 * A snapshot can be useful before it is fresh.  In particular, a durable
 * last-known-good document remains the only evidence the browser can show
 * while the control worker replaces an invalidated generation.  Do not make
 * stale/refreshing part of this predicate: those fields describe freshness,
 * not whether the document can render.
 */
export function snapshotIsRenderable(payload: unknown): payload is Json {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const status = snapshotStatus(payload);
  return status !== "warming_up"
    && status !== "unavailable"
    && snapshotHasData(payload)
    && snapshotHasReadableSection(payload as Json);
}

export function snapshotIsHealthy(payload: unknown): payload is Json {
  if (!snapshotIsRenderable(payload)) return false;
  const snapshot = payload as Json;
  // A completed document may be degraded because one named section is
  // unavailable. Its other sections are still authoritative and must render;
  // freshness is intentionally a separate condition from renderability.
  // An error document may retain several readable sections. It belongs on
  // screen as degraded evidence, but it must stay on the retry path rather
  // than being promoted to the next healthy last-known-good generation.
  return snapshotStatus(snapshot) !== "error"
    && !Boolean(snapshot.stale)
    && !Boolean(snapshot.refreshing)
    && snapshotHasData(snapshot);
}

/**
 * Snapshot builders can return an error document containing several usable
 * sections.  Surface that as a partial page rather than a total front-end
 * failure.  `document_status` retains the raw server result for diagnostics.
 */
export function snapshotPresentationStatus(payload: unknown) {
  const status = snapshotStatus(payload);
  return status === "error" && snapshotIsRenderable(payload) ? "degraded" : status;
}

/**
 * The API may expose worker availability explicitly.  Accept the compact
 * names used by older and newer releases so a browser deployed independently
 * of the API still stops aggressive retrying when no builder can run.
 */
export function snapshotRefreshUnavailable(payload: unknown) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const snapshot = payload as Json;
  if (snapshotStatus(snapshot) === "unavailable") return true;
  if (snapshot.refresh_available === false || snapshot.worker_available === false) return true;
  const workerStatus = String(
    snapshot.snapshot_worker_status
      || snapshot.refresh_worker_status
      || snapshot.worker_status
      || "",
  ).trim().toLowerCase();
  if (/^(inactive|disabled|offline|unavailable|failed|error|stopped)$/.test(workerStatus)) return true;
  if (/(?:snapshot|refresh|worker).*(?:unavailable|inactive|disabled|offline|failed|stopped)/.test(workerStatus)) return true;
  const error = snapshot.error && typeof snapshot.error === "object" && !Array.isArray(snapshot.error)
    ? snapshot.error as Json
    : {};
  const code = String(
    snapshot.refresh_error_code
      || snapshot.error_code
      || error.code
      || error.kind
      || "",
  ).trim().toLowerCase();
  return /(?:snapshot|refresh|worker).*(?:unavailable|inactive|disabled|offline|failed|stopped)/.test(code)
    || code === "snapshot_build_unavailable";
}

function snapshotRetryHintMs(payload: unknown) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return 0;
  const snapshot = payload as Json;
  const seconds = Number(
    snapshot.refresh_retry_after_seconds
      || snapshot.retry_after_seconds
      || snapshot.error?.retry_after_seconds
      || 0,
  );
  return Number.isFinite(seconds) && seconds > 0
    ? Math.min(600_000, Math.max(1_000, Math.round(seconds * 1_000)))
    : 0;
}

/**
 * Keep an active rebuild responsive, but do not turn a stopped worker or a
 * network outage into a request every five seconds from each open console.
 */
export function snapshotRetryDelay(
  payload: unknown,
  attempt: number,
  regularPollMs: number,
) {
  const index = Math.max(0, Math.floor(attempt));
  const retryHintMs = snapshotRetryHintMs(payload);
  if (snapshotRefreshUnavailable(payload)) {
    const delays = [30_000, 60_000, 120_000, 300_000, 600_000];
    return Math.max(retryHintMs, delays[Math.min(index, delays.length - 1)]);
  }
  if (snapshotStatus(payload) === "warming_up" || (payload as Json | null)?.stale || (payload as Json | null)?.refreshing) {
    const delays = [5_000, 10_000, 20_000, 45_000, 90_000, 180_000, 300_000];
    return Math.max(retryHintMs, delays[Math.min(index, delays.length - 1)]);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    const delays = [2_000, 5_000, 15_000, 30_000, 60_000, 120_000, 300_000];
    return delays[Math.min(index, delays.length - 1)];
  }
  if (snapshotStatus(payload) === "error") {
    const delays = [15_000, 30_000, 60_000, 120_000, 300_000];
    return Math.max(retryHintMs, delays[Math.min(index, delays.length - 1)]);
  }
  return regularPollMs;
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
  if (snapshotRefreshUnavailable(payload)) {
    return hasVisibleData
      ? "后台页面快照刷新器暂不可用，继续展示最近一次已验证页面快照，正在等待下一个完整快照"
      : "后台页面快照刷新器暂不可用，正在等待服务恢复后生成首个页面快照";
  }
  if (snapshotStatus(payload) === "warming_up") {
    return hasVisibleData
      ? "服务端页面快照正在生成，继续展示最近一次可验证数据，正在等待下一个完整快照"
      : "服务端页面快照正在生成，尚无可验证的页面数据";
  }
  if (payload.stale) {
    return hasVisibleData
      ? "服务端页面快照已失效，继续展示最近一次可验证数据，正在等待下一个完整快照"
      : "服务端页面快照已失效，正在等待首个可验证版本";
  }
  if (payload.refreshing) return "服务端正在生成新的页面快照";
  if (snapshotStatus(payload) === "error") {
    return hasVisibleData
      ? "本次页面快照生成未完成，继续展示最近一次可验证数据，正在等待下一个完整快照"
      : "本次页面快照生成未完成，正在等待下一个完整版本";
  }
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

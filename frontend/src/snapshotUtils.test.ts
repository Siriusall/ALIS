import { describe, expect, it } from "vitest";
import {
  consoleDataIsRenderable,
  payloadIsComplete,
  snapshotIsHealthy,
  snapshotIsRenderable,
  snapshotPresentationStatus,
  snapshotRefreshUnavailable,
  snapshotRetryDelay,
  snapshotRetryMessage,
  snapshotSectionErrors,
  snapshotStatus,
} from "./snapshotUtils";

describe("snapshotIsHealthy", () => {
  it("accepts a completed document with data", () => {
    expect(snapshotIsHealthy({
      status: "ready",
      stale: false,
      refreshing: false,
      data: { owner: { summary: "ok" } },
    })).toBe(true);
  });

  it("rejects warming, stale, or empty documents as fresh", () => {
    expect(snapshotIsHealthy({ status: "warming_up", data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", stale: true, data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", refreshing: true, data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", data: {} })).toBe(false);
  });
});

describe("snapshot renderability and recovery", () => {
  it("keeps a stale last-known-good document renderable", () => {
    const payload = {
      status: "ready",
      stale: true,
      refreshing: true,
      data: { owner: { summary: "last known good" } },
    };

    expect(snapshotIsRenderable(payload)).toBe(true);
    expect(snapshotIsHealthy(payload)).toBe(false);
  });

  it("presents partial error documents as degraded rather than a page failure", () => {
    const payload = {
      status: "error",
      data: { owner: { summary: "still readable" }, broken: { status: "unavailable" } },
    };

    expect(snapshotIsRenderable(payload)).toBe(true);
    expect(snapshotIsHealthy(payload)).toBe(false);
    expect(snapshotPresentationStatus(payload)).toBe("degraded");
  });

  it("does not render cold warming or unavailable envelopes", () => {
    expect(snapshotIsRenderable({ status: "warming_up", data: {} })).toBe(false);
    expect(snapshotIsRenderable({ status: "unavailable", data: { snapshot: { status: "unavailable" } } })).toBe(false);
    expect(snapshotIsRenderable({
      status: "error",
      data: { snapshot: { status: "unavailable", error: { code: "snapshot_build_unavailable" } } },
    })).toBe(false);
  });

  it("recognizes worker outage fields across compatible API releases", () => {
    expect(snapshotRefreshUnavailable({ status: "ready", refresh_available: false })).toBe(true);
    expect(snapshotRefreshUnavailable({ status: "ready", snapshot_worker_status: "inactive" })).toBe(true);
    expect(snapshotRefreshUnavailable({
      status: "ready",
      error: { code: "snapshot_worker_unavailable" },
    })).toBe(true);
    expect(snapshotRefreshUnavailable({ status: "ready", worker_status: "running" })).toBe(false);
  });

  it("backs off a stopped worker while keeping active builds responsive", () => {
    expect(snapshotRetryDelay({ status: "ready", stale: true, refreshing: true }, 0, 600_000)).toBe(5_000);
    expect(snapshotRetryDelay({ status: "ready", stale: true, refreshing: true }, 6, 600_000)).toBe(300_000);
    expect(snapshotRetryDelay({
      status: "ready", stale: true, refreshing: true, refresh_retry_after_seconds: 45,
    }, 0, 600_000)).toBe(45_000);
    expect(snapshotRetryDelay({ status: "ready", refresh_available: false }, 0, 600_000)).toBe(30_000);
    expect(snapshotRetryDelay({ status: "ready", refresh_available: false }, 9, 600_000)).toBe(600_000);
  });
});

describe("snapshotSectionErrors", () => {
  it("keeps research governance sections from becoming page-level errors", () => {
    const errors = snapshotSectionErrors({
      page: "research",
      sections: {
        researchGovernance: { status: "degraded" },
        theses: { status: "error" },
        pools: { status: "error", error: { code: "request_timeout" } },
      },
    });
    expect(errors.researchGovernance).toBeUndefined();
    expect(errors.theses).toBeUndefined();
    expect(errors.pools).toMatch(/查询超时/);
  });
});

describe("snapshotRetryMessage", () => {
  it("distinguishes visible stale data from empty warmup", () => {
    expect(snapshotRetryMessage({ status: "warming_up" }, true)).toMatch(/继续展示.*完整快照/);
    expect(snapshotRetryMessage({ status: "warming_up" }, false)).toMatch(/尚无可验证/);
    expect(snapshotRetryMessage({ status: "ready", stale: true }, true)).toMatch(/已失效.*完整快照/);
  });

  it("explains that a worker outage preserves the last verified document", () => {
    expect(snapshotRetryMessage({ status: "ready", refresh_available: false }, true)).toMatch(/刷新器暂不可用.*完整快照/);
  });

  it("keeps a readable error document on the recoverable retry path", () => {
    expect(snapshotRetryMessage({ status: "error" }, true)).toMatch(/继续展示.*完整快照/);
  });
});

describe("payloadIsComplete", () => {
  it("treats degraded terminal responses as complete", () => {
    expect(payloadIsComplete("owner", {
      ok: false,
      status: "degraded",
      data_available: false,
      component: "owner",
    })).toBe(true);
  });

  it("requires ledger equity fields", () => {
    expect(payloadIsComplete("ledger", { cash_balance: 1 })).toBe(false);
    expect(payloadIsComplete("ledger", {
      total_equity: 1,
      cash_balance: 1,
      position_market_value: 0,
    })).toBe(true);
  });
});

describe("consoleDataIsRenderable", () => {
  it("ignores snapshot metadata alone", () => {
    expect(consoleDataIsRenderable({ __snapshot: { page: "overview" } })).toBe(false);
    expect(consoleDataIsRenderable({ owner: { summary: "ok" }, __snapshot: { page: "overview" } })).toBe(true);
  });

  it("reports snapshot status safely", () => {
    expect(snapshotStatus(null)).toBe("unavailable");
    expect(snapshotStatus({ status: "Degraded" })).toBe("degraded");
  });
});

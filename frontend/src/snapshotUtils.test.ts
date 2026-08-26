import { describe, expect, it } from "vitest";
import {
  consoleDataIsRenderable,
  payloadIsComplete,
  snapshotIsHealthy,
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

  it("rejects warming, stale, or empty documents", () => {
    expect(snapshotIsHealthy({ status: "warming_up", data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", stale: true, data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", refreshing: true, data: { owner: {} } })).toBe(false);
    expect(snapshotIsHealthy({ status: "ready", data: {} })).toBe(false);
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
    expect(snapshotRetryMessage({ status: "warming_up" }, true)).toMatch(/继续展示/);
    expect(snapshotRetryMessage({ status: "warming_up" }, false)).toMatch(/尚无可验证/);
    expect(snapshotRetryMessage({ status: "ready", stale: true }, true)).toMatch(/已失效/);
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

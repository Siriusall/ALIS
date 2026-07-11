import { Router } from "express";
import { getDb, getLatestDataRunId, getLatestOrRunningRun, markLatestRunningRunStopped } from "../db.js";
import { getCrawlLogs, isCrawlRunning, stopCrawl, triggerCrawl } from "../services/crawlerRunner.js";
import { exportFactorsToCsv } from "../services/csvExporter.js";

const router = Router();

router.get("/status", (_req, res) => {
  let row = getLatestOrRunningRun();
  const processRunning = isCrawlRunning();
  if (!processRunning && row?.status === "running") {
    markLatestRunningRunStopped("crawler process is not running");
    row = getLatestOrRunningRun();
  }
  res.json({
    running: processRunning,
    processRunning,
    latest: row || null,
  });
});

router.get("/coverage", (req, res) => {
  const runId = req.query.runId ? Number(req.query.runId) : getLatestDataRunId();
  if (!runId) {
    return res.json({ runId: null, total: 0, fields: {} });
  }

  const fields = [
    "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
    "gross_margin", "net_margin", "roe", "roic",
    "ocf_to_profit", "free_cashflow",
    "debt_ratio", "interest_debt", "cash_coverage",
    "dividend_yield", "payout_ratio", "buyback_amount",
    "profit_volatility_5y", "cyclicality",
    "pe", "pb", "ps", "valuation_percentile",
    "fund_holding", "northbound", "etf_holding", "research_count",
  ];
  const db = getDb();
  try {
    const total = safeGetCount(db, "stock_factors", "crawl_run_id = ?", [runId]);
    const result = {};
    for (const field of fields) {
      const nonNull = safeGetCount(db, "stock_factors", `crawl_run_id = ? AND ${field} IS NOT NULL`, [runId]);
      result[field] = {
        nonNull,
        coverage: total ? Number((nonNull / total).toFixed(4)) : 0,
      };
    }
    res.json({ runId, total, fields: result });
  } finally {
    db.close();
  }
});

router.get("/errors", (req, res) => {
  const latest = getLatestOrRunningRun();
  const runId = req.query.runId ? Number(req.query.runId) : latest?.id;
  if (!runId) {
    return res.json({ runId: null, total: 0, items: [] });
  }
  const limit = Math.min(500, Math.max(1, Number(req.query.limit || 100)));
  const db = getDb();
  try {
    const items = safeGetRows(
      db,
      `
      SELECT code, market, error, failed_at
      FROM crawl_errors
      WHERE crawl_run_id = ?
      ORDER BY id DESC
      LIMIT ?
      `,
      [runId, limit]
    );
    res.json({ runId, total: items.length, items });
  } finally {
    db.close();
  }
});

router.get("/logs", (req, res) => {
  const limit = Math.min(1000, Math.max(1, Number(req.query.limit || 300)));
  const items = getCrawlLogs({ limit });
  res.json({ total: items.length, items });
});

router.post("/trigger", async (_req, res) => {
  try {
    if (isCrawlRunning()) {
      return res.status(409).json({ error: "Crawl already running" });
    }

    let latest = getLatestOrRunningRun();
    if (latest?.status === "running") {
      markLatestRunningRunStopped("crawler process is not running");
      latest = getLatestOrRunningRun();
    }

    // Find the latest run that actually has data — skip empty failed/stopped runs
    let resumeRunId;
    if (latest && latest.status !== "completed" && (latest.success || 0) > 0) {
      resumeRunId = latest.id;
    } else if (latest && (latest.success || 0) === 0) {
      // Latest run has no data — look back for the last run with data
      const { getDb } = await import("../db.js");
      const db = getDb();
      try {
        const row = db.prepare(
          "SELECT id FROM crawl_runs WHERE success > 0 ORDER BY id DESC LIMIT 1"
        ).get();
        resumeRunId = row?.id ?? undefined;
      } finally {
        db.close();
      }
    }
    const job = await triggerCrawl({
      resumeRunId,
    });
    res.status(202).json({
      message: resumeRunId ? "Crawl resumed" : "Crawl started",
      pid: job.pid,
      running: true,
      processRunning: true,
      runId: resumeRunId ?? null,
    });
    job.done?.catch((err) => {
      console.error("Crawl failed:", err.message);
    });
    if (job.done) {
      job.done.then(() => {
        exportFactorsToCsv().catch((err) => console.error("CSV export failed:", err.message));
      });
    }
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
});

router.post("/stop", (_req, res) => {
  if (!isCrawlRunning()) {
    return res.status(409).json({ error: "No crawl is running" });
  }
  stopCrawl();
  const runId = markLatestRunningRunStopped();
  res.json({ message: "Stop signal sent", runId });
});

export default router;

function safeGetCount(db, table, whereSql, params) {
  try {
    return db.prepare(`SELECT COUNT(*) AS cnt FROM ${table} WHERE ${whereSql}`).get(...params).cnt;
  } catch {
    return 0;
  }
}

function safeGetRows(db, sql, params) {
  try {
    return db.prepare(sql).all(...params);
  } catch {
    return [];
  }
}

import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import { getDb, getWritableDb } from "../db.js";
import { ALL_FIELDS } from "./verifier.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CRAWLER_DIR = path.resolve(__dirname, "../../../crawler");
const VENV_PYTHON = path.join(CRAWLER_DIR, ".venv/bin/python3");

const LOG_BUFFER = [];
const MAX_LOG_LINES = 1000;

function appendLog(level, text) {
  const lines = String(text).split(/\r?\n/).map(l => l.trimEnd()).filter(Boolean);
  const timestamp = new Date().toISOString();
  for (const line of lines) {
    LOG_BUFFER.push({ timestamp, level, message: line });
  }
  if (LOG_BUFFER.length > MAX_LOG_LINES) {
    LOG_BUFFER.splice(0, LOG_BUFFER.length - MAX_LOG_LINES);
  }
}

export function getVerifyLogs({ limit = 300 } = {}) {
  return LOG_BUFFER.slice(-limit);
}

/**
 * Scrape a single stock's factors using the Python crawler's `single` command.
 * Returns parsed factor object or null on failure.
 */
function scrapeSingleStock(code) {
  return new Promise((resolve) => {
    if (!VENV_PYTHON) {
      appendLog("error", "No Python venv found for crawler");
      resolve(null);
      return;
    }

    const args = ["main.py", "single", code, "--summary"];
    const child = spawn(VENV_PYTHON, args, {
      cwd: CRAWLER_DIR,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", NODE_OPTIONS: "" },
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (buf) => { stdout += buf.toString("utf8"); });
    child.stderr.on("data", (buf) => { stderr += buf.toString("utf8"); });

    child.on("close", (code) => {
      if (code !== 0) {
        appendLog("error", `scrape ${code} failed: ${stderr.slice(0, 200)}`);
        resolve(null);
        return;
      }
      try {
        const data = JSON.parse(stdout.trim());
        resolve(data);
      } catch {
        appendLog("error", `scrape ${code} parse error: ${stdout.slice(0, 200)}`);
        resolve(null);
      }
    });

    child.on("error", (err) => {
      appendLog("error", `scrape ${code} spawn error: ${err.message}`);
      resolve(null);
    });
  });
}

function calcDeviation(dbVal, realVal) {
  if (dbVal === null || dbVal === undefined) return realVal !== null ? 100 : 0;
  if (realVal === null || realVal === undefined) return 0;
  if (dbVal === 0) return realVal !== 0 ? 100 : 0;
  return Math.abs((dbVal - realVal) / Math.abs(realVal)) * 100;
}

function isCorrect(deviation) {
  return deviation < 5;
}

/**
 * Compare sampled stocks against real web data.
 * Updates verify_results with correct/wrong status.
 */
export async function compareSamples(runId) {
  const db = getDb();
  let stocks;
  try {
    stocks = db.prepare(
      "SELECT DISTINCT code FROM verify_results WHERE verify_run_id = ? AND status = 'pending'"
    ).all(runId);
  } finally {
    db.close();
  }

  if (stocks.length === 0) {
    appendLog("info", "No pending samples to compare");
    return;
  }

  appendLog("info", `Comparing ${stocks.length} stocks against web data...`);

  for (let i = 0; i < stocks.length; i++) {
    const stock = stocks[i];
    appendLog("info", `[${i + 1}/${stocks.length}] Comparing ${stock.code}...`);

    const realData = await scrapeSingleStock(stock.code);
    if (!realData) {
      appendLog("warn", `${stock.code}: scrape failed, skipping`);
      continue;
    }

    const writable = getWritableDb();
    try {
      const results = writable.prepare(
        "SELECT field, db_value FROM verify_results WHERE verify_run_id = ? AND code = ?"
      ).all(runId, stock.code);

      for (const row of results) {
        const realVal = realData[row.field] ?? null;
        const deviation = calcDeviation(row.db_value, realVal);
        const status = isCorrect(deviation) ? "correct" : "wrong";

        writable.prepare(
          "UPDATE verify_results SET real_value = ?, deviation = ?, status = ? WHERE verify_run_id = ? AND code = ? AND field = ?"
        ).run(realVal, deviation, status, runId, stock.code, row.field);
      }
    } finally {
      writable.close();
    }
  }

  appendLog("info", "Comparison complete");
}

/**
 * Get field accuracy from a verify run.
 * Returns { field, accuracy }[] sorted by accuracy ascending.
 */
function getFieldAccuracy(runId) {
  const db = getDb();
  try {
    const fields = [];
    for (const field of ALL_FIELDS) {
      const stats = db.prepare(`
        SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'correct' THEN 1 ELSE 0 END) AS correct
        FROM verify_results WHERE verify_run_id = ? AND field = ?
      `).get(runId, field);
      const total = stats.total || 0;
      const correct = stats.correct || 0;
      if (total > 0) {
        fields.push({ field, accuracy: Math.round((correct / total) * 100) });
      }
    }
    return fields.sort((a, b) => a.accuracy - b.accuracy);
  } finally {
    db.close();
  }
}

/**
 * Correct a specific field for all stocks in the DB.
 * Scrapes each stock and updates the factor value.
 */
export async function correctField(runId, field) {
  const db = getDb();
  let stocks;
  try {
    const crawlRunId = db.prepare(
      `SELECT cr.id FROM crawl_runs cr
       WHERE EXISTS (SELECT 1 FROM stock_factors sf WHERE sf.crawl_run_id = cr.id)
       ORDER BY cr.id DESC LIMIT 1`
    ).get();
    if (!crawlRunId) { appendLog("error", "No crawl data found"); return 0; }

    stocks = db.prepare(
      "SELECT code FROM stock_factors WHERE crawl_run_id = ? ORDER BY code ASC"
    ).all(crawlRunId.id);
  } finally {
    db.close();
  }

  appendLog("info", `Correcting field "${field}" for ${stocks.length} stocks...`);
  let corrected = 0;

  for (let i = 0; i < stocks.length; i++) {
    const stock = stocks[i];
    if (i % 50 === 0) appendLog("info", `  [${i}/${stocks.length}] correcting ${field}...`);

    const realData = await scrapeSingleStock(stock.code);
    if (!realData || realData[field] === null || realData[field] === undefined) continue;

    const writable = getWritableDb();
    try {
      const crawlRunId = writable.prepare(
        `SELECT cr.id FROM crawl_runs cr
         WHERE EXISTS (SELECT 1 FROM stock_factors sf WHERE sf.crawl_run_id = cr.id)
         ORDER BY cr.id DESC LIMIT 1`
      ).get();
      if (!crawlRunId) continue;

      writable.prepare(
        `UPDATE stock_factors SET ${field} = ? WHERE crawl_run_id = ? AND code = ?`
      ).run(realData[field], crawlRunId.id, stock.code);

      // Also update verify_results if this stock was sampled
      if (runId) {
        writable.prepare(
          "UPDATE verify_results SET real_value = ?, status = 'corrected', corrected_at = ? WHERE verify_run_id = ? AND code = ? AND field = ?"
        ).run(realData[field], new Date().toISOString(), runId, stock.code, field);
      }

      corrected++;
    } finally {
      writable.close();
    }
  }

  appendLog("info", `Corrected ${corrected}/${stocks.length} for "${field}"`);
  return corrected;
}

/**
 * Full verify pipeline: sample → compare → auto-correct low-accuracy fields → re-check
 */
export async function startVerify({ sampleSize = 30 } = {}) {
  const { runVerify, getVerifyRunStatus } = await import("./verifier.js");

  // Step 1: Sample
  appendLog("info", "Starting verify pipeline...");
  const sample = await runVerify({ sampleSize });
  const runId = sample.runId;
  appendLog("info", `Sample complete: ${sample.total} stocks`);

  // Step 2: Compare against web data
  await compareSamples(runId);

  // Step 3: Check accuracy and auto-correct
  const accuracy = getFieldAccuracy(runId);
  const lowFields = accuracy.filter(f => f.accuracy < 80);

  if (lowFields.length === 0) {
    appendLog("info", "All fields have acceptable accuracy (>= 80%)");
    return { runId, status: "completed", lowFields: [] };
  }

  appendLog("info", `Low accuracy fields: ${lowFields.map(f => `${f.field}=${f.accuracy}%`).join(", ")}`);

  // Step 4: Correct low-accuracy fields (up to 3 rounds)
  for (let round = 1; round <= 3; round++) {
    const currentAccuracy = getFieldAccuracy(runId);
    const stillLow = currentAccuracy.filter(f => f.accuracy < 80);

    if (stillLow.length === 0) {
      appendLog("info", `All fields达标 after round ${round}`);
      break;
    }

    appendLog("info", `=== Correction round ${round} ===`);
    for (const f of stillLow) {
      appendLog("info", `Correcting field: ${f.field} (accuracy: ${f.accuracy}%)`);
      await correctField(runId, f.field);
    }

    // Re-check after correction
    appendLog("info", "Re-checking accuracy...");
    await compareSamples(runId);
  }

  const finalAccuracy = getFieldAccuracy(runId);
  appendLog("info", `Verify pipeline complete. Final accuracy: ${JSON.stringify(finalAccuracy)}`);

  return { runId, status: "completed", lowFields: finalAccuracy.filter(f => f.accuracy < 80) };
}

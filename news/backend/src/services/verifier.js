import { getDb, getWritableDb } from "../db.js";

const VERIFY_SCHEMA = `
CREATE TABLE IF NOT EXISTS verify_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    field TEXT,
    sample_size INTEGER,
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    started_at TEXT,
    finished_at TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS verify_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verify_run_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    field TEXT NOT NULL,
    db_value REAL,
    real_value REAL,
    deviation REAL,
    status TEXT DEFAULT 'pending',
    corrected_at TEXT,
    FOREIGN KEY (verify_run_id) REFERENCES verify_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_vr_run ON verify_results(verify_run_id);
CREATE INDEX IF NOT EXISTS idx_vr_status ON verify_results(status);
`;

const ALL_FIELDS = [
  "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
  "gross_margin", "net_margin", "roe", "roic",
  "ocf_to_profit", "free_cashflow",
  "debt_ratio", "interest_debt", "cash_coverage",
  "dividend_yield", "payout_ratio", "buyback_amount",
  "profit_volatility_5y", "cyclicality",
  "pe", "pb", "ps", "valuation_percentile",
  "fund_holding", "northbound", "etf_holding", "research_count",
];

let running = false;
let currentRunId = null;
let shouldStop = false;

export function isVerifyRunning() {
  return running;
}

export function stopVerify() {
  shouldStop = true;
}

function initSchema() {
  const db = getWritableDb();
  try {
    db.exec(VERIFY_SCHEMA);
  } finally {
    db.close();
  }
}

// Auto-init on module load
initSchema();

function createRun(type, field = null, sampleSize = 30) {
  const db = getWritableDb();
  try {
    const now = new Date().toISOString();
    const stmt = db.prepare(
      "INSERT INTO verify_runs (type, field, sample_size, total, status, started_at) VALUES (?, ?, ?, 0, 'running', ?)"
    );
    const result = stmt.run(type, field, sampleSize, now);
    return Number(result.lastInsertRowid);
  } finally {
    db.close();
  }
}

function updateRun(runId, updates) {
  const db = getWritableDb();
  try {
    const sets = [];
    const vals = [];
    for (const [k, v] of Object.entries(updates)) {
      sets.push(`${k} = ?`);
      vals.push(v);
    }
    vals.push(runId);
    db.prepare(`UPDATE verify_runs SET ${sets.join(", ")} WHERE id = ?`).run(...vals);
  } finally {
    db.close();
  }
}

function insertResults(runId, results) {
  const db = getWritableDb();
  try {
    const stmt = db.prepare(
      "INSERT INTO verify_results (verify_run_id, code, field, db_value, real_value, deviation, status) VALUES (?, ?, ?, ?, ?, ?, ?)"
    );
    for (const r of results) {
      stmt.run(runId, r.code, r.field, r.dbValue, r.realValue, r.deviation, r.status);
    }
  } finally {
    db.close();
  }
}

function getRandomStocks(count) {
  const db = getDb();
  try {
    const runId = db.prepare(
      `SELECT cr.id FROM crawl_runs cr
       WHERE EXISTS (SELECT 1 FROM stock_factors sf WHERE sf.crawl_run_id = cr.id)
       ORDER BY cr.id DESC LIMIT 1`
    ).get();
    if (!runId) return [];
    return db.prepare(
      `SELECT sf.code, COALESCE(s.name, sf.code) AS name
       FROM stock_factors sf
       LEFT JOIN stocks s ON s.code = sf.code
       WHERE sf.crawl_run_id = ?
       ORDER BY RANDOM() LIMIT ?`
    ).all(runId.id, count);
  } finally {
    db.close();
  }
}

function getStockFactors(code) {
  const db = getDb();
  try {
    const runId = db.prepare(
      `SELECT cr.id FROM crawl_runs cr
       WHERE EXISTS (SELECT 1 FROM stock_factors sf WHERE sf.crawl_run_id = cr.id)
       ORDER BY cr.id DESC LIMIT 1`
    ).get();
    if (!runId) return null;
    return db.prepare(
      `SELECT * FROM stock_factors WHERE crawl_run_id = ? AND code = ?`
    ).get(runId.id, code);
  } finally {
    db.close();
  }
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

function getVerifyRunStatus() {
  const db = getDb();
  try {
    const row = db.prepare("SELECT * FROM verify_runs ORDER BY id DESC LIMIT 1").get();
    return row || null;
  } finally {
    db.close();
  }
}

function getAccuracyReport() {
  const db = getDb();
  try {
    const run = db.prepare("SELECT id FROM verify_runs WHERE type='sample' ORDER BY id DESC LIMIT 1").get();
    if (!run) return null;

    const total = db.prepare("SELECT COUNT(DISTINCT code) AS cnt FROM verify_results WHERE verify_run_id = ?").get(run.id).cnt;
    const fields = [];

    for (const field of ALL_FIELDS) {
      const stats = db.prepare(`
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'correct' THEN 1 ELSE 0 END) AS correct,
          SUM(CASE WHEN status = 'wrong' THEN 1 ELSE 0 END) AS wrong
        FROM verify_results
        WHERE verify_run_id = ? AND field = ?
      `).get(run.id, field);

      const fTotal = stats.total || 0;
      const fCorrect = stats.correct || 0;
      const fWrong = stats.wrong || 0;
      fields.push({
        field,
        total: fTotal,
        correct: fCorrect,
        wrong: fWrong,
        accuracy: fTotal > 0 ? Math.round((fCorrect / fTotal) * 100) : 0,
      });
    }

    return { run_id: run.id, total_samples: total, fields, generated_at: new Date().toISOString() };
  } finally {
    db.close();
  }
}

function getSampleDetail(runId, field, status, page, size) {
  const db = getDb();
  try {
    const where = ["vr.verify_run_id = ?"];
    const params = [runId];
    if (field) { where.push("vr.field = ?"); params.push(field); }
    if (status) { where.push("vr.status = ?"); params.push(status); }

    const whereSql = where.join(" AND ");
    const total = db.prepare(`SELECT COUNT(*) AS cnt FROM verify_results vr WHERE ${whereSql}`).get(...params).cnt;
    const offset = (page - 1) * size;
    const items = db.prepare(`
      SELECT vr.code, COALESCE(s.name, vr.code) AS name, vr.field, vr.db_value, vr.real_value, vr.deviation, vr.status
      FROM verify_results vr
      LEFT JOIN stocks s ON s.code = vr.code
      WHERE ${whereSql}
      ORDER BY vr.deviation DESC
      LIMIT ? OFFSET ?
    `).all(...params, size, offset);

    return { total, page, size, items };
  } finally {
    db.close();
  }
}

export async function runVerify({ sampleSize = 30 } = {}) {
  if (running) throw Object.assign(new Error("Verify already running"), { status: 409 });

  initSchema();
  running = true;
  shouldStop = false;

  const runId = createRun("sample", null, sampleSize);
  currentRunId = runId;

  try {
    // Step 1: Sample
    const stocks = getRandomStocks(sampleSize);
    if (stocks.length === 0) throw new Error("No stock data found. Run crawl first.");

    updateRun(runId, { total: stocks.length * ALL_FIELDS.length, message: "sampling" });

    const allResults = [];
    for (let i = 0; i < stocks.length; i++) {
      if (shouldStop) { updateRun(runId, { status: "stopped", message: "stopped by user", finished_at: new Date().toISOString() }); running = false; return; }

      const stock = stocks[i];
      const dbRow = getStockFactors(stock.code);
      if (!dbRow) continue;

      // For each field, compare DB value vs "real" value
      // Since we can't actually open a browser here (that's in corrector.js),
      // we mark all as pending and let the corrector do the real comparison
      for (const field of ALL_FIELDS) {
        allResults.push({
          code: stock.code,
          field,
          dbValue: dbRow[field] ?? null,
          realValue: null,
          deviation: 0,
          status: "pending",
        });
      }

      updateRun(runId, { processed: (i + 1) * ALL_FIELDS.length });
    }

    insertResults(runId, allResults);
    updateRun(runId, { status: "completed", message: "sample done", finished_at: new Date().toISOString() });
    running = false;
    return { runId, total: stocks.length };
  } catch (err) {
    updateRun(runId, { status: "failed", message: err.message, finished_at: new Date().toISOString() });
    running = false;
    throw err;
  }
}

export { getVerifyRunStatus, getAccuracyReport, getSampleDetail, ALL_FIELDS };

import { writeFileSync, readFileSync, existsSync, mkdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { getDb, getLatestDataRunId } from "../db.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CACHE_DIR = path.resolve(__dirname, "../../cache");
const CSV_PATH = path.join(CACHE_DIR, "stock_factors.csv");

const FACTOR_COLUMNS = [
  "code", "name", "market", "sector",
  "revenue_yoy", "deduct_profit_yoy", "cagr_3y",
  "gross_margin", "net_margin", "roe", "roic",
  "ocf_to_profit", "free_cashflow",
  "debt_ratio", "interest_debt", "cash_coverage",
  "dividend_yield", "payout_ratio", "buyback_amount",
  "profit_volatility_5y", "cyclicality",
  "pe", "pb", "ps", "valuation_percentile",
  "fund_holding", "northbound", "etf_holding", "research_count",
  "crawled_at",
];

function escapeCsv(val) {
  if (val === null || val === undefined) return "";
  const s = String(val);
  if (s.includes(",") || s.includes('"') || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

/** Get DB row count for the latest data run */
function getDbRowCount() {
  const runId = getLatestDataRunId();
  if (!runId) return 0;
  const db = getDb();
  try {
    const row = db.prepare("SELECT COUNT(*) AS cnt FROM stock_factors WHERE crawl_run_id = ?").get(runId);
    return row.cnt;
  } finally {
    db.close();
  }
}

/** Get CSV row count (excluding header) */
function getCsvRowCount() {
  if (!existsSync(CSV_PATH)) return 0;
  const text = readFileSync(CSV_PATH, "utf-8").trim();
  if (!text) return 0;
  return text.split("\n").length - 1;
}

/**
 * Export all factors from DB to CSV.
 * Before writing, checks if CSV is already up-to-date with DB.
 */
export function exportFactorsToCsv() {
  const dbCount = getDbRowCount();
  if (dbCount === 0) {
    console.log("CSV export: no data in DB, skipping");
    return;
  }

  const csvCount = getCsvRowCount();
  if (csvCount === dbCount) {
    console.log(`CSV export: already up-to-date (${dbCount} rows), skipping`);
    return;
  }

  if (!existsSync(CACHE_DIR)) {
    mkdirSync(CACHE_DIR, { recursive: true });
  }

  const runId = getLatestDataRunId();
  const db = getDb();
  try {
    const rows = db
      .prepare(
        `
        SELECT
          sf.code,
          COALESCE(s.name, sf.code) AS name,
          COALESCE(s.market, CASE WHEN sf.code LIKE '6%' THEN 'SH' ELSE 'SZ' END) AS market,
          sec.name AS sector,
          sf.revenue_yoy, sf.deduct_profit_yoy, sf.cagr_3y,
          sf.gross_margin, sf.net_margin, sf.roe, sf.roic,
          sf.ocf_to_profit, sf.free_cashflow,
          sf.debt_ratio, sf.interest_debt, sf.cash_coverage,
          sf.dividend_yield, sf.payout_ratio, sf.buyback_amount,
          sf.profit_volatility_5y, sf.cyclicality,
          sf.pe, sf.pb, sf.ps, sf.valuation_percentile,
          sf.fund_holding, sf.northbound, sf.etf_holding, sf.research_count,
          sf.crawled_at
        FROM stock_factors sf
        LEFT JOIN stocks s ON s.code = sf.code
        LEFT JOIN sectors sec ON sec.id = s.sector_id
        WHERE sf.crawl_run_id = ?
        ORDER BY sf.code ASC
        `
      )
      .all(runId);

    if (!rows || rows.length === 0) return;

    const header = FACTOR_COLUMNS.join(",");
    const lines = rows.map((row) =>
      FACTOR_COLUMNS.map((col) => escapeCsv(row[col])).join(",")
    );
    const csv = [header, ...lines].join("\n");

    writeFileSync(CSV_PATH, csv, "utf-8");
    console.log(`CSV exported: ${CSV_PATH} (${rows.length} rows)`);
  } finally {
    db.close();
  }
}

export function getCsvPath() {
  return CSV_PATH;
}

export function csvExists() {
  return existsSync(CSV_PATH);
}

/** Check if CSV is consistent with DB. Returns { ok, dbCount, csvCount } */
export function checkCsvConsistency() {
  const dbCount = getDbRowCount();
  const csvCount = getCsvRowCount();
  return { ok: dbCount === csvCount, dbCount, csvCount };
}

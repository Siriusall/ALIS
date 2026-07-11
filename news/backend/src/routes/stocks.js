import { Router } from "express";
import { getDb, getLatestDataRunId } from "../db.js";
import { checkCsvConsistency, csvExists, exportFactorsToCsv } from "../services/csvExporter.js";

const router = Router();

router.get("/sectors", (_req, res) => {
  const runId = getLatestDataRunId();
  if (!runId) {
    return res.json([]);
  }
  const db = getDb();
  try {
    const rows = db
      .prepare(
        `SELECT DISTINCT sec.name AS name
         FROM stock_factors sf
         LEFT JOIN stocks s ON s.code = sf.code
         LEFT JOIN sectors sec ON sec.id = s.sector_id
         WHERE sf.crawl_run_id = ? AND sec.name IS NOT NULL
         ORDER BY sec.name ASC`
      )
      .all(runId);

    const sectors = rows.map((r, i) => ({
      id: i + 1,
      code: `SEC:${r.name}`,
      name: r.name,
      stock_count: 0,
    }));

    // Fill stock_count
    for (const sec of sectors) {
      const cnt = db
        .prepare(
          `SELECT COUNT(*) AS cnt
           FROM stock_factors sf
           LEFT JOIN stocks s ON s.code = sf.code
           LEFT JOIN sectors sec ON sec.id = s.sector_id
           WHERE sf.crawl_run_id = ? AND sec.name = ?`
        )
        .get(runId, sec.name).cnt;
      sec.stock_count = cnt;
    }

    res.json(sectors);
  } finally {
    db.close();
  }
});

router.get("/", (req, res) => {
  const runId = getLatestDataRunId();
  if (!runId) {
    return res.json({ total: 0, page: 1, size: 50, items: [] });
  }

  const page = Math.max(1, parseInt(req.query.page || "1", 10));
  const size = Math.min(200, Math.max(1, parseInt(req.query.size || "50", 10)));
  const offset = (page - 1) * size;
  const q = (req.query.q || "").trim();
  const sector = (req.query.sector || "").trim();

  const where = ["sf.crawl_run_id = ?"];
  const params = [runId];

  if (q) {
    where.push("(sf.code LIKE ? OR COALESCE(s.name, sf.code) LIKE ?)");
    params.push(`%${q}%`, `%${q}%`);
  }
  if (sector) {
    where.push("sec.name = ?");
    params.push(sector);
  }

  const whereSql = where.join(" AND ");
  const db = getDb();
  try {
    const total = db
      .prepare(
        `SELECT COUNT(*) AS cnt
         FROM stock_factors sf
         LEFT JOIN stocks s ON s.code = sf.code
         LEFT JOIN sectors sec ON sec.id = s.sector_id
         WHERE ${whereSql}`
      )
      .get(...params).cnt;

    const items = db
      .prepare(
        `SELECT
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
         WHERE ${whereSql}
         ORDER BY s.code ASC
         LIMIT ? OFFSET ?`
      )
      .all(...params, size, offset);

    res.json({ total, page, size, items });
  } finally {
    db.close();
  }
});

router.get("/:code", (req, res) => {
  const runId = getLatestDataRunId();
  if (!runId) {
    return res.status(404).json({ error: "No crawl data" });
  }

  const db = getDb();
  try {
    const row = db
      .prepare(
        `SELECT
          sf.code,
          COALESCE(s.name, sf.code) AS name,
          COALESCE(s.market, CASE WHEN sf.code LIKE '6%' THEN 'SH' ELSE 'SZ' END) AS market,
          sec.name AS sector,
          sf.*
         FROM stock_factors sf
         LEFT JOIN stocks s ON s.code = sf.code
         LEFT JOIN sectors sec ON sec.id = s.sector_id
         WHERE sf.crawl_run_id = ? AND sf.code = ?`
      )
      .get(runId, req.params.code);

    if (!row) {
      return res.status(404).json({ error: "Stock not found" });
    }
    res.json(row);
  } finally {
    db.close();
  }
});

export default router;

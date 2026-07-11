import cors from "cors";
import express from "express";
import crawlRouter from "./routes/crawl.js";
import stocksRouter from "./routes/stocks.js";
import verifyRouter from "./routes/verify.js";
import { checkCsvConsistency, csvExists, exportFactorsToCsv } from "./services/csvExporter.js";

// Prevent unhandled rejections from crashing the process
process.on("unhandledRejection", (err) => {
  console.error("Unhandled rejection:", err?.message || err);
});

const app = express();
const PORT = process.env.PORT || 3001;

app.use(cors());
app.use(express.json());
app.use((_req, res, next) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  next();
});

app.get("/api/health", (_req, res) => {
  res.json({ ok: true });
});

app.use("/api/crawl", crawlRouter);
app.use("/api/stocks", stocksRouter);
app.use("/api/verify", verifyRouter);

// CSV cache check endpoint
app.get("/api/cache/check", (_req, res) => {
  const result = checkCsvConsistency();
  res.json({
    ok: result.ok,
    dbCount: result.dbCount,
    csvCount: result.csvCount,
    csvExists: csvExists(),
  });
});

// CSV cache sync endpoint — force re-export (manual only, crawler handles live sync)
app.post("/api/cache/sync", (_req, res) => {
  try {
    exportFactorsToCsv();
    const result = checkCsvConsistency();
    res.json({ ok: result.ok, dbCount: result.dbCount, csvCount: result.csvCount });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Periodic CSV sync is handled by the crawler (appends per-stock).
// Manual sync available via POST /api/cache/sync.

app.listen(PORT, () => {
  console.log(`Backend listening on http://localhost:${PORT}`);
});

import { Router } from "express";
import { isVerifyRunning, stopVerify, getVerifyRunStatus, getAccuracyReport, getSampleDetail, runVerify } from "../services/verifier.js";
import { startVerify, compareSamples, correctField, getVerifyLogs } from "../services/corrector.js";
import { exportFactorsToCsv } from "../services/csvExporter.js";

const router = Router();

router.get("/status", (_req, res) => {
  const running = isVerifyRunning();
  const latest = getVerifyRunStatus();
  res.json({ running, latest_run: latest || null });
});

router.post("/start", async (_req, res) => {
  try {
    if (isVerifyRunning()) {
      return res.status(409).json({ error: "Verify already running" });
    }
    // Start in background
    const result = startVerify({ sampleSize: 30 });
    res.status(202).json({ message: "Verify started", running: true });
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
});

router.post("/stop", (_req, res) => {
  stopVerify();
  res.json({ message: "Stop signal sent" });
});

router.get("/report", (_req, res) => {
  const report = getAccuracyReport();
  if (!report) return res.json({ run_id: null, total_samples: 0, fields: [] });
  res.json(report);
});

router.get("/sample-detail", (req, res) => {
  const runId = parseInt(req.query.runId) || 0;
  const field = req.query.field || null;
  const status = req.query.status || null;
  const page = Math.max(1, parseInt(req.query.page || "1", 10));
  const size = Math.min(200, Math.max(1, parseInt(req.query.size || "50", 10)));

  if (!runId) {
    const latest = getVerifyRunStatus();
    if (!latest) return res.json({ total: 0, page, size, items: [] });
    return res.json(getSampleDetail(latest.id, field, status, page, size));
  }
  res.json(getSampleDetail(runId, field, status, page, size));
});

router.get("/logs", (req, res) => {
  const limit = Math.min(1000, Math.max(1, Number(req.query.limit || 300)));
  const items = getVerifyLogs({ limit });
  res.json({ total: items.length, items });
});

export default router;

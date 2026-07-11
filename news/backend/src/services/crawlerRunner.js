import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "../../..");
const CRAWLER_DIR = path.join(ROOT, "crawler");
const VENV_PYTHON = path.join(CRAWLER_DIR, ".venv/bin/python3");
const DEFAULT_PYTHON_BINS = [
  "Z:\\Python314\\python.exe",
  "C:\\Python314\\python.exe",
];

let running = false;
let currentChild = null;
const logBuffer = [];
const MAX_LOG_LINES = 1000;

export function isCrawlRunning() {
  reconcileChildState();
  return running;
}

export function getCrawlLogs({ limit = 300 } = {}) {
  return logBuffer.slice(-limit);
}

export async function triggerCrawl({ sector, limit, codes, codesFile, resumeRunId, newRun } = {}) {
  if (running) {
    const err = new Error("Crawl already running");
    err.status = 409;
    throw err;
  }

  const args = ["main.py", "crawl"];
  if (sector) args.push("--sector", sector);
  if (limit) args.push("--limit", String(limit));
  if (codes) args.push("--codes", Array.isArray(codes) ? codes.join(",") : String(codes));
  if (codesFile) args.push("--codes-file", String(codesFile));
  if (resumeRunId) args.push("--resume-run-id", String(resumeRunId));
  if (newRun) args.push("--new-run");

  const child = await spawnCrawlerProcess(args);

  const logs = [];

  running = true;
  currentChild = child;

  child.stdout.on("data", (buf) => {
    const text = buf.toString("utf8");
    logs.push(text);
    appendLog("stdout", text);
  });
  child.stderr.on("data", (buf) => {
    const text = buf.toString("utf8");
    logs.push(text);
    appendLog("stderr", text);
  });

  const done = new Promise((resolve, reject) => {
    const finish = (code, signal = null) => {
      if (currentChild === child) {
        running = false;
        currentChild = null;
      }
      const detail = signal ? `signal ${signal}` : `code ${code}`;
      appendLog(code === 0 ? "info" : "error", `crawler exited with ${detail}`);
      if (code === 0) resolve({ code, logs: logs.join("") });
      else reject(Object.assign(new Error(`Crawler exited with ${detail}`), { code, signal, logs: logs.join("") }));
    };

    child.once("close", finish);
    child.on("error", (err) => {
      if (currentChild === child) {
        running = false;
        currentChild = null;
      }
      appendLog("error", err.message);
      reject(err);
    });

    // If the process already exited before close listener registration, reconcile immediately.
    if (child.exitCode !== null || child.signalCode !== null) {
      finish(child.exitCode, child.signalCode);
    }
  });

  return { pid: child.pid, done };
}

export function stopCrawl() {
  if (currentChild) {
    appendLog("info", "stop signal sent");
    currentChild.kill("SIGTERM");
  }
}

function appendLog(level, text) {
  const lines = String(text)
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter(Boolean);
  const timestamp = new Date().toISOString();
  for (const line of lines) {
    logBuffer.push({ timestamp, level, message: line });
  }
  if (logBuffer.length > MAX_LOG_LINES) {
    logBuffer.splice(0, logBuffer.length - MAX_LOG_LINES);
  }
}

function reconcileChildState() {
  if (!running || !currentChild) return;
  if (currentChild.exitCode !== null || currentChild.signalCode !== null || currentChild.killed) {
    const detail = currentChild.signalCode ? `signal ${currentChild.signalCode}` : `code ${currentChild.exitCode}`;
    appendLog("error", `crawler state reconciled as exited with ${detail}`);
    running = false;
    currentChild = null;
  }
}

function resolvePythonCandidates() {
  const candidates = [];
  // Prefer project venv python3.11 (has playwright with chromium)
  if (fs.existsSync(VENV_PYTHON)) {
    candidates.push({ command: VENV_PYTHON, prefixArgs: [], shell: false });
  }
  const configured = process.env.PYTHON_BIN;
  if (configured) {
    if (fs.existsSync(configured)) {
      candidates.push({ command: configured, prefixArgs: [], shell: false });
    }
  }
  for (const p of DEFAULT_PYTHON_BINS) {
    if (fs.existsSync(p)) {
      candidates.push({ command: p, prefixArgs: [], shell: false });
    }
  }
  // PATH fallback: this is the most portable option on user host machines.
  candidates.push({ command: process.env.PYTHON_CMD || "python", prefixArgs: [], shell: false });
  candidates.push({ command: "py", prefixArgs: ["-3"], shell: false });
  return candidates;
}

async function spawnCrawlerProcess(args) {
  const candidates = resolvePythonCandidates();
  let lastError = null;
  for (const candidate of candidates) {
    const spawnArgs = [...candidate.prefixArgs, ...args];
    appendLog("info", `spawn try: ${candidate.command} ${spawnArgs.join(" ")}`);
    const child = spawn(candidate.command, spawnArgs, {
      cwd: CRAWLER_DIR,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", NODE_OPTIONS: "" },
      shell: candidate.shell,
      stdio: ["ignore", "pipe", "pipe"],
    });
    try {
      await waitForSpawn(child);
      appendLog("info", `spawn ok: ${candidate.command}`);
      return child;
    } catch (err) {
      lastError = err;
      const detail = [
        err?.message || "spawn error",
        err?.code ? `code=${err.code}` : "",
        err?.path ? `path=${err.path}` : "",
        `cwd=${CRAWLER_DIR}`,
      ].filter(Boolean).join(" ");
      appendLog("error", detail);
    }
  }
  const failure = new Error(`Unable to start crawler process: ${lastError?.message || "unknown error"}`);
  failure.status = 500;
  throw failure;
}


function waitForSpawn(child) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        resolve();
      }
    }, 1500);

    child.once("spawn", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve();
    });

    child.once("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      running = false;
      currentChild = null;
      reject(Object.assign(err, { status: 500 }));
    });
  });
}

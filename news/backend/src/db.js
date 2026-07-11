import { DatabaseSync } from "node:sqlite";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.NEWS_DB_PATH || path.resolve(__dirname, "../../data/news.db");

export function getDb() {
  const db = new DatabaseSync(DB_PATH);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec("PRAGMA busy_timeout=5000");
  return db;
}

export function getWritableDb() {
  const db = new DatabaseSync(DB_PATH);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec("PRAGMA busy_timeout=5000");
  return db;
}

export function getLatestRunId() {
  const db = getDb();
  try {
    const row = db
      .prepare(
        `SELECT id FROM crawl_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1`
      )
      .get();
    return row?.id ?? null;
  } finally {
    db.close();
  }
}

export function getLatestDataRunId() {
  const db = getDb();
  try {
    const row = db
      .prepare(
        `
        SELECT cr.id
        FROM crawl_runs cr
        WHERE EXISTS (
          SELECT 1
          FROM stock_factors sf
          WHERE sf.crawl_run_id = cr.id
        )
        ORDER BY cr.id DESC
        LIMIT 1
        `
      )
      .get();
    return row?.id ?? null;
  } finally {
    db.close();
  }
}

export function getLatestOrRunningRun() {
  const db = getDb();
  try {
    // Prefer the currently running run (if it has data or was started recently)
    const running = db
      .prepare(`SELECT * FROM crawl_runs WHERE status = 'running' AND (success > 0 OR started_at > datetime('now', '-1 hour')) ORDER BY id DESC LIMIT 1`)
      .get();
    if (running) return running;
    // Fall back to latest by id
    return db
      .prepare(`SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1`)
      .get();
  } finally {
    db.close();
  }
}

export function markLatestRunningRunStopped(message = "stopped by user") {
  const writable = getWritableDb();
  try {
    const row = writable
      .prepare(`SELECT id FROM crawl_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1`)
      .get();
    if (!row) return null;
    writable
      .prepare(
        `
        UPDATE crawl_runs
        SET status = 'stopped',
            message = ?,
            finished_at = datetime('now', 'localtime')
        WHERE id = ?
        `
      )
      .run(message, row.id);
    return row.id;
  } finally {
    writable.close();
  }
}

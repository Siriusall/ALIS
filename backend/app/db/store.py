"""Persistence for strategies, executions, and operations.

PostgreSQL is the required runtime store. The adapter keeps a narrow SQLite
compatibility path only for explicit migration/test database URLs; production
startup never creates or falls back to a SQLite file.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, unquote

from app.core.a_share_fees import (
    ETF_FEE_SCHEDULE_VERSION,
    FEE_SCHEDULE_VERSION,
    commission,
    is_exchange_etf,
    stamp_tax,
    transfer_fee,
)
from app.core.config import settings
from app.core.trade_calendar import get_last_trade_date
from app.core.runtime_guard import assert_database_boundary
from app.core.db_connection_gate import DatabaseConnectionGate
from app.core.db_connect import connect_postgres

logger = logging.getLogger("quant")

_DEFAULT_STRATEGY_NAME = "ALIS Champion 综合策略"
_LEGACY_DEFAULT_STRATEGY_NAME = "默认策略"
_DEFAULT_STRATEGY_CONTENT = {
    "schema_version": "alis_champion_selection_v1",
    "universe": {"exchange_group": "HS", "exclude_st": True, "exclude_suspended": True},
    "filters": [
        {"factor": "is_st", "op": "==", "value": 0},
        {"factor": "is_suspended", "op": "==", "value": 0},
    ],
    "score": {"factors": {
        "momentum_20": 0.25,
        "momentum_5": 0.10,
        "volume_ratio_5": 0.10,
        "turnover": 0.10,
        "float_mv": 0.05,
        "total_mv": 0.05,
        "pe_ttm": -0.05,
        "pb": -0.05,
        "volatility_20": -0.15,
        "change_pct": 0.05,
    }},
    "rebalance": {"freq": "daily", "top_n": 20},
    "lookback_days": 60,
    "strategy_controller": "strategy_controller",
    "portfolio_optimizer": "empirical_cvar_slsqp_v2",
    "execution_contract": "t_close_signal_t_plus_1_open_v1",
}

_BUSINESS_DB_CONNECTION_GATE = DatabaseConnectionGate(
    "ALIS_BUSINESS_DB_MAX_CONNECTIONS", default=10
)
_ACTIVE_BUSINESS_TRANSACTION: ContextVar[Optional["_ConnWrapper"]] = ContextVar(
    "alis_active_business_transaction", default=None,
)


def _json_load(s: Any, default: Any) -> Any:
    try:
        return json.loads(s) if isinstance(s, str) else default
    except Exception:
        return default


def _is_mysql() -> bool:
    url = (settings.database_url or "").strip().lower()
    return url.startswith("mysql://") or url.startswith("mysql+pymysql://")


def _is_postgres() -> bool:
    url = (settings.database_url or "").strip().lower()
    return url.startswith("postgresql://") or url.startswith("postgres://") or url.startswith("postgresql+psycopg://")


def _db_path() -> Path:
    """从 settings.database_url 解析 SQLite 文件路径（仅支持 sqlite:/// 形式）。"""
    url = (settings.database_url or "").strip()
    if url.startswith("sqlite:///"):
        path_str = url.replace("sqlite:///", "", 1)
        p = Path(path_str)
    else:
        p = Path(__file__).resolve().parent.parent.parent / ".cache" / "quant.db"
    return p


def _mysql_connect_kwargs() -> dict:
    """从 DATABASE_URL 解析 MySQL 连接参数。格式：mysql+pymysql://user:pass@host:port/dbname"""
    url = (settings.database_url or "").strip()
    # 兼容 mysql:// 和 mysql+pymysql://
    if url.startswith("mysql+pymysql://"):
        url = url.replace("mysql+pymysql://", "mysql://", 1)
    elif url.startswith("mysql://"):
        pass
    else:
        raise ValueError("DATABASE_URL 格式应为 mysql://user:pass@host:port/dbname 或 mysql+pymysql://...")
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else "root",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": (parsed.path or "/").lstrip("/") or "quant",
        "charset": "utf8mb4",
    }


class _ConnWrapper:
    """统一连接封装：SQLite 与 MySQL 使用相同的 execute / last_insert_id 接口。"""

    def __init__(self, raw_conn, is_mysql: bool, is_postgres: bool = False):
        self._conn = raw_conn
        # Domain code historically uses _is_mysql to select the server-SQL
        # branch. PostgreSQL shares that branch, while the wrapper handles the
        # actual driver differences below.
        self._is_mysql = is_mysql or is_postgres
        self._is_postgres = is_postgres
        self._last_cursor = None

    def execute(self, sql: str, params: Optional[tuple] = None):
        if self._is_postgres:
            sql = sql.replace("?", "%s")
            self._last_cursor = self._conn.cursor()
            self._last_cursor.execute(sql, params or ())
            return self._last_cursor
        if self._is_mysql:
            import pymysql.cursors
            sql = sql.replace("?", "%s")
            self._last_cursor = self._conn.cursor(pymysql.cursors.DictCursor)
            self._last_cursor.execute(sql, params or ())
            return self._last_cursor
        else:
            self._last_cursor = self._conn.execute(sql, params or ())
            return self._last_cursor

    def executescript(self, sql: str) -> None:
        if self._is_postgres:
            for stmt in _split_sql_statements(sql):
                if stmt.strip():
                    self.execute(stmt)
            return
        if self._is_mysql:
            # MySQL 不支持多语句一次执行，按分号拆分并逐条执行（忽略空与注释）
            for stmt in _split_sql_statements(sql):
                if stmt.strip():
                    with self._conn.cursor() as cur:
                        cur.execute(stmt)
        else:
            self._conn.executescript(sql)

    def last_insert_id(self) -> int:
        if self._is_postgres:
            row = self._conn.execute("SELECT lastval()").fetchone()
            return int(row["lastval"] if isinstance(row, dict) else row[0])
        if self._is_mysql:
            return self._conn.insert_id()
        row = self._conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0])

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _rounded_account_balance_expression(conn: _ConnWrapper, expression: str) -> str:
    """Round a floating-point account expression on every supported SQL dialect."""
    if getattr(conn, "_is_postgres", False):
        return f"ROUND(({expression})::numeric, 2)::double precision"
    return f"ROUND({expression}, 2)"


def _acquire_simulation_account_lock(conn: _ConnWrapper) -> None:
    """Serialize all cash/position writers across API and worker processes."""
    if getattr(conn, "_is_postgres", False):
        conn.execute("SELECT pg_advisory_xact_lock(?)", (1095522643,))  # 'ALIS'


def _split_sql_statements(sql: str) -> List[str]:
    """按分号拆分 SQL，忽略空段和仅注释。"""
    parts = re.split(r";\s*", sql)
    return [p.strip() for p in parts if p.strip() and not p.strip().startswith("--")]


@contextmanager
def _conn():
    active = _ACTIVE_BUSINESS_TRANSACTION.get()
    if active is not None:
        # Reuse the plan-level transaction for nested ledger/store calls.  The
        # outer owner alone commits, rolls back through connection close, and
        # releases the connection-gate slot.
        yield active
        return
    if _is_postgres():
        import psycopg
        from psycopg.rows import dict_row
        url = (settings.database_url or "").strip()
        assert_database_boundary(url, role="business")
        if url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        logger.debug("PostgreSQL 连接: %s", url.split("@")[-1])
        with _BUSINESS_DB_CONNECTION_GATE.slot(settings.database_connect_timeout_seconds):
            raw = connect_postgres(
                psycopg.connect,
                url,
                row_factory=dict_row,
                timeout=settings.database_connect_timeout_seconds,
                role="business",
            )
            from app.core.autonomy_step_control import register_postgres_connection, unregister_postgres_connection
            cancellation_registration = register_postgres_connection(raw)
            w = None
            try:
                raw.execute("SELECT set_config('statement_timeout', %s, false)", (str(settings.database_statement_timeout_ms),))
                raw.execute(
                    "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
                    (os.getenv("DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS", "120000"),),
                )
                w = _ConnWrapper(raw, is_mysql=False, is_postgres=True)
                yield w
                w.commit()
            finally:
                unregister_postgres_connection(cancellation_registration)
                if w is not None:
                    w.close()
                else:
                    raw.close()
    elif _is_mysql():
        import pymysql
        kw = _mysql_connect_kwargs()
        logger.debug("MySQL 连接参数: host=%s port=%s database=%s user=%s", kw["host"], kw["port"], kw["database"], kw["user"])
        raw = pymysql.connect(**kw)
        try:
            w = _ConnWrapper(raw, is_mysql=True)
            yield w
            w.commit()
        finally:
            w.close()
    else:
        raise RuntimeError(
            "DATABASE_URL 未配置为 PostgreSQL；SQLite 已禁用，请设置 DATABASE_URL。"
        )


@contextmanager
def atomic_business_transaction():
    """Make nested business-store mutations one commit/rollback unit."""
    if _ACTIVE_BUSINESS_TRANSACTION.get() is not None:
        raise RuntimeError("不允许嵌套开启模拟账本原子事务")
    with _conn() as connection:
        _acquire_simulation_account_lock(connection)
        token = _ACTIVE_BUSINESS_TRANSACTION.set(connection)
        try:
            yield connection
        finally:
            _ACTIVE_BUSINESS_TRANSACTION.reset(token)


def _migrate_positions_operations_name(c: _ConnWrapper) -> None:
    """为 positions、operations 表添加 name 列（若不存在）。"""
    for table in ("positions", "operations"):
        if c._is_mysql:
            cur = c.execute(f"SHOW COLUMNS FROM {table} LIKE 'name'")
            if cur.fetchone() is None:
                c.execute(f"ALTER TABLE {table} ADD COLUMN name VARCHAR(255) NOT NULL DEFAULT ''")
        else:
            cur = c.execute(f"PRAGMA table_info({table})")
            cols = [row[1] for row in cur.fetchall()]
            if "name" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN name TEXT NOT NULL DEFAULT ''")


def _migrate_positions_bought_at(c: _ConnWrapper) -> None:
    """为 positions 表添加 bought_at 列（若不存在）。"""
    if c._is_mysql:
        cur = c.execute("SHOW COLUMNS FROM positions LIKE 'bought_at'")
        if cur.fetchone() is None:
            c.execute("ALTER TABLE positions ADD COLUMN bought_at VARCHAR(32) NULL")
    else:
        cur = c.execute("PRAGMA table_info(positions)")
        cols = [row[1] for row in cur.fetchall()]
        if "bought_at" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN bought_at TEXT NULL")


def _migrate_backtests_data_quality(c: _ConnWrapper) -> None:
    """Add backtest evidence columns for data/trading-risk review."""
    if c._is_mysql and not c._is_postgres:
        for col in ("data_quality_json", "trade_constraints_json"):
            cur = c.execute(f"SHOW COLUMNS FROM backtests LIKE '{col}'")
            if cur.fetchone() is None:
                c.execute(f"ALTER TABLE backtests ADD COLUMN {col} LONGTEXT NULL")
        return
    cur = c.execute("PRAGMA table_info(backtests)")
    cols = [row[1] for row in cur.fetchall()]
    if "data_quality_json" not in cols:
        c.execute("ALTER TABLE backtests ADD COLUMN data_quality_json TEXT NULL")
    if "trade_constraints_json" not in cols:
        c.execute("ALTER TABLE backtests ADD COLUMN trade_constraints_json TEXT NULL")


def _migrate_backtests_model_version(c: _ConnWrapper) -> None:
    """Track model-version validation history in backtest records."""
    if c._is_mysql:
        for col, ddl in (
            ("model_version_id", "BIGINT NULL"),
            ("model_version", "VARCHAR(64) NULL"),
        ):
            cur = c.execute(f"SHOW COLUMNS FROM backtests LIKE '{col}'")
            if cur.fetchone() is None:
                c.execute(f"ALTER TABLE backtests ADD COLUMN {col} {ddl}")
        return
    cur = c.execute("PRAGMA table_info(backtests)")
    cols = [row[1] for row in cur.fetchall()]
    if "model_version_id" not in cols:
        c.execute("ALTER TABLE backtests ADD COLUMN model_version_id INTEGER NULL")
    if "model_version" not in cols:
        c.execute("ALTER TABLE backtests ADD COLUMN model_version TEXT NULL")


def _migrate_operations_execution_id_nullable(c: _ConnWrapper) -> None:
    """允许 operations.execution_id 为 NULL（手动操作）。"""
    if c._is_mysql:
        try:
            c.execute("ALTER TABLE operations MODIFY execution_id BIGINT NULL")
        except Exception:
            pass
        return
    cur = c.execute("PRAGMA table_info(operations)")
    cols = [row[1] for row in cur.fetchall()]
    if "execution_id" not in cols:
        return
    cur = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'")
    row = cur.fetchone()
    if not row or "execution_id INTEGER NOT NULL" not in (row["sql"] or ""):
        return
    has_name = "name" in cols
    c.execute(
        """CREATE TABLE operations_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            at TEXT NOT NULL,
            remark TEXT,
            name TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (execution_id) REFERENCES executions(id)
        )"""
    )
    sel = "id, execution_id, symbol, side, price, quantity, at, remark, " + ("name" if has_name else "''")
    c.execute(
        "INSERT INTO operations_new (id, execution_id, symbol, side, price, quantity, at, remark, name) SELECT " + sel + " FROM operations"
    )
    c.execute("DROP TABLE operations")
    c.execute("ALTER TABLE operations_new RENAME TO operations")


def _migrate_target_weight_contracts(c: _ConnWrapper) -> None:
    for table in ("operations", "decision_snapshots"):
        try:
            columns = {str(row["name"]) for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if "target_weight_contract_json" not in columns:
                c.execute(f"ALTER TABLE {table} ADD COLUMN target_weight_contract_json TEXT NOT NULL DEFAULT '{{}}'")
        except Exception:
            pass


def _migrate_quant_run_task_detail_json(c: _ConnWrapper) -> None:
    """Add structured run explanation payload for decision review."""
    if c._is_mysql:
        cur = c.execute("SHOW COLUMNS FROM quant_run_task_runs LIKE 'detail_json'")
        if cur.fetchone() is None:
            c.execute("ALTER TABLE quant_run_task_runs ADD COLUMN detail_json LONGTEXT NULL")
        return
    cur = c.execute("PRAGMA table_info(quant_run_task_runs)")
    cols = [row[1] for row in cur.fetchall()]
    if "detail_json" not in cols:
        c.execute("ALTER TABLE quant_run_task_runs ADD COLUMN detail_json TEXT NULL")


def _migrate_quant_replay_schema(c: _ConnWrapper) -> None:
    """
    量化补跑相关迁移：
    1) quant_run_tasks 增加 replay 透明字段
    2) quant_run_task_runs 增加 (task_id, signal_date) 唯一约束，防重复补跑
    3) 创建 quant_replay_waterline 水位线表
    """
    replay_cols = {
        "replay_mode": ("TEXT NOT NULL DEFAULT 'pragmatic_recover'", "VARCHAR(32) NOT NULL DEFAULT 'pragmatic_recover'"),
        "replay_status": ("TEXT NOT NULL DEFAULT ''", "VARCHAR(64) NOT NULL DEFAULT ''"),
        "replay_gap_days": ("INTEGER NOT NULL DEFAULT 0", "INT NOT NULL DEFAULT 0"),
        "replay_current_execute_date": ("TEXT NULL", "VARCHAR(16) NULL"),
        "replay_target_execute_date": ("TEXT NULL", "VARCHAR(16) NULL"),
    }

    if c._is_mysql:
        # 增加 replay 列
        for col, (_, mysql_def) in replay_cols.items():
            cur = c.execute(f"SHOW COLUMNS FROM quant_run_tasks LIKE '{col}'")
            if cur.fetchone() is None:
                c.execute(f"ALTER TABLE quant_run_tasks ADD COLUMN {col} {mysql_def}")
        # 建唯一索引前先清理历史重复（保留最小 id）
        try:
            c.execute(
                """
                DELETE r1 FROM quant_run_task_runs r1
                INNER JOIN quant_run_task_runs r2
                  ON r1.task_id = r2.task_id
                 AND r1.signal_date = r2.signal_date
                 AND r1.id > r2.id
                """
            )
        except Exception:
            pass
        # 唯一索引：task_id + signal_date
        cur = c.execute("SHOW INDEX FROM quant_run_task_runs WHERE Key_name = 'uq_task_signal'")
        if cur.fetchone() is None:
            c.execute("CREATE UNIQUE INDEX uq_task_signal ON quant_run_task_runs (task_id, signal_date)")
        # 水位线表
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_replay_waterline (
                strategy_id BIGINT NOT NULL,
                exchange_group VARCHAR(16) NOT NULL DEFAULT 'HS',
                last_successful_signal_date VARCHAR(16) NULL,
                last_successful_execute_date VARCHAR(16) NULL,
                last_run_mode VARCHAR(32) NOT NULL DEFAULT 'pragmatic_recover',
                last_run_status VARCHAR(32) NOT NULL DEFAULT '',
                updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (strategy_id, exchange_group)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        return

    # SQLite
    cur = c.execute("PRAGMA table_info(quant_run_tasks)")
    cols = [row[1] for row in cur.fetchall()]
    for col, (sqlite_def, _) in replay_cols.items():
        if col not in cols:
            c.execute(f"ALTER TABLE quant_run_tasks ADD COLUMN {col} {sqlite_def}")
    # 建唯一索引前先清理历史重复（保留最小 id）
    try:
        c.execute(
            """
            DELETE FROM quant_run_task_runs
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM quant_run_task_runs
                GROUP BY task_id, signal_date
            )
            """
        )
    except Exception:
        pass
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_task_signal ON quant_run_task_runs (task_id, signal_date)")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_replay_waterline (
            strategy_id INTEGER NOT NULL,
            exchange_group TEXT NOT NULL DEFAULT 'HS',
            last_successful_signal_date TEXT,
            last_successful_execute_date TEXT,
            last_run_mode TEXT NOT NULL DEFAULT 'pragmatic_recover',
            last_run_status TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (strategy_id, exchange_group)
        )
        """
    )


def _migrate_quant_task_perf_schema(c: _ConnWrapper) -> None:
    """
    量化任务性能与可追踪增强：
    1) 任务元信息表（策略快照、风控配置）
    2) 任务健康表（心跳、步耗时、风险命中）
    3) 关键索引（operations / quant_run_task_runs / quant_run_tasks）
    """
    if c._is_mysql:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_run_task_meta (
                task_id BIGINT PRIMARY KEY,
                strategy_snapshot_json LONGTEXT NOT NULL,
                risk_config_json LONGTEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL,
                FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_run_task_health (
                task_id BIGINT PRIMARY KEY,
                heartbeat_at VARCHAR(64) NULL,
                last_step_latency_ms BIGINT NOT NULL DEFAULT 0,
                avg_step_latency_ms BIGINT NOT NULL DEFAULT 0,
                step_count INT NOT NULL DEFAULT 0,
                risk_hit_count INT NOT NULL DEFAULT 0,
                consecutive_pause_count INT NOT NULL DEFAULT 0,
                last_risk_code VARCHAR(64) NOT NULL DEFAULT '',
                updated_at VARCHAR(64) NOT NULL,
                FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        try:
            c.execute("CREATE INDEX idx_ops_execution_at ON operations (execution_id, at)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX idx_ops_symbol_at ON operations (symbol, at)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX idx_task_runs_task_execute ON quant_run_task_runs (task_id, execute_date)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX idx_tasks_status_updated ON quant_run_tasks (status, updated_at)")
        except Exception:
            pass
        return

    # SQLite
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_run_task_meta (
            task_id INTEGER PRIMARY KEY,
            strategy_snapshot_json TEXT NOT NULL,
            risk_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_run_task_health (
            task_id INTEGER PRIMARY KEY,
            heartbeat_at TEXT,
            last_step_latency_ms INTEGER NOT NULL DEFAULT 0,
            avg_step_latency_ms INTEGER NOT NULL DEFAULT 0,
            step_count INTEGER NOT NULL DEFAULT 0,
            risk_hit_count INTEGER NOT NULL DEFAULT 0,
            consecutive_pause_count INTEGER NOT NULL DEFAULT 0,
            last_risk_code TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_ops_execution_at ON operations (execution_id, at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ops_symbol_at ON operations (symbol, at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_task_execute ON quant_run_task_runs (task_id, execute_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON quant_run_tasks (status, updated_at)")


def _migrate_quant_task_status_semantics(c: _ConnWrapper) -> None:
    """
    任务状态语义升级：
    - 历史 completed 统一转为 paused
    - 仅手动停止使用 stopped
    """
    now = datetime.now().isoformat()
    c.execute(
        """
        UPDATE quant_run_tasks
        SET status = ?, finished_at = NULL, updated_at = ?
        WHERE status = ?
        """,
        ("paused", now, "completed"),
    )


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('formula', 'code', 'preset')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    executed_at TEXT NOT NULL,
    budget REAL NOT NULL,
    top_n INTEGER NOT NULL,
    orders_json TEXT NOT NULL,
    release_id INTEGER,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    at TEXT NOT NULL,
    remark TEXT,
    name TEXT NOT NULL DEFAULT '',
    commission REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    slippage_bps REAL NOT NULL DEFAULT 0,
    fee_schedule_version TEXT NOT NULL DEFAULT '',
    operation_uid TEXT,
    target_weight_contract_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL DEFAULT 0,
    high_water_mark REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO account (id, balance, high_water_mark) VALUES (1, 200000.0, 200000.0);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    rebalance_freq TEXT NOT NULL,
    top_n INTEGER NOT NULL,
    benchmark_symbol TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    data_quality_json TEXT,
    trade_constraints_json TEXT,
    model_version_id INTEGER,
    model_version TEXT,
    equity_json TEXT NOT NULL,
    benchmark_json TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);
CREATE TABLE IF NOT EXISTS quant_run_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    strategy_id INTEGER NOT NULL,
    top_n INTEGER NOT NULL,
    budget_ratio REAL NOT NULL,
    exchange_group TEXT NOT NULL DEFAULT 'HS',
    start_date TEXT,
    max_steps INTEGER NOT NULL DEFAULT 5,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    processed_steps INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 0,
    last_signal_date TEXT,
    last_execute_date TEXT,
    last_message TEXT,
    error TEXT,
    replay_mode TEXT NOT NULL DEFAULT 'pragmatic_recover',
    replay_status TEXT NOT NULL DEFAULT '',
    replay_gap_days INTEGER NOT NULL DEFAULT 0,
    replay_current_execute_date TEXT,
    replay_target_execute_date TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quant_run_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    signal_date TEXT NOT NULL,
    execute_date TEXT NOT NULL,
    sold_count INTEGER NOT NULL,
    bought_count INTEGER NOT NULL,
    total_buy_cost REAL NOT NULL,
    execution_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_task_signal ON quant_run_task_runs (task_id, signal_date);
CREATE TABLE IF NOT EXISTS quant_replay_waterline (
    strategy_id INTEGER NOT NULL,
    exchange_group TEXT NOT NULL DEFAULT 'HS',
    last_successful_signal_date TEXT,
    last_successful_execute_date TEXT,
    last_run_mode TEXT NOT NULL DEFAULT 'pragmatic_recover',
    last_run_status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, exchange_group)
);
CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    config_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_version ON model_versions (model_key, version);
CREATE TABLE IF NOT EXISTS candidate_lifecycle_records (
    model_version_id INTEGER PRIMARY KEY,
    candidate_type TEXT NOT NULL,
    runtime_status TEXT NOT NULL,
    eligibility_status TEXT NOT NULL,
    change_type TEXT NOT NULL,
    root_experiment_id TEXT NOT NULL,
    parent_model_version_id INTEGER,
    supersedes_model_version_id INTEGER,
    problem_family TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    observation_contract TEXT NOT NULL,
    production_change_forbidden INTEGER NOT NULL DEFAULT 1,
    stop_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
);
CREATE TABLE IF NOT EXISTS candidate_lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    runtime_status TEXT NOT NULL,
    eligibility_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_runtime
ON candidate_lifecycle_records(runtime_status,candidate_type);
CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_events_version
ON candidate_lifecycle_events(model_version_id,id);
CREATE TABLE IF NOT EXISTS candidate_simulation_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version_id INTEGER NOT NULL,
    contract_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    latest_cash REAL NOT NULL,
    latest_equity REAL NOT NULL,
    latest_trade_date TEXT NOT NULL DEFAULT '',
    data_contract_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(model_version_id,contract_hash),
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
);
CREATE TABLE IF NOT EXISTS candidate_simulation_daily_equity (
    account_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    cash REAL NOT NULL,
    stock_value REAL NOT NULL,
    total_value REAL NOT NULL,
    benchmark_value REAL,
    positions_json TEXT NOT NULL DEFAULT '{}',
    point_in_time_passed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,trade_date),
    FOREIGN KEY (account_id) REFERENCES candidate_simulation_accounts(id)
);
CREATE TABLE IF NOT EXISTS candidate_simulation_decisions (
    account_id INTEGER NOT NULL,
    signal_date TEXT NOT NULL,
    execute_date TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,signal_date),
    FOREIGN KEY (account_id) REFERENCES candidate_simulation_accounts(id)
);
CREATE TABLE IF NOT EXISTS candidate_simulation_operations (
    account_id INTEGER NOT NULL,
    operation_uid TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    operation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,operation_uid),
    FOREIGN KEY (account_id) REFERENCES candidate_simulation_accounts(id)
);
CREATE TABLE IF NOT EXISTS candidate_simulation_blocked_events (
    account_id INTEGER NOT NULL,
    event_uid TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,event_uid),
    FOREIGN KEY (account_id) REFERENCES candidate_simulation_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_sim_equity_date
ON candidate_simulation_daily_equity(trade_date,account_id);
CREATE INDEX IF NOT EXISTS idx_candidate_sim_operations_date
ON candidate_simulation_operations(trade_date,account_id);
CREATE TABLE IF NOT EXISTS model_train_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    model_version_id INTEGER,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
);
CREATE TABLE IF NOT EXISTS evolution_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    priority INTEGER NOT NULL DEFAULT 100,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    next_run_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evolution_tasks_status_next
ON evolution_tasks(status, next_run_at, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_evolution_tasks_type_id
ON evolution_tasks(task_type, id DESC);
CREATE TABLE IF NOT EXISTS strategy_shadow_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version_id INTEGER NOT NULL,
    champion_version_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    candidate_return REAL NOT NULL DEFAULT 0,
    champion_return REAL NOT NULL DEFAULT 0,
    excess_delta REAL NOT NULL DEFAULT 0,
    candidate_drawdown REAL NOT NULL DEFAULT 0,
    champion_drawdown REAL NOT NULL DEFAULT 0,
    point_in_time_passed INTEGER NOT NULL DEFAULT 0,
    behavior_passed INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id),
    FOREIGN KEY (champion_version_id) REFERENCES model_versions(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_shadow_observation
ON strategy_shadow_observations(model_version_id, trade_date);
CREATE TABLE IF NOT EXISTS strategy_promotion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    from_champion_version_id INTEGER,
    to_champion_version_id INTEGER,
    candidate_model_version_id INTEGER,
    reason_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_promotion_events_created
ON strategy_promotion_events(created_at);
CREATE TABLE IF NOT EXISTS factor_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_key TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    name_cn TEXT NOT NULL,
    category TEXT NOT NULL,
    formula_json TEXT NOT NULL DEFAULT '{}',
    inputs_json TEXT NOT NULL DEFAULT '[]',
    direction INTEGER NOT NULL DEFAULT 1,
    window_sessions INTEGER NOT NULL DEFAULT 1,
    available_time_rule TEXT NOT NULL,
    missing_policy TEXT NOT NULL,
    leakage_status TEXT NOT NULL DEFAULT 'passed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_definition_version
ON factor_definitions(factor_key, version);
CREATE INDEX IF NOT EXISTS idx_factor_definition_status
ON factor_definitions(status, factor_key);
CREATE TABLE IF NOT EXISTS factor_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_definition_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    raw_value REAL,
    standardized_value REAL,
    available_time TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_value
ON factor_values(factor_definition_id, trade_date, ts_code);
CREATE INDEX IF NOT EXISTS idx_factor_values_date
ON factor_values(trade_date, factor_definition_id);
CREATE TABLE IF NOT EXISTS factor_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_definition_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    information_coefficient REAL,
    rank_ic REAL,
    top_bottom_return REAL,
    turnover REAL,
    coverage REAL,
    cost_after_incremental_oos REAL,
    market_state_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
);
CREATE INDEX IF NOT EXISTS idx_factor_evaluations_factor
ON factor_evaluations(factor_definition_id, end_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_evaluation_window
ON factor_evaluations(factor_definition_id, start_date, end_date);
CREATE TABLE IF NOT EXISTS factor_version_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_definition_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reason_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
);
CREATE TABLE IF NOT EXISTS system_components (
    component_key TEXT PRIMARY KEY,
    component_type TEXT NOT NULL,
    name TEXT NOT NULL,
    decision_critical INTEGER NOT NULL DEFAULT 0,
    native_registry TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_component_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_key TEXT NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    native_table TEXT NOT NULL DEFAULT '',
    native_id INTEGER,
    git_commit TEXT NOT NULL DEFAULT '',
    artifact_uri TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'registered',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (component_key) REFERENCES system_components(component_key),
    UNIQUE(component_key,version,content_hash)
);
CREATE TABLE IF NOT EXISTS system_component_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_version_id INTEGER NOT NULL,
    dependency_component_key TEXT NOT NULL,
    constraint_type TEXT NOT NULL,
    constraint_json TEXT NOT NULL DEFAULT '{}',
    required INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (component_version_id) REFERENCES system_component_versions(id)
);
CREATE TABLE IF NOT EXISTS system_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    git_commit TEXT NOT NULL DEFAULT '',
    git_dirty INTEGER NOT NULL DEFAULT 0,
    sim_db_revision TEXT NOT NULL DEFAULT '',
    data_db_revision TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS system_release_components (
    release_id INTEGER NOT NULL,
    component_key TEXT NOT NULL,
    component_version_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'required',
    required INTEGER NOT NULL DEFAULT 1,
    frozen_payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (release_id,component_key),
    FOREIGN KEY (release_id) REFERENCES system_releases(id),
    FOREIGN KEY (component_version_id) REFERENCES system_component_versions(id)
);
CREATE TABLE IF NOT EXISTS system_release_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (release_id) REFERENCES system_releases(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_system_release_one_active
ON system_releases(status) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_system_release_events_release
ON system_release_events(release_id,created_at);
"""

_MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS strategies (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS executions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    strategy_id BIGINT NOT NULL,
    executed_at VARCHAR(64) NOT NULL,
    budget DOUBLE NOT NULL,
    top_n INT NOT NULL,
    orders_json TEXT NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS operations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    execution_id BIGINT NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,
    price DOUBLE NOT NULL,
    quantity INT NOT NULL,
    at VARCHAR(64) NOT NULL,
    remark TEXT,
    name VARCHAR(255) NOT NULL DEFAULT '',
    commission DOUBLE NOT NULL DEFAULT 0,
    transfer_fee DOUBLE NOT NULL DEFAULT 0,
    stamp_tax DOUBLE NOT NULL DEFAULT 0,
    slippage_bps DOUBLE NOT NULL DEFAULT 0,
    fee_schedule_version VARCHAR(64) NOT NULL DEFAULT '',
    FOREIGN KEY (execution_id) REFERENCES executions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS account (
    id INT PRIMARY KEY CHECK (id = 1),
    balance DOUBLE NOT NULL DEFAULT 0,
    high_water_mark DOUBLE NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT IGNORE INTO account (id, balance, high_water_mark) VALUES (1, 100000.0, 100000.0);
CREATE TABLE IF NOT EXISTS positions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL UNIQUE,
    quantity INT NOT NULL,
    price DOUBLE NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS backtests (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    strategy_id BIGINT NOT NULL,
    strategy_name VARCHAR(255) NOT NULL,
    started_at VARCHAR(64) NOT NULL,
    start_date VARCHAR(16) NOT NULL,
    end_date VARCHAR(16) NOT NULL,
    initial_cash DOUBLE NOT NULL,
    rebalance_freq VARCHAR(32) NOT NULL,
    top_n INT NOT NULL,
    benchmark_symbol VARCHAR(32) NOT NULL,
    metrics_json LONGTEXT NOT NULL,
    warnings_json LONGTEXT NOT NULL,
    data_quality_json LONGTEXT NULL,
    trade_constraints_json LONGTEXT NULL,
    model_version_id BIGINT NULL,
    model_version VARCHAR(64) NULL,
    equity_json LONGTEXT NOT NULL,
    benchmark_json LONGTEXT NOT NULL,
    trades_json LONGTEXT NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS quant_run_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    strategy_id BIGINT NOT NULL,
    top_n INT NOT NULL,
    budget_ratio DOUBLE NOT NULL,
    exchange_group VARCHAR(16) NOT NULL DEFAULT 'HS',
    start_date VARCHAR(16) NULL,
    max_steps INT NOT NULL DEFAULT 5,
    stop_requested TINYINT NOT NULL DEFAULT 0,
    processed_steps INT NOT NULL DEFAULT 0,
    total_steps INT NOT NULL DEFAULT 0,
    last_signal_date VARCHAR(16) NULL,
    last_execute_date VARCHAR(16) NULL,
    last_message TEXT NULL,
    error TEXT NULL,
    replay_mode VARCHAR(32) NOT NULL DEFAULT 'pragmatic_recover',
    replay_status VARCHAR(64) NOT NULL DEFAULT '',
    replay_gap_days INT NOT NULL DEFAULT 0,
    replay_current_execute_date VARCHAR(16) NULL,
    replay_target_execute_date VARCHAR(16) NULL,
    started_at VARCHAR(64) NULL,
    finished_at VARCHAR(64) NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS quant_run_task_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT NOT NULL,
    signal_date VARCHAR(16) NOT NULL,
    execute_date VARCHAR(16) NOT NULL,
    sold_count INT NOT NULL,
    bought_count INT NOT NULL,
    total_buy_cost DOUBLE NOT NULL,
    execution_id BIGINT NULL,
    message TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (task_id) REFERENCES quant_run_tasks(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS quant_replay_waterline (
    strategy_id BIGINT NOT NULL,
    exchange_group VARCHAR(16) NOT NULL DEFAULT 'HS',
    last_successful_signal_date VARCHAR(16) NULL,
    last_successful_execute_date VARCHAR(16) NULL,
    last_run_mode VARCHAR(32) NOT NULL DEFAULT 'pragmatic_recover',
    last_run_status VARCHAR(32) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (strategy_id, exchange_group)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS model_versions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    model_key VARCHAR(64) NOT NULL,
    version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    artifact_path TEXT NOT NULL,
    config_json LONGTEXT NOT NULL,
    metrics_json LONGTEXT NOT NULL,
    validation_json LONGTEXT NOT NULL,
    is_active TINYINT NOT NULL DEFAULT 0,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_model_version (model_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS model_train_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    phase VARCHAR(64) NOT NULL DEFAULT '',
    progress DOUBLE NOT NULL DEFAULT 0,
    message TEXT NOT NULL,
    model_version_id BIGINT NULL,
    metrics_json LONGTEXT NOT NULL,
    error TEXT NULL,
    started_at VARCHAR(64) NULL,
    finished_at VARCHAR(64) NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS evolution_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_type VARCHAR(64) NOT NULL,
    dedupe_key VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    phase VARCHAR(64) NOT NULL DEFAULT '',
    progress DOUBLE NOT NULL DEFAULT 0,
    message TEXT NOT NULL,
    payload_json LONGTEXT NOT NULL,
    checkpoint_json LONGTEXT NOT NULL,
    result_json LONGTEXT NOT NULL,
    error TEXT NULL,
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    priority INT NOT NULL DEFAULT 100,
    stop_requested TINYINT NOT NULL DEFAULT 0,
    deadline_at VARCHAR(64) NULL,
    lease_owner VARCHAR(128) NULL,
    lease_expires_at VARCHAR(64) NULL,
    heartbeat_at VARCHAR(64) NULL,
    next_run_at VARCHAR(64) NULL,
    started_at VARCHAR(64) NULL,
    finished_at VARCHAR(64) NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    INDEX idx_evolution_tasks_status_next (status, next_run_at, priority, created_at),
    INDEX idx_evolution_tasks_type_id (task_type, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS strategy_shadow_observations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    model_version_id BIGINT NOT NULL,
    champion_version_id BIGINT NOT NULL,
    trade_date VARCHAR(16) NOT NULL,
    candidate_return DOUBLE NOT NULL DEFAULT 0,
    champion_return DOUBLE NOT NULL DEFAULT 0,
    excess_delta DOUBLE NOT NULL DEFAULT 0,
    candidate_drawdown DOUBLE NOT NULL DEFAULT 0,
    champion_drawdown DOUBLE NOT NULL DEFAULT 0,
    point_in_time_passed TINYINT NOT NULL DEFAULT 0,
    behavior_passed TINYINT NOT NULL DEFAULT 0,
    metrics_json LONGTEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_strategy_shadow_observation (model_version_id, trade_date),
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id),
    FOREIGN KEY (champion_version_id) REFERENCES model_versions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS strategy_promotion_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(32) NOT NULL,
    from_champion_version_id BIGINT,
    to_champion_version_id BIGINT,
    candidate_model_version_id BIGINT,
    reason_json LONGTEXT NOT NULL,
    evidence_json LONGTEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_strategy_promotion_events_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS factor_definitions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    factor_key VARCHAR(128) NOT NULL,
    version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    name_cn VARCHAR(255) NOT NULL,
    category VARCHAR(64) NOT NULL,
    formula_json LONGTEXT NOT NULL,
    inputs_json LONGTEXT NOT NULL,
    direction INT NOT NULL DEFAULT 1,
    window_sessions INT NOT NULL DEFAULT 1,
    available_time_rule VARCHAR(128) NOT NULL,
    missing_policy VARCHAR(128) NOT NULL,
    leakage_status VARCHAR(32) NOT NULL DEFAULT 'passed',
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_factor_definition_version (factor_key, version),
    INDEX idx_factor_definition_status (status, factor_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS factor_values (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    factor_definition_id BIGINT NOT NULL,
    trade_date VARCHAR(16) NOT NULL,
    ts_code VARCHAR(32) NOT NULL,
    raw_value DOUBLE,
    standardized_value DOUBLE,
    available_time VARCHAR(64) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_factor_value (factor_definition_id, trade_date, ts_code),
    INDEX idx_factor_values_date (trade_date, factor_definition_id),
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS factor_evaluations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    factor_definition_id BIGINT NOT NULL,
    start_date VARCHAR(16) NOT NULL,
    end_date VARCHAR(16) NOT NULL,
    sample_count INT NOT NULL DEFAULT 0,
    information_coefficient DOUBLE,
    rank_ic DOUBLE,
    top_bottom_return DOUBLE,
    turnover DOUBLE,
    coverage DOUBLE,
    cost_after_incremental_oos DOUBLE,
    market_state_json LONGTEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_factor_evaluation_window (factor_definition_id, start_date, end_date),
    INDEX idx_factor_evaluations_factor (factor_definition_id, end_date),
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS factor_version_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    factor_definition_id BIGINT NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    reason_json LONGTEXT NOT NULL,
    evidence_json LONGTEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    FOREIGN KEY (factor_definition_id) REFERENCES factor_definitions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_db() -> None:
    """Create tables and seed default strategy if missing."""
    url = (settings.database_url or "").strip()
    if _is_postgres():
        logger.info("数据库: PostgreSQL")
    elif _is_mysql():
        kw = _mysql_connect_kwargs()
        logger.info(
            "数据库: MySQL %s:%s/%s (user=%s)",
            kw["host"], kw["port"], kw["database"], kw["user"],
        )
    else:
        raise RuntimeError("DATABASE_URL 必须指向 PostgreSQL；SQLite 已禁用")

    try:
        with _conn() as c:
            logger.info("数据库连接成功，正在建表...")
            if _is_postgres():
                logger.info("数据库: PostgreSQL；校验并补齐运行 schema")
                postgres_ddl = _SQLITE_DDL.replace(
                    "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
                ).replace(
                    "INSERT OR IGNORE INTO account", "INSERT INTO account"
                ).replace(
                    "VALUES (1, 200000.0, 200000.0);",
                    "VALUES (1, 200000.0, 200000.0) ON CONFLICT (id) DO NOTHING;",
                )
                c.executescript(postgres_ddl)
                c.executescript("""
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS name VARCHAR(255) NOT NULL DEFAULT '';
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS bought_at VARCHAR(32) NULL;
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS name VARCHAR(255) NOT NULL DEFAULT '';
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS commission DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS transfer_fee DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS stamp_tax DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS slippage_bps DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS fee_schedule_version VARCHAR(64) NOT NULL DEFAULT '';
                ALTER TABLE operations ADD COLUMN IF NOT EXISTS operation_uid VARCHAR(64);
                ALTER TABLE operations ALTER COLUMN execution_id DROP NOT NULL;
                ALTER TABLE evolution_tasks ADD COLUMN IF NOT EXISTS stop_requested INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE evolution_tasks ADD COLUMN IF NOT EXISTS deadline_at VARCHAR(64);
                ALTER TABLE quant_run_task_runs ADD COLUMN IF NOT EXISTS detail_json TEXT NULL;
                CREATE TABLE IF NOT EXISTS quant_run_task_meta (
                    task_id BIGINT PRIMARY KEY REFERENCES quant_run_tasks(id),
                    strategy_snapshot_json TEXT NOT NULL,
                    risk_config_json TEXT NOT NULL,
                    created_at VARCHAR(64) NOT NULL,
                    updated_at VARCHAR(64) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_run_task_health (
                    task_id BIGINT PRIMARY KEY REFERENCES quant_run_tasks(id),
                    heartbeat_at VARCHAR(64),
                    last_step_latency_ms BIGINT NOT NULL DEFAULT 0,
                    avg_step_latency_ms BIGINT NOT NULL DEFAULT 0,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    risk_hit_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_pause_count INTEGER NOT NULL DEFAULT 0,
                    last_risk_code VARCHAR(64) NOT NULL DEFAULT '',
                    updated_at VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ops_execution_at ON operations (execution_id, at);
                UPDATE operations SET operation_uid=MD5('legacy:' || id::text) WHERE operation_uid IS NULL OR operation_uid='';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_uid ON operations(operation_uid);
                CREATE INDEX IF NOT EXISTS idx_ops_symbol_at ON operations (symbol, at);
                CREATE INDEX IF NOT EXISTS idx_task_runs_task_execute ON quant_run_task_runs (task_id, execute_date);
                CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON quant_run_tasks (status, updated_at);
                WITH ranked AS (
                    SELECT id,ROW_NUMBER() OVER (PARTITION BY model_key ORDER BY id DESC) AS rn
                    FROM model_versions WHERE is_active=1
                )
                UPDATE model_versions SET is_active=0,status='superseded'
                WHERE id IN (SELECT id FROM ranked WHERE rn>1);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_model_versions_one_active
                ON model_versions(model_key) WHERE is_active=1;
                """)
                c.execute("ALTER TABLE account ADD COLUMN IF NOT EXISTS high_water_mark DOUBLE PRECISION NOT NULL DEFAULT 0")
                c.execute("UPDATE account SET high_water_mark = GREATEST(high_water_mark, balance) WHERE id = 1")
                # A previous SQLite/MySQL migration can leave PostgreSQL
                # serial sequences behind the copied ids. Reconcile them at
                # startup so the first new backtest/task cannot collide with
                # an existing primary key.
                for table in (
                    "backtests", "executions", "operations", "quant_run_tasks",
                    "quant_run_task_runs", "model_train_runs", "evolution_tasks",
                    "strategy_shadow_observations", "strategy_promotion_events",
                    "factor_definitions", "factor_values", "factor_evaluations",
                    "factor_version_events",
                ):
                    c.execute(
                        "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM " + table + "), 1), EXISTS (SELECT 1 FROM " + table + "))",
                        (table,),
                    )
                c.execute("""CREATE TABLE IF NOT EXISTS quant_performance_reports (
                    id BIGSERIAL PRIMARY KEY, task_id BIGINT NULL, period_type VARCHAR(16) NOT NULL,
                    scope_key VARCHAR(64) NOT NULL DEFAULT 'global',
                    period VARCHAR(32) NOT NULL, payload_json TEXT NOT NULL, created_at VARCHAR(64) NOT NULL,
                    UNIQUE(scope_key, period_type, period))""")
                c.execute(
                    "ALTER TABLE quant_performance_reports ADD COLUMN IF NOT EXISTS scope_key VARCHAR(64) NOT NULL DEFAULT 'global'"
                )
                c.execute(
                    """UPDATE quant_performance_reports
                       SET scope_key=CASE WHEN task_id IS NULL THEN 'global' ELSE 'task:' || task_id::text END
                       WHERE scope_key IS NULL OR scope_key='' OR (task_id IS NOT NULL AND scope_key='global')"""
                )
                # Performance rows are derived snapshots.  Keep the latest
                # copy from the legacy NULL-task uniqueness bug, then enforce
                # one durable row per report scope and period.
                c.execute(
                    """DELETE FROM quant_performance_reports older
                       USING quant_performance_reports newer
                       WHERE older.scope_key=newer.scope_key
                         AND older.period_type=newer.period_type
                         AND older.period=newer.period
                         AND older.id<newer.id"""
                )
                c.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS uq_quant_performance_scope_period
                       ON quant_performance_reports(scope_key,period_type,period)"""
                )
                # Factor evaluations are future observations. Retries must not
                # turn one date pair into multiple samples for a promotion gate.
                c.execute(
                    """DELETE FROM factor_evaluations older
                       USING factor_evaluations newer
                       WHERE older.factor_definition_id=newer.factor_definition_id
                         AND older.start_date=newer.start_date
                         AND older.end_date=newer.end_date
                         AND older.id<newer.id"""
                )
                c.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_evaluation_window
                       ON factor_evaluations(factor_definition_id,start_date,end_date)"""
                )
                c.executescript("""
                CREATE TABLE IF NOT EXISTS investment_theses (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, title VARCHAR(255) NOT NULL,
                    thesis_type VARCHAR(64) NOT NULL DEFAULT 'quality_growth', core_thesis TEXT NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'watch', confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    next_review_at VARCHAR(64), created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_thesis_symbol_status ON investment_theses(ts_code,status);
                CREATE TABLE IF NOT EXISTS thesis_hypotheses (
                    id BIGSERIAL PRIMARY KEY, thesis_id BIGINT NOT NULL REFERENCES investment_theses(id) ON DELETE CASCADE,
                    statement TEXT NOT NULL, metric VARCHAR(128) NOT NULL DEFAULT '', necessary BOOLEAN NOT NULL DEFAULT FALSE,
                    expected_json TEXT NOT NULL DEFAULT '{}', status VARCHAR(32) NOT NULL DEFAULT 'unverified',
                    evidence_json TEXT NOT NULL DEFAULT '[]', last_checked_at VARCHAR(64), created_at VARCHAR(64) NOT NULL,
                    updated_at VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hypothesis_thesis ON thesis_hypotheses(thesis_id,status);
                CREATE TABLE IF NOT EXISTS research_score_snapshots (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, as_of_time VARCHAR(64) NOT NULL,
                    model_version VARCHAR(64) NOT NULL, dimensions_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0, created_at VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_snapshot_symbol_time ON research_score_snapshots(ts_code,as_of_time);
                CREATE TABLE IF NOT EXISTS valuation_snapshots (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, as_of_time VARCHAR(64) NOT NULL,
                    method VARCHAR(64) NOT NULL, bear_value DOUBLE PRECISION, base_value DOUBLE PRECISION,
                    bull_value DOUBLE PRECISION, current_price DOUBLE PRECISION, assumptions_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, created_at VARCHAR(64) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS earnings_forecast_baselines (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, target_period VARCHAR(8) NOT NULL,
                    model_version VARCHAR(64) NOT NULL, revenue_forecast DOUBLE PRECISION,
                    profit_forecast DOUBLE PRECISION, confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    inputs_json TEXT NOT NULL, available_time VARCHAR(64) NOT NULL, created_at VARCHAR(64) NOT NULL,
                    UNIQUE(ts_code,target_period,model_version)
                );
                CREATE TABLE IF NOT EXISTS stock_pool_memberships (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, pool_type VARCHAR(32) NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', status VARCHAR(32) NOT NULL DEFAULT 'active',
                    entered_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                    UNIQUE(ts_code,pool_type)
                );
                CREATE TABLE IF NOT EXISTS decision_snapshots (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL, decision_type VARCHAR(32) NOT NULL,
                    decision_time VARCHAR(64) NOT NULL, price DOUBLE PRECISION, thesis_id BIGINT,
                    execution_id BIGINT, operation_id BIGINT,
                    data_as_of_time VARCHAR(64) NOT NULL, model_version VARCHAR(64) NOT NULL,
                    market_state_json TEXT NOT NULL, research_state_json TEXT NOT NULL,
                    valuation_json TEXT NOT NULL, portfolio_context_json TEXT NOT NULL,
                    target_weight_contract_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL, invalidation_conditions_json TEXT NOT NULL,
                    evidence_hash VARCHAR(128) NOT NULL DEFAULT '', evidence_schema_version VARCHAR(64) NOT NULL DEFAULT 'evidence_freeze_v1',
                    created_at VARCHAR(64) NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_symbol_time ON decision_snapshots(ts_code,decision_time);
                CREATE INDEX IF NOT EXISTS idx_decision_time ON decision_snapshots(decision_time DESC,id DESC);
                CREATE TABLE IF NOT EXISTS review_records (
                    id BIGSERIAL PRIMARY KEY, decision_snapshot_id BIGINT REFERENCES decision_snapshots(id),
                    review_time VARCHAR(64) NOT NULL, outcome_json TEXT NOT NULL, attribution_json TEXT NOT NULL,
                    error_type VARCHAR(64), lessons TEXT NOT NULL DEFAULT '', created_at VARCHAR(64) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_states (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL UNIQUE,
                    state VARCHAR(32) NOT NULL, thesis_id BIGINT, reason TEXT NOT NULL,
                    changed_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_state_history (
                    id BIGSERIAL PRIMARY KEY, ts_code VARCHAR(16) NOT NULL,
                    from_state VARCHAR(32), to_state VARCHAR(32) NOT NULL,
                    reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    rule_version VARCHAR(64) NOT NULL DEFAULT '',
                    model_version VARCHAR(64) NOT NULL DEFAULT '',
                    automatic_decision_json TEXT NOT NULL DEFAULT '{}',
                    intervention_type VARCHAR(32) NOT NULL DEFAULT 'none',
                    intervention_actor VARCHAR(128) NOT NULL DEFAULT '',
                    changed_at VARCHAR(64) NOT NULL
                );
                """)
            elif _is_mysql():
                c.executescript(_MYSQL_DDL)
            else:
                c.executescript(_SQLITE_DDL)
            if not _is_postgres():
                _migrate_positions_operations_name(c)
                _migrate_positions_bought_at(c)
                _migrate_backtests_data_quality(c)
                _migrate_backtests_model_version(c)
                _migrate_operations_execution_id_nullable(c)
                _migrate_quant_replay_schema(c)
                _migrate_quant_task_perf_schema(c)
                _migrate_quant_task_status_semantics(c)
                _migrate_quant_run_task_detail_json(c)
                _migrate_target_weight_contracts(c)
            else:
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS evidence_hash VARCHAR(128) NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS evidence_schema_version VARCHAR(64) NOT NULL DEFAULT 'evidence_freeze_v1'")
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS execution_id BIGINT")
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS operation_id BIGINT")
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS target_weight_contract_json TEXT NOT NULL DEFAULT '{}'")
                c.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS release_id BIGINT")
                c.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS release_id BIGINT")
                c.execute("ALTER TABLE operations ADD COLUMN IF NOT EXISTS target_weight_contract_json TEXT NOT NULL DEFAULT '{}'")
                c.execute("CREATE INDEX IF NOT EXISTS idx_decision_execution ON decision_snapshots(execution_id,operation_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_decision_time ON decision_snapshots(decision_time DESC,id DESC)")
                c.execute("ALTER TABLE position_state_history ADD COLUMN IF NOT EXISTS rule_version VARCHAR(64) NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE position_state_history ADD COLUMN IF NOT EXISTS model_version VARCHAR(64) NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE position_state_history ADD COLUMN IF NOT EXISTS automatic_decision_json TEXT NOT NULL DEFAULT '{}'")
                c.execute("ALTER TABLE position_state_history ADD COLUMN IF NOT EXISTS intervention_type VARCHAR(32) NOT NULL DEFAULT 'none'")
                c.execute("ALTER TABLE position_state_history ADD COLUMN IF NOT EXISTS intervention_actor VARCHAR(128) NOT NULL DEFAULT ''")
            cur = c.execute("SELECT id FROM strategies WHERE name = ?", (_DEFAULT_STRATEGY_NAME,))
            if cur.fetchone() is None:
                now = datetime.now().isoformat()
                legacy = c.execute("SELECT id FROM strategies WHERE name = ? ORDER BY id LIMIT 1", (_LEGACY_DEFAULT_STRATEGY_NAME,)).fetchone()
                if legacy:
                    c.execute(
                        "UPDATE strategies SET name=?, type='formula', content=?, updated_at=? WHERE id=?",
                        (_DEFAULT_STRATEGY_NAME, json.dumps(_DEFAULT_STRATEGY_CONTENT, ensure_ascii=False), now, int(legacy["id"])),
                    )
                else:
                    c.execute(
                        """INSERT INTO strategies (name, type, content, created_at, updated_at)
                           VALUES (?, 'formula', ?, ?, ?)""",
                        (_DEFAULT_STRATEGY_NAME, json.dumps(_DEFAULT_STRATEGY_CONTENT, ensure_ascii=False), now, now),
                    )
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.exception("数据库初始化失败: %s", e)
        raise


def database_health() -> dict[str, Any]:
    """Run a cheap readiness probe against the configured primary database."""
    try:
        with _conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "engine": "postgresql" if _is_postgres() else "mysql",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:200]}


def get_default_strategy_id() -> Optional[int]:
    """Return the ALIS Champion selection strategy, with legacy fallback."""
    with _conn() as c:
        cur = c.execute(
            "SELECT id FROM strategies WHERE name IN (?, ?) ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, id LIMIT 1",
            (_DEFAULT_STRATEGY_NAME, _LEGACY_DEFAULT_STRATEGY_NAME, _DEFAULT_STRATEGY_NAME),
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None


def list_strategies() -> List[Dict[str, Any]]:
    """Return all strategies (id, name, type, content, created_at, updated_at)."""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, name, type, content, created_at, updated_at FROM strategies ORDER BY id"
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        content = r["content"]
        try:
            content = json.loads(content) if isinstance(content, str) else content
        except (json.JSONDecodeError, TypeError):
            pass
        out.append({
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "content": content,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def get_strategy(strategy_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy by id, or None."""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, name, type, content, created_at, updated_at FROM strategies WHERE id = ?",
            (strategy_id,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    content = r["content"]
    try:
        content = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "id": r["id"],
        "name": r["name"],
        "type": r["type"],
        "content": content,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def create_strategy(name: str, type_: str, content: Any) -> Dict[str, Any]:
    """Insert strategy; content will be JSON-serialized if dict/list."""
    now = datetime.now().isoformat()
    payload = json.dumps(content) if isinstance(content, (dict, list)) else str(content)
    with _conn() as c:
        c.execute(
            """INSERT INTO strategies (name, type, content, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, type_, payload, now, now),
        )
        sid = c.last_insert_id()
    return get_strategy(sid) or {}


def update_strategy(strategy_id: int, name: Optional[str], type_: Optional[str], content: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Update strategy by id. None fields are left unchanged."""
    current = get_strategy(strategy_id)
    if not current:
        return None
    now = datetime.now().isoformat()
    name = name if name is not None else current["name"]
    type_ = type_ if type_ is not None else current["type"]
    if content is not None:
        payload = json.dumps(content) if isinstance(content, (dict, list)) else str(content)
    else:
        payload = current["content"]
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
    with _conn() as c:
        c.execute(
            "UPDATE strategies SET name = ?, type = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, type_, payload, now, strategy_id),
        )
    return get_strategy(strategy_id)


def delete_strategy(strategy_id: int) -> bool:
    """Delete strategy. Returns False if id is default or not found."""
    default_id = get_default_strategy_id()
    if default_id is not None and strategy_id == default_id:
        return False
    with _conn() as c:
        cur = c.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
    return cur.rowcount > 0


def _operation_uid(execution_id: int | None, symbol: Any, side: Any, at: Any, quantity: Any, price: Any) -> str:
    payload = "|".join((str(execution_id or 0), str(symbol or "").strip().upper(), str(side or "").strip().upper(), str(at or ""), str(int(quantity or 0)), f"{float(price or 0):.8f}"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_operation_fields(order: Dict[str, Any], *, default_side: str = "BUY") -> dict[str, Any]:
    """Normalize a persisted operation without silently coercing invalid trades."""
    symbol = str(order.get("symbol") or "").strip().upper()
    side = str(order.get("side") or default_side).strip().upper()
    quantity_raw = order.get("quantity")
    try:
        quantity_value = float(quantity_raw)
        quantity = int(quantity_value)
        price = float(order.get("price"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_operation") from None
    if (
        not symbol
        or side not in {"BUY", "SELL"}
        or not math.isfinite(quantity_value)
        or quantity_value != quantity
        or quantity <= 0
        or not math.isfinite(price)
        or price <= 0
    ):
        raise ValueError("invalid_operation")
    fees: dict[str, float] = {}
    for key in ("commission", "transfer_fee", "stamp_tax"):
        try:
            value = float(order.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid_operation_fees") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid_operation_fees")
        fees[key] = value
    try:
        slippage_bps = float(order.get("slippage_bps") or 0)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_operation_slippage") from None
    if not math.isfinite(slippage_bps):
        raise ValueError("invalid_operation_slippage")
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "commission": fees["commission"],
        "transfer_fee": fees["transfer_fee"],
        "stamp_tax": fees["stamp_tax"],
        "slippage_bps": slippage_bps,
    }


def create_execution(strategy_id: int, executed_at: str, budget: float, top_n: int, orders: List[Dict[str, Any]], *, release_id: int | None = None) -> int:
    """Insert execution and operations (buy orders). Returns execution id."""
    try:
        normalized_budget = float(budget)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_execution_budget") from None
    if not math.isfinite(normalized_budget) or normalized_budget < 0:
        raise ValueError("invalid_execution_budget")
    normalized_orders = [
        {**dict(order), **_validated_operation_fields(order)}
        for order in orders
    ]
    with _conn() as c:
        c.execute(
            """INSERT INTO executions (strategy_id, executed_at, budget, top_n, orders_json,release_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (strategy_id, executed_at, normalized_budget, top_n, json.dumps(normalized_orders), int(release_id) if release_id else None),
        )
        eid = c.last_insert_id()
        for o in normalized_orders:
            operation_uid = _operation_uid(eid, o.get("symbol", ""), o.get("side", "BUY"), executed_at, o.get("quantity", 0), o.get("price", 0))
            c.execute(
                """INSERT INTO operations
                   (execution_id, symbol, side, price, quantity, at, remark, name,
                    commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version, operation_uid, target_weight_contract_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    eid,
                    o.get("symbol", ""),
                    o.get("side", "BUY"),
                    float(o.get("price", 0)),
                    int(o.get("quantity", 0)),
                    executed_at,
                    None,
                    (o.get("name") or "").strip(),
                    float(o.get("commission", 0) or 0),
                    float(o.get("transfer_fee", 0) or 0),
                    float(o.get("stamp_tax", 0) or 0),
                    float(o.get("slippage_bps", 0) or 0),
                    str(o.get("fee_schedule_version") or FEE_SCHEDULE_VERSION),
                    operation_uid,
                    json.dumps(o.get("target_weight_contract") or {}, ensure_ascii=False),
                ),
            )
    return eid


def append_operations_to_execution(execution_id: int, executed_at: str, orders: List[Dict[str, Any]]) -> int:
    """
    向已有 execution 追加操作记录（常用于补充 SELL 关联）。
    返回成功插入条数。
    """
    eid = int(execution_id)
    if eid <= 0 or not orders:
        return 0
    normalized_orders = [
        {**dict(order), **_validated_operation_fields(order, default_side="")}
        for order in orders
    ]
    inserted = 0
    with _conn() as c:
        for o in normalized_orders:
            operation_at = str(o.get("at") or executed_at)
            operation_uid = _operation_uid(eid, o.get("symbol", ""), o.get("side", ""), operation_at, o.get("quantity", 0), o.get("price", 0))
            if c.execute("SELECT id FROM operations WHERE operation_uid=?", (operation_uid,)).fetchone():
                continue
            c.execute(
                """INSERT INTO operations
                   (execution_id, symbol, side, price, quantity, at, remark, name,
                    commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version, operation_uid, target_weight_contract_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    eid,
                    (o.get("symbol") or "").strip(),
                    str(o.get("side") or "").upper(),
                    float(o.get("price", 0) or 0),
                    int(o.get("quantity", 0) or 0),
                    operation_at,
                    o.get("remark"),
                    (o.get("name") or "").strip(),
                    float(o.get("commission", 0) or 0),
                    float(o.get("transfer_fee", 0) or 0),
                    float(o.get("stamp_tax", 0) or 0),
                    float(o.get("slippage_bps", 0) or 0),
                    str(o.get("fee_schedule_version") or FEE_SCHEDULE_VERSION),
                    operation_uid,
                    json.dumps(o.get("target_weight_contract") or {}, ensure_ascii=False),
                ),
            )
            inserted += 1
    return inserted


def create_and_apply_buy_execution(
    strategy_id: int,
    executed_at: str,
    budget: float,
    top_n: int,
    orders: List[Dict[str, Any]],
    *,
    name_by_symbol: Optional[Dict[str, str]] = None,
    release_id: int | None = None,
) -> int:
    """Atomically debit cash, persist BUY operations, and project positions."""
    buy_orders = [
        item for item in orders
        if str(item.get("side") or "BUY").upper() == "BUY"
        and int(item.get("quantity") or 0) > 0
    ]
    if not buy_orders:
        raise ValueError("buy orders are required")
    for item in buy_orders:
        symbol = str(item.get("symbol") or "").strip()
        quantity = int(item.get("quantity") or 0)
        price = float(item.get("price") or 0)
        fees = [float(item.get(key) or 0) for key in ("commission", "transfer_fee", "stamp_tax")]
        if not symbol or quantity <= 0 or not math.isfinite(price) or price <= 0:
            raise ValueError("invalid_buy_order")
        if any(not math.isfinite(fee) or fee < 0 for fee in fees):
            raise ValueError("invalid_buy_order_fees")
    total_cost = round(sum(
        float(item.get("price") or 0) * int(item.get("quantity") or 0)
        + sum(float(item.get(key) or 0) for key in ("commission", "transfer_fee", "stamp_tax"))
        for item in buy_orders
    ), 2)
    if abs(total_cost - round(float(budget or 0), 2)) > 0.05:
        raise ValueError(f"买入预算与逐笔成本不一致: budget={float(budget or 0):.2f}, ledger={total_cost:.2f}")
    names = name_by_symbol or {}
    with _conn() as c:
        _acquire_simulation_account_lock(c)
        account_row = c.execute(
            "SELECT balance FROM account WHERE id=1" + (" FOR UPDATE" if c._is_mysql else "")
        ).fetchone()
        balance = float(account_row["balance"] or 0) if account_row else 0.0
        if balance + 0.005 < total_cost:
            raise ValueError(f"账户余额不足: balance={balance:.2f}, required={total_cost:.2f}")
        c.execute(
            """INSERT INTO executions (strategy_id,executed_at,budget,top_n,orders_json,release_id)
               VALUES(?,?,?,?,?,?)""",
            (strategy_id, executed_at, total_cost, top_n, json.dumps(buy_orders), int(release_id) if release_id else None),
        )
        execution_id = c.last_insert_id()
        now = executed_at[:19] if executed_at else datetime.now().isoformat()[:19]
        for item in buy_orders:
            symbol = str(item.get("symbol") or "").strip()
            quantity = int(item.get("quantity") or 0)
            price = float(item.get("price") or 0)
            operation_uid = _operation_uid(execution_id, symbol, "BUY", executed_at, quantity, price)
            c.execute(
                """INSERT INTO operations(
                       execution_id,symbol,side,price,quantity,at,remark,name,
                       commission,transfer_fee,stamp_tax,slippage_bps,
                       fee_schedule_version,operation_uid,target_weight_contract_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id, symbol, "BUY", price, quantity, executed_at,
                    item.get("remark"), str(names.get(symbol) or item.get("name") or "").strip(),
                    float(item.get("commission") or 0), float(item.get("transfer_fee") or 0),
                    float(item.get("stamp_tax") or 0), float(item.get("slippage_bps") or 0),
                    str(item.get("fee_schedule_version") or FEE_SCHEDULE_VERSION),
                    operation_uid,
                    json.dumps(item.get("target_weight_contract") or {}, ensure_ascii=False),
                ),
            )
            _merge_position_row(
                c,
                symbol=symbol,
                quantity=quantity,
                price=price,
                updated_at=now,
                name=str(names.get(symbol) or item.get("name") or "").strip(),
                bought_at=(executed_at or now)[:10],
            )
        debit = c.execute(
            f"UPDATE account SET balance={_rounded_account_balance_expression(c, 'balance-?')} WHERE id=1 AND balance>=?",
            (total_cost, total_cost),
        )
        if int(getattr(debit, "rowcount", 1) or 0) != 1:
            raise ValueError("账户余额在并发执行中发生变化，买入已整体回滚")
    return int(execution_id)


def link_recorded_operations_to_execution(execution_id: int, orders: List[Dict[str, Any]]) -> int:
    """Attach every already-atomic SELL row to its enclosing execution.

    A partial link would leave cash/positions changed without an auditable
    execution binding.  Treat it as a transaction failure instead of silently
    returning a smaller count to the caller.
    """
    eid = int(execution_id or 0)
    if eid <= 0:
        raise ValueError("invalid_execution_id_for_operation_link")
    normalized_orders = [
        {**dict(order), **_validated_operation_fields(order, default_side="")}
        for order in orders
    ]
    linked = 0
    with _conn() as c:
        for item in normalized_orders:
            operation_at = str(item.get("at") or "")
            uid = _operation_uid(
                None, item.get("symbol"), item.get("side"), operation_at,
                item.get("quantity"), item.get("price"),
            )
            cur = c.execute(
                """UPDATE operations SET execution_id=?
                   WHERE operation_uid=? AND execution_id IS NULL""",
                (eid, uid),
            )
            linked_count = int(getattr(cur, "rowcount", 0) or 0)
            if linked_count != 1:
                raise RuntimeError("卖出成交与执行记录绑定失败，已拒绝将断链执行标记为完成")
            linked += linked_count
    return linked


def get_execution(execution_id: int) -> Optional[Dict[str, Any]]:
    """Return execution with orders parsed from JSON."""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, strategy_id, executed_at, budget, top_n, orders_json FROM executions WHERE id = ?",
            (execution_id,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    orders = []
    try:
        orders = json.loads(r["orders_json"])
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "id": r["id"],
        "strategy_id": r["strategy_id"],
        "executed_at": r["executed_at"],
        "budget": r["budget"],
        "top_n": r["top_n"],
        "orders": orders,
    }


def count_executions() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS total FROM executions").fetchone()
    return int(row["total"] or 0) if row else 0


def list_executions(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Return recent executions (newest first)."""
    n = max(1, int(limit))
    skip = max(0, int(offset))
    with _conn() as c:
        cur = c.execute(
            """SELECT id, strategy_id, executed_at, budget, top_n, orders_json
               FROM executions ORDER BY id DESC LIMIT ? OFFSET ?""",
            (n, skip),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        try:
            orders = json.loads(r["orders_json"])
        except (json.JSONDecodeError, TypeError):
            orders = []
        out.append({
            "id": r["id"],
            "strategy_id": r["strategy_id"],
            "executed_at": r["executed_at"],
            "budget": r["budget"],
            "top_n": r["top_n"],
            "orders": orders,
        })
    return out


def create_backtest_result(result: Dict[str, Any]) -> int:
    """
    持久化一次回测结果，返回 backtest id。
    result 来自 run_backtest()。
    """
    with _conn() as c:
        c.execute(
            """INSERT INTO backtests (
                   strategy_id, strategy_name, started_at, start_date, end_date,
                   initial_cash, rebalance_freq, top_n, benchmark_symbol,
                   metrics_json, warnings_json, data_quality_json, trade_constraints_json,
                   model_version_id, model_version, equity_json, benchmark_json, trades_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(result.get("strategy_id") or 0),
                str(result.get("strategy_name") or ""),
                datetime.now().isoformat(),
                str(result.get("start_date") or ""),
                str(result.get("end_date") or ""),
                float(result.get("initial_cash") or 0),
                str(result.get("rebalance_freq") or "daily"),
                int(result.get("top_n") or 0),
                str(result.get("benchmark_symbol") or "000300"),
                json.dumps(result.get("metrics") or {}, ensure_ascii=False),
                json.dumps(result.get("warnings") or [], ensure_ascii=False),
                json.dumps(result.get("data_quality") or {}, ensure_ascii=False),
                json.dumps(result.get("trade_constraints") or {}, ensure_ascii=False),
                int(result.get("model_version_id")) if result.get("model_version_id") else None,
                str(result.get("model_version") or "") or None,
                json.dumps(result.get("equity_series") or [], ensure_ascii=False),
                json.dumps(result.get("benchmark_series") or [], ensure_ascii=False),
                json.dumps(result.get("trades") or [], ensure_ascii=False),
            ),
        )
        return c.last_insert_id()


def get_backtest_result(backtest_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            """SELECT
                   id, strategy_id, strategy_name, started_at, start_date, end_date,
                   initial_cash, rebalance_freq, top_n, benchmark_symbol,
                   metrics_json, warnings_json, data_quality_json, trade_constraints_json,
                   model_version_id, model_version, equity_json, benchmark_json, trades_json
               FROM backtests WHERE id = ?""",
            (backtest_id,),
        )
        r = cur.fetchone()
    if r is None:
        return None

    def _load(s: Any, default: Any) -> Any:
        try:
            return json.loads(s) if isinstance(s, str) else default
        except (json.JSONDecodeError, TypeError):
            return default

    return {
        "id": r["id"],
        "strategy_id": r["strategy_id"],
        "model_version_id": int(r["model_version_id"]) if r["model_version_id"] is not None else None,
        "model_version": r["model_version"],
        "strategy_name": r["strategy_name"],
        "started_at": r["started_at"],
        "start_date": r["start_date"],
        "end_date": r["end_date"],
        "initial_cash": r["initial_cash"],
        "rebalance_freq": r["rebalance_freq"],
        "top_n": r["top_n"],
        "benchmark_symbol": r["benchmark_symbol"],
        "metrics": _load(r["metrics_json"], {}),
        "warnings": _load(r["warnings_json"], []),
        "data_quality": _load(r["data_quality_json"], {}),
        "trade_constraints": _load(r["trade_constraints_json"], {}),
        "equity_series": _load(r["equity_json"], []),
        "benchmark_series": _load(r["benchmark_json"], []),
        "trades": _load(r["trades_json"], []),
    }


def list_backtests(limit: int = 30) -> List[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            """SELECT id, strategy_id, strategy_name, started_at, start_date, end_date, rebalance_freq,
                      model_version_id, model_version
               FROM backtests ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": x["id"],
            "strategy_id": x["strategy_id"],
            "model_version_id": int(x["model_version_id"]) if x["model_version_id"] is not None else None,
            "model_version": x["model_version"],
            "strategy_name": x["strategy_name"],
            "started_at": x["started_at"],
            "start_date": x["start_date"],
            "end_date": x["end_date"],
            "rebalance_freq": x["rebalance_freq"],
        }
        for x in rows
    ]


def delete_backtest_result(backtest_id: int) -> bool:
    """删除一条历史回测记录。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM backtests WHERE id = ?", (backtest_id,))
    return cur.rowcount > 0


def insert_manual_operation(
    symbol: str,
    side: str,
    price: float,
    quantity: int,
    at: str,
    name: str = "",
    remark: Optional[str] = None,
) -> int:
    """插入一条手动操作记录（execution_id 为 NULL）。返回新记录的 id。"""
    normalized = _validated_operation_fields({
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
    }, default_side="")
    with _conn() as c:
        c.execute(
            """INSERT INTO operations (execution_id, symbol, side, price, quantity, at, remark, name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (None, normalized["symbol"], normalized["side"], round(normalized["price"], 2), normalized["quantity"], at, remark or None, (name or "").strip()),
        )
        return c.last_insert_id()


def _norm_trade_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    s = s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _today_trade_date_iso() -> str:
    return get_last_trade_date(for_data_availability=True).strftime("%Y-%m-%d")


def _is_t1_sell_allowed(bought_at: Optional[str], trade_date: Optional[str]) -> bool:
    buy = _norm_trade_date(bought_at)
    td = _norm_trade_date(trade_date) or _today_trade_date_iso()
    if not buy:
        return True
    try:
        return date.fromisoformat(td) > date.fromisoformat(buy)
    except ValueError:
        return False


def sell_position(
    position_id: int,
    quantity: Optional[int] = None,
    price: float = 0.0,
    trade_date: Optional[str] = None,
    record_operation: bool = True,
    operation_at: Optional[str] = None,
    operation_remark: Optional[str] = None,
    operation_execution_id: Optional[int] = None,
    fee_breakdown: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    卖出持仓（全部或部分）。写入 SELL operation，扣减持仓，卖出所得加回账户余额。
    quantity 未传或等于当前持仓数量则全部卖出；price 为卖出单价。
    返回更新后的持仓（部分卖出）或 None（全部卖出后删除）。
    """
    sell_qty = 0
    pos_qty = 0
    with _conn() as c:
        _acquire_simulation_account_lock(c)
        lock_suffix = " FOR UPDATE" if getattr(c, "_is_postgres", False) or getattr(c, "_is_mysql", False) else ""
        cur = c.execute(
            "SELECT id, symbol, quantity, price, name, bought_at FROM positions WHERE id = ?" + lock_suffix,
            (position_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        pos_qty = int(row["quantity"])
        pos_price = float(row["price"])
        sym = row["symbol"]
        name = (row["name"] or "").strip() if "name" in row.keys() else ""
        bought_at = row["bought_at"] if "bought_at" in row.keys() else None
        # Read, validate, mutate the position, credit cash and persist the
        # operation under one row lock and one transaction.
        effective_trade_date = trade_date or _today_trade_date_iso()
        if not _is_t1_sell_allowed(bought_at, effective_trade_date):
            raise ValueError("T+1 限制：当日买入仓位不可卖出")
        sell_qty = min(quantity if quantity is not None and quantity > 0 else pos_qty, pos_qty)
        if sell_qty <= 0:
            return None
        sell_price = round(float(price), 2)
        if not math.isfinite(sell_price) or sell_price <= 0:
            raise ValueError("卖出价格无效，禁止以 0 元成交")
        now = datetime.now().isoformat()[:19]
        if sell_qty >= pos_qty:
            c.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        else:
            new_qty = pos_qty - sell_qty
            c.execute(
                "UPDATE positions SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, position_id),
            )
        # 使用同一连接写 operation，避免 SQLite 嵌套写连接导致 database is locked
        fee_comm = fee_trans = fee_tax = 0.0
        if sell_price > 0:
            amount = round(sell_qty * sell_price, 2)
            if fee_breakdown:
                fee_comm = round(float(fee_breakdown.get("commission") or 0), 2)
                fee_trans = round(float(fee_breakdown.get("transfer_fee") or 0), 2)
                fee_tax = round(float(fee_breakdown.get("stamp_tax") or 0), 2)
            else:
                fee_comm = round(commission(amount), 2)
                fee_trans = 0.0 if is_exchange_etf(sym) else round(transfer_fee(amount), 2)
                fee_tax = 0.0 if is_exchange_etf(sym) else round(stamp_tax(amount), 2)
            if any(not math.isfinite(fee) or fee < 0 for fee in (fee_comm, fee_trans, fee_tax)):
                raise ValueError("卖出费用无效，卖出已整体回滚")
            net = max(0.0, round(amount - fee_comm - fee_trans - fee_tax, 2))
            credited = c.execute(
                f"UPDATE account SET balance={_rounded_account_balance_expression(c, 'balance+?')} WHERE id=1",
                (net,),
            )
            if int(getattr(credited, "rowcount", 1) or 0) != 1:
                raise ValueError("模拟账户不存在，卖出已整体回滚")
        if record_operation:
            recorded_at = str(operation_at or now)
            operation_uid = _operation_uid(operation_execution_id, sym, "SELL", recorded_at, sell_qty, sell_price)
            c.execute(
                """INSERT INTO operations
                   (execution_id, symbol, side, price, quantity, at, remark, name,
                    commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version, operation_uid, target_weight_contract_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (operation_execution_id, sym, "SELL", sell_price, sell_qty, recorded_at, operation_remark or "手动出售", name,
                 fee_comm, fee_trans, fee_tax,
                 float((fee_breakdown or {}).get("slippage_bps") or 0),
                 str((fee_breakdown or {}).get("fee_schedule_version") or (
                     ETF_FEE_SCHEDULE_VERSION if is_exchange_etf(sym) else FEE_SCHEDULE_VERSION
                 )),
                 operation_uid, "{}"),
            )
    if sell_qty >= pos_qty:
        return None
    out = get_positions()
    for x in out:
        if x.get("id") == position_id:
            return x
    return None


def count_operations(execution_id: Optional[int] = None) -> int:
    with _conn() as c:
        if execution_id is not None:
            row = c.execute(
                "SELECT COUNT(*) AS total FROM operations WHERE execution_id = ?",
                (int(execution_id),),
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS total FROM operations").fetchone()
    return int(row["total"] or 0) if row else 0


def list_operations(
    execution_id: Optional[int] = None,
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return operations, optionally filtered by execution_id."""
    n = max(1, int(limit))
    skip = max(0, int(offset))
    with _conn() as c:
        if execution_id is not None:
            cur = c.execute(
                """SELECT id, execution_id, symbol, side, price, quantity, at, remark, name,
                          commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version, operation_uid, target_weight_contract_json
                   FROM operations WHERE execution_id = ? ORDER BY id DESC LIMIT ? OFFSET ?""",
                (execution_id, n, skip),
            )
        else:
            cur = c.execute(
                """SELECT id, execution_id, symbol, side, price, quantity, at, remark, name,
                          commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version, operation_uid, target_weight_contract_json
                   FROM operations ORDER BY id DESC LIMIT ? OFFSET ?""",
                (n, skip),
            )
        rows = cur.fetchall()
    return _operation_rows_to_dicts(rows)


def _operation_rows_to_dicts(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "execution_id": r["execution_id"],
            "symbol": r["symbol"],
            "side": r["side"],
            "price": r["price"],
            "quantity": r["quantity"],
            "at": r["at"],
            "remark": r["remark"],
            "name": r["name"] if "name" in r.keys() else "",
            "commission": float(r["commission"] or 0),
            "transfer_fee": float(r["transfer_fee"] or 0),
            "stamp_tax": float(r["stamp_tax"] or 0),
            "slippage_bps": float(r["slippage_bps"] or 0),
            "fee_schedule_version": r["fee_schedule_version"] or "",
            "operation_uid": r["operation_uid"] or "",
            "target_weight_contract": _json_load(r["target_weight_contract_json"] if "target_weight_contract_json" in r.keys() else "{}", {}),
        }
        for r in rows
    ]


def list_operations_for_execution_ids(
    execution_ids: Iterable[int],
    *,
    limit_per_execution: int = 5000,
) -> List[Dict[str, Any]]:
    """Read operations for several executions in one bounded SQL query."""
    ids = sorted({int(value) for value in execution_ids if value is not None})
    if not ids:
        return []
    per_execution = max(1, min(int(limit_per_execution or 5000), 5000))
    placeholders = ",".join("?" for _ in ids)
    with _conn() as c:
        cur = c.execute(
            f"""SELECT id, execution_id, symbol, side, price, quantity, at, remark, name,
                          commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version,
                          operation_uid, target_weight_contract_json
                   FROM (
                       SELECT id, execution_id, symbol, side, price, quantity, at, remark, name,
                              commission, transfer_fee, stamp_tax, slippage_bps, fee_schedule_version,
                              operation_uid, target_weight_contract_json,
                              ROW_NUMBER() OVER (PARTITION BY execution_id ORDER BY id DESC) AS row_num
                       FROM operations
                       WHERE execution_id IN ({placeholders})
                   ) operation_rows
                   WHERE row_num <= ?
                   ORDER BY id DESC""",
            (*ids, per_execution),
        )
        rows = cur.fetchall()
    return _operation_rows_to_dicts(rows)


def get_positions() -> List[Dict[str, Any]]:
    """当前持仓（来自 positions 表，支持用户自定义）。表为空时直接返回空列表，不再自动回填。"""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, symbol, quantity, price, updated_at, name, bought_at FROM positions WHERE quantity > 0 ORDER BY symbol"
        )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "symbol": r["symbol"],
            "quantity": r["quantity"],
            "price": r["price"],
            "side": "BUY",
            "updated_at": r["updated_at"],
            "name": r["name"] if "name" in r.keys() else "",
            "bought_at": r["bought_at"] if "bought_at" in r.keys() and r["bought_at"] else None,
        }
        for r in rows
    ]


def _merge_position_row(
    c: Any, *, symbol: str, quantity: int, price: float, updated_at: str,
    name: str, bought_at: str,
) -> int:
    """Atomically insert or merge one holding, including concurrent buyers."""
    values = (symbol, quantity, round(price, 2), updated_at, name, bought_at)
    if c._is_mysql and not c._is_postgres:
        c.execute(
            """INSERT INTO positions (symbol,quantity,price,updated_at,name,bought_at)
               VALUES (?,?,?,?,?,?)
               ON DUPLICATE KEY UPDATE
                 price=ROUND((quantity*price + VALUES(quantity)*VALUES(price))/(quantity+VALUES(quantity)),2),
                 quantity=quantity+VALUES(quantity),updated_at=VALUES(updated_at),
                 name=CASE WHEN VALUES(name)<>'' THEN VALUES(name) ELSE name END,
                 bought_at=VALUES(bought_at)""",
            values,
        )
    else:
        c.execute(
            """INSERT INTO positions (symbol,quantity,price,updated_at,name,bought_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 price=ROUND(((positions.quantity*positions.price + excluded.quantity*excluded.price)
                   /(positions.quantity+excluded.quantity))::numeric,2)::double precision,
                 quantity=positions.quantity+excluded.quantity,updated_at=excluded.updated_at,
                 name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE positions.name END,
                 bought_at=excluded.bought_at""",
            values,
        )
    row = c.execute("SELECT id FROM positions WHERE symbol=?", (symbol,)).fetchone()
    return int(row["id"])


def merge_execution_into_positions(
    orders: List[Dict[str, Any]], executed_at: str, name_by_symbol: Optional[Dict[str, str]] = None
) -> None:
    """将一次执行的买入订单合并进 positions 表（同 symbol 累加数量、加权成本）。可选传入 name_by_symbol 写入名称（来自选股，无需再调行情源）。"""
    if not orders:
        return
    now = executed_at[:19] if executed_at else datetime.now().isoformat()[:19]
    name_by_symbol = name_by_symbol or {}
    with _conn() as c:
        for o in orders:
            sym = (o.get("symbol") or "").strip()
            qty = int(o.get("quantity") or 0)
            pr = float(o.get("price") or 0)
            if not sym or qty <= 0 or not math.isfinite(pr) or pr <= 0:
                continue
            name = (name_by_symbol.get(sym) or o.get("name") or "").strip()
            bought_date = (executed_at or now)[:10]
            _merge_position_row(
                c, symbol=sym, quantity=qty, price=pr, updated_at=now,
                name=name or "", bought_at=bought_date,
            )


def add_position(symbol: str, quantity: int, price: float) -> Dict[str, Any]:
    """添加或累加持仓。同 symbol 则累加数量、加权成本。"""
    symbol = (symbol or "").strip()
    quantity = int(quantity)
    price = float(price)
    if not symbol or quantity <= 0 or not math.isfinite(price) or price <= 0:
        raise ValueError("invalid_position_input")
    price = round(price, 2)
    now = datetime.now().isoformat()[:19]
    bought_date = now[:10]
    with _conn() as c:
        pid = _merge_position_row(
            c, symbol=symbol, quantity=quantity, price=price, updated_at=now,
            name="", bought_at=bought_date,
        )
    out = get_positions()
    for p in out:
        if p.get("id") == pid:
            return p
    return {"id": pid, "symbol": symbol, "quantity": quantity, "price": price, "side": "BUY", "updated_at": now, "name": "", "bought_at": bought_date}


def update_position(position_id: int, quantity: Optional[int] = None, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """更新持仓数量或成本价。数量为 0 时删除该条。"""
    with _conn() as c:
        cur = c.execute("SELECT id, symbol, quantity, price FROM positions WHERE id = ?", (position_id,))
        row = cur.fetchone()
        if not row:
            return None
        q = int(row["quantity"]) if quantity is None else max(0, int(quantity))
        p = float(row["price"]) if price is None else float(price)
        if not math.isfinite(p) or p <= 0:
            raise ValueError("invalid_position_price")
        p = round(p, 2)
        now = datetime.now().isoformat()[:19]
        if q <= 0:
            c.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            return None
        c.execute(
            "UPDATE positions SET quantity = ?, price = ?, updated_at = ? WHERE id = ?",
            (q, p, now, position_id),
        )
    out = get_positions()
    for x in out:
        if x.get("id") == position_id:
            return x
    return None


def delete_position(position_id: int) -> bool:
    """删除一条持仓。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        return cur.rowcount > 0


def create_user(username: str, password_hash: str) -> Dict[str, Any]:
    """创建用户，返回 { id, username, created_at }。用户名已存在则抛错由调用方处理。"""
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username.strip(), password_hash, now),
        )
        uid = c.last_insert_id()
    return {"id": uid, "username": username.strip(), "created_at": now}


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """按用户名查询用户，返回 { id, username, password_hash, created_at } 或 None。"""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
            (username.strip(),),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "username": r["username"],
        "password_hash": r["password_hash"],
        "created_at": r["created_at"],
    }


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """按 id 查询用户（不含密码），返回 { id, username, created_at } 或 None。"""
    with _conn() as c:
        cur = c.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {"id": r["id"], "username": r["username"], "created_at": r["created_at"]}


def get_account_balance() -> float:
    """模拟账户当前余额（元）。"""
    with _conn() as c:
        cur = c.execute("SELECT balance FROM account WHERE id = 1")
        row = cur.fetchone()
    return float(row["balance"]) if row is not None else 0.0


def get_account_high_water_mark() -> float:
    with _conn() as c:
        cur = c.execute("SELECT high_water_mark FROM account WHERE id = 1")
        row = cur.fetchone()
    return float(row["high_water_mark"] or 0.0) if row is not None and "high_water_mark" in row.keys() else get_account_baseline()


def update_account_high_water_mark(equity: float) -> float:
    equity = max(0.0, round(float(equity), 2))
    with _conn() as c:
        c.execute("UPDATE account SET high_water_mark = CASE WHEN high_water_mark < ? THEN ? ELSE high_water_mark END WHERE id = 1", (equity, equity))
        cur = c.execute("SELECT high_water_mark FROM account WHERE id = 1")
        row = cur.fetchone()
    return float(row["high_water_mark"] or 0.0) if row is not None else equity


def get_account_baseline() -> float:
    """
    V1模拟账户基准本金（默认 200000），用于展示总收益分解。
    """
    return 200000.0


def set_account_balance(balance: float) -> float:
    """直接设置余额（自定义初始资金等）。"""
    balance = max(0.0, round(balance, 2))
    with _conn() as c:
        c.execute("UPDATE account SET balance = ? WHERE id = 1", (balance,))
    return balance


def add_account_balance(delta: float) -> float:
    """增减余额（正数充值，负数提现/扣款）。返回更新后余额；不足时不变并返回当前余额。"""
    with _conn() as c:
        cur = c.execute("SELECT balance FROM account WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            return 0.0
        new_balance = max(0.0, round(float(row["balance"]) + delta, 2))
        c.execute("UPDATE account SET balance = ? WHERE id = 1", (new_balance,))
    return new_balance


def create_quant_run_task(
    *,
    strategy_id: int,
    top_n: int,
    budget_ratio: float,
    exchange_group: str = "HS",
    start_date: Optional[str] = None,
    max_steps: int = 5,
    replay_mode: str = "pragmatic_recover",
) -> int:
    now = datetime.now().isoformat()
    norm_exchange_group = str(exchange_group or "HS")
    norm_start_date = str(start_date) if start_date else None
    norm_replay_mode = str(replay_mode or "pragmatic_recover")
    with _conn() as c:
        # SQLite 下显式拿写锁，保证“查重 + 插入”原子性，避免并发重复创建。
        if not _is_mysql() and not _is_postgres():
            try:
                c.execute("BEGIN IMMEDIATE")
            except Exception:
                pass

        # 防重：同参数的活动任务（未终态）只保留一条，避免外部重复触发导致任务雪崩。
        cur = c.execute(
            """SELECT id
               FROM quant_run_tasks
               WHERE strategy_id = ?
                 AND top_n = ?
                 AND budget_ratio = ?
                 AND exchange_group = ?
                 AND COALESCE(start_date, '') = COALESCE(?, '')
                 AND replay_mode = ?
                 AND status IN ('pending', 'running', 'paused', 'stopping')
               ORDER BY id DESC
               LIMIT 1""",
            (
                int(strategy_id),
                int(top_n),
                float(budget_ratio),
                norm_exchange_group,
                norm_start_date,
                norm_replay_mode,
            ),
        )
        existed = cur.fetchone()
        if existed is not None:
            task_id = int(existed["id"])
            c.execute(
                "UPDATE quant_run_tasks SET updated_at = ?, last_message = ? WHERE id = ?",
                (now, "检测到重复创建请求，复用已有任务", task_id),
            )
            return task_id

        c.execute(
            """INSERT INTO quant_run_tasks (
                   status, strategy_id, top_n, budget_ratio, exchange_group, start_date, max_steps,
                   stop_requested, processed_steps, total_steps,
                   last_signal_date, last_execute_date, last_message, error,
                   replay_mode, replay_status, replay_gap_days, replay_current_execute_date, replay_target_execute_date,
                   started_at, finished_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, NULL, NULL, ?, NULL, ?, '', 0, NULL, NULL, NULL, NULL, ?, ?)""",
            (
                "pending",
                int(strategy_id),
                int(top_n),
                float(budget_ratio),
                norm_exchange_group,
                norm_start_date,
                max(1, int(max_steps or 5)),
                "任务已创建，等待执行",
                norm_replay_mode,
                now,
                now,
            ),
        )
        return c.last_insert_id()


def upsert_quant_run_task_meta(
    task_id: int,
    *,
    strategy_snapshot: Dict[str, Any],
    risk_config: Dict[str, Any],
) -> None:
    now = datetime.now().isoformat()
    strategy_payload = json.dumps(strategy_snapshot or {}, ensure_ascii=False)
    risk_payload = json.dumps(risk_config or {}, ensure_ascii=False)
    try:
        with _conn() as c:
            if c._is_mysql and not c._is_postgres:
                c.execute(
                    """
                    INSERT INTO quant_run_task_meta (
                        task_id, strategy_snapshot_json, risk_config_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        strategy_snapshot_json=VALUES(strategy_snapshot_json),
                        risk_config_json=VALUES(risk_config_json),
                        updated_at=VALUES(updated_at)
                    """,
                    (int(task_id), strategy_payload, risk_payload, now, now),
                )
                return
            c.execute(
                """
                INSERT INTO quant_run_task_meta (
                    task_id, strategy_snapshot_json, risk_config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    strategy_snapshot_json=excluded.strategy_snapshot_json,
                    risk_config_json=excluded.risk_config_json,
                    updated_at=excluded.updated_at
                """,
                (int(task_id), strategy_payload, risk_payload, now, now),
            )
    except Exception:
        return


def get_quant_run_task_meta(task_id: int) -> Dict[str, Any]:
    try:
        with _conn() as c:
            cur = c.execute(
                """SELECT strategy_snapshot_json, risk_config_json, created_at, updated_at
                   FROM quant_run_task_meta WHERE task_id = ? LIMIT 1""",
                (int(task_id),),
            )
            r = cur.fetchone()
    except Exception:
        return {}
    if r is None:
        return {}
    strategy_raw = r["strategy_snapshot_json"] if "strategy_snapshot_json" in r.keys() else None
    risk_raw = r["risk_config_json"] if "risk_config_json" in r.keys() else None
    try:
        strategy_snapshot = json.loads(strategy_raw) if strategy_raw else {}
    except Exception:
        strategy_snapshot = {}
    try:
        risk_config = json.loads(risk_raw) if risk_raw else {}
    except Exception:
        risk_config = {}
    return {
        "strategy_snapshot": strategy_snapshot,
        "risk_config": risk_config,
        "meta_created_at": str(r["created_at"] or ""),
        "meta_updated_at": str(r["updated_at"] or ""),
    }


def upsert_quant_run_task_health(task_id: int, **fields: Any) -> None:
    now = datetime.now().isoformat()
    payload = {
        "heartbeat_at": fields.get("heartbeat_at"),
        "last_step_latency_ms": int(fields.get("last_step_latency_ms") or 0),
        "avg_step_latency_ms": int(fields.get("avg_step_latency_ms") or 0),
        "step_count": int(fields.get("step_count") or 0),
        "risk_hit_count": int(fields.get("risk_hit_count") or 0),
        "consecutive_pause_count": int(fields.get("consecutive_pause_count") or 0),
        "last_risk_code": str(fields.get("last_risk_code") or ""),
    }
    try:
        with _conn() as c:
            if c._is_mysql and not c._is_postgres:
                c.execute(
                    """
                    INSERT INTO quant_run_task_health (
                        task_id, heartbeat_at, last_step_latency_ms, avg_step_latency_ms, step_count,
                        risk_hit_count, consecutive_pause_count, last_risk_code, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        heartbeat_at=VALUES(heartbeat_at),
                        last_step_latency_ms=VALUES(last_step_latency_ms),
                        avg_step_latency_ms=VALUES(avg_step_latency_ms),
                        step_count=VALUES(step_count),
                        risk_hit_count=VALUES(risk_hit_count),
                        consecutive_pause_count=VALUES(consecutive_pause_count),
                        last_risk_code=VALUES(last_risk_code),
                        updated_at=VALUES(updated_at)
                    """,
                    (
                        int(task_id),
                        payload["heartbeat_at"],
                        payload["last_step_latency_ms"],
                        payload["avg_step_latency_ms"],
                        payload["step_count"],
                        payload["risk_hit_count"],
                        payload["consecutive_pause_count"],
                        payload["last_risk_code"],
                        now,
                    ),
                )
                return
            c.execute(
                """
                INSERT INTO quant_run_task_health (
                    task_id, heartbeat_at, last_step_latency_ms, avg_step_latency_ms, step_count,
                    risk_hit_count, consecutive_pause_count, last_risk_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    heartbeat_at=excluded.heartbeat_at,
                    last_step_latency_ms=excluded.last_step_latency_ms,
                    avg_step_latency_ms=excluded.avg_step_latency_ms,
                    step_count=excluded.step_count,
                    risk_hit_count=excluded.risk_hit_count,
                    consecutive_pause_count=excluded.consecutive_pause_count,
                    last_risk_code=excluded.last_risk_code,
                    updated_at=excluded.updated_at
                """,
                (
                    int(task_id),
                    payload["heartbeat_at"],
                    payload["last_step_latency_ms"],
                    payload["avg_step_latency_ms"],
                    payload["step_count"],
                    payload["risk_hit_count"],
                    payload["consecutive_pause_count"],
                    payload["last_risk_code"],
                    now,
                ),
            )
    except Exception:
        return


def get_quant_run_task_health(task_id: int) -> Dict[str, Any]:
    try:
        with _conn() as c:
            cur = c.execute(
                """SELECT heartbeat_at, last_step_latency_ms, avg_step_latency_ms, step_count,
                          risk_hit_count, consecutive_pause_count, last_risk_code, updated_at
                   FROM quant_run_task_health WHERE task_id = ? LIMIT 1""",
                (int(task_id),),
            )
            r = cur.fetchone()
    except Exception:
        return {}
    if r is None:
        return {}
    return {
        "heartbeat_at": str(r["heartbeat_at"]) if r["heartbeat_at"] else None,
        "last_step_latency_ms": int(r["last_step_latency_ms"] or 0),
        "avg_step_latency_ms": int(r["avg_step_latency_ms"] or 0),
        "step_count": int(r["step_count"] or 0),
        "risk_hit_count": int(r["risk_hit_count"] or 0),
        "consecutive_pause_count": int(r["consecutive_pause_count"] or 0),
        "last_risk_code": str(r["last_risk_code"] or ""),
        "health_updated_at": str(r["updated_at"] or ""),
    }


def get_quant_run_task_runtime_config(task_id: int) -> Dict[str, Any]:
    meta = get_quant_run_task_meta(task_id)
    cfg = meta.get("risk_config") if isinstance(meta, dict) else {}
    return cfg if isinstance(cfg, dict) else {}


def _list_task_meta_batch(task_ids: list[int]) -> dict[int, Dict[str, Any]]:
    if not task_ids:
        return {}
    placeholders = ", ".join(["?"] * len(task_ids))
    try:
        with _conn() as c:
            cur = c.execute(
                f"""SELECT task_id, strategy_snapshot_json, risk_config_json, created_at, updated_at
                    FROM quant_run_task_meta
                    WHERE task_id IN ({placeholders})""",
                tuple(int(x) for x in task_ids),
            )
            rows = cur.fetchall()
    except Exception:
        return {}
    out: dict[int, Dict[str, Any]] = {}
    for r in rows:
        tid = int(r["task_id"])
        strategy_raw = r["strategy_snapshot_json"] if "strategy_snapshot_json" in r.keys() else None
        risk_raw = r["risk_config_json"] if "risk_config_json" in r.keys() else None
        try:
            strategy_snapshot = json.loads(strategy_raw) if strategy_raw else {}
        except Exception:
            strategy_snapshot = {}
        try:
            risk_config = json.loads(risk_raw) if risk_raw else {}
        except Exception:
            risk_config = {}
        out[tid] = {
            "strategy_snapshot": strategy_snapshot,
            "risk_config": risk_config,
            "meta_created_at": str(r["created_at"] or ""),
            "meta_updated_at": str(r["updated_at"] or ""),
        }
    return out


def _list_task_health_batch(task_ids: list[int]) -> dict[int, Dict[str, Any]]:
    if not task_ids:
        return {}
    placeholders = ", ".join(["?"] * len(task_ids))
    try:
        with _conn() as c:
            cur = c.execute(
                f"""SELECT task_id, heartbeat_at, last_step_latency_ms, avg_step_latency_ms, step_count,
                           risk_hit_count, consecutive_pause_count, last_risk_code, updated_at
                    FROM quant_run_task_health
                    WHERE task_id IN ({placeholders})""",
                tuple(int(x) for x in task_ids),
            )
            rows = cur.fetchall()
    except Exception:
        return {}
    out: dict[int, Dict[str, Any]] = {}
    for r in rows:
        tid = int(r["task_id"])
        out[tid] = {
            "heartbeat_at": str(r["heartbeat_at"]) if r["heartbeat_at"] else None,
            "last_step_latency_ms": int(r["last_step_latency_ms"] or 0),
            "avg_step_latency_ms": int(r["avg_step_latency_ms"] or 0),
            "step_count": int(r["step_count"] or 0),
            "risk_hit_count": int(r["risk_hit_count"] or 0),
            "consecutive_pause_count": int(r["consecutive_pause_count"] or 0),
            "last_risk_code": str(r["last_risk_code"] or ""),
            "health_updated_at": str(r["updated_at"] or ""),
        }
    return out


def get_quant_run_task(task_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            """SELECT id, status, strategy_id, top_n, budget_ratio, exchange_group, start_date, max_steps,
                      stop_requested, processed_steps, total_steps, last_signal_date, last_execute_date,
                      last_message, error, replay_mode, replay_status, replay_gap_days,
                      replay_current_execute_date, replay_target_execute_date,
                      started_at, finished_at, created_at, updated_at
               FROM quant_run_tasks WHERE id = ?""",
            (int(task_id),),
        )
        r = cur.fetchone()
    if r is None:
        return None
    out = {
        "id": int(r["id"]),
        "status": str(r["status"] or ""),
        "strategy_id": int(r["strategy_id"] or 0),
        "top_n": int(r["top_n"] or 0),
        "budget_ratio": float(r["budget_ratio"] or 0),
        "exchange_group": str(r["exchange_group"] or "HS"),
        "start_date": str(r["start_date"]) if r["start_date"] else None,
        "max_steps": int(r["max_steps"] or 0),
        "stop_requested": bool(int(r["stop_requested"] or 0)),
        "processed_steps": int(r["processed_steps"] or 0),
        "total_steps": int(r["total_steps"] or 0),
        "last_signal_date": str(r["last_signal_date"]) if r["last_signal_date"] else None,
        "last_execute_date": str(r["last_execute_date"]) if r["last_execute_date"] else None,
        "last_message": str(r["last_message"]) if r["last_message"] else None,
        "error": str(r["error"]) if r["error"] else None,
        "replay_mode": str(r["replay_mode"] or "pragmatic_recover"),
        "replay_status": str(r["replay_status"] or ""),
        "replay_gap_days": int(r["replay_gap_days"] or 0),
        "replay_current_execute_date": str(r["replay_current_execute_date"]) if r["replay_current_execute_date"] else None,
        "replay_target_execute_date": str(r["replay_target_execute_date"]) if r["replay_target_execute_date"] else None,
        "started_at": str(r["started_at"]) if r["started_at"] else None,
        "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
        "created_at": str(r["created_at"]) if r["created_at"] else None,
        "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
    }
    out.update(get_quant_run_task_meta(int(r["id"])))
    out.update(get_quant_run_task_health(int(r["id"])))
    return out


def list_quant_run_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    n = max(1, min(int(limit or 50), 500))
    with _conn() as c:
        cur = c.execute(
            """SELECT id, status, strategy_id, top_n, budget_ratio, exchange_group, start_date, max_steps,
                      stop_requested, processed_steps, total_steps, last_signal_date, last_execute_date,
                      last_message, error, replay_mode, replay_status, replay_gap_days,
                      replay_current_execute_date, replay_target_execute_date,
                      started_at, finished_at, created_at, updated_at
               FROM quant_run_tasks ORDER BY id DESC LIMIT ?""",
            (n,),
        )
        rows = cur.fetchall()
    task_ids = [int(r["id"]) for r in rows]
    meta_map = _list_task_meta_batch(task_ids)
    health_map = _list_task_health_batch(task_ids)
    out: List[Dict[str, Any]] = []
    for r in rows:
        tid = int(r["id"])
        item = {
                "id": tid,
                "status": str(r["status"] or ""),
                "strategy_id": int(r["strategy_id"] or 0),
                "top_n": int(r["top_n"] or 0),
                "budget_ratio": float(r["budget_ratio"] or 0),
                "exchange_group": str(r["exchange_group"] or "HS"),
                "start_date": str(r["start_date"]) if r["start_date"] else None,
                "max_steps": int(r["max_steps"] or 0),
                "stop_requested": bool(int(r["stop_requested"] or 0)),
                "processed_steps": int(r["processed_steps"] or 0),
                "total_steps": int(r["total_steps"] or 0),
                "last_signal_date": str(r["last_signal_date"]) if r["last_signal_date"] else None,
                "last_execute_date": str(r["last_execute_date"]) if r["last_execute_date"] else None,
                "last_message": str(r["last_message"]) if r["last_message"] else None,
                "error": str(r["error"]) if r["error"] else None,
                "replay_mode": str(r["replay_mode"] or "pragmatic_recover"),
                "replay_status": str(r["replay_status"] or ""),
                "replay_gap_days": int(r["replay_gap_days"] or 0),
                "replay_current_execute_date": str(r["replay_current_execute_date"]) if r["replay_current_execute_date"] else None,
                "replay_target_execute_date": str(r["replay_target_execute_date"]) if r["replay_target_execute_date"] else None,
                "started_at": str(r["started_at"]) if r["started_at"] else None,
                "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
            }
        item.update(meta_map.get(tid, {}))
        item.update(health_map.get(tid, {}))
        out.append(item)
    return out


def update_quant_run_task(task_id: int, **fields: Any) -> bool:
    allowed = {
        "status",
        "top_n",
        "budget_ratio",
        "exchange_group",
        "start_date",
        "max_steps",
        "stop_requested",
        "processed_steps",
        "total_steps",
        "last_signal_date",
        "last_execute_date",
        "last_message",
        "error",
        "replay_mode",
        "replay_status",
        "replay_gap_days",
        "replay_current_execute_date",
        "replay_target_execute_date",
        "started_at",
        "finished_at",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return False
    data["updated_at"] = datetime.now().isoformat()
    cols = ", ".join([f"{k} = ?" for k in data.keys()])
    params = tuple(data.values()) + (int(task_id),)
    with _conn() as c:
        cur = c.execute(f"UPDATE quant_run_tasks SET {cols} WHERE id = ?", params)
    return cur.rowcount > 0


def list_active_quant_run_tasks() -> List[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            """SELECT id, status, strategy_id, top_n, budget_ratio, exchange_group, start_date, max_steps,
                      stop_requested, processed_steps, total_steps, last_signal_date, last_execute_date,
                      last_message, error, replay_mode, replay_status, replay_gap_days,
                      replay_current_execute_date, replay_target_execute_date,
                      started_at, finished_at, created_at, updated_at
               FROM quant_run_tasks
               WHERE status IN ('pending', 'running', 'paused')
               ORDER BY id ASC"""
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "status": str(r["status"] or ""),
            "strategy_id": int(r["strategy_id"] or 0),
            "top_n": int(r["top_n"] or 0),
            "budget_ratio": float(r["budget_ratio"] or 0),
            "exchange_group": str(r["exchange_group"] or "HS"),
            "start_date": str(r["start_date"]) if r["start_date"] else None,
            "max_steps": int(r["max_steps"] or 0),
            "stop_requested": bool(int(r["stop_requested"] or 0)),
            "processed_steps": int(r["processed_steps"] or 0),
            "total_steps": int(r["total_steps"] or 0),
            "last_signal_date": str(r["last_signal_date"]) if r["last_signal_date"] else None,
            "last_execute_date": str(r["last_execute_date"]) if r["last_execute_date"] else None,
            "last_message": str(r["last_message"]) if r["last_message"] else None,
            "error": str(r["error"]) if r["error"] else None,
            "replay_mode": str(r["replay_mode"] or "pragmatic_recover"),
            "replay_status": str(r["replay_status"] or ""),
            "replay_gap_days": int(r["replay_gap_days"] or 0),
            "replay_current_execute_date": str(r["replay_current_execute_date"]) if r["replay_current_execute_date"] else None,
            "replay_target_execute_date": str(r["replay_target_execute_date"]) if r["replay_target_execute_date"] else None,
            "started_at": str(r["started_at"]) if r["started_at"] else None,
            "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        }
        for r in rows
    ]


def create_quant_run_task_record(
    *,
    task_id: int,
    signal_date: str,
    execute_date: str,
    sold_count: int,
    bought_count: int,
    total_buy_cost: float,
    execution_id: Optional[int],
    message: str,
    detail: Optional[Dict[str, Any]] = None,
    plan_claim_token: str = "",
) -> int:
    now = datetime.now().isoformat()
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    with _conn() as c:
        # 幂等防重：同一 task_id + signal_date 只允许一条执行记录。
        exists = c.execute(
            "SELECT id FROM quant_run_task_runs WHERE task_id = ? AND signal_date = ? LIMIT 1",
            (int(task_id), str(signal_date)),
        ).fetchone()
        if exists is not None:
            return 0
        c.execute(
            """INSERT INTO quant_run_task_runs (
                   task_id, signal_date, execute_date, sold_count, bought_count,
                   total_buy_cost, execution_id, message, detail_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(task_id),
                str(signal_date),
                str(execute_date),
                int(sold_count),
                int(bought_count),
                round(float(total_buy_cost), 2),
                int(execution_id) if execution_id is not None else None,
                str(message or ""),
                detail_json,
                now,
            ),
        )
        record_id = c.last_insert_id()
        if plan_claim_token:
            signal = str(signal_date or "").replace("-", "")[:8]
            changed = c.execute(
                """UPDATE daily_rebalance_plans
                   SET status='executed',execution_id=?,executed_at=?
                   WHERE id=(SELECT id FROM daily_rebalance_plans
                             WHERE replace(trade_date,'-','')=? AND status='executing'
                               AND execution_claim_token=?
                             ORDER BY version DESC LIMIT 1)""",
                (
                    int(execution_id) if execution_id is not None else None,
                    now,
                    signal,
                    str(plan_claim_token),
                ),
            )
            if int(getattr(changed, "rowcount", 0) or 0) != 1:
                raise ValueError("正式调仓计划执行租约不存在，任务记录与计划状态已整体回滚")
        return record_id


def get_quant_replay_waterline(strategy_id: int, exchange_group: str = "HS") -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            """SELECT strategy_id, exchange_group, last_successful_signal_date, last_successful_execute_date,
                      last_run_mode, last_run_status, updated_at
               FROM quant_replay_waterline
               WHERE strategy_id = ? AND exchange_group = ?
               LIMIT 1""",
            (int(strategy_id), str(exchange_group or "HS")),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return {
        "strategy_id": int(r["strategy_id"] or 0),
        "exchange_group": str(r["exchange_group"] or "HS"),
        "last_successful_signal_date": str(r["last_successful_signal_date"]) if r["last_successful_signal_date"] else None,
        "last_successful_execute_date": str(r["last_successful_execute_date"]) if r["last_successful_execute_date"] else None,
        "last_run_mode": str(r["last_run_mode"] or "pragmatic_recover"),
        "last_run_status": str(r["last_run_status"] or ""),
        "updated_at": str(r["updated_at"] or ""),
    }


def upsert_quant_replay_waterline(
    *,
    strategy_id: int,
    exchange_group: str = "HS",
    last_successful_signal_date: Optional[str] = None,
    last_successful_execute_date: Optional[str] = None,
    last_run_mode: str = "pragmatic_recover",
    last_run_status: str = "",
) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        if c._is_mysql and not c._is_postgres:
            c.execute(
                """
                INSERT INTO quant_replay_waterline (
                    strategy_id, exchange_group, last_successful_signal_date, last_successful_execute_date,
                    last_run_mode, last_run_status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    last_successful_signal_date = VALUES(last_successful_signal_date),
                    last_successful_execute_date = VALUES(last_successful_execute_date),
                    last_run_mode = VALUES(last_run_mode),
                    last_run_status = VALUES(last_run_status),
                    updated_at = VALUES(updated_at)
                """,
                (
                    int(strategy_id),
                    str(exchange_group or "HS"),
                    str(last_successful_signal_date) if last_successful_signal_date else None,
                    str(last_successful_execute_date) if last_successful_execute_date else None,
                    str(last_run_mode or "pragmatic_recover"),
                    str(last_run_status or ""),
                    now,
                ),
            )
            return

        # SQLite
        c.execute(
            """
            INSERT INTO quant_replay_waterline (
                strategy_id, exchange_group, last_successful_signal_date, last_successful_execute_date,
                last_run_mode, last_run_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, exchange_group) DO UPDATE SET
                last_successful_signal_date=excluded.last_successful_signal_date,
                last_successful_execute_date=excluded.last_successful_execute_date,
                last_run_mode=excluded.last_run_mode,
                last_run_status=excluded.last_run_status,
                updated_at=excluded.updated_at
            """,
            (
                int(strategy_id),
                str(exchange_group or "HS"),
                str(last_successful_signal_date) if last_successful_signal_date else None,
                str(last_successful_execute_date) if last_successful_execute_date else None,
                str(last_run_mode or "pragmatic_recover"),
                str(last_run_status or ""),
                now,
            ),
        )


def list_quant_run_task_records(task_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    n = max(1, min(int(limit or 200), 2000))
    with _conn() as c:
        cur = c.execute(
            """SELECT id, task_id, signal_date, execute_date, sold_count, bought_count,
                      total_buy_cost, execution_id, message, detail_json, created_at
               FROM quant_run_task_runs
               WHERE task_id = ?
               ORDER BY id ASC
               LIMIT ?""",
            (int(task_id), n),
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "task_id": int(r["task_id"]),
            "signal_date": str(r["signal_date"] or ""),
            "execute_date": str(r["execute_date"] or ""),
            "sold_count": int(r["sold_count"] or 0),
            "bought_count": int(r["bought_count"] or 0),
            "total_buy_cost": float(r["total_buy_cost"] or 0),
            "execution_id": int(r["execution_id"]) if r["execution_id"] is not None else None,
            "message": str(r["message"] or ""),
            "detail": _json_load(r["detail_json"], {}) if "detail_json" in r.keys() else {},
            "created_at": str(r["created_at"] or ""),
        }
        for r in rows
    ]


def list_recent_quant_run_task_records(
    limit: int = 20,
    *,
    include_detail: bool = False,
) -> List[Dict[str, Any]]:
    """
    返回跨任务的最近执行记录（按 id 倒序），用于量化看板“最近执行明细”。
    """
    n = max(1, min(int(limit or 20), 500))
    with _conn() as c:
        detail_column = ", detail_json" if include_detail else ""
        cur = c.execute(
            f"""SELECT id, task_id, signal_date, execute_date, sold_count, bought_count,
                      total_buy_cost, execution_id, message, created_at{detail_column}
               FROM quant_run_task_runs
               ORDER BY id DESC
               LIMIT ?""",
            (n,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(r["id"]),
            "task_id": int(r["task_id"]),
            "signal_date": str(r["signal_date"] or ""),
            "execute_date": str(r["execute_date"] or ""),
            "sold_count": int(r["sold_count"] or 0),
            "bought_count": int(r["bought_count"] or 0),
            "total_buy_cost": float(r["total_buy_cost"] or 0),
            "execution_id": int(r["execution_id"]) if r["execution_id"] is not None else None,
            "message": str(r["message"] or ""),
            **({"detail": _json_load(r["detail_json"], {})} if include_detail else {}),
            "created_at": str(r["created_at"] or ""),
        }
        for r in rows
    ]


def delete_quant_run_task(task_id: int) -> bool:
    """
    删除任务及其执行记录。
    返回 True 表示删除成功（任务存在），False 表示任务不存在。
    """
    tid = int(task_id)
    with _conn() as c:
        c.execute("DELETE FROM quant_run_task_runs WHERE task_id = ?", (tid,))
        cur = c.execute("DELETE FROM quant_run_tasks WHERE id = ?", (tid,))
    return cur.rowcount > 0


def create_model_train_run(message: str = "等待训练") -> int:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO model_train_runs (
                status, phase, progress, message, metrics_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("running", "queued", 0.0, str(message or ""), "{}", now, now),
        )
        return c.last_insert_id()


def update_model_train_run(
    run_id: int,
    *,
    status: str | None = None,
    phase: str | None = None,
    progress: float | None = None,
    message: str | None = None,
    model_version_id: int | None = None,
    metrics: dict | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    fields: list[str] = ["updated_at = ?"]
    params: list[Any] = [now]
    if status is not None:
        fields.append("status = ?")
        params.append(str(status))
    if phase is not None:
        fields.append("phase = ?")
        params.append(str(phase))
    if progress is not None:
        fields.append("progress = ?")
        params.append(float(progress))
    if message is not None:
        fields.append("message = ?")
        params.append(str(message))
    if model_version_id is not None:
        fields.append("model_version_id = ?")
        params.append(int(model_version_id))
    if metrics is not None:
        fields.append("metrics_json = ?")
        params.append(json.dumps(metrics, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        params.append(str(error))
    if started_at is not None:
        fields.append("started_at = ?")
        params.append(str(started_at))
    if finished_at is not None:
        fields.append("finished_at = ?")
        params.append(str(finished_at))
    params.append(int(run_id))
    with _conn() as c:
        c.execute(f"UPDATE model_train_runs SET {', '.join(fields)} WHERE id = ?", tuple(params))


def create_model_version(
    *,
    model_key: str,
    version: str,
    status: str,
    artifact_path: str,
    config: dict,
    metrics: dict,
    validation: dict | None = None,
    is_active: bool = False,
) -> int:
    now = datetime.now().isoformat()
    with _conn() as c:
        if c._is_postgres:
            # Databases migrated from SQLite may contain explicit ids while
            # the PostgreSQL sequence still starts at 1. Synchronize before
            # inserting so model governance remains append-only.
            c.execute(
                """SELECT setval(pg_get_serial_sequence('model_versions','id'),
                       COALESCE(MAX(id),1), COUNT(*)>0) FROM model_versions"""
            )
        if is_active:
            c.execute("UPDATE model_versions SET is_active = 0 WHERE model_key = ?", (str(model_key),))
        c.execute(
            """INSERT INTO model_versions (
                model_key, version, status, artifact_path, config_json, metrics_json,
                validation_json, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(model_key),
                str(version),
                str(status),
                str(artifact_path),
                json.dumps(config or {}, ensure_ascii=False),
                json.dumps(metrics or {}, ensure_ascii=False),
                json.dumps(validation or {}, ensure_ascii=False),
                1 if is_active else 0,
                now,
                now,
            ),
        )
        model_version_id = c.last_insert_id()
        if str(status) == "candidate" and not is_active:
            from app.services.candidate_lifecycle import register_candidate_lifecycle

            register_candidate_lifecycle(
                c,
                model_version_id=int(model_version_id),
                model_key=str(model_key),
                config=dict(config or {}),
                validation=dict(validation or {}),
            )
        return model_version_id


def set_active_model_version(model_version_id: int) -> None:
    row = get_model_version(model_version_id)
    if not row:
        return
    with _conn() as c:
        c.execute("UPDATE model_versions SET is_active = 0 WHERE model_key = ?", (row["model_key"],))
        c.execute(
            "UPDATE model_versions SET is_active = 1, status = ?, updated_at = ? WHERE id = ?",
            ("active", datetime.now().isoformat(), int(model_version_id)),
        )


def update_model_version_status(
    model_version_id: int, *, status: str | None = None, is_active: bool | None = None
) -> None:
    fields: List[str] = []
    params: List[Any] = []
    if status is not None:
        fields.append("status = ?")
        params.append(str(status))
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(int(model_version_id))
    with _conn() as c:
        c.execute(f"UPDATE model_versions SET {', '.join(fields)} WHERE id = ?", tuple(params))


def update_model_version_validation(model_version_id: int, *, validation: dict[str, Any]) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE model_versions SET validation_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(validation or {}, ensure_ascii=False), now, int(model_version_id)),
        )


def update_model_version_metrics(model_version_id: int, *, metrics: dict[str, Any]) -> None:
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE model_versions SET metrics_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metrics or {}, ensure_ascii=False), now, int(model_version_id)),
        )


def _model_version_from_row(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "model_key": str(r["model_key"] or ""),
        "version": str(r["version"] or ""),
        "status": str(r["status"] or ""),
        "artifact_path": str(r["artifact_path"] or ""),
        "config": _json_load(r["config_json"], {}),
        "metrics": _json_load(r["metrics_json"], {}),
        "validation": _json_load(r["validation_json"], {}),
        "is_active": bool(int(r["is_active"] or 0)),
        "created_at": str(r["created_at"] or ""),
        "updated_at": str(r["updated_at"] or ""),
    }


def get_model_version(model_version_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute("SELECT * FROM model_versions WHERE id = ?", (int(model_version_id),))
        row = cur.fetchone()
    return _model_version_from_row(row) if row else None


def get_active_model_version(model_key: str = "main") -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM model_versions WHERE model_key = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (str(model_key),),
        )
        row = cur.fetchone()
    return _model_version_from_row(row) if row else None


def list_model_versions(model_key: str = "main", limit: int = 20) -> List[Dict[str, Any]]:
    n = max(1, min(int(limit or 20), 200))
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM model_versions WHERE model_key = ? ORDER BY id DESC LIMIT ?",
            (str(model_key), n),
        )
        rows = cur.fetchall()
    return [_model_version_from_row(r) for r in rows]


def _train_run_from_row(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "status": str(r["status"] or ""),
        "phase": str(r["phase"] or ""),
        "progress": float(r["progress"] or 0),
        "message": str(r["message"] or ""),
        "model_version_id": int(r["model_version_id"]) if r["model_version_id"] is not None else None,
        "metrics": _json_load(r["metrics_json"], {}),
        "error": str(r["error"] or ""),
        "started_at": str(r["started_at"] or ""),
        "finished_at": str(r["finished_at"] or ""),
        "created_at": str(r["created_at"] or ""),
        "updated_at": str(r["updated_at"] or ""),
    }


def get_model_train_run(run_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute("SELECT * FROM model_train_runs WHERE id = ?", (int(run_id),))
        row = cur.fetchone()
    return _train_run_from_row(row) if row else None


def get_latest_model_train_run() -> Optional[Dict[str, Any]]:
    with _conn() as c:
        cur = c.execute("SELECT * FROM model_train_runs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    return _train_run_from_row(row) if row else None


def list_model_train_runs(limit: int = 20) -> List[Dict[str, Any]]:
    n = max(1, min(int(limit or 20), 200))
    with _conn() as c:
        cur = c.execute("SELECT * FROM model_train_runs ORDER BY id DESC LIMIT ?", (n,))
        rows = cur.fetchall()
    return [_train_run_from_row(r) for r in rows]


def save_performance_reports(task_id: int | None, reports: dict[str, list[dict[str, Any]]]) -> int:
    from app.services.production_strategy import resolve_production_strategy_champion

    champion = resolve_production_strategy_champion()
    """Persist the latest period snapshots idempotently."""
    now = datetime.now().isoformat()
    saved = 0

    def normalize_report(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row or {})
        total_cost = payload.get("total_cost", payload.get("transaction_cost", payload.get("cost", 0.0)))
        turnover = payload.get("turnover_pct", payload.get("turnover_rate", 0.0))
        attribution = payload.get("attribution")
        if not isinstance(attribution, dict):
            attribution = {
                "schema_version": "performance_attribution_v1",
                "stock_selection": payload.get("stock_selection_contribution", 0.0),
                "industry_allocation": payload.get("industry_allocation_contribution", 0.0),
                "market": payload.get("market_contribution", 0.0),
                "execution": payload.get("execution_contribution", 0.0),
                "cash_defensive": payload.get("cash_defensive_contribution", 0.0),
                "status": payload.get("attribution_status", "complete"),
                "limitations": payload.get("attribution_limitations", []),
            }
        payload.update({
            "schema_version": payload.get("schema_version") or "formal_performance_report_v1",
            "period_type": payload.get("period_type") or kind,
            "return": payload.get("return", payload.get("return_pct", payload.get("period_return", 0.0))),
            "period_return": payload.get("period_return", payload.get("return_pct", 0.0)),
            "drawdown": payload.get("drawdown", payload.get("max_drawdown_pct", payload.get("drawdown_pct", 0.0))),
            "max_drawdown": payload.get("max_drawdown", payload.get("max_drawdown_pct", 0.0)),
            "cost": total_cost,
            "total_cost": total_cost,
            "costs": payload.get("costs") or {
                "total": total_cost,
                "commission": payload.get("commission", 0.0),
                "transfer_fee": payload.get("transfer_fee", 0.0),
                "stamp_tax": payload.get("stamp_tax", 0.0),
            },
            "turnover_pct": turnover,
            "attribution": attribution,
            "frozen": True,
            "frozen_at": payload.get("frozen_at") or now,
            "production_strategy_model_key": champion["champion_model_key"],
            "production_strategy_version_id": champion["champion_id"],
            "production_strategy_version": champion["champion_version"],
            "strategy_controller_version_id": champion["strategy_controller_version_id"],
            "strategy_controller_version": champion["strategy_controller_version"],
        })
        return payload

    with _conn() as c:
        scope_key = "global" if task_id is None else f"task:{int(task_id)}"
        for kind, rows in reports.items():
            for row in rows:
                period = str(row.get("period") or "")
                if not period:
                    continue
                payload = normalize_report(str(kind), row)
                c.execute("""INSERT INTO quant_performance_reports
                    (task_id, scope_key, period_type, period, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (scope_key, period_type, period) DO UPDATE SET
                    payload_json=EXCLUDED.payload_json, created_at=EXCLUDED.created_at""",
                    (task_id, scope_key, kind, period, json.dumps(payload, ensure_ascii=False), now))
                saved += 1
    return saved


def list_performance_reports(task_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    n = max(1, min(int(limit or 100), 500))
    columns = "id,task_id,scope_key,period_type,period,payload_json,created_at"
    with _conn() as c:
        if task_id is None:
            cur = c.execute(f"SELECT {columns} FROM quant_performance_reports ORDER BY id DESC LIMIT ?", (n,))
        else:
            cur = c.execute(f"SELECT {columns} FROM quant_performance_reports WHERE task_id = ? ORDER BY id DESC LIMIT ?", (int(task_id), n))
        rows = cur.fetchall()
    return [{
        "id": int(r["id"]),
        "task_id": r["task_id"],
        "scope_key": r.get("scope_key") if hasattr(r, "get") else r["scope_key"],
        "period_type": r["period_type"],
        "period": r["period"],
        "payload": _json_load(r["payload_json"], {}),
        "created_at": r["created_at"],
    } for r in rows]

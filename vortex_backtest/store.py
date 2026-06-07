from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import AccountCreate, EngineName, OrderCreate


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT,
    initial_cash REAL NOT NULL,
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    order_batch_id TEXT NOT NULL DEFAULT 'default',
    request_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_type TEXT,
    limit_price REAL,
    comment TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, order_batch_id, request_id),
    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    order_batch_id TEXT NOT NULL DEFAULT 'default',
    market_data_set_id TEXT NOT NULL DEFAULT 'default-qfq',
    frequency TEXT NOT NULL DEFAULT '1min',
    price_adjustment TEXT NOT NULL DEFAULT 'qfq',
    order_price_adjustment TEXT NOT NULL DEFAULT 'qfq',
    default_price_type TEXT NOT NULL DEFAULT 'close',
    status TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    report_dir TEXT,
    summary_json TEXT,
    request_json TEXT,
    progress_json TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
);
CREATE TABLE IF NOT EXISTS strategy_meta (
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, strategy_id)
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_dt(value: datetime) -> str:
    return value.isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


class DataStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "vortex_backtest.sqlite3"
        self.report_root = self.state_dir / "reports"
        self.report_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_account_engines(conn)
            self._migrate_orders_table(conn)
            self._migrate_jobs_table(conn)
            self._ensure_indexes(conn)

    def _migrate_account_engines(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE accounts SET engine = ? WHERE engine IN (?, ?, ?, ?)",
            ("replay", "backtrader", "rqalpha", "ashare_replay", "qlib"),
        )

    def _migrate_orders_table(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "orders")
        unique_columns = self._unique_index_columns(conn, "orders")
        desired_unique = ("account_id", "order_batch_id", "request_id")
        if "order_batch_id" in columns and desired_unique in unique_columns:
            return

        conn.execute("ALTER TABLE orders RENAME TO orders_old")
        conn.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                order_batch_id TEXT NOT NULL DEFAULT 'default',
                request_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_type TEXT,
                limit_price REAL,
                comment TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, order_batch_id, request_id),
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            )
            """
        )
        old_columns = self._table_columns(conn, "orders_old")
        order_batch_expr = (
            "COALESCE(order_batch_id, 'default')" if "order_batch_id" in old_columns else "'default'"
        )
        price_type_expr = "price_type" if "price_type" in old_columns else "NULL"
        conn.execute(
            f"""
            INSERT INTO orders(
                id, account_id, order_batch_id, request_id, trade_date, symbol,
                side, quantity, price_type, limit_price, comment, created_at
            )
            SELECT
                id, account_id, {order_batch_expr}, request_id, trade_date, symbol,
                side, quantity, {price_type_expr}, limit_price, comment, created_at
            FROM orders_old
            """
        )
        conn.execute("DROP TABLE orders_old")

    def _migrate_jobs_table(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "jobs")
        if "order_batch_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN order_batch_id TEXT NOT NULL DEFAULT 'default'")
        if "market_data_set_id" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN market_data_set_id TEXT NOT NULL DEFAULT 'default-qfq'"
            )
        if "frequency" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN frequency TEXT NOT NULL DEFAULT '1min'")
        if "price_adjustment" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN price_adjustment TEXT NOT NULL DEFAULT 'qfq'")
        if "order_price_adjustment" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN order_price_adjustment TEXT NOT NULL DEFAULT 'qfq'"
            )
        if "default_price_type" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN default_price_type TEXT NOT NULL DEFAULT 'close'")
        if "request_json" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN request_json TEXT")
        if "progress_json" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_account_batch_date
            ON orders(account_id, order_batch_id, trade_date, id)
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)"
        )

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _unique_index_columns(self, conn: sqlite3.Connection, table_name: str) -> set[tuple[str, ...]]:
        result: set[tuple[str, ...]] = set()
        indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        for index in indexes:
            if int(index["unique"]) != 1:
                continue
            rows = conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
            columns = tuple(str(row["name"]) for row in sorted(rows, key=lambda row: row["seqno"]))
            result.add(columns)
        return result

    def create_account(self, payload: AccountCreate) -> dict[str, Any]:
        created_at = encode_dt(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(account_id, name, initial_cash, engine, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload.account_id,
                    payload.name,
                    payload.initial_cash,
                    payload.engine.value,
                    created_at,
                ),
            )
        return self.get_account(payload.account_id)

    def get_account(self, account_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return dict(row)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY created_at, account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_order(self, account_id: str, payload: OrderCreate) -> dict[str, Any]:
        self.get_account(account_id)
        created_at = encode_dt(utc_now())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders(
                    account_id, order_batch_id, request_id, trade_date, symbol, side, quantity,
                    price_type, limit_price, comment, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    payload.order_batch_id,
                    payload.request_id,
                    payload.trade_date.isoformat(),
                    payload.symbol,
                    int(payload.side.value),
                    payload.quantity,
                    payload.price_type.value if payload.price_type else None,
                    payload.limit_price,
                    payload.comment,
                    created_at,
                ),
            )
            order_id = int(cursor.lastrowid)
        return self.get_order(account_id, order_id)

    def get_order(self, account_id: str, order_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE account_id = ? AND id = ?",
                (account_id, order_id),
            ).fetchone()
        if row is None:
            raise KeyError(str(order_id))
        return dict(row)

    def list_orders(
        self,
        account_id: str,
        order_batch_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [account_id]
        conditions = ["account_id = ?"]
        if order_batch_id is not None:
            conditions.append("order_batch_id = ?")
            params.append(order_batch_id)
        if start_date is not None:
            conditions.append("trade_date >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            conditions.append("trade_date <= ?")
            params.append(end_date.isoformat())
        sql = f"""
            SELECT * FROM orders
            WHERE {" AND ".join(conditions)}
            ORDER BY trade_date, id
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def create_job(
        self,
        job_id: str,
        account_id: str,
        order_batch_id: str,
        market_data_set_id: str,
        frequency: str,
        price_adjustment: str,
        order_price_adjustment: str,
        default_price_type: str,
        start_date: date | None,
        end_date: date | None,
        request_json: str | None = None,
    ) -> dict[str, Any]:
        # 入队（异步作业模型，ADR-3）：建作业即为 'queued'，由后台 worker 领取执行。
        created_at = encode_dt(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                    job_id, account_id, order_batch_id, market_data_set_id,
                    frequency, price_adjustment, order_price_adjustment, default_price_type,
                    status, start_date, end_date, created_at, request_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    account_id,
                    order_batch_id,
                    market_data_set_id,
                    frequency,
                    price_adjustment,
                    order_price_adjustment,
                    default_price_type,
                    start_date.isoformat() if start_date else None,
                    end_date.isoformat() if end_date else None,
                    created_at,
                    request_json,
                ),
            )
        return self.get_job(job_id)

    def claim_next_queued_job(self) -> dict[str, Any] | None:
        """原子领取一个 queued 作业并置为 running；无则返回 None。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY created_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            cursor = conn.execute(
                "UPDATE jobs SET status = 'running' WHERE job_id = ? AND status = 'queued'",
                (job_id,),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_job(job_id)

    def update_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress_json = ? WHERE job_id = ?",
                (json.dumps(progress, ensure_ascii=False), job_id),
            )

    def requeue_interrupted(self) -> int:
        """启动时把上次残留的 running 作业重新入队（worker 崩溃恢复）。返回重排数量。"""
        with self.connect() as conn:
            cursor = conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
            return int(cursor.rowcount)

    def complete_job(self, job_id: str, report_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
        completed_at = encode_dt(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'completed',
                    completed_at = ?,
                    report_dir = ?,
                    summary_json = ?
                WHERE job_id = ?
                """,
                (completed_at, str(report_dir), json.dumps(summary, ensure_ascii=False), job_id),
            )
        return self.get_job(job_id)

    def fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        completed_at = encode_dt(utc_now())
        summary = {"error": error}
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    completed_at = ?,
                    summary_json = ?
                WHERE job_id = ?
                """,
                (completed_at, json.dumps(summary, ensure_ascii=False), job_id),
            )
        return self.get_job(job_id)

    def cancel_queued_job(self, job_id: str) -> bool:
        """取消**排队中**的作业（置 cancelled）。运行中无法安全中断（同步执行），返回 False。"""
        completed_at = encode_dt(utc_now())
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'cancelled', completed_at = ? WHERE job_id = ? AND status = 'queued'",
                (completed_at, job_id),
            )
            return cursor.rowcount == 1

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def latest_completed_job(self, account_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE account_id = ? AND status = 'completed'
                ORDER BY completed_at DESC, created_at DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return dict(row)

    def list_jobs(
        self, account_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        conditions: list[str] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [dict(row) for row in rows]

    def get_strategy_meta(self, account_id: str) -> dict[str, dict[str, Any]]:
        """{strategy_id: {favorite, pinned, tags[]}}（看板的收藏/置顶/标签,非策略定义）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_meta WHERE account_id = ?", (account_id,)
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out[str(row["strategy_id"])] = {
                "favorite": bool(row["favorite"]),
                "pinned": bool(row["pinned"]),
                "tags": json.loads(row["tags"]) if row["tags"] else [],
            }
        return out

    def set_strategy_meta(
        self,
        account_id: str,
        strategy_id: str,
        *,
        favorite: bool | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        current = self.get_strategy_meta(account_id).get(
            strategy_id, {"favorite": False, "pinned": False, "tags": []}
        )
        fav = current["favorite"] if favorite is None else bool(favorite)
        pin = current["pinned"] if pinned is None else bool(pinned)
        tag = current["tags"] if tags is None else [str(t) for t in tags]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_meta(account_id, strategy_id, favorite, pinned, tags, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, strategy_id) DO UPDATE SET
                    favorite = excluded.favorite, pinned = excluded.pinned,
                    tags = excluded.tags, updated_at = excluded.updated_at
                """,
                (account_id, strategy_id, int(fav), int(pin),
                 json.dumps(tag, ensure_ascii=False), encode_dt(utc_now())),
            )
        return {"favorite": fav, "pinned": pin, "tags": tag}


def normalize_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row["account_id"],
        "name": row["name"],
        "initial_cash": row["initial_cash"],
        "engine": EngineName(row["engine"]),
        "created_at": parse_dt(row["created_at"]),
    }


def normalize_order(row: dict[str, Any]) -> dict[str, Any]:
    side = int(row["side"])
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "order_batch_id": row.get("order_batch_id", "default"),
        "request_id": row["request_id"],
        "trade_date": parse_date(row["trade_date"]),
        "symbol": row["symbol"],
        "side": side,
        "side_name": "BUY" if side == 1 else "SELL",
        "quantity": row["quantity"],
        "price_type": row["price_type"],
        "limit_price": row["limit_price"],
        "comment": row["comment"],
        "created_at": parse_dt(row["created_at"]),
    }


def normalize_job(row: dict[str, Any]) -> dict[str, Any]:
    summary_json = row.get("summary_json")
    return {
        "job_id": row["job_id"],
        "account_id": row["account_id"],
        "order_batch_id": row.get("order_batch_id", "default"),
        "market_data_set_id": row.get("market_data_set_id", "default-qfq"),
        "frequency": row.get("frequency", "1min"),
        "price_adjustment": row.get("price_adjustment", "qfq"),
        "order_price_adjustment": row.get("order_price_adjustment", "qfq"),
        "default_price_type": row.get("default_price_type", "close"),
        "status": row["status"],
        "start_date": parse_date(row["start_date"]) if row["start_date"] else None,
        "end_date": parse_date(row["end_date"]) if row["end_date"] else None,
        "created_at": parse_dt(row["created_at"]),
        "completed_at": parse_dt(row["completed_at"]) if row["completed_at"] else None,
        "report_dir": Path(row["report_dir"]) if row["report_dir"] else None,
        "summary": json.loads(summary_json) if summary_json else None,
        "progress": json.loads(row["progress_json"]) if row.get("progress_json") else None,
    }

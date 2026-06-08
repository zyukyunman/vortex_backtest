from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import AccountCreate, EngineName


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT,
    initial_cash REAL NOT NULL,
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',          -- open | closed
    level TEXT NOT NULL DEFAULT '1min',           -- daily | 1min
    start_date TEXT,
    end_date TEXT,
    sim_time TEXT,                                 -- 当前模拟时钟（单调推进）
    cash REAL NOT NULL,
    initial_cash REAL NOT NULL,
    positions_json TEXT NOT NULL DEFAULT '{}',     -- {symbol: {quantity,cost_basis,sellable_quantity}}
    universe_json TEXT NOT NULL DEFAULT '[]',
    open_orders_json TEXT NOT NULL DEFAULT '[]',   -- next_bar 停泊队列
    config_json TEXT NOT NULL DEFAULT '{}',        -- fill_timing/slippage/fees/default_price_type/...
    trade_counter INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
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

    def _migrate_account_engines(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE accounts SET engine = ? WHERE engine IN (?, ?, ?, ?)",
            ("replay", "backtrader", "rqalpha", "ashare_replay", "qlib"),
        )

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

    # ------------------------------------------------------------------
    # 会话（design/18：sessions/data/advance/close）
    # ------------------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        account_id: str,
        level: str,
        start_date: str | None,
        end_date: str | None,
        sim_time: str | None,
        initial_cash: float,
        universe: list[str],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        self.get_account(account_id)  # 不存在 → KeyError
        now = encode_dt(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(
                    session_id, account_id, status, level, start_date, end_date,
                    sim_time, cash, initial_cash, positions_json, universe_json,
                    open_orders_json, config_json, trade_counter, created_at, updated_at
                )
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, '{}', ?, '[]', ?, 0, ?, ?)
                """,
                (
                    session_id, account_id, level, start_date, end_date,
                    sim_time, initial_cash, initial_cash,
                    json.dumps(list(universe)), json.dumps(config), now, now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def list_sessions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if account_id:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE account_id = ? ORDER BY created_at",
                    (account_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        """更新会话可变状态。允许列：status/sim_time/cash/positions_json/universe_json/
        open_orders_json/config_json/trade_counter。"""
        allowed = {
            "status", "sim_time", "cash", "positions_json", "universe_json",
            "open_orders_json", "config_json", "trade_counter",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return self.get_session(session_id)
        sets["updated_at"] = encode_dt(utc_now())
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET {assignments} WHERE session_id = ?",
                (*sets.values(), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(session_id)
        return self.get_session(session_id)

def normalize_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row["account_id"],
        "name": row["name"],
        "initial_cash": row["initial_cash"],
        "engine": EngineName(row["engine"]),
        "created_at": parse_dt(row["created_at"]),
    }

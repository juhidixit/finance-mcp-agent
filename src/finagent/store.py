"""SQLite system of record for the synthetic company.

Every MCP server opens the same database file. Money is stored as integer cents;
the tool layer converts to and from dollars.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, department TEXT NOT NULL,
    title TEXT NOT NULL, manager_id TEXT
);
CREATE TABLE IF NOT EXISTS gl_accounts (code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, merchant TEXT NOT NULL, mcc TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, txn_date TEXT NOT NULL, attendees INTEGER NOT NULL DEFAULT 1,
    memo TEXT NOT NULL DEFAULT '', receipt_id TEXT, gl_code TEXT,
    flagged INTEGER NOT NULL DEFAULT 0, flag_reason TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY, txn_id TEXT, merchant TEXT NOT NULL, total_cents INTEGER NOT NULL,
    line_items TEXT NOT NULL, notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS expense_claims (
    id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, expense_date TEXT NOT NULL, gl_code TEXT NOT NULL,
    status TEXT NOT NULL, receipt_id TEXT
);
CREATE TABLE IF NOT EXISTS reimbursements (
    id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
    claim_ids TEXT NOT NULL, memo TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, bank_account TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bills (
    id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, due_date TEXT NOT NULL,
    description TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, vendor_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
    bank_account TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, to_addr TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Tables an episode may write to; graders diff these.
MUTABLE_TABLES = ("transactions", "expense_claims", "reimbursements", "vendors", "bills", "payments", "outbox")


def dollars(cents: int) -> float:
    return round(cents / 100.0, 2)


def cents(amount: float) -> int:
    return int(round(float(amount) * 100))


class Store:
    """Thin wrapper over a SQLite file. Safe to open from several servers in one process."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    @classmethod
    def create(cls, path: str | Path) -> Store:
        p = Path(path)
        if p.exists():
            p.unlink()
        s = cls(p)
        s.conn.executescript(SCHEMA)
        return s

    # -- generic helpers -------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        self.conn.execute(sql, tuple(params))

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        qs = ",".join("?" for _ in cols)
        self.conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({qs})", [tuple(r[c] for c in cols) for r in rows]
        )

    def next_id(self, table: str, prefix: str) -> str:
        n = self.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]  # type: ignore[index]
        return f"{prefix}{n + 1:04d}"

    def get_meta(self, key: str, default: str = "") -> str:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Full copy of mutable tables, used by graders to diff state."""
        return {t: self.q(f"SELECT * FROM {t} ORDER BY id") for t in MUTABLE_TABLES}

    def close(self) -> None:
        self.conn.close()


def to_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)

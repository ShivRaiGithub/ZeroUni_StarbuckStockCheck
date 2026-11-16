"""Private SQLite store for the stock service. No other service gets access (ServiceBoundary).

All writes go through Database.transaction(): one process-wide lock plus BEGIN IMMEDIATE, so a
check-and-reserve is atomic and two orders can never both take the last unit.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    store_id   TEXT PRIMARY KEY,          -- snapshot ID as a string, leading zeros kept
    store_name TEXT NOT NULL,
    store_open INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    component_name TEXT PRIMARY KEY,
    component_id   TEXT NOT NULL,
    component_type TEXT NOT NULL CHECK (component_type IN ('drink_base', 'add_in'))
);
CREATE TABLE IF NOT EXISTS store_stock (
    store_id          TEXT NOT NULL REFERENCES stores(store_id),
    component_name    TEXT NOT NULL REFERENCES components(component_name),
    quantity_on_hand  INTEGER NOT NULL CHECK (quantity_on_hand >= 0),
    reserved_quantity INTEGER NOT NULL CHECK (reserved_quantity >= 0),
    last_updated_at   TEXT,
    PRIMARY KEY (store_id, component_name)
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id         TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    store_id        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active', 'confirmed', 'released', 'expired')),
    expires_at      TEXT NOT NULL,
    expires_epoch   REAL NOT NULL,
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);
CREATE TABLE IF NOT EXISTS hold_items (
    hold_id        TEXT NOT NULL REFERENCES holds(hold_id),
    position       INTEGER NOT NULL,
    component_name TEXT NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (hold_id, component_name)
);
CREATE TABLE IF NOT EXISTS idempotency (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    body         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def seed(self, rows: list[dict]) -> None:
        """Load snapshot-format rows once (only into an empty database). The snapshot is never written."""
        with self.transaction() as c:
            if c.execute("SELECT COUNT(*) FROM stores").fetchone()[0]:
                return
            for r in rows:
                c.execute("INSERT OR IGNORE INTO stores VALUES (?, ?, ?)",
                          (r["store_id"], r["store_name"], r["store_open"].strip().lower() == "true"))
                c.execute("INSERT OR IGNORE INTO components VALUES (?, ?, ?)",
                          (r["component_name"], r["component_id"], r["component_type"]))
                c.execute("INSERT OR REPLACE INTO store_stock VALUES (?, ?, ?, ?, ?)",
                          (r["store_id"], r["component_name"], int(r["quantity_on_hand"]),
                           int(r["reserved_quantity"]), r.get("last_updated_at")))

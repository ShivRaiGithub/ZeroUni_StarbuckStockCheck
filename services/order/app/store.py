"""The order service's private SQLite store. Holds orders only: no stock counts, no store status.

Every durable save names a save point and goes through `fail_saves` first (a test-only hook; the
image entry point always passes None). Save points: "order", "attempt", "hold_result", "paying",
plus "charged", "rehold", "refunding", "status" and "final".
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    request         TEXT NOT NULL,          -- normalised order (drinks with line_item)
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,  -- last hold attempt n saved before :hold:n was sent
    rehold          INTEGER NOT NULL DEFAULT 0,
    hold_id         TEXT,
    payment_id      TEXT,
    confirm_pending INTEGER NOT NULL DEFAULT 0,
    pending_refund  TEXT,                   -- the refund response to give once the refund is done
    final_status    INTEGER,
    final_body      TEXT
);
"""


class StorageError(Exception):
    """A save did not happen. The caller must not send anything that depends on it."""


class OrderStore:
    def __init__(self, path: str, fail_saves: Callable[[str], bool] | None = None):
        self.fail_saves = fail_saves
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _check(self, point: str) -> None:
        if self.fail_saves is not None and self.fail_saves(point):
            raise StorageError(f"save point {point!r} failed")

    def get(self, order_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["request"] = json.loads(rec["request"])
        return rec

    def create(self, order_id: str, request_hash: str, store_id: str, request: dict) -> dict:
        self._check("order")
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO orders (order_id, request_hash, store_id, request, status) VALUES (?, ?, ?, ?, ?)",
                    (order_id, request_hash, store_id, json.dumps(request, ensure_ascii=False), "holding"))
        except sqlite3.Error as e:
            raise StorageError(str(e)) from e
        return self.get(order_id)

    def update(self, order_id: str, point: str, **fields) -> dict:
        self._check(point)
        if "final_body" in fields and not isinstance(fields["final_body"], (str, type(None))):
            fields["final_body"] = json.dumps(fields["final_body"], ensure_ascii=False)
        if "pending_refund" in fields and not isinstance(fields["pending_refund"], (str, type(None))):
            fields["pending_refund"] = json.dumps(fields["pending_refund"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        try:
            with self.lock:
                self.conn.execute(f"UPDATE orders SET {cols} WHERE order_id = ?", (*fields.values(), order_id))
        except sqlite3.Error as e:
            raise StorageError(str(e)) from e
        return self.get(order_id)

    def with_status(self, *statuses: str) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        with self.lock:
            ids = [r[0] for r in self.conn.execute(f"SELECT order_id FROM orders WHERE status IN ({marks})", statuses)]
        return [self.get(i) for i in ids]

    def confirm_pending(self) -> list[dict]:
        with self.lock:
            ids = [r[0] for r in self.conn.execute("SELECT order_id FROM orders WHERE confirm_pending = 1")]
        return [self.get(i) for i in ids]

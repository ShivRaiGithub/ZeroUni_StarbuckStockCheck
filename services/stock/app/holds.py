"""Holds: the single authoritative "can this store make these line items right now" check.

available = quantity_on_hand - reserved_quantity. Every hold is confirmed, released, or expires on
its own; each terminal transition changes reserved_quantity exactly once, in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone

from . import catalog
from .db import Database

MAX_TTL_SECONDS = 600
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "SB-"


def public_id(store_id: str) -> str:
    return PREFIX + store_id


def internal_id(public: str) -> str:
    return public[len(PREFIX):]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def err(code: str, message: str, details: list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or []}}


def new_hold_id() -> str:
    return "hold_" + "".join(secrets.choice(CROCKFORD) for _ in range(26))


def request_hash(req) -> str:
    # expires_at is excluded so a retry that recomputes it is still the same request.
    canonical = {"store_id": req.store_id, "lines": [ln.model_dump() for ln in req.lines]}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def component_type(c, name: str) -> str:
    row = c.execute("SELECT component_type FROM components WHERE component_name = ?", (name,)).fetchone()
    return row["component_type"] if row else (catalog.catalog_type(name) or "add_in")


def hold_dict(c, hold) -> dict:
    items = c.execute("SELECT component_name, quantity FROM hold_items WHERE hold_id = ? ORDER BY position",
                      (hold["hold_id"],)).fetchall()
    return {"hold_id": hold["hold_id"], "store_id": public_id(hold["store_id"]), "status": hold["status"],
            "expires_at": hold["expires_at"],
            "items": [{"component_name": i["component_name"], "quantity": i["quantity"]} for i in items]}


def _close(c, hold, status: str, now: datetime, consume: bool) -> None:
    for item in c.execute("SELECT component_name, quantity FROM hold_items WHERE hold_id = ?", (hold["hold_id"],)):
        if consume:
            c.execute("UPDATE store_stock SET quantity_on_hand = MAX(0, quantity_on_hand - ?), "
                      "reserved_quantity = MAX(0, reserved_quantity - ?) WHERE store_id = ? AND component_name = ?",
                      (item["quantity"], item["quantity"], hold["store_id"], item["component_name"]))
        else:
            c.execute("UPDATE store_stock SET reserved_quantity = MAX(0, reserved_quantity - ?) "
                      "WHERE store_id = ? AND component_name = ?",
                      (item["quantity"], hold["store_id"], item["component_name"]))
    c.execute("UPDATE holds SET status = ?, closed_at = ? WHERE hold_id = ? AND status = 'active'",
              (status, now.isoformat(), hold["hold_id"]))


def expire_overdue(c, now: datetime) -> int:
    overdue = c.execute("SELECT * FROM holds WHERE status = 'active' AND expires_epoch <= ?",
                        (now.timestamp(),)).fetchall()
    for hold in overdue:
        _close(c, hold, "expired", now, consume=False)
    return len(overdue)


class StockService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ holds

    def sweep(self) -> int:
        with self.db.transaction() as c:
            return expire_overdue(c, now_utc())

    def create_hold(self, key: str, req) -> tuple[int, dict]:
        now = now_utc()
        with self.db.transaction() as c:
            expire_overdue(c, now)
            h = request_hash(req)
            prior = c.execute("SELECT request_hash, status_code, body FROM idempotency WHERE key = ?",
                              (key,)).fetchone()
            if prior:
                if prior["request_hash"] != h:
                    return 409, err("idempotency_key_reused",
                                    f"Idempotency-Key {key} was already used with a different request.")
                return prior["status_code"], json.loads(prior["body"])
            if c.execute("SELECT 1 FROM tombstones WHERE key = ?", (key,)).fetchone():
                return 409, err("hold_released", f"Idempotency-Key {key} was released before this hold arrived.")
            ttl = (req.expires_at - now).total_seconds()
            if not 0 < ttl <= MAX_TTL_SECONDS:
                return 400, err("invalid_expiry", f"expires_at must be in the future and at most "
                                                  f"{MAX_TTL_SECONDS} s ahead.",
                                [{"reason": "invalid_expiry", "field": "expires_at"}])
            status, payload = self._evaluate_and_reserve(c, key, req, now)
            c.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)", (key, h, status, json.dumps(payload)))
            return status, payload

    def _evaluate_and_reserve(self, c, key, req, now) -> tuple[int, dict]:
        store = c.execute("SELECT * FROM stores WHERE store_id = ?", (internal_id(req.store_id),)).fetchone()
        if store is None:
            return 404, err("unknown_store", f"Store {req.store_id} does not exist.",
                            [{"reason": "unknown_store", "store_id": req.store_id}])
        seeded_add_ins = frozenset(r["component_name"] for r in c.execute(
            "SELECT component_name FROM components WHERE component_type = 'add_in'"))
        problems = catalog.unrecognised(req.lines, seeded_add_ins)
        if problems:
            return 400, err("invalid_order", catalog.unrecognised_message(problems), problems)
        if not store["store_open"]:
            return 409, err("store_closed", f"Store {req.store_id} is closed.",
                            [{"reason": "store_closed", "store_id": req.store_id, "store_name": store["store_name"]}])

        needs = catalog.needs(req.lines)
        rows = {r["component_name"]: r for r in c.execute(
            "SELECT * FROM store_stock WHERE store_id = ?", (store["store_id"],))}
        shortfalls = []
        for comp, n in needs.items():
            row = rows.get(comp)
            available = row["quantity_on_hand"] - row["reserved_quantity"] if row else 0
            if available < n["need"]:
                ctype = component_type(c, comp)
                if available <= 0:
                    reason = "base_out_of_stock" if ctype == "drink_base" else "add_in_out_of_stock"
                else:
                    reason = "insufficient_quantity"
                shortfalls.append({"reason": reason, "line_item": n["line_item"], "missing_component": comp,
                                   "component_type": ctype, "needed": n["need"], "available": available})
        if shortfalls:
            return 409, err("component_unavailable",
                            f"{len(shortfalls)} component(s) short at {req.store_id}.", shortfalls)

        hold_id = new_hold_id()
        expires_at = req.expires_at.astimezone(timezone.utc)
        c.execute("INSERT INTO holds VALUES (?, ?, ?, 'active', ?, ?, ?, NULL)",
                  (hold_id, key, store["store_id"], expires_at.isoformat(), expires_at.timestamp(), now.isoformat()))
        for position, (comp, n) in enumerate(needs.items()):
            c.execute("UPDATE store_stock SET reserved_quantity = reserved_quantity + ?, last_updated_at = ? "
                      "WHERE store_id = ? AND component_name = ?", (n["need"], now.isoformat(), store["store_id"], comp))
            c.execute("INSERT INTO hold_items VALUES (?, ?, ?, ?)", (hold_id, position, comp, n["need"]))
        hold = c.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
        return 201, hold_dict(c, hold)

    def get_hold(self, hold_id: str) -> tuple[int, dict]:
        with self.db.transaction() as c:
            expire_overdue(c, now_utc())
            hold = c.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
            if hold is None:
                return 404, err("hold_not_found", f"Hold {hold_id} does not exist.")
            return 200, hold_dict(c, hold)

    def confirm(self, hold_id: str) -> tuple[int, dict]:
        return self._finish(hold_id, target="confirmed", consume=True, idempotent_from={"confirmed"})

    def release(self, hold_id: str) -> tuple[int, dict]:
        return self._finish(hold_id, target="released", consume=False, idempotent_from={"released", "expired"})

    def _finish(self, hold_id, target, consume, idempotent_from) -> tuple[int, dict]:
        now = now_utc()
        with self.db.transaction() as c:
            expire_overdue(c, now)
            hold = c.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
            if hold is None:
                return 404, err("hold_not_found", f"Hold {hold_id} does not exist.")
            if hold["status"] == "active":
                _close(c, hold, target, now, consume=consume)
                hold = c.execute("SELECT * FROM holds WHERE hold_id = ?", (hold_id,)).fetchone()
            elif hold["status"] not in idempotent_from:
                return 409, err("hold_not_active", f"Hold {hold_id} is {hold['status']}; cannot be {target}.")
            return 200, hold_dict(c, hold)

    def release_by_key(self, key: str) -> tuple[int, dict]:
        now = now_utc()
        with self.db.transaction() as c:
            expire_overdue(c, now)
            hold = c.execute("SELECT * FROM holds WHERE idempotency_key = ?", (key,)).fetchone()
            if hold is None:
                c.execute("INSERT OR IGNORE INTO tombstones VALUES (?, ?)", (key, now.isoformat()))
                return 200, {"idempotency_key": key, "outcome": "tombstoned", "hold_id": None}
            if hold["status"] == "active":
                _close(c, hold, "released", now, consume=False)
                return 200, {"idempotency_key": key, "outcome": "released", "hold_id": hold["hold_id"]}
            return 200, {"idempotency_key": key, "outcome": "already_closed", "hold_id": hold["hold_id"]}

    # ------------------------------------------------------------------ stock

    def availability(self, public: str) -> tuple[int, dict]:
        with self.db.transaction() as c:
            expire_overdue(c, now_utc())
            store = c.execute("SELECT * FROM stores WHERE store_id = ?", (internal_id(public),)).fetchone()
            if store is None:
                return 404, err("unknown_store", f"Store {public} does not exist.",
                                [{"reason": "unknown_store", "store_id": public}])
            rows = c.execute("SELECT s.*, k.component_type FROM store_stock s JOIN components k "
                             "USING (component_name) WHERE s.store_id = ? ORDER BY k.component_type, s.component_name",
                             (store["store_id"],)).fetchall()
            components = []
            for r in rows:
                available = r["quantity_on_hand"] - r["reserved_quantity"]
                components.append({"component_name": r["component_name"], "component_type": r["component_type"],
                                   "available": available, "status": "SOLD OUT" if available <= 0 else "IN STOCK"})
            return 200, {"store_id": public, "store_name": store["store_name"],
                         "store_open": bool(store["store_open"]), "components": components}

    def adjust(self, public: str, component: str, delta: int) -> tuple[int, dict]:
        now = now_utc()
        with self.db.transaction() as c:
            store = c.execute("SELECT * FROM stores WHERE store_id = ?", (internal_id(public),)).fetchone()
            if store is None:
                return 404, err("unknown_store", f"Store {public} does not exist.",
                                [{"reason": "unknown_store", "store_id": public}])
            known = c.execute("SELECT 1 FROM components WHERE component_name = ?", (component,)).fetchone()
            if not known:
                ctype = catalog.catalog_type(component)
                if ctype is None:
                    return 404, err("unknown_component", f"Component {component} does not exist.")
                c.execute("INSERT INTO components VALUES (?, ?, ?)",
                          (component, component.lower().replace(" ", "_"), ctype))
            c.execute("INSERT OR IGNORE INTO store_stock VALUES (?, ?, 0, 0, ?)",
                      (store["store_id"], component, now.isoformat()))
            c.execute("UPDATE store_stock SET quantity_on_hand = MAX(0, quantity_on_hand + ?), last_updated_at = ? "
                      "WHERE store_id = ? AND component_name = ?", (delta, now.isoformat(), store["store_id"], component))
            r = c.execute("SELECT * FROM store_stock WHERE store_id = ? AND component_name = ?",
                          (store["store_id"], component)).fetchone()
            return 200, {"store_id": public, "component_name": component,
                         "quantity_on_hand": r["quantity_on_hand"], "reserved_quantity": r["reserved_quantity"],
                         "available": r["quantity_on_hand"] - r["reserved_quantity"]}

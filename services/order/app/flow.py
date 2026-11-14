"""The order flow (plan.md "What the order does when the hold call goes wrong" and "Save before send").

holding --hold 201--> paying --charged--> confirming --> accepted
   |  stock refusal -> refused (409)          | declined -> payment_declined (hold released at once)
   |  no answer     -> hold_unknown (503)     | no answer -> payment_unknown (503, hold kept)
                                              confirm says hold expired -> reholding -> accepted | refunded
Payment is only ever requested after this service itself received a 201 for the hold.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from . import messages as msg
from .store import OrderStore, StorageError

log = logging.getLogger("order")

HOLD_TIMEOUT = 0.6          # per attempt; one retry with the same Idempotency-Key
RELEASE_AFTER_TIMEOUT = 0.2  # fire-and-forget release-by-key right after a hold timed out
RELEASE_BEFORE_RETRY = 0.6  # must get a 200 before the next hold attempt
PAYMENT_TIMEOUT = 1.0
CONFIRM_TIMEOUT = 0.3
HOLD_TTL_SECONDS = 120
STORE_ID = re.compile(r"^SB-[0-9]+$")
# The request ID of the request being handled; saved with a final error so a repeat replays it exactly.
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

FINAL = {"accepted", "refused", "rejected", "payment_declined", "refunded"}
PUBLIC_STATUS = {"holding": "hold_unknown", "hold_unknown": "hold_unknown", "paying": "payment_unknown",
                 "payment_unknown": "payment_unknown", "confirming": "payment_unknown",
                 "reholding": "payment_unknown", "refunding": "payment_unknown"}


class NoAnswer(Exception):
    """Timed out, could not connect, 5xx, or an unreadable body: the outcome is unknown."""


def error(code: str, message: str, details: list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or []}}


def retryable(code: str, message: str) -> dict:
    return error(code, message, [{"reason": code, "retryable": True}])


# ---------------------------------------------------------------- validation

def validate(raw) -> tuple[dict | None, dict | None]:
    """Shape checks only (400 invalid_order). Names are checked by the stock service's catalog."""
    if not isinstance(raw, dict):
        return None, error("invalid_order", "The order must be a JSON object.",
                           [{"reason": "invalid_value", "field": "order_id"}])
    problems: list[tuple[str, dict]] = []

    def need_str(field):
        value = raw.get(field)
        if value is None or value == "":
            problems.append((f"{field} is required.", {"reason": "missing_field", "field": field}))
        elif not isinstance(value, str):
            problems.append((f"{field} must be a string.", {"reason": "invalid_value", "field": field}))

    need_str("order_id")
    need_str("store_id")
    drinks = raw.get("drinks")
    if not isinstance(drinks, list) or not drinks:
        reason = "missing_field" if drinks is None else ("empty_list" if drinks == [] else "invalid_value")
        problems.append(("At least one drink is required.", {"reason": reason, "field": "drinks"}))
        drinks = []
    lines = []
    for n, d in enumerate(drinks, start=1):
        if not isinstance(d, dict):
            problems.append((f"Line {n}: each drink must be an object.",
                             {"reason": "invalid_value", "field": "drinks", "line_item": n}))
            continue
        for field in ("drink", "size", "milk"):
            if not isinstance(d.get(field), str) or not d.get(field):
                problems.append((f"Line {n}: {field} is required.",
                                 {"reason": "missing_field", "field": field, "line_item": n}))
        add_ins = d.get("add_ins", [])
        if not isinstance(add_ins, list) or not all(isinstance(a, str) and a for a in add_ins):
            problems.append((f"Line {n}: add_ins must be a list of names.",
                             {"reason": "invalid_value", "field": "add_ins", "line_item": n}))
            add_ins = []
        quantity = d.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 20:
            problems.append((f"Line {n}: quantity must be a whole number from 1 to 20.",
                             {"reason": "invalid_value", "field": "quantity", "line_item": n}))
            quantity = 1
        line = {"line_item": n, "drink": d.get("drink"), "size": d.get("size"), "milk": d.get("milk"),
                "add_ins": list(add_ins), "quantity": quantity}
        if isinstance(d.get("total_price"), (int, float)) and not isinstance(d.get("total_price"), bool):
            line["total_price"] = d["total_price"]
        lines.append(line)
    if problems:
        return None, error("invalid_order", " ".join(m for m, _ in problems), [p for _, p in problems])
    return {"order_id": raw["order_id"], "store_id": raw["store_id"],
            "submitted_at": raw.get("submitted_at"), "drinks": lines}, None


def request_hash(order: dict) -> str:
    canonical = {"store_id": order["store_id"],
                 "drinks": [{k: v for k, v in d.items() if k != "total_price"} for d in order["drinks"]]}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def order_view(rec: dict, status: str | None = None) -> dict:
    status = status or rec["status"]
    return {"order_id": rec["order_id"], "store_id": rec["store_id"],
            "status": status if status in FINAL else PUBLIC_STATUS.get(status, "hold_unknown"),
            "drinks": rec["request"]["drinks"]}


def detail_sort_key(lines: list[dict], line_item: int, component: str) -> tuple:
    """Fixed order: by line, then the milk before add-ins, then add-ins as the customer listed them."""
    line = lines[line_item - 1] if 0 < line_item <= len(lines) else None
    if line is None:
        return (line_item, 2, 0)
    if component == line["milk"]:
        return (line_item, 0, 0)
    if component in line["add_ins"]:
        return (line_item, 1, line["add_ins"].index(component))
    return (line_item, 2, 0)


# ---------------------------------------------------------------- the flow

class OrderFlow:
    def __init__(self, store: OrderStore, stock: httpx.AsyncClient, payment: httpx.AsyncClient):
        self.store = store
        self.stock = stock
        self.payment = payment
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _call(self, client: httpx.AsyncClient, method: str, path: str, timeout: float, rid: str,
                    **kwargs) -> httpx.Response:
        headers = {"X-Request-ID": rid, **kwargs.pop("headers", {})}
        try:
            r = await client.request(method, path, timeout=timeout, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise NoAnswer(f"{method} {path}: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise NoAnswer(f"{method} {path}: HTTP {r.status_code}")
        try:
            r.json()
        except ValueError as e:
            raise NoAnswer(f"{method} {path}: unreadable body") from e
        return r

    def _save_quietly(self, order_id: str, point: str, **fields) -> None:
        """For saves after the outcome is already decided (the result is still returned)."""
        try:
            self.store.update(order_id, point, **fields)
        except StorageError:
            log.exception("could not save %s for %s", point, order_id)

    def _final(self, rec: dict, status_code: int, body: dict, status: str) -> tuple[int, dict]:
        if "error" in body:
            body = {**body, "request_id": REQUEST_ID.get()}
        self._save_quietly(rec["order_id"], "final", status=status, final_status=status_code, final_body=body)
        return status_code, body

    @staticmethod
    def _hold_body(rec: dict) -> dict:
        expires = datetime.now(timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)
        lines = [{k: d[k] for k in ("line_item", "drink", "size", "milk", "add_ins", "quantity")}
                 for d in rec["request"]["drinks"]]
        return {"store_id": rec["store_id"], "expires_at": expires.isoformat(), "lines": lines}

    # -------------------------------------------------------------- entry points

    async def place(self, raw, rid: str) -> tuple[int, dict]:
        order, problem = validate(raw)
        if problem:
            return 400, problem
        if not STORE_ID.match(order["store_id"]):
            return 404, error("unknown_store", msg.unknown_store(order["store_id"]),
                              [{"reason": "unknown_store", "store_id": order["store_id"]}])
        order_id, h = order["order_id"], request_hash(order)
        REQUEST_ID.set(rid)
        async with self.locks[order_id]:
            rec = self.store.get(order_id)
            if rec is None:
                try:
                    rec = self.store.create(order_id, h, order["store_id"], order)
                except StorageError:
                    return 503, retryable("order_storage_unavailable", msg.STORAGE_UNAVAILABLE)
            elif rec["request_hash"] != h:
                return 409, error("order_id_conflict", msg.order_id_conflict(order_id),
                                  [{"reason": "order_id_conflict", "order_id": order_id}])
            elif rec["final_status"] is not None:
                return rec["final_status"], json.loads(rec["final_body"])
            return await self._advance(rec, rid)

    async def _advance(self, rec: dict, rid: str) -> tuple[int, dict]:
        status = rec["status"]
        if status in ("holding", "hold_unknown"):
            return await self._hold(rec, rid)
        if status in ("paying", "payment_unknown"):
            return await self._pay(rec, rid)
        if status == "confirming":
            return await self._confirm(rec, rid)
        if status == "reholding":
            return await self._rehold(rec, rid)
        if status == "refunding":
            return await self._refund(rec, rid, json.loads(rec["pending_refund"]))
        return 500, error("internal_error", msg.INTERNAL)

    # -------------------------------------------------------------- hold

    async def _release_key(self, key: str, rid: str, timeout: float) -> bool:
        try:
            r = await self._call(self.stock, "POST", "/v1/hold-releases", timeout, rid, json={"idempotency_key": key})
        except NoAnswer:
            return False
        return r.status_code == 200

    async def _hold(self, rec: dict, rid: str) -> tuple[int, dict]:
        order_id, n = rec["order_id"], rec["attempt"]
        # Release before retry: attempt n must be confirmed released (or tombstoned) first.
        if n >= 1 and not await self._release_key(f"{order_id}:hold:{n}", rid, RELEASE_BEFORE_RETRY):
            return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)
        n += 1
        try:  # Save before send: no key goes out unless its attempt number is saved.
            rec = self.store.update(order_id, "attempt", attempt=n, status="holding")
        except StorageError:
            return 503, retryable("order_storage_unavailable", msg.STORAGE_UNAVAILABLE)
        key = f"{order_id}:hold:{n}"
        body = self._hold_body(rec)
        response = None
        for _ in range(2):  # one retry with the same key: a lost first hold is replayed, not doubled
            try:
                response = await self._call(self.stock, "POST", "/v1/holds", HOLD_TIMEOUT, rid, json=body,
                                            headers={"Idempotency-Key": key})
                break
            except NoAnswer as e:
                log.warning("hold %s: %s", key, e)
        if response is None:
            await self._release_key(key, rid, RELEASE_AFTER_TIMEOUT)  # best effort; checked again before n+1
            self._save_quietly(order_id, "status", status="hold_unknown")
            return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)
        return await self._on_hold(rec, response, key, rid)

    async def _on_hold(self, rec: dict, r: httpx.Response, key: str, rid: str) -> tuple[int, dict]:
        order_id, body = rec["order_id"], r.json()
        if r.status_code == 201:
            try:
                self.store.update(order_id, "hold_result", hold_id=body["hold_id"])
                rec = self.store.update(order_id, "paying", status="paying")
            except StorageError:
                await self._release_hold(body["hold_id"], rid)
                return 503, retryable("order_storage_unavailable", msg.STORAGE_UNAVAILABLE)
            return await self._pay(rec, rid)
        code = body.get("error", {}).get("code")
        details = body.get("error", {}).get("details", [])
        if r.status_code == 404 and code == "unknown_store":
            return self._final(rec, 404, error("unknown_store", msg.unknown_store(rec["store_id"]),
                                               [{"reason": "unknown_store", "store_id": rec["store_id"]}]), "rejected")
        if r.status_code == 400 and code == "invalid_order":
            problems = [{k: p[k] for k in ("reason", "field", "value", "line_item") if k in p} for p in details]
            return self._final(rec, 400, error("invalid_order", msg.unrecognised(problems), problems), "rejected")
        if r.status_code == 409 and code == "store_closed":
            return self._final(rec, 409, self._store_closed(details), "refused")
        if r.status_code == 409 and code == "component_unavailable":
            return self._final(rec, 409, self._component_unavailable(rec, details), "refused")
        if r.status_code == 409 and code == "hold_released":
            self._save_quietly(order_id, "status", status="hold_unknown")
            return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)
        log.error("unexpected hold response for %s: %s %s", key, r.status_code, body)
        await self._release_key(key, rid, RELEASE_AFTER_TIMEOUT)
        self._save_quietly(order_id, "status", status="hold_unknown")
        return 500, error("internal_error", msg.INTERNAL)

    @staticmethod
    def _store_closed(details: list[dict]) -> dict:
        d = details[0] if details else {}
        name = d.get("store_name", d.get("store_id", "This store"))
        return error("store_closed", msg.store_closed(name),
                     [{"reason": "store_closed", "store_id": d.get("store_id", ""), "store_name": name}])

    def _component_unavailable(self, rec: dict, shortfalls: list[dict]) -> dict:
        lines = rec["request"]["drinks"]
        items = []
        for s in sorted(shortfalls, key=lambda s: detail_sort_key(lines, s["line_item"], s["missing_component"])):
            line = lines[s["line_item"] - 1]
            items.append({"reason": s["reason"], "line_item": s["line_item"], "missing_component": s["missing_component"],
                          "message": msg.component_unavailable(s["line_item"], line["size"], line["drink"],
                                                               s["missing_component"])})
        return error("component_unavailable", items[0]["message"], items)

    async def _release_hold(self, hold_id: str, rid: str) -> None:
        try:
            await self._call(self.stock, "POST", f"/v1/holds/{hold_id}/release", RELEASE_BEFORE_RETRY, rid)
        except NoAnswer:
            log.warning("release of %s did not answer; its expiry frees it", hold_id)

    # -------------------------------------------------------------- payment

    async def _pay(self, rec: dict, rid: str) -> tuple[int, dict]:
        order_id = rec["order_id"]
        amount = round(sum(d.get("total_price", 0) for d in rec["request"]["drinks"]), 2)
        try:
            r = await self._call(self.payment, "POST", "/v1/payments", PAYMENT_TIMEOUT, rid,
                                 json={"order_id": order_id, "amount": amount},
                                 headers={"Idempotency-Key": f"{order_id}:pay"})
        except NoAnswer:
            self._save_quietly(order_id, "status", status="payment_unknown")  # the hold is kept
            return 503, retryable("payment_status_unknown", msg.PAYMENT_UNKNOWN)
        if r.status_code == 402:
            await self._release_hold(rec["hold_id"], rid)  # released immediately (FR-013)
            return self._final(rec, 402, error("payment_declined", msg.PAYMENT_DECLINED), "payment_declined")
        if r.status_code != 201:
            self._save_quietly(order_id, "status", status="payment_unknown")
            return 503, retryable("payment_status_unknown", msg.PAYMENT_UNKNOWN)
        rec = self.store.get(order_id)
        self._save_quietly(order_id, "charged", payment_id=r.json().get("payment_id"), status="confirming")
        rec = self.store.get(order_id) or rec
        return await self._confirm(rec, rid)

    # -------------------------------------------------------------- confirm, rehold, refund

    async def _confirm(self, rec: dict, rid: str) -> tuple[int, dict]:
        try:
            r = await self._call(self.stock, "POST", f"/v1/holds/{rec['hold_id']}/confirm", CONFIRM_TIMEOUT, rid)
        except NoAnswer:  # charged and held: accepted; the confirm is retried in the background
            self._save_quietly(rec["order_id"], "status", confirm_pending=1)
            return self._final(rec, 201, order_view(rec, "accepted"), "accepted")
        if r.status_code == 200:
            self._save_quietly(rec["order_id"], "status", confirm_pending=0)
            return self._final(rec, 201, order_view(rec, "accepted"), "accepted")
        if r.status_code in (404, 409):  # the hold expired before confirm: recheck with a fresh hold
            return await self._rehold(rec, rid)
        self._save_quietly(rec["order_id"], "status", confirm_pending=1)
        return self._final(rec, 201, order_view(rec, "accepted"), "accepted")

    async def _rehold(self, rec: dict, rid: str) -> tuple[int, dict]:
        order_id, m = rec["order_id"], rec["rehold"]
        if rec["status"] == "reholding" and m >= 1 and not await self._release_key(
                f"{order_id}:rehold:{m}", rid, RELEASE_BEFORE_RETRY):
            return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)
        m += 1
        try:
            rec = self.store.update(order_id, "rehold", rehold=m, status="reholding", confirm_pending=0)
        except StorageError:
            return 503, retryable("order_storage_unavailable", msg.STORAGE_UNAVAILABLE)
        key = f"{order_id}:rehold:{m}"
        r = None
        for _ in range(2):
            try:
                r = await self._call(self.stock, "POST", "/v1/holds", HOLD_TIMEOUT, rid, json=self._hold_body(rec),
                                     headers={"Idempotency-Key": key})
                break
            except NoAnswer as e:
                log.warning("rehold %s: %s", key, e)
        if r is None:
            return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)
        body = r.json()
        if r.status_code == 201:
            self._save_quietly(order_id, "hold_result", hold_id=body["hold_id"], status="confirming")
            return await self._confirm(self.store.get(order_id) or {**rec, "hold_id": body["hold_id"]}, rid)
        code = body.get("error", {}).get("code")
        details = body.get("error", {}).get("details", [])
        if r.status_code == 409 and code == "component_unavailable":
            return await self._refund(rec, rid, self._refund_ran_out(rec, details))
        if r.status_code == 409 and code == "store_closed":
            name = (details[0] if details else {}).get("store_name", rec["store_id"])
            return await self._refund(rec, rid, error("order_refunded", msg.refund_store_closed(name), [
                {"reason": "store_closed", "order_id": order_id, "refunded": True, "store_name": name}]))
        return 503, retryable("stock_service_unavailable", msg.STOCK_UNAVAILABLE)

    def _refund_ran_out(self, rec: dict, shortfalls: list[dict]) -> dict:
        lines = rec["request"]["drinks"]
        ordered = sorted(shortfalls, key=lambda s: detail_sort_key(lines, s["line_item"], s["missing_component"]))
        names = list(dict.fromkeys(s["missing_component"] for s in ordered))  # each once, order kept
        details = [{"reason": "component_ran_out", "order_id": rec["order_id"], "refunded": True,
                    "line_item": s["line_item"], "missing_component": s["missing_component"]} for s in ordered]
        return error("order_refunded", msg.refund_ran_out(names), details)

    async def _refund(self, rec: dict, rid: str, outcome: dict) -> tuple[int, dict]:
        order_id = rec["order_id"]
        self._save_quietly(order_id, "refunding", status="refunding", pending_refund=outcome)
        try:
            r = await self._call(self.payment, "POST", "/v1/refunds", PAYMENT_TIMEOUT, rid,
                                 json={"order_id": order_id, "payment_id": rec.get("payment_id")},
                                 headers={"Idempotency-Key": f"{order_id}:refund"})
        except NoAnswer:
            return 503, retryable("payment_status_unknown", msg.PAYMENT_UNKNOWN)
        if r.status_code != 201:
            return 503, retryable("payment_status_unknown", msg.PAYMENT_UNKNOWN)
        return self._final(rec, 409, outcome, "refunded")

    # -------------------------------------------------------------- background

    async def startup_sweep(self) -> None:
        """Release the saved attempt's key for every order left mid-hold, without waiting for a retry."""
        for rec in self.store.with_status("holding", "hold_unknown"):
            if rec["attempt"] >= 1:
                async with self.locks[rec["order_id"]]:
                    await self._release_key(f"{rec['order_id']}:hold:{rec['attempt']}", "startup-sweep",
                                            RELEASE_BEFORE_RETRY)

    async def retry_pending_confirms(self) -> None:
        for rec in self.store.confirm_pending():
            async with self.locks[rec["order_id"]]:
                try:
                    r = await self._call(self.stock, "POST", f"/v1/holds/{rec['hold_id']}/confirm",
                                         CONFIRM_TIMEOUT, "confirm-retry")
                except NoAnswer:
                    continue
                if r.status_code == 200:
                    self._save_quietly(rec["order_id"], "status", confirm_pending=0)

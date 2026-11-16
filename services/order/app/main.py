"""Order service HTTP API. Contract: contracts/order-service.yaml."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import messages as msg
from .flow import OrderFlow, error, order_view
from .store import OrderStore

log = logging.getLogger("order")
CONFIRM_RETRY_SECONDS = 2
FORBIDDEN_ENV_PREFIXES = ("FAIL_SAVES", "ORDER_FAIL")


def respond(rid: str, status: int, payload: dict) -> JSONResponse:
    headers = {"X-Request-ID": rid}
    if "error" in payload and "request_id" not in payload:
        payload = {**payload, "request_id": rid}
    if status == 503:
        headers["Retry-After"] = "1"
    return JSONResponse(payload, status_code=status, headers=headers)


def create_app(*, db_path: str, stock_client: httpx.AsyncClient, payment_client: httpx.AsyncClient,
               fail_saves: Callable[[str], bool] | None = None) -> FastAPI:
    """fail_saves is a TEST-ONLY fault hook (see plan.md "fail_saves never ships")."""
    store = OrderStore(db_path, fail_saves=fail_saves)
    flow = OrderFlow(store, stock_client, payment_client)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await flow.startup_sweep()
        except Exception:
            log.exception("startup sweep failed; holds still expire on their own")

        async def confirm_loop():
            while True:
                await asyncio.sleep(CONFIRM_RETRY_SECONDS)
                try:
                    await flow.retry_pending_confirms()
                except Exception:
                    log.exception("confirm retry failed")

        task = asyncio.create_task(confirm_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Order Service", version="1.0.0", lifespan=lifespan)

    @app.post("/v1/orders")
    async def place_order(request: Request):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        try:
            raw = json.loads(await request.body())
        except ValueError:
            return respond(rid, 400, error("invalid_order", "The order must be valid JSON.",
                                           [{"reason": "invalid_value", "field": "order_id"}]))
        status, payload = await flow.place(raw, rid)
        return respond(rid, status, payload)

    @app.get("/v1/orders/{order_id}")
    async def get_order(order_id: str, request: Request):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        rec = store.get(order_id)
        if rec is None:
            return respond(rid, 404, error("order_not_found", msg.order_not_found(order_id)))
        return respond(rid, 200, order_view(rec))

    return app


def create_app_from_env() -> FastAPI:
    """Container entry point. fail_saves is always None and is never read from the environment."""
    forbidden = sorted(k for k in os.environ if k.upper().startswith(FORBIDDEN_ENV_PREFIXES))
    if forbidden:
        raise SystemExit(f"refusing to start: test-only fault variables are set: {', '.join(forbidden)}")
    return create_app(
        db_path=os.environ.get("ORDER_DB_PATH", "/data/order.db"),
        stock_client=httpx.AsyncClient(base_url=os.environ["STOCK_URL"]),
        payment_client=httpx.AsyncClient(base_url=os.environ["PAYMENT_URL"]),
        fail_saves=None,
    )

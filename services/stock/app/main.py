"""Store stock service HTTP API. Contract: contracts/stock-service.yaml."""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .db import Database
from .holds import StockService, err

log = logging.getLogger("stock")
SWEEP_SECONDS = 5


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_item: int = Field(ge=1)
    drink: str
    size: str
    milk: str
    add_ins: list[str]
    quantity: int = Field(ge=1, le=20)


class HoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_id: str = Field(pattern=r"^SB-[0-9]+$")
    expires_at: AwareDatetime
    lines: list[Line] = Field(min_length=1)


class ReleaseByKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)


class Adjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_id: str = Field(pattern=r"^SB-[0-9]+$")
    component_name: str
    delta: int


def request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def respond(request: Request, status: int, payload: dict) -> JSONResponse:
    rid = request_id(request)
    if "error" in payload:
        payload = {**payload, "request_id": rid}
    return JSONResponse(payload, status_code=status, headers={"X-Request-ID": rid})


def create_app(*, db_path: str, seed_rows: list[dict] | None = None) -> FastAPI:
    db = Database(db_path)
    if seed_rows:
        db.seed(seed_rows)
    service = StockService(db)

    @asynccontextmanager
    async def lifespan(_app):
        async def sweeper():
            while True:
                await asyncio.sleep(SWEEP_SECONDS)
                try:
                    await asyncio.to_thread(service.sweep)
                except Exception:  # keep sweeping; lazy expiry still runs on every write
                    log.exception("hold sweep failed")

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Store Stock Service", version="1.0.0", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        details = []
        for e in exc.errors():
            loc = [str(p) for p in e.get("loc", ()) if p not in ("body", "header", "path", "query")]
            details.append({"reason": "missing_field" if e.get("type") == "missing" else "invalid_value",
                            "field": ".".join(loc) or "body"})
        return respond(request, 400, err("invalid_request", "The request is not valid.", details))

    @app.post("/v1/holds")
    def create_hold(body: HoldRequest, request: Request,
                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
        return respond(request, *service.create_hold(idempotency_key, body))

    @app.get("/v1/holds/{hold_id}")
    def get_hold(hold_id: str, request: Request):
        return respond(request, *service.get_hold(hold_id))

    @app.post("/v1/holds/{hold_id}/confirm")
    def confirm_hold(hold_id: str, request: Request):
        return respond(request, *service.confirm(hold_id))

    @app.post("/v1/holds/{hold_id}/release")
    def release_hold(hold_id: str, request: Request):
        return respond(request, *service.release(hold_id))

    @app.post("/v1/hold-releases")
    def release_hold_by_key(body: ReleaseByKey, request: Request):
        return respond(request, *service.release_by_key(body.idempotency_key))

    @app.get("/v1/stores/{store_id}/availability")
    def availability(store_id: str, request: Request):
        return respond(request, *service.availability(store_id))

    @app.post("/v1/stock-adjustments")
    def adjust_stock(body: Adjustment, request: Request):
        return respond(request, *service.adjust(body.store_id, body.component_name, body.delta))

    return app


def create_app_from_env() -> FastAPI:
    """Container entry point: STOCK_DB_PATH (private SQLite file), STOCK_SNAPSHOT_PATH (CSV seed)."""
    snapshot = os.environ.get("STOCK_SNAPSHOT_PATH", "/srv/StockSnapshot")
    with open(snapshot, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return create_app(db_path=os.environ.get("STOCK_DB_PATH", "/data/stock.db"), seed_rows=rows)

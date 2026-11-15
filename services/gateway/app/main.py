"""Mobile order gateway HTTP API. Contract: contracts/gateway.yaml.

Routing and response translation only. The gateway holds no stock or order logic, keeps no
storage, and never decides whether an order is accepted. Its only downstream is the order
service, reached over HTTP through `order_client`.

- Order-service answers (2xx, 4xx, and its own JSON 503s) pass through unchanged, except that an
  error body's request_id is set to the gateway's request ID.
- The gateway itself emits only 503 order_service_unavailable: the order service is unreachable,
  does not answer within ORDER_TIMEOUT (2.8 s of the 3 s budget), answers any other 5xx, or answers
  with a body that is not JSON. It never emits 502 or 504, and a refusal (409) is never turned
  into a 5xx.
"""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ORDER_TIMEOUT = 2.8
UNAVAILABLE_MESSAGE = "We can't take orders right now. Please try again shortly."
# The order service's own retryable 503s, passed through as-is (contracts/gateway.yaml).
PASS_THROUGH_503 = {"stock_service_unavailable", "payment_status_unknown", "order_storage_unavailable"}


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def _unavailable(rid: str) -> JSONResponse:
    body = {"error": {"code": "order_service_unavailable", "message": UNAVAILABLE_MESSAGE,
                      "details": [{"reason": "order_service_unavailable", "retryable": True}]},
            "request_id": rid}
    return JSONResponse(body, status_code=503, headers={"X-Request-ID": rid, "Retry-After": "1"})


def _retry_after(upstream: httpx.Response) -> str:
    value = upstream.headers.get("retry-after", "")
    return value if value.isdigit() and int(value) >= 1 else "1"


def _translate(upstream: httpx.Response, rid: str) -> JSONResponse:
    try:
        body = upstream.json()
    except ValueError:
        return _unavailable(rid)
    status = upstream.status_code
    if status >= 500:
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        if status != 503 or code not in PASS_THROUGH_503:
            return _unavailable(rid)
    headers = {"X-Request-ID": rid}
    if isinstance(body, dict) and "error" in body:
        body = {**body, "request_id": rid}
    if status == 503:
        headers["Retry-After"] = _retry_after(upstream)
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(*, order_client: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="Mobile Order Gateway", version="1.0.0")

    async def forward(request: Request, method: str, path: str, content: bytes | None = None) -> JSONResponse:
        rid = _request_id(request)
        headers = {"X-Request-ID": rid}
        if content is not None:
            headers["Content-Type"] = request.headers.get("content-type", "application/json")
        try:
            upstream = await order_client.request(method, path, content=content, headers=headers,
                                                  timeout=ORDER_TIMEOUT)
        except httpx.HTTPError:
            return _unavailable(rid)
        return _translate(upstream, rid)

    @app.post("/v1/orders")
    async def place_order(request: Request):
        # The body is forwarded byte-for-byte: the gateway does not validate or rewrite orders.
        return await forward(request, "POST", "/v1/orders", content=await request.body())

    @app.get("/v1/orders/{order_id}")
    async def get_order(order_id: str, request: Request):
        return await forward(request, "GET", f"/v1/orders/{order_id}")

    return app


def create_app_from_env() -> FastAPI:
    """Container entry point: ORDER_URL is the order service's base URL (e.g. http://order:8000)."""
    return create_app(order_client=httpx.AsyncClient(base_url=os.environ["ORDER_URL"]))

"""Fake payment provider, started in-process by the tests (no third-party API is called).

Interface the order service must use:
    POST /v1/payments
      headers: Idempotency-Key: "{order_id}:pay"
      body:    {"order_id": str, "amount": number}
      201 {"payment_id": str, "status": "charged"}
      402 {"status": "declined"}
A repeated Idempotency-Key returns the original result and is not charged again.

    POST /v1/refunds
      headers: Idempotency-Key: "{order_id}:refund"
      body:    {"order_id": str, "payment_id": str}
      201 {"refund_id": str, "status": "refunded"}   (repeat key -> same result, refunded once)
"""
from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse


class FakePayment:
    def __init__(self, decline_order_ids: set[str] | None = None):
        self.decline_order_ids = set(decline_order_ids or ())
        self.calls: list[dict] = []          # every request received
        self.charges: dict[str, dict] = {}   # idempotency key -> result
        self.refunds: dict[str, dict] = {}   # idempotency key -> refund
        self.app = self._build()

    @property
    def refunded_order_ids(self) -> list[str]:
        return [r["order_id"] for r in self.refunds.values()]

    @property
    def charged_order_ids(self) -> list[str]:
        return [r["order_id"] for r in self.charges.values() if r["status"] == "charged"]

    def calls_for(self, order_id: str) -> int:
        return sum(1 for c in self.calls if c["body"].get("order_id") == order_id)

    def _build(self) -> FastAPI:
        app = FastAPI(title="Fake payment provider")

        @app.post("/v1/payments")
        async def pay(request: Request, idempotency_key: str = Header(...)):
            body = await request.json()
            self.calls.append({"key": idempotency_key, "body": body})
            if idempotency_key not in self.charges:
                if body.get("order_id") in self.decline_order_ids:
                    self.charges[idempotency_key] = {"order_id": body.get("order_id"), "status": "declined"}
                else:
                    self.charges[idempotency_key] = {
                        "order_id": body.get("order_id"),
                        "status": "charged",
                        "payment_id": f"pay_{len(self.charges) + 1}",
                    }
            result = self.charges[idempotency_key]
            if result["status"] == "declined":
                return JSONResponse({"status": "declined"}, status_code=402)
            return JSONResponse({"payment_id": result["payment_id"], "status": "charged"}, status_code=201)

        @app.post("/v1/refunds")
        async def refund(request: Request, idempotency_key: str = Header(...)):
            body = await request.json()
            if idempotency_key not in self.refunds:
                self.refunds[idempotency_key] = {"order_id": body.get("order_id"),
                                                 "refund_id": f"ref_{len(self.refunds) + 1}"}
            return JSONResponse({"refund_id": self.refunds[idempotency_key]["refund_id"],
                                 "status": "refunded"}, status_code=201)

        return app


# Module-level app so the fake can also run as its own container in the image tests:
#   uvicorn payment:app --app-dir /fakes
app = FakePayment().app

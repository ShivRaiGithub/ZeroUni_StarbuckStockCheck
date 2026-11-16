"""Contract tests: gateway vs contracts/gateway.yaml.

Pass-through and failure mapping use a stub order service (httpx.MockTransport). The last tests
run the full chain gateway -> order -> stock in-process.
"""
import json

import httpx
import pytest

from tests.contract.harness import (asgi_client, line, load_service, mock_client, order_body,
                                    gateway_service, seed)
from tests.contract.openapi_check import GATEWAY
from tests.fakes.payment import FakePayment

ACCEPTED = {"order_id": "MO-1", "store_id": "SB-10482", "status": "accepted",
            "drinks": [{"drink": "Caffè Latte", "size": "Grande", "milk": "Oatmilk", "add_ins": [],
                        "quantity": 1, "line_item": 1}]}


def envelope(code, message, details):
    return {"error": {"code": code, "message": message, "details": details}, "request_id": "from-order"}


UPSTREAM = {
    "201_accepted": (201, ACCEPTED),
    "400_invalid_order": (400, envelope("invalid_order", "store_id is required.",
                                        [{"reason": "missing_field", "field": "store_id"}])),
    "402_payment_declined": (402, envelope("payment_declined", "Your payment was declined.", [])),
    "404_unknown_store": (404, envelope("unknown_store", "Store SB-99999 does not exist.",
                                        [{"reason": "unknown_store", "store_id": "SB-99999"}])),
    "409_store_closed": (409, envelope("store_closed",
                                       "Bellevue Square is closed right now and can't take mobile orders.",
                                       [{"reason": "store_closed", "store_id": "SB-20516",
                                         "store_name": "Bellevue Square"}])),
    "409_component_unavailable": (409, envelope(
        "component_unavailable",
        "Line 1, Grande Caffè Latte: Almondmilk is unavailable at this store right now.",
        [{"reason": "base_out_of_stock", "line_item": 1, "missing_component": "Almondmilk"},
         {"reason": "base_out_of_stock", "line_item": 2, "missing_component": "Oatmilk",
          "message": "Line 2, Venti Chai Tea Latte: Oatmilk is unavailable at this store right now."}])),
    "409_order_refunded": (409, envelope(
        "order_refunded",
        "We've refunded your payment in full. Oatmilk ran out before we could make your order. "
        "Please try again with a different option.",
        [{"reason": "component_ran_out", "order_id": "MO-1", "refunded": True,
          "line_item": 1, "missing_component": "Oatmilk"}])),
    "503_stock_service_unavailable": (503, envelope(
        "stock_service_unavailable", "We couldn't check stock just now. Please try again shortly.",
        [{"reason": "stock_service_unavailable", "retryable": True}])),
}


def stub_order(status, body, extra_headers=None):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body, headers=extra_headers or {})

    return seen, handler


def place(client, **headers):
    return client.post("/v1/orders", headers=headers,
                       json=order_body("MO-1", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))


@pytest.mark.parametrize("case", list(UPSTREAM))
def test_order_service_responses_pass_through_unchanged(case):
    status, upstream_body = UPSTREAM[case]
    headers = {"Retry-After": "1"} if status == 503 else None
    seen, handler = stub_order(status, upstream_body, headers)
    with gateway_service(mock_client(handler, "http://order")) as gw:
        r = place(gw)
        body = GATEWAY.check(r)
        assert r.status_code == status
        assert r.status_code not in (502, 504)
        assert len(seen) == 1 and seen[0].url.path == "/v1/orders"
        if "error" in upstream_body:
            assert body["error"] == upstream_body["error"], "refusal reason must not be collapsed or rewritten"
            assert body["request_id"] == r.headers["X-Request-ID"]
        else:
            assert body == upstream_body


def test_request_body_is_forwarded_untouched_and_request_id_propagated():
    seen, handler = stub_order(201, ACCEPTED)
    with gateway_service(mock_client(handler, "http://order")) as gw:
        raw = {"order_id": "MO-1", "drinks": [], "unexpected": {"kept": True}}
        r = gw.post("/v1/orders", json=raw, headers={"X-Request-ID": "req-abc"})
        GATEWAY.check(r)
        assert r.headers["X-Request-ID"] == "req-abc"
        assert json.loads(seen[0].content) == raw, "gateway must not validate or rewrite the order"
        assert seen[0].headers["X-Request-ID"] == "req-abc"


def test_error_request_id_is_the_gateways():
    status, upstream_body = UPSTREAM["409_component_unavailable"]
    _, handler = stub_order(status, upstream_body)
    with gateway_service(mock_client(handler, "http://order")) as gw:
        r = place(gw, **{"X-Request-ID": "req-xyz"})
        assert GATEWAY.check(r)["request_id"] == "req-xyz"


def _down(exc):
    def handler(request):
        raise exc("order service unavailable", request=request)
    return handler


def _answers(status):
    def handler(request):
        return httpx.Response(status, text="upstream failure")
    return handler


@pytest.mark.parametrize("handler", [
    _down(httpx.ConnectError), _down(httpx.ConnectTimeout), _down(httpx.ReadTimeout),
    _answers(500), _answers(502), _answers(504),
], ids=["connect_refused", "connect_timeout", "read_timeout", "upstream_500", "upstream_502", "upstream_504"])
def test_order_service_unreachable_is_503_never_502_or_504(handler):
    with gateway_service(mock_client(handler, "http://order")) as gw:
        r = place(gw)
        body = GATEWAY.check(r)
        assert r.status_code == 503
        assert body["error"]["code"] == "order_service_unavailable"
        assert "try again shortly" in body["error"]["message"].lower(), "FR-015 wording"
        assert int(r.headers["Retry-After"]) >= 1


def test_get_order_pass_through_and_unreachable():
    seen, ok = stub_order(200, ACCEPTED)
    with gateway_service(mock_client(ok, "http://order")) as gw:
        r = gw.get("/v1/orders/MO-1")
        assert GATEWAY.check(r) == ACCEPTED
        assert seen[0].url.path == "/v1/orders/MO-1"

    _, missing = stub_order(404, envelope("order_not_found", "Order MO-2 was not found.", []))
    with gateway_service(mock_client(missing, "http://order")) as gw:
        r = gw.get("/v1/orders/MO-2")
        assert GATEWAY.check(r)["error"]["code"] == "order_not_found"
        assert r.status_code == 404

    with gateway_service(mock_client(_down(httpx.ConnectError), "http://order")) as gw:
        r = gw.get("/v1/orders/MO-3")
        assert GATEWAY.check(r)["error"]["code"] == "order_service_unavailable"
        assert r.status_code == 503


# ------------------------------------------------------------------ full chain, in-process

def _full_chain(tmp_path, rows):
    stock_app = load_service("stock").create_app(db_path=str(tmp_path / "stock.db"), seed_rows=rows)
    payment = FakePayment()
    order_app = load_service("order").create_app(
        db_path=str(tmp_path / "order.db"),
        stock_client=asgi_client(stock_app, "http://stock"),
        payment_client=asgi_client(payment.app, "http://payment"))
    return gateway_service(asgi_client(order_app, "http://order")), payment


def test_full_chain_store_closed_rule4(tmp_path):
    gw_ctx, payment = _full_chain(tmp_path, seed())
    with gw_ctx as gw:
        r = gw.post("/v1/orders", json=order_body("MO-250309-0040", "SB-20516",
                                                  line("Flat White", "Grande", "Whole Milk")))
        body = GATEWAY.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "store_closed"
        assert body["error"]["message"] == "Bellevue Square is closed right now and can't take mobile orders."
        assert payment.calls == []


def test_full_chain_component_unavailable_rule5(tmp_path):
    gw_ctx, payment = _full_chain(tmp_path, seed({"10482": {"Oatmilk": (0, 0)}}))
    with gw_ctx as gw:
        r = gw.post("/v1/orders", json=order_body("MO-250307-0031", "SB-10482",
                                                  line("Matcha Tea Latte", "Grande", "Oatmilk")))
        body = GATEWAY.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "component_unavailable"
        assert body["error"]["message"] == \
            "Line 1, Grande Matcha Tea Latte: Oatmilk is unavailable at this store right now."
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", 1, "Oatmilk")]
        assert payment.calls == []


def test_full_chain_accepted(tmp_path):
    gw_ctx, payment = _full_chain(tmp_path, seed())
    with gw_ctx as gw:
        r = gw.post("/v1/orders", json=order_body("MO-250303-0003", "SB-10482",
                                                  line("Pike Place Roast", "Venti", "None")))
        body = GATEWAY.check(r)
        assert r.status_code == 201
        assert body["status"] == "accepted"
        assert payment.charged_order_ids == ["MO-250303-0003"]

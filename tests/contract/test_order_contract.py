"""Contract tests: order service vs contracts/order-service.yaml, with CLAUDE.md rules 4-8.

The order service runs against the real stock service and the fake payment provider, all
in-process. Messages are compared character for character with CLAUDE.md and spec.md.
"""
import json

import httpx
import pytest

from tests.contract.harness import (line, mock_client, order_body, order_service, seed,
                                    stock_levels)
from tests.contract.openapi_check import ORDER, STOCK
from tests.fakes.payment import FakePayment

SHAKEN = "Iced Brown Sugar Oatmilk Shaken Espresso"


def place(env, body, **headers):
    return env.order.post("/v1/orders", json=body, headers=headers)


def assert_refused(env, r, order_id, code, message):
    body = ORDER.check(r)
    assert r.status_code == 409
    assert body["error"]["code"] == code
    assert body["error"]["message"] == message
    assert env.payment.calls == [], "payment must never be requested for a refused order"
    return body


# ------------------------------------------------------------------ 201 accepted

def test_accepted_order_takes_last_unit_and_pays_once(tmp_path):
    # MO-250307-0023: Oatmilk on hand 1, reserved 0 -> accepted, Oatmilk available drops to 0.
    with order_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as env:
        r = place(env, order_body("MO-250307-0023", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 201
        assert (body["order_id"], body["store_id"], body["status"]) == ("MO-250307-0023", "SB-10482", "accepted")
        assert body["drinks"][0]["line_item"] == 1
        assert env.payment.calls_for("MO-250307-0023") == 1
        levels = stock_levels(env.stock, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"], levels["available"]) == (0, 0, 0)


def test_accepted_order_with_two_add_ins_is_one_order(tmp_path):
    # MO-250303-0004 at SB-11937: 2% Milk, Vanilla Syrup, Caramel Drizzle all available 50.
    with order_service(tmp_path, seed()) as env:
        r = place(env, order_body("MO-250303-0004", "SB-11937",
                                  line("Iced Caramel Macchiato", "Venti", "2% Milk",
                                       ["Vanilla Syrup", "Caramel Drizzle"])))
        body = ORDER.check(r)
        assert r.status_code == 201
        assert body["status"] == "accepted"
        assert body["drinks"][0]["add_ins"] == ["Vanilla Syrup", "Caramel Drizzle"]
        for comp in ("2% Milk", "Vanilla Syrup", "Caramel Drizzle"):
            assert stock_levels(env.stock, "SB-11937", comp)["quantity_on_hand"] == 49


def test_repeat_order_id_returns_original_result_without_reserving_or_paying_again(tmp_path):
    body_in = order_body("MO-250303-0003", "SB-10482", line("Caffè Latte", "Venti", "Oatmilk"))
    with order_service(tmp_path, seed({"10482": {"Oatmilk": (5, 0)}})) as env:
        first, second = place(env, body_in), place(env, body_in)
        b1, b2 = ORDER.check(first), ORDER.check(second)
        assert (first.status_code, second.status_code) == (201, 201)
        assert b1 == b2
        assert env.payment.calls_for("MO-250303-0003") == 1
        assert stock_levels(env.stock, "SB-10482", "Oatmilk")["quantity_on_hand"] == 4


# ------------------------------------------------------------------ 409 refusals (CLAUDE.md 4-8)

def test_rule4_store_closed(tmp_path):
    # MO-250303-0001: SB-20516 Bellevue Square is closed.
    with order_service(tmp_path, seed()) as env:
        r = place(env, order_body("MO-250303-0001", "SB-20516", line("Pike Place Roast", "Grande", "None")))
        body = assert_refused(env, r, "MO-250303-0001", "store_closed",
                              "Bellevue Square is closed right now and can't take mobile orders.")
        assert body["error"]["details"][0]["store_name"] == "Bellevue Square"


def test_rule4_store_closed_wins_over_out_of_stock(tmp_path):
    with order_service(tmp_path, seed({"20516": {"Oatmilk": (0, 0)}})) as env:
        r = place(env, order_body("MO-C-0002", "SB-20516", line("Caffè Latte", "Grande", "Oatmilk")))
        body = assert_refused(env, r, "MO-C-0002", "store_closed",
                              "Bellevue Square is closed right now and can't take mobile orders.")
        assert all(d["reason"] == "store_closed" for d in body["error"]["details"])


def test_rule5_base_out_of_stock(tmp_path):
    # MO-250304-0007: Almondmilk on hand 1, reserved 1 -> available 0.
    with order_service(tmp_path, seed({"10482": {"Almondmilk": (1, 1)}})) as env:
        r = place(env, order_body("MO-250304-0007", "SB-10482",
                                  line("Caffè Latte", "Grande", "Almondmilk", ["Vanilla Syrup"])))
        body = assert_refused(env, r, "MO-250304-0007", "component_unavailable",
                              "Line 1, Grande Caffè Latte: Almondmilk is unavailable at this store right now.")
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", 1, "Almondmilk")]
        assert stock_levels(env.stock, "SB-10482", "Vanilla Syrup")["reserved_quantity"] == 0


def test_rule5_first_hill_oat_milk_sold_out_refuses_the_whole_order(tmp_path):
    # MO-250307-0029, the case the project is built around: First Hill (SB-10482), Venti Iced Brown
    # Sugar Oatmilk Shaken Espresso, Oatmilk on hand 0, reserved 0 -> available 0. Brown Sugar Syrup
    # and Extra Shot are in stock (50), so Oatmilk is the only thing named.
    with order_service(tmp_path, seed({"10482": {"Oatmilk": (0, 0)}})) as env:
        r = place(env, order_body("MO-250307-0029", "SB-10482",
                                  line(SHAKEN, "Venti", "Oatmilk", ["Brown Sugar Syrup", "Extra Shot"])))
        body = assert_refused(
            env, r, "MO-250307-0029", "component_unavailable",
            f"Line 1, Venti {SHAKEN}: Oatmilk is unavailable at this store right now.")
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", 1, "Oatmilk")]
        assert "Brown Sugar Syrup" not in r.text and "Extra Shot" not in r.text
        for comp in ("Oatmilk", "Brown Sugar Syrup", "Extra Shot"):
            assert stock_levels(env.stock, "SB-10482", comp)["reserved_quantity"] == 0, comp
        assert stock_levels(env.stock, "SB-10482", "Oatmilk")["quantity_on_hand"] == 0


def test_rule6_add_in_out_of_stock_never_dropped(tmp_path):
    # MO-250306-0019: Caramel Drizzle available 0, Vanilla Syrup available 50.
    with order_service(tmp_path, seed({"10482": {"Caramel Drizzle": (0, 0)}})) as env:
        r = place(env, order_body("MO-250306-0019", "SB-10482",
                                  line("Iced Caramel Macchiato", "Grande", "2% Milk",
                                       ["Vanilla Syrup", "Caramel Drizzle"])))
        body = assert_refused(
            env, r, "MO-250306-0019", "component_unavailable",
            "Line 1, Grande Iced Caramel Macchiato: Caramel Drizzle is unavailable at this store right now.")
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("add_in_out_of_stock", 1, "Caramel Drizzle")]
        assert "Vanilla Syrup" not in r.text


def test_rule7_stock_runs_out_mid_order(tmp_path):
    # MO-250307-0024: Oatmilk 1 at the first check, a POS sale takes it, 0 at commit.
    with order_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as env:
        first = env.stock.get("/v1/stores/SB-10482/availability")
        oat = next(c for c in STOCK.check(first)["components"] if c["component_name"] == "Oatmilk")
        assert oat["available"] == 1
        STOCK.check(env.stock.post("/v1/stock-adjustments",
                                   json={"store_id": "SB-10482", "component_name": "Oatmilk", "delta": -1}))
        r = place(env, order_body("MO-250307-0024", "SB-10482",
                                  line(SHAKEN, "Grande", "Oatmilk", ["Brown Sugar Syrup"])))
        body = assert_refused(
            env, r, "MO-250307-0024", "component_unavailable",
            f"Line 1, Grande {SHAKEN}: Oatmilk is unavailable at this store right now.")
        assert [(d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [(1, "Oatmilk")]
        assert stock_levels(env.stock, "SB-10482", "Oatmilk")["quantity_on_hand"] == 0


def test_rule8_insufficient_quantity_and_stock_never_negative(tmp_path):
    # MO-250307-0028: Brown Sugar Syrup on hand 5, reserved 2 -> available 3; 4 drinks need 4.
    with order_service(tmp_path, seed({"11937": {"Brown Sugar Syrup": (5, 2)}})) as env:
        r = place(env, order_body("MO-250307-0028", "SB-11937",
                                  line(SHAKEN, "Grande", "Oatmilk", ["Brown Sugar Syrup"], quantity=4)))
        body = assert_refused(
            env, r, "MO-250307-0028", "component_unavailable",
            f"Line 1, Grande {SHAKEN}: Brown Sugar Syrup is unavailable at this store right now.")
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("insufficient_quantity", 1, "Brown Sugar Syrup")]
        levels = stock_levels(env.stock, "SB-11937", "Brown Sugar Syrup")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"], levels["available"]) == (5, 2, 3)


def test_every_failing_line_listed_in_one_refusal(tmp_path):
    # Line 1 Almondmilk 0, line 2 Oatmilk 0 (spec US3 scenario 5).
    with order_service(tmp_path, seed({"10482": {"Almondmilk": (0, 0), "Oatmilk": (0, 0)}})) as env:
        r = place(env, order_body("MO-C-0005", "SB-10482", line("Caffè Latte", "Grande", "Almondmilk"),
                                  line("Chai Tea Latte", "Venti", "Oatmilk")))
        body = assert_refused(env, r, "MO-C-0005", "component_unavailable",
                              "Line 1, Grande Caffè Latte: Almondmilk is unavailable at this store right now.")
        details = body["error"]["details"]
        assert [(d["line_item"], d["missing_component"]) for d in details] == [(1, "Almondmilk"), (2, "Oatmilk")]
        assert details[1]["message"] == "Line 2, Venti Chai Tea Latte: Oatmilk is unavailable at this store right now."


def test_same_order_id_with_different_contents_is_409(tmp_path):
    with order_service(tmp_path, seed()) as env:
        ORDER.check(place(env, order_body("MO-C-0006", "SB-10482", line("Caffè Latte", "Grande", "2% Milk"))))
        r = place(env, order_body("MO-C-0006", "SB-10482", line("Caffè Latte", "Venti", "2% Milk")))
        body = ORDER.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "order_id_conflict"
        assert env.payment.calls_for("MO-C-0006") == 1


# ------------------------------------------------------------------ 400 / 404

@pytest.mark.parametrize("payload, field, message", [
    ({"order_id": "MO-C-0010", "drinks": [line("Caffè Latte", "Grande", "2% Milk")]},
     "store_id", "store_id is required."),
    ({"order_id": "MO-C-0011", "store_id": "SB-10482", "drinks": []},
     "drinks", "At least one drink is required."),
    (order_body("MO-C-0012", "SB-10482", line("Caffè Latte", "Grande", "2% Milk", ["Vanila Syrup"])),
     "add_ins", "Unrecognised add-in: Vanila Syrup."),
], ids=["missing_store_id", "empty_drinks", "unrecognised_add_in"])
def test_invalid_order_is_400(tmp_path, payload, field, message):
    with order_service(tmp_path, seed()) as env:
        r = place(env, payload)
        body = ORDER.check(r)
        assert r.status_code == 400
        assert body["error"]["code"] == "invalid_order"
        assert body["error"]["message"] == message
        assert field in [d["field"] for d in body["error"]["details"]]
        assert env.payment.calls == []
        assert stock_levels(env.stock, "SB-10482", "2% Milk")["reserved_quantity"] == 0


def test_unknown_store_is_404(tmp_path):
    with order_service(tmp_path, seed()) as env:
        r = place(env, order_body("MO-C-0013", "SB-99999", line("Caffè Latte", "Grande", "2% Milk")))
        body = ORDER.check(r)
        assert r.status_code == 404
        assert body["error"]["code"] == "unknown_store"
        assert body["error"]["message"] == "Store SB-99999 does not exist."
        assert env.payment.calls == []


# ------------------------------------------------------------------ 402 payment declined

def test_payment_declined_releases_hold_immediately(tmp_path):
    payment = FakePayment(decline_order_ids={"MO-C-0014"})
    with order_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}}), payment=payment) as env:
        r = place(env, order_body("MO-C-0014", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 402
        assert body["error"]["code"] == "payment_declined"
        levels = stock_levels(env.stock, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"], levels["available"]) == (1, 0, 1)


# ------------------------------------------------------------------ 503 (not refusals)

def _stock_stub(fail_with):
    """Stub stock service: POST /v1/holds fails; records every request."""
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path == "/v1/holds":
            if isinstance(fail_with, int):
                return httpx.Response(fail_with, json={"error": {"code": "internal_error", "message": "boom",
                                                                 "details": []}, "request_id": "s"})
            raise fail_with("stock did not answer", request=request)
        if request.url.path == "/v1/hold-releases":
            return httpx.Response(200, json={"idempotency_key": "x", "outcome": "tombstoned", "hold_id": None})
        return httpx.Response(404)

    return calls, handler


@pytest.mark.parametrize("fail_with", [httpx.ReadTimeout, httpx.ConnectError, 500],
                         ids=["hold_times_out", "stock_unreachable", "stock_5xx"])
def test_hold_call_failure_is_503_without_payment_and_releases_by_key(tmp_path, fail_with):
    calls, handler = _stock_stub(fail_with)
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock")) as env:
        r = place(env, order_body("MO-C-0015", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 503
        assert body["error"]["code"] == "stock_service_unavailable"
        assert env.payment.calls == []
        holds = [c for c in calls if c.url.path == "/v1/holds"]
        assert len(holds) == 2, "one retry, no more"
        assert holds[0].headers["Idempotency-Key"] == holds[1].headers["Idempotency-Key"]
        releases = [c for c in calls if c.url.path == "/v1/hold-releases"]
        assert len(releases) == 1
        assert holds[0].headers["Idempotency-Key"] in releases[0].content.decode()


def test_payment_timeout_is_503_and_keeps_the_hold(tmp_path):
    def payment_times_out(request):
        raise httpx.ReadTimeout("payment did not answer", request=request)

    with order_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}}),
                       payment_client=mock_client(payment_times_out, "http://payment")) as env:
        r = place(env, order_body("MO-C-0016", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 503
        assert body["error"]["code"] == "payment_status_unknown"
        levels = stock_levels(env.stock, "SB-10482", "Oatmilk")
        assert (levels["reserved_quantity"], levels["available"]) == (1, 0), "hold kept while payment unknown"


# ------------------------------------------------------------------ save before send, release before retry

HOLD_C = "hold_" + "C" * 26


def _scripted_stock(hold_behaviour, release_behaviour):
    """Stock stub. hold_behaviour(key) / release_behaviour(key) return "ok" or "timeout"."""
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        path = request.url.path
        if path == "/v1/holds":
            if hold_behaviour(request.headers["Idempotency-Key"]) == "timeout":
                raise httpx.ReadTimeout("stock did not answer", request=request)
            return httpx.Response(201, json={"hold_id": HOLD_C, "store_id": "SB-10482", "status": "active",
                                             "expires_at": "2030-01-01T00:00:00Z",
                                             "items": [{"component_name": "Oatmilk", "quantity": 1}]})
        if path == "/v1/hold-releases":
            key = json.loads(request.content)["idempotency_key"]
            if release_behaviour(key) == "timeout":
                raise httpx.ReadTimeout("stock did not answer", request=request)
            return httpx.Response(200, json={"idempotency_key": key, "outcome": "tombstoned", "hold_id": None})
        if path.endswith("/confirm") or path.endswith("/release"):
            status = "confirmed" if path.endswith("/confirm") else "released"
            return httpx.Response(200, json={"hold_id": HOLD_C, "store_id": "SB-10482", "status": status,
                                             "expires_at": "2030-01-01T00:00:00Z", "items": []})
        return httpx.Response(404, json={"request_id": "s", "error": {"code": "hold_not_found",
                                                                       "message": "no", "details": []}})

    return calls, handler


def stock_sequence(calls) -> list[tuple[str, str]]:
    """("hold", key) / ("release", key) / ("other", path), in the order the stock stub saw them."""
    out = []
    for c in calls:
        if c.url.path == "/v1/holds":
            out.append(("hold", c.headers["Idempotency-Key"]))
        elif c.url.path == "/v1/hold-releases":
            out.append(("release", json.loads(c.content)["idempotency_key"]))
        else:
            out.append(("other", c.url.path))
    return out


@pytest.mark.parametrize("save_point", ["order", "attempt"])
def test_failed_save_sends_nothing_and_returns_503(tmp_path, save_point):
    # The order service cannot save its state: nothing may go to stock or payment, the app gets a
    # retryable 503, and once storage recovers the same order_id starts at attempt 1.
    storage = {"broken": True}
    calls, handler = _scripted_stock(lambda key: "ok", lambda key: "ok")
    order_in = order_body("MO-C-0021", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk"))
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock"),
                       fail_saves=lambda point: storage["broken"] and point == save_point) as env:
        r = place(env, order_in)
        body = ORDER.check(r)
        assert r.status_code == 503
        assert body["error"]["code"] == "order_storage_unavailable"
        assert body["error"]["message"] == "We couldn't take your order just now. Please try again shortly."
        assert int(r.headers["Retry-After"]) >= 1
        assert calls == [], "a failed save must send nothing to the stock service"
        assert env.payment.calls == [], "a failed save must send nothing to payment"

        storage["broken"] = False
        retry = place(env, order_in)
        assert ORDER.check(retry)["status"] == "accepted"
        assert retry.status_code == 201
        assert stock_sequence(calls)[0] == ("hold", "MO-C-0021:hold:1"), "nothing was sent, so attempt 1 again"
        assert env.payment.calls_for("MO-C-0021") == 1


def test_release_timeout_blocks_the_next_hold(tmp_path):
    # Every hold and every release times out. The retry must not place :hold:2, because :hold:1 was
    # never confirmed released or tombstoned.
    calls, handler = _scripted_stock(lambda key: "timeout", lambda key: "timeout")
    order_in = order_body("MO-C-0022", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk"))
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock")) as env:
        first, second = place(env, order_in), place(env, order_in)
        for r in (first, second):
            assert ORDER.check(r)["error"]["code"] == "stock_service_unavailable"
            assert r.status_code == 503
        seq = stock_sequence(calls)
        holds = [key for kind, key in seq if kind == "hold"]
        releases = [key for kind, key in seq if kind == "release"]
        assert holds == ["MO-C-0022:hold:1", "MO-C-0022:hold:1"], "only the first request's same-key retry"
        assert not any(key.endswith(":hold:2") for key in holds), "no second hold while :hold:1 is unreleased"
        assert len(releases) >= 2 and set(releases) == {"MO-C-0022:hold:1"}, "release tried again before any new hold"
        assert env.payment.calls == []


def test_restart_releases_saved_attempt_before_next_hold(tmp_path):
    # Attempt 1 is saved and :hold:1 times out (its release too). The order service restarts on the
    # same database. Its first stock call must release :hold:1; only then may :hold:2 go out.
    order_in = order_body("MO-C-0023", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk"))
    _, before = _scripted_stock(lambda key: "timeout", lambda key: "timeout")
    with order_service(tmp_path, stock_client=mock_client(before, "http://stock")) as env:
        r = place(env, order_in)
        assert ORDER.check(r)["error"]["code"] == "stock_service_unavailable"

    calls, after = _scripted_stock(lambda key: "ok", lambda key: "ok")
    with order_service(tmp_path, stock_client=mock_client(after, "http://stock")) as env:  # the restart
        retry = place(env, order_in)
        assert ORDER.check(retry)["status"] == "accepted"
        assert retry.status_code == 201
        seq = [s for s in stock_sequence(calls) if s[0] in ("hold", "release")]
        assert seq[0] == ("release", "MO-C-0023:hold:1"), "first stock call after restart releases the saved attempt"
        assert [key for kind, key in seq if kind == "hold"] == ["MO-C-0023:hold:2"]
        assert seq.index(("hold", "MO-C-0023:hold:2")) > seq.index(("release", "MO-C-0023:hold:1"))
        assert env.payment.calls_for("MO-C-0023") == 1


# ------------------------------------------------------------------ hold expired before confirm

HOLD_A = "hold_" + "A" * 26
HOLD_B = "hold_" + "B" * 26


def _short(line_item, component, component_type):
    return {"reason": "base_out_of_stock" if component_type == "drink_base" else "add_in_out_of_stock",
            "line_item": line_item, "missing_component": component, "component_type": component_type,
            "needed": 1, "available": 0}


DEFAULT_SHORT = [_short(1, "Oatmilk", "drink_base")]


def _expired_hold_stub(rehold_outcome, shortfalls=None):
    """Stock stub: the first hold expires before confirm (409 hold_not_active).

    rehold_outcome: "available" -> the fresh hold succeeds; "gone" -> the fresh hold is refused
    with `shortfalls` (default: Oatmilk), returned in the order given, which may be scrambled.
    """
    calls = []

    def hold(hold_id):
        return {"hold_id": hold_id, "store_id": "SB-10482", "status": "active",
                "expires_at": "2030-01-01T00:00:00Z", "items": [{"component_name": "Oatmilk", "quantity": 1}]}

    def handler(request: httpx.Request):
        calls.append(request)
        path, key = request.url.path, request.headers.get("Idempotency-Key", "")
        if path == "/v1/holds" and ":rehold:" not in key:
            return httpx.Response(201, json=hold(HOLD_A))
        if path == "/v1/holds":
            if rehold_outcome == "available":
                return httpx.Response(201, json=hold(HOLD_B))
            return httpx.Response(409, json={"request_id": "s", "error": {
                "code": "component_unavailable", "message": "components short at SB-10482.",
                "details": shortfalls or DEFAULT_SHORT}})
        if path == f"/v1/holds/{HOLD_A}/confirm":
            return httpx.Response(409, json={"request_id": "s", "error": {
                "code": "hold_not_active", "message": "Hold expired.", "details": []}})
        if path == f"/v1/holds/{HOLD_B}/confirm":
            return httpx.Response(200, json={**hold(HOLD_B), "status": "confirmed"})
        if path.endswith("/release") or path == "/v1/hold-releases":
            return httpx.Response(200, json={"idempotency_key": key, "outcome": "already_closed", "hold_id": None})
        return httpx.Response(404, json={"request_id": "s", "error": {"code": "hold_not_found",
                                                                       "message": "no", "details": []}})

    return calls, handler


def test_hold_expired_before_confirm_fresh_hold_keeps_order_accepted(tmp_path):
    # Charged, then confirm finds the hold expired. A fresh hold (the commit-time recheck)
    # succeeds, so the customer still gets accepted, charged exactly once, never refunded.
    calls, handler = _expired_hold_stub("available")
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock")) as env:
        r = place(env, order_body("MO-C-0018", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 201
        assert body["status"] == "accepted"
        assert env.payment.calls_for("MO-C-0018") == 1
        assert env.payment.refunded_order_ids == []
        hold_keys = [c.headers["Idempotency-Key"] for c in calls if c.url.path == "/v1/holds"]
        assert len(hold_keys) == 2
        assert ":rehold:" in hold_keys[1] and hold_keys[1] != hold_keys[0]
        assert [c.url.path for c in calls if c.url.path.endswith("/confirm")] == [
            f"/v1/holds/{HOLD_A}/confirm", f"/v1/holds/{HOLD_B}/confirm"]


def test_hold_expired_and_stock_gone_refunds_and_never_says_unavailable(tmp_path):
    # Charged, hold expired, and the fresh hold is refused: refund in full, 409 order_refunded.
    calls, handler = _expired_hold_stub("gone")
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock")) as env:
        r = place(env, order_body("MO-C-0019", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        body = ORDER.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "order_refunded"
        assert body["error"]["message"] == (
            "We've refunded your payment in full. Oatmilk ran out before we could make your order. "
            "Please try again with a different option.")
        assert "unavailable" not in body["error"]["message"].lower()
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("component_ran_out", 1, "Oatmilk")]
        assert env.payment.calls_for("MO-C-0019") == 1
        assert env.payment.refunded_order_ids == ["MO-C-0019"]
        got = env.order.get("/v1/orders/MO-C-0019")
        assert ORDER.check(got)["status"] == "refunded"
        repeat = place(env, order_body("MO-C-0019", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        assert ORDER.check(repeat) == body
        assert env.payment.refunded_order_ids == ["MO-C-0019"], "refunded once, not again on repeat"


REFUND_LEAD = "We've refunded your payment in full. "
REFUND_TAIL = " ran out before we could make your order. Please try again with a different option."

LATTE_2_ADDINS = line("Caffè Latte", "Grande", "Oatmilk", ["Vanilla Syrup", "Caramel Drizzle"])


@pytest.mark.parametrize("lines, shortfalls, expected, names", [
    pytest.param(
        [line("Caffè Latte", "Grande", "Oatmilk", ["Vanilla Syrup"])],
        [_short(1, "Vanilla Syrup", "add_in"), _short(1, "Oatmilk", "drink_base")],
        [(1, "Oatmilk"), (1, "Vanilla Syrup")], "Oatmilk and Vanilla Syrup",
        id="two_short_milk_before_add_in"),
    pytest.param(
        [LATTE_2_ADDINS, line("Chai Tea Latte", "Venti", "Almondmilk")],
        [_short(2, "Almondmilk", "drink_base"), _short(1, "Caramel Drizzle", "add_in"),
         _short(1, "Oatmilk", "drink_base"), _short(1, "Vanilla Syrup", "add_in")],
        [(1, "Oatmilk"), (1, "Vanilla Syrup"), (1, "Caramel Drizzle"), (2, "Almondmilk")],
        "Oatmilk, Vanilla Syrup, Caramel Drizzle and Almondmilk",
        id="four_short_line_then_milk_then_add_ins_as_listed"),
    pytest.param(
        [line("Caffè Latte", "Grande", "Oatmilk"), line("Chai Tea Latte", "Venti", "Oatmilk")],
        [_short(2, "Oatmilk", "drink_base"), _short(1, "Oatmilk", "drink_base")],
        [(1, "Oatmilk"), (2, "Oatmilk")], "Oatmilk",
        id="same_component_on_two_lines_named_once"),
])
def test_refund_names_every_short_component_in_stable_order(tmp_path, lines, shortfalls, expected, names):
    # The stock stub returns the shortfalls scrambled; the order service must sort them
    # (line_item, then milk before add-ins, then add-ins as listed) and name each once.
    calls, handler = _expired_hold_stub("gone", shortfalls)
    with order_service(tmp_path, stock_client=mock_client(handler, "http://stock")) as env:
        r = place(env, order_body("MO-C-0020", "SB-10482", *lines))
        body = ORDER.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "order_refunded"
        assert body["error"]["message"] == REFUND_LEAD + names + REFUND_TAIL
        assert "unavailable" not in body["error"]["message"].lower()
        assert [(d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == expected
        assert all(d["reason"] == "component_ran_out" for d in body["error"]["details"])
        assert env.payment.refunded_order_ids == ["MO-C-0020"]


# ------------------------------------------------------------------ GET /v1/orders/{order_id}

def test_get_order_200_and_404(tmp_path):
    with order_service(tmp_path, seed()) as env:
        ORDER.check(place(env, order_body("MO-C-0017", "SB-10482", line("Caffè Latte", "Grande", "2% Milk"))))
        r = env.order.get("/v1/orders/MO-C-0017")
        body = ORDER.check(r)
        assert r.status_code == 200
        assert (body["order_id"], body["status"]) == ("MO-C-0017", "accepted")
        missing = env.order.get("/v1/orders/MO-NOPE")
        assert ORDER.check(missing)["error"]["code"] == "order_not_found"
        assert missing.status_code == 404


# ------------------------------------------------------------------ FR-002: every unrecognised name

def test_every_unrecognised_name_is_listed_and_the_whole_order_rejected(tmp_path):
    # FR-002: unknown drink, size, milk and add-in on one order. The whole order is rejected,
    # each bad name is listed, and nothing is reserved or paid.
    with order_service(tmp_path, seed()) as env:
        r = place(env, order_body("MO-C-0030", "SB-10482",
                                  line("Caffè Latte", "Grande", "2% Milk"),
                                  line("Mocha Frappe Supreme", "Gigantic", "Camel Milk", ["Vanila Syrup"])))
        body = ORDER.check(r)
        assert r.status_code == 400
        assert body["error"]["code"] == "invalid_order"
        assert body["error"]["message"] == ("Unrecognised drink: Mocha Frappe Supreme. Unrecognised size: Gigantic. "
                                            "Unrecognised milk: Camel Milk. Unrecognised add-in: Vanila Syrup.")
        assert [(d["field"], d["value"], d["line_item"]) for d in body["error"]["details"]] == [
            ("drink", "Mocha Frappe Supreme", 2), ("size", "Gigantic", 2),
            ("milk", "Camel Milk", 2), ("add_ins", "Vanila Syrup", 2)]
        assert env.payment.calls == []
        assert stock_levels(env.stock, "SB-10482", "2% Milk")["reserved_quantity"] == 0, "line 1 not processed alone"


# ------------------------------------------------------------------ FR-009 / FR-012: call order

class _Recorder(httpx.AsyncBaseTransport):
    """Delegates to `inner` and logs (service, METHOD, path) for every outgoing request."""

    def __init__(self, service, inner, log):
        self.service, self.inner, self.log = service, inner, log

    async def handle_async_request(self, request):
        self.log.append((self.service, request.method, request.url.path))
        return await self.inner.handle_async_request(request)


def _recorded_order_service(tmp_path, rows, log, payment=None):
    from tests.contract.harness import load_service
    stock_app = load_service("stock").create_app(db_path=str(tmp_path / "stock.db"), seed_rows=rows)
    payment = payment or FakePayment()
    stock_client = httpx.AsyncClient(
        transport=_Recorder("stock", httpx.ASGITransport(app=stock_app), log), base_url="http://stock")
    payment_client = httpx.AsyncClient(
        transport=_Recorder("payment", httpx.ASGITransport(app=payment.app), log), base_url="http://payment")
    return order_service(tmp_path, stock_client=stock_client, payment_client=payment_client, payment=payment)


def test_payment_requested_only_after_hours_and_stock_checks_pass(tmp_path):
    # FR-009 / US1 scenario 4: hold (hours + stock check) succeeds -> payment -> confirm, in that
    # order. A refused order (closed store, sold-out milk) never reaches payment.
    log = []
    rows = seed({"10482": {"Almondmilk": (0, 0)}})
    with _recorded_order_service(tmp_path, rows, log) as env:
        ok = place(env, order_body("MO-C-0031", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk")))
        assert ORDER.check(ok)["status"] == "accepted"
        steps = [(svc, path) for svc, _, path in log]
        hold = steps.index(("stock", "/v1/holds"))
        pay = steps.index(("payment", "/v1/payments"))
        confirm = next(i for i, (svc, path) in enumerate(steps) if path.endswith("/confirm"))
        assert hold < pay < confirm, f"expected hold -> payment -> confirm, got {steps}"
        assert sum(1 for s in steps if s == ("payment", "/v1/payments")) == 1

        for order_id, store, milk in (("MO-C-0032", "SB-20516", "Oatmilk"),
                                      ("MO-C-0033", "SB-10482", "Almondmilk")):
            log.clear()
            r = place(env, order_body(order_id, store, line("Caffè Latte", "Grande", milk)))
            assert ORDER.check(r)["error"]["code"] in ("store_closed", "component_unavailable")
            assert r.status_code == 409
            assert not any(svc == "payment" for svc, _, _ in log), f"{order_id} reached payment: {log}"


def test_order_service_reaches_stock_only_through_its_contract_and_keeps_no_stock_data(tmp_path):
    # FR-012 (ServiceBoundary): the order service talks to stock only via operations in
    # contracts/stock-service.yaml, keeps its own database, and holds no stock columns.
    import sqlite3
    from pathlib import Path

    log = []
    with _recorded_order_service(tmp_path, seed({"10482": {"Oatmilk": (0, 0)}}), log) as env:
        ORDER.check(place(env, order_body("MO-C-0034", "SB-10482", line("Caffè Latte", "Grande", "2% Milk"))))
        ORDER.check(place(env, order_body("MO-C-0035", "SB-10482", line("Caffè Latte", "Grande", "Oatmilk"))))
        ORDER.check(place(env, order_body("MO-C-0036", "SB-20516", line("Caffè Latte", "Grande", "2% Milk"))))
    stock_calls = [(m, p) for svc, m, p in log if svc == "stock"]
    assert stock_calls, "the order service must ask the stock service, not decide on its own"
    for method, path in stock_calls:
        STOCK.operation(method, path)  # raises if the call is not in the stock contract

    with sqlite3.connect(tmp_path / "order.db") as db:
        tables = [t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {c[1] for t in tables for c in db.execute(f"PRAGMA table_info('{t}')")}
    assert tables, "the order service keeps its own data"
    assert not columns & {"quantity_on_hand", "reserved_quantity", "store_open"}, columns

    source = "\n".join(p.read_text(encoding="utf-8")
                       for p in (Path(__file__).resolve().parents[2] / "services/order/app").glob("*.py"))
    for forbidden in ("services.stock", "_svc_stock", "stock.db", "StockSnapshot"):
        assert forbidden not in source, f"order service source references {forbidden!r}"


# ------------------------------------------------------------------ SC-003: answer within 2 seconds

def test_p95_answer_within_two_seconds(tmp_path):
    # SC-003: 95% of orders get an accept or refuse answer within 2 s under normal load.
    # 40 sequential orders (accepted, closed store, sold out) through order -> stock -> fake payment,
    # in-process. This bounds the services' own work; it does not measure network or a real provider.
    import time

    shapes = [("SB-10482", "2% Milk", 201), ("SB-20516", "2% Milk", 409), ("SB-10482", "Almondmilk", 409),
              ("SB-11937", "Oatmilk", 201)]
    timings = []
    with order_service(tmp_path, seed({"10482": {"Almondmilk": (0, 0)}})) as env:
        for i in range(40):
            store, milk, expected = shapes[i % len(shapes)]
            start = time.perf_counter()
            r = place(env, order_body(f"MO-C-{100 + i:04d}", store, line("Caffè Latte", "Grande", milk)))
            timings.append(time.perf_counter() - start)
            ORDER.check(r)
            assert r.status_code == expected
    timings.sort()
    p95 = timings[int(0.95 * len(timings)) - 1]
    assert p95 < 2.0, f"p95 {p95:.3f}s exceeds 2 s"

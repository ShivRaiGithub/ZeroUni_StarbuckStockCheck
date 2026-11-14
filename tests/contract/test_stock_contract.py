"""Contract tests: store stock service vs contracts/stock-service.yaml.

Expected values are worked out by hand from the seed in each test:
available = quantity_on_hand - reserved_quantity.
"""
import asyncio
import time

from tests.contract.harness import (async_client_for, hold_body, line, seed, stock_levels,
                                    stock_service)
from tests.contract.openapi_check import STOCK

LATTE_OAT = line("Caffè Latte", "Grande", "Oatmilk")


def post_hold(client, body, key):
    return client.post("/v1/holds", json=body, headers={"Idempotency-Key": key})


def availability(client, store_id, component):
    r = client.get(f"/v1/stores/{store_id}/availability")
    body = STOCK.check(r)
    assert r.status_code == 200
    return next(c for c in body["components"] if c["component_name"] == component)


# ------------------------------------------------------------------ POST /v1/holds 201

def test_hold_reserves_last_unit_and_returns_201(tmp_path):
    # Oatmilk on hand 1, reserved 0 -> available 1; one latte needs 1.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        r = post_hold(c, hold_body("SB-10482", LATTE_OAT), "MO-1:hold:1")
        body = STOCK.check(r)
        assert r.status_code == 201
        assert body["status"] == "active"
        assert body["store_id"] == "SB-10482"
        assert {"component_name": "Oatmilk", "quantity": 1} in body["items"]
        oat = availability(c, "SB-10482", "Oatmilk")
        assert (oat["available"], oat["status"]) == (0, "SOLD OUT")
        levels = stock_levels(c, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"]) == (1, 1)


def test_hold_replay_with_same_key_does_not_reserve_twice(tmp_path):
    # Oatmilk available 2. Same key twice -> one reservation, available 1.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (2, 0)}})) as c:
        body = hold_body("SB-10482", LATTE_OAT)
        first = post_hold(c, body, "MO-2:hold:1")
        second = post_hold(c, body, "MO-2:hold:1")
        b1, b2 = STOCK.check(first), STOCK.check(second)
        assert (first.status_code, second.status_code) == (201, 201)
        assert b1["hold_id"] == b2["hold_id"]
        assert stock_levels(c, "SB-10482", "Oatmilk")["reserved_quantity"] == 1


def test_hold_sums_need_across_lines_and_quantity(tmp_path):
    # Brown Sugar Syrup available 5. Line 1: 2 drinks, line 2: 3 drinks -> need 5 -> accepted.
    shaken = "Iced Brown Sugar Oatmilk Shaken Espresso"
    with stock_service(tmp_path, seed({"11937": {"Brown Sugar Syrup": (7, 2)}})) as c:
        r = post_hold(c, hold_body("SB-11937",
                                   line(shaken, "Grande", "Oatmilk", ["Brown Sugar Syrup"], 2),
                                   line(shaken, "Venti", "Oatmilk", ["Brown Sugar Syrup"], 3)), "k-sum")
        body = STOCK.check(r)
        assert r.status_code == 201
        assert {"component_name": "Brown Sugar Syrup", "quantity": 5} in body["items"]
        assert {"component_name": "Oatmilk", "quantity": 5} in body["items"]
        assert availability(c, "SB-11937", "Brown Sugar Syrup")["available"] == 0


# ------------------------------------------------------------------ POST /v1/holds 409

def test_store_closed_takes_precedence_over_stock(tmp_path):
    # Bellevue Square is closed and its Oatmilk is also at 0: only store_closed is reported.
    with stock_service(tmp_path, seed({"20516": {"Oatmilk": (0, 0)}})) as c:
        r = post_hold(c, hold_body("SB-20516", LATTE_OAT), "k-closed")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "store_closed"
        assert body["error"]["details"] == [
            {"reason": "store_closed", "store_id": "SB-20516", "store_name": "Bellevue Square"}]


def test_base_out_of_stock(tmp_path):
    # Almondmilk on hand 1, reserved 1 -> available 0 (rule 5). Vanilla Syrup is fine.
    with stock_service(tmp_path, seed({"10482": {"Almondmilk": (1, 1)}})) as c:
        r = post_hold(c, hold_body("SB-10482", line("Caffè Latte", "Grande", "Almondmilk", ["Vanilla Syrup"])),
                      "k-base")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "component_unavailable"
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", 1, "Almondmilk")]
        assert stock_levels(c, "SB-10482", "Vanilla Syrup")["reserved_quantity"] == 0


def test_add_in_out_of_stock_is_never_dropped(tmp_path):
    # Caramel Drizzle 3 on hand, 3 reserved -> available 0 (rule 6). 2% Milk and Vanilla fine.
    with stock_service(tmp_path, seed({"10482": {"Caramel Drizzle": (3, 3)}})) as c:
        r = post_hold(c, hold_body("SB-10482", line("Iced Caramel Macchiato", "Grande", "2% Milk",
                                                    ["Vanilla Syrup", "Caramel Drizzle"])), "k-addin")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("add_in_out_of_stock", 1, "Caramel Drizzle")]
        assert stock_levels(c, "SB-10482", "2% Milk")["reserved_quantity"] == 0
        assert stock_levels(c, "SB-10482", "Vanilla Syrup")["reserved_quantity"] == 0


def test_insufficient_quantity_reports_needed_and_available(tmp_path):
    # Brown Sugar Syrup on hand 5, reserved 2 -> available 3; 4 drinks need 4 (rule 8).
    shaken = line("Iced Brown Sugar Oatmilk Shaken Espresso", "Grande", "Oatmilk", ["Brown Sugar Syrup"], 4)
    with stock_service(tmp_path, seed({"11937": {"Brown Sugar Syrup": (5, 2)}})) as c:
        r = post_hold(c, hold_body("SB-11937", shaken), "k-insuff")
        body = STOCK.check(r)
        assert r.status_code == 409
        d = body["error"]["details"]
        assert len(d) == 1
        assert (d[0]["reason"], d[0]["line_item"], d[0]["missing_component"]) == (
            "insufficient_quantity", 1, "Brown Sugar Syrup")
        assert (d[0]["needed"], d[0]["available"]) == (4, 3)
        levels = stock_levels(c, "SB-11937", "Brown Sugar Syrup")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"]) == (5, 2)


def test_stock_runs_out_between_browse_and_hold(tmp_path):
    # Rule 7: availability says 1, a POS sale takes it, the hold must re-check and refuse.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        assert availability(c, "SB-10482", "Oatmilk")["available"] == 1
        adj = c.post("/v1/stock-adjustments",
                     json={"store_id": "SB-10482", "component_name": "Oatmilk", "delta": -1})
        adj_body = STOCK.check(adj)
        assert adj.status_code == 200
        assert (adj_body["quantity_on_hand"], adj_body["available"]) == (0, 0)
        r = post_hold(c, hold_body("SB-10482", line("Iced Brown Sugar Oatmilk Shaken Espresso", "Grande",
                                                    "Oatmilk", ["Brown Sugar Syrup"])), "k-mid")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert [(d["reason"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", "Oatmilk")]
        assert stock_levels(c, "SB-10482", "Brown Sugar Syrup")["reserved_quantity"] == 0


def test_every_failing_line_is_listed(tmp_path):
    # Line 1 Almondmilk 0, line 2 Oatmilk 0 -> two entries, nothing reserved.
    with stock_service(tmp_path, seed({"10482": {"Almondmilk": (0, 0), "Oatmilk": (0, 0)}})) as c:
        r = post_hold(c, hold_body("SB-10482", line("Caffè Latte", "Grande", "Almondmilk"),
                                   line("Chai Tea Latte", "Venti", "Oatmilk")), "k-multi")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert [(d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            (1, "Almondmilk"), (2, "Oatmilk")]


def test_concurrent_holds_for_last_unit_exactly_one_wins(tmp_path):
    # Oatmilk available 1; 10 different orders race for it. Stock must never go negative.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        async def race():
            async with async_client_for(c) as client:
                return await asyncio.gather(*[
                    client.post("/v1/holds", json=hold_body("SB-10482", LATTE_OAT),
                                headers={"Idempotency-Key": f"race-{i}:hold:1"}) for i in range(10)])

        responses = asyncio.run(race())
        for r in responses:
            STOCK.check(r)
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201] + [409] * 9
        levels = stock_levels(c, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"], levels["available"]) == (1, 1, 0)


def test_same_key_different_body_is_rejected(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        STOCK.check(post_hold(c, hold_body("SB-10482", LATTE_OAT), "k-reuse"))
        r = post_hold(c, hold_body("SB-10482", line("Caffè Latte", "Venti", "2% Milk")), "k-reuse")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert body["error"]["code"] == "idempotency_key_reused"
        assert stock_levels(c, "SB-10482", "2% Milk")["reserved_quantity"] == 0


# ------------------------------------------------------------------ POST /v1/holds 400 / 404

def test_unrecognised_add_in_is_400_and_reserves_nothing(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = post_hold(c, hold_body("SB-10482", line("Caffè Latte", "Grande", "2% Milk", ["Vanila Syrup"])),
                      "k-unknown-item")
        body = STOCK.check(r)
        assert r.status_code == 400
        assert body["error"]["code"] == "invalid_order"
        assert {"reason": "unrecognised", "field": "add_ins", "value": "Vanila Syrup", "line_item": 1} in \
            body["error"]["details"]
        assert stock_levels(c, "SB-10482", "2% Milk")["reserved_quantity"] == 0


def test_expiry_in_the_past_is_400_invalid_expiry(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = post_hold(c, hold_body("SB-10482", LATTE_OAT, ttl_seconds=-5), "k-past")
        body = STOCK.check(r)
        assert r.status_code == 400
        assert body["error"]["code"] == "invalid_expiry"


def test_expiry_beyond_600_seconds_is_400_invalid_expiry(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = post_hold(c, hold_body("SB-10482", LATTE_OAT, ttl_seconds=601), "k-far")
        body = STOCK.check(r)
        assert r.status_code == 400
        assert body["error"]["code"] == "invalid_expiry"


def test_hold_for_unknown_store_is_404(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = post_hold(c, hold_body("SB-99999", LATTE_OAT), "k-404")
        body = STOCK.check(r)
        assert r.status_code == 404
        assert body["error"]["code"] == "unknown_store"


# ------------------------------------------------------------------ hold lifecycle

def _active_hold(c, key="k-life"):
    r = post_hold(c, hold_body("SB-10482", LATTE_OAT), key)
    body = STOCK.check(r)
    assert r.status_code == 201
    return body["hold_id"]


def test_get_hold_200_and_404(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        hold_id = _active_hold(c)
        r = c.get(f"/v1/holds/{hold_id}")
        body = STOCK.check(r)
        assert r.status_code == 200
        assert (body["hold_id"], body["status"]) == (hold_id, "active")
        missing = c.get("/v1/holds/hold_00000000000000000000000000")
        assert STOCK.check(missing)["error"]["code"] == "hold_not_found"
        assert missing.status_code == 404


def test_confirm_consumes_stock_once(tmp_path):
    # Oatmilk 3 on hand. Hold 1 -> reserved 1. Confirm -> on hand 2, reserved 0.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (3, 0)}})) as c:
        hold_id = _active_hold(c)
        for _ in range(2):  # second confirm is idempotent
            r = c.post(f"/v1/holds/{hold_id}/confirm")
            assert STOCK.check(r)["status"] == "confirmed"
            assert r.status_code == 200
        levels = stock_levels(c, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"]) == (2, 0)


def test_release_returns_stock_once(tmp_path):
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (3, 0)}})) as c:
        hold_id = _active_hold(c)
        for _ in range(2):  # second release is idempotent
            r = c.post(f"/v1/holds/{hold_id}/release")
            assert STOCK.check(r)["status"] == "released"
            assert r.status_code == 200
        levels = stock_levels(c, "SB-10482", "Oatmilk")
        assert (levels["quantity_on_hand"], levels["reserved_quantity"]) == (3, 0)


def test_confirm_after_release_is_409_and_release_after_confirm_is_409(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        released = _active_hold(c, "k-a")
        STOCK.check(c.post(f"/v1/holds/{released}/release"))
        r = c.post(f"/v1/holds/{released}/confirm")
        assert STOCK.check(r)["error"]["code"] == "hold_not_active"
        assert r.status_code == 409

        confirmed = _active_hold(c, "k-b")
        STOCK.check(c.post(f"/v1/holds/{confirmed}/confirm"))
        r = c.post(f"/v1/holds/{confirmed}/release")
        assert STOCK.check(r)["error"]["code"] == "hold_not_active"
        assert r.status_code == 409


def test_confirm_and_release_unknown_hold_are_404(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        for action in ("confirm", "release"):
            r = c.post(f"/v1/holds/hold_00000000000000000000000000/{action}")
            assert STOCK.check(r)["error"]["code"] == "hold_not_found"
            assert r.status_code == 404


def test_hold_expires_on_its_own_and_frees_stock(tmp_path):
    # Oatmilk available 1, held with a 1 s expiry. After expiry the unit is sellable again.
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        r = post_hold(c, hold_body("SB-10482", LATTE_OAT, ttl_seconds=1), "k-exp-1")
        hold_id = STOCK.check(r)["hold_id"]
        assert r.status_code == 201
        time.sleep(1.5)
        got = c.get(f"/v1/holds/{hold_id}")
        assert STOCK.check(got)["status"] == "expired"
        confirm = c.post(f"/v1/holds/{hold_id}/confirm")
        assert STOCK.check(confirm)["error"]["code"] == "hold_not_active"
        again = post_hold(c, hold_body("SB-10482", LATTE_OAT), "k-exp-2")
        assert STOCK.check(again)["status"] == "active"
        assert stock_levels(c, "SB-10482", "Oatmilk")["reserved_quantity"] == 1


# ------------------------------------------------------------------ POST /v1/hold-releases

def test_release_by_key_releases_existing_hold(tmp_path):
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        hold_id = _active_hold(c, "MO-9:hold:1")
        r = c.post("/v1/hold-releases", json={"idempotency_key": "MO-9:hold:1"})
        body = STOCK.check(r)
        assert r.status_code == 200
        assert (body["outcome"], body["hold_id"]) == ("released", hold_id)
        assert stock_levels(c, "SB-10482", "Oatmilk")["reserved_quantity"] == 0


def test_release_by_key_tombstones_so_late_hold_cannot_reserve(tmp_path):
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (1, 0)}})) as c:
        r = c.post("/v1/hold-releases", json={"idempotency_key": "MO-10:hold:1"})
        assert STOCK.check(r)["outcome"] == "tombstoned"
        late = post_hold(c, hold_body("SB-10482", LATTE_OAT), "MO-10:hold:1")
        assert STOCK.check(late)["error"]["code"] == "hold_released"
        assert late.status_code == 409
        assert stock_levels(c, "SB-10482", "Oatmilk")["reserved_quantity"] == 0


def test_release_by_key_requires_a_key(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = c.post("/v1/hold-releases", json={"idempotency_key": ""})
        assert STOCK.check(r)["error"]["code"] == "invalid_request"
        assert r.status_code == 400


# ------------------------------------------------------------------ availability / adjustments

def test_availability_uses_on_hand_minus_reserved_and_keeps_leading_zeros(tmp_path):
    # SLU (09815) Oatmilk on hand 2, reserved 1 -> available 1, not 2.
    with stock_service(tmp_path, seed({"09815": {"Oatmilk": (2, 1), "Almondmilk": (4, 4)}})) as c:
        r = c.get("/v1/stores/SB-09815/availability")
        body = STOCK.check(r)
        assert r.status_code == 200
        assert body["store_id"] == "SB-09815"
        assert body["store_open"] is False
        by_name = {x["component_name"]: x for x in body["components"]}
        assert (by_name["Oatmilk"]["available"], by_name["Oatmilk"]["status"]) == (1, "IN STOCK")
        assert (by_name["Almondmilk"]["available"], by_name["Almondmilk"]["status"]) == (0, "SOLD OUT")


def test_availability_unknown_store_is_404(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        r = c.get("/v1/stores/SB-99999/availability")
        assert STOCK.check(r)["error"]["code"] == "unknown_store"
        assert r.status_code == 404


def test_adjustment_floors_on_hand_at_zero(tmp_path):
    with stock_service(tmp_path, seed({"10482": {"Oatmilk": (3, 0)}})) as c:
        r = c.post("/v1/stock-adjustments",
                   json={"store_id": "SB-10482", "component_name": "Oatmilk", "delta": -100})
        body = STOCK.check(r)
        assert r.status_code == 200
        assert (body["quantity_on_hand"], body["available"]) == (0, 0)


def test_adjustment_errors_400_and_404(tmp_path):
    with stock_service(tmp_path, seed()) as c:
        bad = c.post("/v1/stock-adjustments", json={"store_id": "SB-10482", "component_name": "Oatmilk"})
        assert STOCK.check(bad)["error"]["code"] == "invalid_request"
        assert bad.status_code == 400
        unknown = c.post("/v1/stock-adjustments",
                         json={"store_id": "SB-10482", "component_name": "Unobtainium", "delta": 1})
        assert STOCK.check(unknown)["error"]["code"] == "unknown_component"
        assert unknown.status_code == 404

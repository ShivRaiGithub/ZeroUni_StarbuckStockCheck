"""The stock service against the real, committed StockSnapshot (its own step, separate from the
28 contract tests, which seed their own stock).

Expected values are worked out by hand from StockSnapshot (available = on hand - reserved):
  First Hill (10482):    Oatmilk 0-0=0 SOLD OUT, Almondmilk 38-1=37, 2% Milk 96-3=93,
                         Brown Sugar Syrup 64-2=62, Caramel Drizzle 52-1=51
  Capitol Hill (11307):  Oatmilk 2-1=1 IN STOCK, Almondmilk 0-0=0 SOLD OUT
  South Lake Union (09815): closed (store_open=false); leading zero kept
In container mode (STOCK_IMAGE set) this runs the image exactly as shipped, with its baked-in
snapshot, and only proceeds once Docker's healthcheck reports "healthy".
"""
import hashlib
from datetime import datetime, timedelta, timezone

from tests.contract.harness import SNAPSHOT_PATH, STOCK_IMAGE, snapshot_stock_service, stock_levels
from tests.contract.openapi_check import STOCK


def hold(client, key, store, drink, size, milk, add_ins=(), quantity=1):
    expires = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    return client.post("/v1/holds", headers={"Idempotency-Key": key}, json={
        "store_id": store, "expires_at": expires,
        "lines": [{"line_item": 1, "drink": drink, "size": size, "milk": milk,
                   "add_ins": list(add_ins), "quantity": quantity}]})


def by_name(body):
    return {c["component_name"]: (c["available"], c["status"]) for c in body["components"]}


def test_real_snapshot_availability_matches_hand_calculation(tmp_path):
    with snapshot_stock_service(tmp_path) as (c, health):
        assert health == ("healthy" if STOCK_IMAGE else "in-process")
        first_hill = STOCK.check(c.get("/v1/stores/SB-10482/availability"))
        assert (first_hill["store_name"], first_hill["store_open"]) == ("First Hill - Madison & Boylston", True)
        fh = by_name(first_hill)
        assert fh["Oatmilk"] == (0, "SOLD OUT")
        assert fh["Almondmilk"] == (37, "IN STOCK")
        assert fh["2% Milk"] == (93, "IN STOCK")
        assert fh["Brown Sugar Syrup"] == (62, "IN STOCK")
        assert fh["Caramel Drizzle"] == (51, "IN STOCK")
        capitol = by_name(STOCK.check(c.get("/v1/stores/SB-11307/availability")))
        assert capitol["Oatmilk"] == (1, "IN STOCK"), "2 on hand, 1 reserved: 1, not 2"
        assert capitol["Almondmilk"] == (0, "SOLD OUT")
        slu = STOCK.check(c.get("/v1/stores/SB-09815/availability"))
        assert (slu["store_id"], slu["store_open"]) == ("SB-09815", False)


def test_real_snapshot_first_hill_oat_milk_order_is_refused(tmp_path):
    # Like MO-250307-0024 (Grande Iced Brown Sugar Oatmilk Shaken Espresso, Oatmilk, Brown Sugar
    # Syrup) at First Hill, where the snapshot has Oatmilk 0. Only Oatmilk is short.
    with snapshot_stock_service(tmp_path) as (c, _):
        r = hold(c, "snap-oat:hold:1", "SB-10482", "Iced Brown Sugar Oatmilk Shaken Espresso", "Grande",
                 "Oatmilk", ["Brown Sugar Syrup"])
        body = STOCK.check(r)
        assert r.status_code == 409
        assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
            ("base_out_of_stock", 1, "Oatmilk")]
        bss = stock_levels(c, "SB-10482", "Brown Sugar Syrup")
        assert (bss["quantity_on_hand"], bss["reserved_quantity"]) == (64, 2), "refusal reserved nothing"


def test_real_snapshot_capitol_hill_last_oat_milk_unit_goes_once(tmp_path):
    # Capitol Hill Oatmilk: 2 on hand, 1 reserved -> 1 available. One latte takes it; the next is refused.
    with snapshot_stock_service(tmp_path) as (c, _):
        first = hold(c, "snap-last-1:hold:1", "SB-11307", "Caffè Latte", "Grande", "Oatmilk")
        assert (first.status_code, STOCK.check(first)["status"]) == (201, "active")
        second = hold(c, "snap-last-2:hold:1", "SB-11307", "Caffè Latte", "Grande", "Oatmilk")
        body = STOCK.check(second)
        assert second.status_code == 409
        assert body["error"]["details"][0]["missing_component"] == "Oatmilk"
        oat = stock_levels(c, "SB-11307", "Oatmilk")
        assert (oat["quantity_on_hand"], oat["reserved_quantity"], oat["available"]) == (2, 2, 0)


def test_real_snapshot_closed_store_and_file_untouched(tmp_path):
    digest = hashlib.sha256(SNAPSHOT_PATH.read_bytes()).hexdigest()
    with snapshot_stock_service(tmp_path) as (c, _):
        r = hold(c, "snap-closed:hold:1", "SB-09815", "Caffè Latte", "Grande", "Oatmilk")
        body = STOCK.check(r)
        assert r.status_code == 409
        assert body["error"]["details"] == [{"reason": "store_closed", "store_id": "SB-09815",
                                             "store_name": "South Lake Union - Westlake & Mercer"}]
    assert hashlib.sha256(SNAPSHOT_PATH.read_bytes()).hexdigest() == digest, "the snapshot must never be written"

"""SC-001: replay all 40 sample orders from Sample_orders.csv through the gateway.

Each order runs gateway -> order service -> stock service in-process, with a fake payment
provider, against its own stock setup. The setups are worked out by hand from each row's
scenario_tag and quantity (Constitution V), because the StockSnapshot does not match the sample
orders (SB-11937 and SB-20516 are not in it, and its Almondmilk is in stock).

Every store x component starts at on hand 50, reserved 0 (available 50). The table below lists
only the overrides, each with the arithmetic that makes the CSV's expected outcome follow from
CLAUDE.md rules 4-8. test_fixtures_produce_the_expected_outcomes checks that arithmetic without
any service, so it passes today; the per-order tests stay red until the services exist.
"""
import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.contract.harness import (asgi_client, gateway_service, line, load_service, order_body,
                                    seed, stock_levels)
from tests.contract.openapi_check import GATEWAY, STOCK
from tests.fakes.payment import FakePayment

ROOT = Path(__file__).resolve().parents[2]
ROWS = list(csv.DictReader((ROOT / "Sample_orders.csv").open(encoding="utf-8", newline="")))
DEFAULT = (50, 0)

# order_id -> stock overrides {component: (on_hand, reserved)}, optional mid-order sale
# (component, delta) applied after the first availability check, and the derivation.
FIXTURES = {
    "MO-250303-0001": dict(why="SB-20516 Bellevue Square is closed (rule 4)."),
    "MO-250303-0002": dict(why="2% Milk 50 >= 1."),
    "MO-250303-0003": dict(why="No milk, no add-ins; nothing to reserve."),
    "MO-250303-0004": dict(why="2% Milk, Vanilla Syrup, Caramel Drizzle all 50 >= 1."),
    "MO-250303-0005": dict(why="Oatmilk, Brown Sugar Syrup 50 >= 1 at SB-11937."),
    "MO-250303-0006": dict(why="Nonfat Milk 50 >= 1."),
    "MO-250304-0007": dict(stock={"Almondmilk": (1, 1)}, why="Almondmilk 1 - 1 = 0 <= 0 (rule 5)."),
    "MO-250304-0008": dict(why="Vanilla Sweet Cream Cold Foam 50 >= 1 at SB-11937."),
    "MO-250304-0009": dict(why="Whole Milk, Extra Shot 50 >= 1."),
    "MO-250304-0010": dict(stock={"Almondmilk": (0, 0)}, why="Almondmilk 0 - 0 = 0 <= 0 (rule 5)."),
    "MO-250304-0011": dict(why="Whole Milk, Caramel Drizzle 50 >= 2 x 1."),
    "MO-250304-0012": dict(why="No milk, no add-ins; quantity 2."),
    "MO-250305-0013": dict(why="SB-20516 closed (rule 4), even though the drink uses Oatmilk."),
    "MO-250305-0014": dict(stock={"Vanilla Sweet Cream Cold Foam": (0, 0)},
                           why="Cold Foam 0 - 0 = 0 <= 0 (rule 6)."),
    "MO-250305-0015": dict(why="Almondmilk at SB-11937 is 50 (only SB-10482 is sold out)."),
    "MO-250305-0016": dict(why="2% Milk, Vanilla Syrup, Extra Shot 50 >= 1."),
    "MO-250305-0017": dict(why="2% Milk, Cinnamon Dolce Topping 50 >= 1."),
    "MO-250306-0018": dict(why="No milk, no add-ins."),
    "MO-250306-0019": dict(stock={"Caramel Drizzle": (0, 0)},
                           why="Caramel Drizzle 0 <= 0 (rule 6); Vanilla Syrup 50 is fine and not named."),
    "MO-250306-0020": dict(why="Coconutmilk 50 >= 1."),
    "MO-250306-0021": dict(why="Whole Milk 50 >= 1."),
    "MO-250306-0022": dict(why="2% Milk, Caramel Drizzle, Extra Shot 50 >= 1."),
    "MO-250307-0023": dict(stock={"Oatmilk": (1, 0)},
                           why="Oatmilk 1 - 0 = 1 >= 1: this order takes the last unit; no other sale."),
    "MO-250307-0024": dict(stock={"Oatmilk": (1, 0)}, mid=("Oatmilk", -1),
                           why="Oatmilk 1 at the first check, POS sale -1, 0 at commit (rule 7)."),
    "MO-250307-0025": dict(stock={"Oatmilk": (1, 0)}, mid=("Oatmilk", -1),
                           why="Oatmilk 1 at the first check, POS sale -1, 0 at commit (rule 7); "
                               "Extra Shot 50 is fine."),
    "MO-250307-0026": dict(stock={"Vanilla Sweet Cream Cold Foam": (1, 0)},
                           mid=("Vanilla Sweet Cream Cold Foam", -1),
                           why="Cold Foam 1 at the first check, POS sale -1, 0 at commit (rule 7); "
                               "Vanilla Syrup 50 is fine."),
    "MO-250307-0027": dict(stock={"Oatmilk": (1, 0)}, mid=("Oatmilk", -1),
                           why="Oatmilk 1 at the first check, POS sale -1, 0 at commit (rule 7)."),
    "MO-250307-0028": dict(stock={"Brown Sugar Syrup": (5, 2)},
                           why="Brown Sugar Syrup 5 - 2 = 3; 0 < 3 < 4 x 1 (rule 8)."),
    "MO-250307-0029": dict(stock={"Oatmilk": (0, 0)},
                           why="Oatmilk 0 - 0 = 0 <= 0 (rule 5), as in the snapshot."),
    "MO-250307-0030": dict(stock={"Brown Sugar Syrup": (2, 0)},
                           why="Brown Sugar Syrup 2 - 0 = 2; 0 < 2 < 3 x 1 (rule 8)."),
    "MO-250307-0031": dict(stock={"Oatmilk": (0, 0)}, why="Oatmilk 0 <= 0 (rule 5)."),
    "MO-250307-0032": dict(why="No milk, no add-ins; quantity 2."),
    "MO-250308-0033": dict(stock={"Oatmilk": (0, 0)}, why="Oatmilk 0 <= 0 (rule 5)."),
    "MO-250308-0034": dict(why="SB-20516 closed (rule 4)."),
    "MO-250308-0035": dict(stock={"Vanilla Sweet Cream Cold Foam": (2, 2)},
                           why="Cold Foam 2 - 2 = 0 <= 0 (rule 6)."),
    "MO-250308-0036": dict(why="Soymilk 50 >= 3 x 1."),
    "MO-250309-0037": dict(stock={"Oatmilk": (0, 0)}, why="Oatmilk 0 <= 0 (rule 5)."),
    "MO-250309-0038": dict(stock={"Brown Sugar Syrup": (0, 0)},
                           why="Brown Sugar Syrup 0 <= 0 at SB-11937 (rule 6)."),
    "MO-250309-0039": dict(stock={"Caramel Drizzle": (0, 0)}, why="Caramel Drizzle 0 <= 0 (rule 6)."),
    "MO-250309-0040": dict(why="SB-20516 closed (rule 4)."),
}

SCENARIO_REASON = {"base_out_of_stock": "base_out_of_stock", "add_in_out_of_stock": "add_in_out_of_stock",
                   "insufficient_quantity": "insufficient_quantity"}


def add_ins(row) -> list[str]:
    return [a for a in row["add_ins"].split(";") if a]


def components(row) -> list[str]:
    return ([] if row["milk"] == "None" else [row["milk"]]) + add_ins(row)


def snapshot_id(public_store_id: str) -> str:
    return public_store_id.removeprefix("SB-")


def expected_reason(row) -> str:
    """details[].reason the refusal must carry, from the scenario and the component's type."""
    if row["scenario_tag"] in SCENARIO_REASON:
        return SCENARIO_REASON[row["scenario_tag"]]
    # stock_runs_out_mid_order: whatever is true at commit (available 0) for that component type.
    return "base_out_of_stock" if row["expected_missing_component"] == row["milk"] else "add_in_out_of_stock"


def available_at_commit(row, component) -> int:
    fx = FIXTURES[row["order_id"]]
    on_hand, reserved = fx.get("stock", {}).get(component, DEFAULT)
    mid = fx.get("mid")
    if mid and mid[0] == component:
        on_hand = max(0, on_hand + mid[1])
    return on_hand - reserved


# ------------------------------------------------------------------ fixtures match the rules

def test_fixtures_produce_the_expected_outcomes():
    """Checks the hand-built setups, without any service: 40 orders, 19 accepted, 21 refused,
    and each setup really produces its CSV outcome under CLAUDE.md rules 4-8."""
    assert len(ROWS) == 40
    assert sum(r["expected_outcome"] == "accepted" for r in ROWS) == 19
    assert sum(r["expected_outcome"] == "refused" for r in ROWS) == 21
    assert set(FIXTURES) == {r["order_id"] for r in ROWS}, "every sample order needs a stock setup"
    for row in ROWS:
        qty = int(row["quantity"])
        closed = row["store_id"] == "SB-20516"
        assert closed == (row["scenario_tag"] == "store_closed"), row["order_id"]
        assert (FIXTURES[row["order_id"]].get("mid") is not None) == (
            row["stock_runs_out_mid_order"] == "true" and row["expected_outcome"] == "refused"), row["order_id"]
        short = [c for c in components(row) if available_at_commit(row, c) < qty]
        if row["expected_outcome"] == "accepted":
            assert not closed and short == [], f"{row['order_id']} would be refused: {short}"
        elif not closed:
            assert short == [row["expected_missing_component"]], f"{row['order_id']}: short {short}"
            if row["scenario_tag"] == "insufficient_quantity":
                assert 0 < available_at_commit(row, short[0]) < qty, row["order_id"]
            else:
                assert available_at_commit(row, short[0]) <= 0, row["order_id"]


# ------------------------------------------------------------------ 40 orders through the gateway

@pytest.mark.parametrize("row", ROWS, ids=[r["order_id"] for r in ROWS])
def test_sample_order_through_gateway(tmp_path, row):
    order_id, store = row["order_id"], row["store_id"]
    qty = int(row["quantity"])
    fx = FIXTURES[order_id]
    used = components(row)

    stock_app = load_service("stock").create_app(
        db_path=str(tmp_path / "stock.db"),
        seed_rows=seed({snapshot_id(store): fx.get("stock", {})}))
    stock = TestClient(stock_app, raise_server_exceptions=False)
    payment = FakePayment()
    order_app = load_service("order").create_app(
        db_path=str(tmp_path / "order.db"),
        stock_client=asgi_client(stock_app, "http://stock"),
        payment_client=asgi_client(payment.app, "http://payment"))

    if "mid" in fx:  # rule 7: in stock when the customer starts, sold at the till before commit
        comp, delta = fx["mid"]
        avail = STOCK.check(stock.get(f"/v1/stores/{store}/availability"))
        assert next(c for c in avail["components"] if c["component_name"] == comp)["available"] == 1
        sale = stock.post("/v1/stock-adjustments",
                          json={"store_id": store, "component_name": comp, "delta": delta})
        assert STOCK.check(sale)["available"] == 0

    before = {c: stock_levels(stock, store, c) for c in used} if store != "SB-20516" else {}

    with gateway_service(asgi_client(order_app, "http://order")) as gw:
        r = gw.post("/v1/orders", json=order_body(
            order_id, store, line(row["drink_name"], row["size"], row["milk"], add_ins(row), qty)))
        body = GATEWAY.check(r)

    if row["expected_outcome"] == "accepted":
        assert r.status_code == 201
        assert (body["order_id"], body["status"]) == (order_id, "accepted")
        assert payment.charged_order_ids == [order_id], "charged exactly once"
        for c in used:
            after = stock_levels(stock, store, c)
            assert after["quantity_on_hand"] == before[c]["quantity_on_hand"] - qty, c
            assert after["reserved_quantity"] == before[c]["reserved_quantity"], f"{c}: hold left behind"
        return

    assert r.status_code == 409
    assert body["error"]["message"] == row["expected_message"]
    assert payment.calls == [], "no payment for a refused order"
    if row["scenario_tag"] == "store_closed":
        assert body["error"]["code"] == "store_closed"
        assert [d["reason"] for d in body["error"]["details"]] == ["store_closed"]
        return
    assert body["error"]["code"] == "component_unavailable"
    assert [(d["reason"], d["line_item"], d["missing_component"]) for d in body["error"]["details"]] == [
        (expected_reason(row), 1, row["expected_missing_component"])]
    for c in used:
        after = stock_levels(stock, store, c)
        assert (after["quantity_on_hand"], after["reserved_quantity"]) == (
            before[c]["quantity_on_hand"], before[c]["reserved_quantity"]), f"{c}: stock changed by a refusal"

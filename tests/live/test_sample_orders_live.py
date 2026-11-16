"""SC-001 against the live stack: replay the 40 orders in starbucksSample, in file order, through
the gateway, each from the stock state its spec.md acceptance scenario gives ("Given ...").

The stack must be seeded with tests/live/SampleStock.csv (STOCK_SEED, see conftest.py): stores
SB-10482 and SB-11937 open, SB-20516 Bellevue Square closed, every menu component at on hand 50,
reserved 0. Before each order the test sets every component the order uses to its Given level,
through the stock service's own API (POST /v1/stock-adjustments; no shared database). Those levels
are the hand derivations in tests/contract/test_sample_orders.py FIXTURES, which match spec.md
(e.g. US2 #1 "Almondmilk (available stock 0)", US1 #3 "Oatmilk has exactly 1 unit available").

Rule 7 orders (stock runs out mid order, spec US2 #4): the component is set to 1, the first check
reads 1, then a till sale of -1 lands before the order is committed.

Each order's ID gets a per-run suffix so a rerun on the same stack is a new order, not a replay of
the first answer (FR-014). A failure prints expected next to actual; with REPLAY_REPORT=<file>
every order also appends one markdown row (order, expected, actual, match) to that file.
"""
import csv
import os
import uuid
from collections import defaultdict
from pathlib import Path

import pytest

from tests.contract.openapi_check import GATEWAY
from tests.contract.test_sample_orders import DEFAULT, FIXTURES, add_ins, components
from tests.live.conftest import available

ROOT = Path(__file__).resolve().parents[2]
ROWS = list(csv.DictReader((ROOT / "starbucksSample").open(encoding="utf-8", newline="")))
RUN = uuid.uuid4().hex[:8]

ORDERS: dict[str, list[dict]] = defaultdict(list)
for _row in ROWS:
    ORDERS[_row["order_id"]].append(_row)


def body(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda r: int(r["line_item_number"]))
    return {"order_id": f"{rows[0]['order_id']}-{RUN}", "store_id": rows[0]["store_id"],
            "submitted_at": rows[0]["submitted_at"],
            "drinks": [{"drink": r["drink_name"], "size": r["size"], "milk": r["milk"],
                        "add_ins": add_ins(r), "quantity": int(r["quantity"]),
                        "total_price": float(r["total_price"])} for r in rows]}


def expected(rows: list[dict]) -> tuple[str, str, str]:
    r = rows[0]  # the outcome is per order; every line of an order carries the same expectation
    return r["expected_outcome"], r["expected_missing_component"], r["expected_message"]


def actual(response) -> tuple[str, str, str]:
    data = response.json()
    if response.status_code == 201:
        return data.get("status", "?"), "", ""
    err = data.get("error", {})
    outcome = "refused" if response.status_code == 409 else f"HTTP {response.status_code} {err.get('code')}"
    if err.get("code") == "store_closed":
        missing = "store_closed"
    else:
        missing = ", ".join(d["missing_component"] for d in err.get("details", []) if "missing_component" in d)
    return outcome, missing, err.get("message", "")


def set_available(stock, store: str, component: str, target: int) -> None:
    delta = target - available(stock, store, component)
    if delta:
        r = stock.post("/v1/stock-adjustments", json={"store_id": store, "component_name": component, "delta": delta})
        assert r.status_code == 200, r.text
    assert available(stock, store, component) == target, f"{store} {component}: could not set available {target}"


def test_sample_has_40_orders():
    assert len(ORDERS) == 40
    assert set(ORDERS) == set(FIXTURES), "every sample order needs its Given stock state"


@pytest.mark.parametrize("order_id", list(ORDERS))
def test_sample_order(gateway, stock, order_id):
    rows = ORDERS[order_id]
    store, fx = rows[0]["store_id"], FIXTURES[order_id]
    open_store = rows[0]["scenario_tag"] != "store_closed"

    if open_store:  # Given: each component at its scenario level (available = on hand - reserved)
        for comp in dict.fromkeys(c for r in rows for c in components(r)):
            on_hand, reserved = fx.get("stock", {}).get(comp, DEFAULT)
            set_available(stock, store, comp, on_hand - reserved)
        if "mid" in fx:  # first check sees 1, a till sale takes it before commit
            comp, delta = fx["mid"]
            assert available(stock, store, comp) == 1
            r = stock.post("/v1/stock-adjustments", json={"store_id": store, "component_name": comp, "delta": delta})
            assert r.status_code == 200, r.text

    r = gateway.post("/v1/orders", json=body(rows))
    want, got = expected(rows), actual(r)
    if os.environ.get("REPLAY_REPORT"):  # one markdown row per order, e.g. for $GITHUB_STEP_SUMMARY
        with open(os.environ["REPLAY_REPORT"], "a", encoding="utf-8") as f:
            f.write(f"| {order_id} | {' / '.join(x for x in want if x)} | {' / '.join(x for x in got if x)} "
                    f"| {'✅' if got == want else '❌'} |\n")
    assert got == want, (
        f"{order_id}\n"
        f"  expected: outcome={want[0]!r} missing={want[1]!r} message={want[2]!r}\n"
        f"  actual:   outcome={got[0]!r} missing={got[1]!r} message={got[2]!r}")
    GATEWAY.check(r)  # and the reply matches the gateway contract

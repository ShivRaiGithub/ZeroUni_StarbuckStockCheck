import csv

import pytest
from fastapi.testclient import TestClient

from app import FIRST_HILL_ID, SNAPSHOT_PATH, app

client = TestClient(app)

# Expected values hand-derived from the snapshot (quantity_on_hand - reserved_quantity).
EXPECTED = {
    "oat_milk": (0, 0, 0, "SOLD OUT"),
    "almond_milk": (38, 1, 37, "IN STOCK"),
    "two_pct_milk": (96, 3, 93, "IN STOCK"),
}


def test_oat_milk_endpoint_sold_out():
    r = client.get("/first-hill/oat-milk")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] == 0
    assert body["status"] == "SOLD OUT"


@pytest.mark.parametrize("component_id", EXPECTED)
def test_every_first_hill_milk_entry(component_id):
    items = {i["component_id"]: i for i in client.get(f"/stores/{FIRST_HILL_ID}/milk").json()}
    on_hand, reserved, avail, status = EXPECTED[component_id]
    item = items[component_id]
    assert (item["quantity_on_hand"], item["reserved_quantity"]) == (on_hand, reserved)
    assert item["available"] == avail
    assert item["status"] == status


def test_covers_exactly_the_milk_entries_in_snapshot():
    with open(SNAPSHOT_PATH, newline="") as f:
        snap = {r["component_id"] for r in csv.DictReader(f)
                if r["store_id"] == FIRST_HILL_ID and r["component_id"].endswith("_milk")}
    assert snap == set(EXPECTED)
    returned = {i["component_id"] for i in client.get(f"/stores/{FIRST_HILL_ID}/milk").json()}
    assert returned == snap


def test_unknown_store_404():
    assert client.get("/stores/00000/milk").status_code == 404

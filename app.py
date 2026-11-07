"""Small stock service: reports milk availability per store from StockSnapshot."""
import csv
from pathlib import Path

from fastapi import FastAPI, HTTPException

SNAPSHOT_PATH = Path(__file__).parent / "StockSnapshot"
FIRST_HILL_ID = "10482"
MILK_IDS = {"oat_milk", "almond_milk", "two_pct_milk"}

app = FastAPI(title="Stock Service")


def load_snapshot(path: Path = SNAPSHOT_PATH) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def available(row: dict) -> int:
    """Inventory rule: available = quantity_on_hand - reserved_quantity."""
    return int(row["quantity_on_hand"]) - int(row["reserved_quantity"])


def to_item(row: dict) -> dict:
    avail = available(row)
    return {
        "store_id": row["store_id"],
        "store_name": row["store_name"],
        "component_id": row["component_id"],
        "component_name": row["component_name"],
        "quantity_on_hand": int(row["quantity_on_hand"]),
        "reserved_quantity": int(row["reserved_quantity"]),
        "available": avail,
        "status": "SOLD OUT" if avail <= 0 else "IN STOCK",
        "last_updated_at": row["last_updated_at"],
    }


def milk_items(store_id: str) -> list[dict]:
    rows = [r for r in load_snapshot()
            if r["store_id"] == store_id and r["component_id"] in MILK_IDS]
    if not rows:
        raise HTTPException(404, f"No milk entries for store {store_id}")
    return [to_item(r) for r in rows]


@app.get("/stores/{store_id}/milk")
def store_milk(store_id: str):
    return milk_items(store_id)


@app.get("/first-hill/oat-milk")
def first_hill_oat_milk():
    return next(i for i in milk_items(FIRST_HILL_ID) if i["component_id"] == "oat_milk")

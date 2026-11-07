# Stock Service

A small FastAPI service that reports milk availability for a store, built from the `StockSnapshot` CSV. Its main job is to answer: **is oat milk available at First Hill?**

## Inventory rule

```
available = quantity_on_hand - reserved_quantity
```

An item is `SOLD OUT` when `available <= 0`, otherwise `IN STOCK`.

This is because the an amount of reserved_quantity milk needs to be stored and not used. Therefore it a 'reserved_quantity' amount of milk is not used.

## Reasoning

- First Hill (store `10482`) oat milk has `quantity_on_hand = 0` and `reserved_quantity = 0`, so `available = 0` → **SOLD OUT**.
- The other First Hill milks are in stock: almond `38 - 1 = 37`, 2% `96 - 3 = 93`.
- Using `available` rather than raw `quantity_on_hand` matters: reserved units are already promised to orders. Capitol Hill oat milk (on hand 2, reserved 1) shows 1 left, not 2.
- "Milk" means the components `oat_milk`, `almond_milk`, `two_pct_milk`.
- Store IDs are kept as strings so IDs with leading zeros (e.g. `09815`) are preserved.
- The snapshot is re-read on each request, so edits to the file show up without a restart. That is fine at this scale.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /first-hill/oat-milk` | First Hill oat milk availability |
| `GET /stores/{store_id}/milk` | All milk entries for a store (404 if none) |

Example:

```json
{"store_id":"10482","component_id":"oat_milk","quantity_on_hand":0,"reserved_quantity":0,"available":0,"status":"SOLD OUT", ...}
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8000
curl localhost:8000/first-hill/oat-milk
```

## Verification

```bash
.venv/bin/pytest -v
```

The tests cover every First Hill milk entry (oat, almond, 2%) against values worked out by hand from the snapshot. They also check that the service returns exactly the milk rows present in the snapshot, that oat milk is `SOLD OUT`, and that an unknown store returns 404.

Verified results (also checked against the running service with `curl`):

| Component | On hand | Reserved | Available | Status |
|---|---|---|---|---|
| oat_milk | 0 | 0 | 0 | SOLD OUT |
| almond_milk | 38 | 1 | 37 | IN STOCK |
| two_pct_milk | 96 | 3 | 93 | IN STOCK |

## Files

- `app.py`: the service
- `test_app.py`: pytest tests
- `StockSnapshot`: input data
- `requirements.txt`: dependencies

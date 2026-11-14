# Quickstart: Validating the Feature

This guide shows how to prove the design end to end. Contract shapes are in [contracts/](contracts/),
entities and state machines are in [data-model.md](data-model.md), and the failure-handling
rationale is in [research.md § R8](research.md#r8-order-flow-and-failure-handling-the-core-design-answer).

## Prerequisites

- Python 3.12, `pip install -r requirements-dev.txt` (FastAPI, uvicorn, httpx, pytest, openapi-core)
- Docker (only for the optional local stack in step 3)
- Nothing outside the machine is contacted. Payment is a fake app started by the tests.

## 1. Run the required check locally (same as CI `contract-tests`)

```bash
pytest -p no:cacheprovider --strict-markers
```

Expected: every test is collected and passes, with **0 skipped** (any skip or xfail fails the session).
Before the services exist the run must **fail with real assertion failures**. It must not stop on a
collection error, and it must not report "no tests ran".

## 2. Scenario checklist (each is a test; expected values worked out by hand)

| # | Scenario | Setup (stock fixture) | Expected |
|---|---|---|---|
| 1 | `MO-250303-0003` accepted | SB-10482 open, no milk | 201 `accepted`; fake payment called once; hold `confirmed` |
| 2 | `MO-250307-0023` last unit | Oatmilk on_hand 1, reserved 0 | 201; Oatmilk available → 0 |
| 3 | `MO-250303-0001` store closed | SB-20516 "Bellevue Square" closed | 409 `store_closed`, exact CLAUDE.md message; payment calls 0; no hold |
| 4 | Closed + out of stock | SB-20516 closed, Oatmilk 0 | Same body as #3; no `component_unavailable` |
| 5 | `MO-250304-0007` base out | Almondmilk available 0 | 409 `component_unavailable`, `details.missing_component=Almondmilk`, `details.line_item_number=1` |
| 6 | `MO-250306-0019` add-in out | Caramel Drizzle 0, Vanilla > 0 | 409, `Caramel Drizzle` only; order not accepted without it |
| 7 | `MO-250307-0028` short qty | Brown Sugar Syrup available 3, qty 4 | 409 `Brown Sugar Syrup`; `reserved_quantity` unchanged |
| 8 | `MO-250307-0024` runs out mid-order | Oatmilk 1 → availability shows 1 → `POST /v1/stock-adjustments` delta −1 → submit | 409 `Oatmilk`; no payment |
| 9 | Two failing lines | L1 Almondmilk 0, L2 Oatmilk 0 | One 409 with 2 `failures`; `message` = both sentences joined |
| 10 | Last unit race | Oatmilk available 1; 20 concurrent orders | Exactly one 201; nineteen 409s; available 0, never < 0 |
| 11 | Same `order_id` twice | Any accepted order | Second call returns the same 201 body; 1 hold; 1 payment |
| 12 | Hold call times out | Stock stub sleeps 0.7 s on both attempts | 503 `stock_service_unavailable`; payment calls 0; `/v1/hold-releases` called with key `…:hold:1` |
| 13 | Lost first hold response | Stock stub creates the hold then drops the response | Retry with the same key replays the hold; `reserved_quantity` +q once |
| 14 | Late hold after release | Release-by-key, then the original create arrives | 409 `hold_released`; reserved unchanged |
| 15 | Payment declined | Fake payment declines | 402 `payment_declined`; hold `released`; reserved restored |
| 16 | Hold expiry | Create hold with `expires_at` = now + 1 s; wait 1.5 s | Hold `expired`; reserved restored exactly once |
| 17 | Order service down | Gateway pointed at a closed port | 503 `order_service_unavailable`; never 502/504 |
| 18 | Refusal pass-through | Through the gateway, scenario #5 | Body byte-identical to the order service apart from `request_id` |
| 19 | Invalid input | missing `store_id`; empty `drinks`; `SB-99999`; "Vanila Syrup" | 400 / 400 / 404 / 400 with the spec messages |
| 20 | All 40 sample orders | Per-row fixtures | 19 accepted, 21 refused with the expected messages (SC-001) |

## 3. Optional: run the three containers locally

```bash
docker compose up --build        # gateway :8080, order :8001, stock :8002, fake-payment :8003
curl -s -X POST localhost:8080/v1/orders -H 'content-type: application/json' -d '{
  "order_id":"MO-250303-0001","store_id":"SB-20516",
  "drinks":[{"drink":"Pike Place Roast","size":"Grande","milk":"None","add_ins":[],"quantity":1}]}'
# → 409 {"error":{"code":"store_closed","message":"Bellevue Square is closed right now and can't take mobile orders.", ...},"request_id":"..."}
docker compose stop order && curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/v1/orders -d '{}'
# → 503
```

## 4. CI

Push a branch and open a PR. The check named exactly **`contract-tests`** runs on both `push` and
`pull_request`, on Python 3.12, and it must report results on the PR's latest commit.

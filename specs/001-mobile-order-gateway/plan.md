# Implementation Plan: Mobile Order Gateway and Pre-Payment Availability Check

**Branch**: `001-mobile-order-gateway` (work currently on git branch `002-stock-contracts`) | **Date**: 2026-11-14 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-mobile-order-gateway/spec.md`. Hard constraints come from `ServiceBoundary` (7 March 2025 boundary review).

**Source of truth**: the committed OpenAPI 3.1 contracts
[`contracts/stock-service.yaml`](../../contracts/stock-service.yaml),
[`contracts/order-service.yaml`](../../contracts/order-service.yaml) and
[`contracts/gateway.yaml`](../../contracts/gateway.yaml), together with the contract tests in
`tests/contract/`. These contracts supersede the earlier design drafts kept under
`specs/001-mobile-order-gateway/contracts/` (see Open items).

## Summary

A mobile order passes through three FastAPI services. The **gateway** only routes `/v1` paths and
translates errors. The **order service** owns orders. The **store stock service** is the sole owner
of `quantity_on_hand`, `reserved_quantity` and `store_open`. The order service makes no stock
decision of its own. It sends the whole order as one `POST /v1/holds` (with `Idempotency-Key` and
`expires_at`) to the stock service, which atomically checks that the store is open, checks every
component against `available = quantity_on_hand - reserved_quantity`, and reserves everything or
nothing. That hold **is** the commit-time check (CLAUDE.md rules 7–8). Payment is requested only
after a 201 hold. A charge confirms the hold; a decline releases it immediately (402
`payment_declined`). Refusals (`store_closed`, `component_unavailable`) are always **HTTP 409**,
never 502/503/504. A 503 is never a refusal; it is a retryable "try again shortly":
`order_service_unavailable` from the gateway, and `stock_service_unavailable` or
`payment_status_unknown` from the order service. See [research.md](research.md) for decisions
R1–R14.

Every error body uses the envelope
`{ "error": { "code", "message", "details" }, "request_id" }`, where `details` is an **array**.
For stock refusals each `details[]` item has `reason`, `line_item` and `missing_component`. It may
also carry its own `message`. `reason` is one of `base_out_of_stock` (rule 5),
`add_in_out_of_stock` (rule 6) or `insufficient_quantity` (rule 8). Rule 7 (stock runs out
mid-order) has no reason value of its own. It is reported with whichever of the three is true when
the hold is attempted.

### What the order does when the hold call goes wrong

| Hold call | Order does | Caller sees |
|---|---|---|
| **Times out** | Retries once with the **same** `Idempotency-Key` (`{order_id}:hold:{attempt}`), so a hold created by the lost call is replayed, not doubled. If there is still no answer: **no payment**, and `POST /v1/hold-releases {idempotency_key}` either releases the hold or tombstones the key so a late create can't reserve. State `hold_unknown`. This is **not** a refusal. | **503** `stock_service_unavailable` with `Retry-After`. Body: `{ "error": { "code": "stock_service_unavailable", "message": "…try again shortly.", "details": [ { "reason": "stock_service_unavailable", "retryable": true } ] }, "request_id" }`. Retryable with the **same** `order_id`. |
| **Errors** (5xx or connection refused) | Same as a timeout: one retry with the same key, release by key, no payment. | **503** `stock_service_unavailable` (same body as above) |
| Invalid input (stock 400 `invalid_order` / 404 `unknown_store`) | Nothing reserved, no payment. | **400** `invalid_order` (one `details[]` item per problem, each unrecognised name listed) / **404** `unknown_store` ("Store SB-99999 does not exist.") |
| **Finds stock gone mid-order** (stock 409 `component_unavailable`) | Nothing was reserved (all or nothing). No payment. State `refused`. This is final and replayed on a repeat `order_id`. | **409** `component_unavailable`. `error.message` = "Line <n>, <Size> <Drink>: <Component> is unavailable at this store right now." for `details[0]`. `details` = `[ { "reason", "line_item", "missing_component" }, … ]`, one item per failing line/component. |
| **Store closed** (stock 409 `store_closed`) | Same as above. Takes precedence over any stock problem. | **409** `store_closed`. `error.message` = "<Store Name> is closed right now and can't take mobile orders." `details` = `[ { "reason": "store_closed", "store_id", "store_name" } ]`, with no `component_unavailable` items. |

The stock service's 409 `store_closed` and 409 `component_unavailable` map one-to-one to the order
service's 409 `store_closed` and 409 `component_unavailable`. The gateway passes them through
unchanged apart from `request_id`. A 409 is never turned into 502/503/504.

After a successful hold, the order is never refused for stock. If the payment call times out, the
hold is **kept** (not released) and the caller gets **503** `payment_status_unknown`, which is
retryable with the same `order_id`. If payment is declined, the hold is released immediately and the
caller gets **402** `payment_declined`.

### What the order does if the hold expires before confirm

The hold alone protects the unit between the stock check and payment. Normally the order service
does **not** recheck stock at confirm. It relies on the hold, which must still be `active`.

1. **Prevent it.** Every path from hold to confirm must finish well inside the hold's 120 s
   `expires_at`. The inline path takes at most about 2.6 s. A `payment_unknown` order is resolved
   by the reconciler within 30 s, and a failed confirm is retried every 2 s. If a confirm is still
   pending after 60 s (half the expiry), an alert fires.
2. **Recheck if it happens anyway.** If confirm returns 409 `hold_not_active` after a charge, the
   order service makes **one fresh hold** for the same lines (`Idempotency-Key`
   `{order_id}:rehold:{n}`). That is a new commit-time stock check (rule 7), never a stale one. If it
   succeeds, the order service confirms it and the order stays `accepted`. The customer sees
   **201** `accepted` or, if the request has already returned, nothing changes. The card is
   charged once.
3. **Refund if the stock is gone.** If the fresh hold is refused (409 `component_unavailable` or
   `store_closed`), the order service refunds the charge in full (`Idempotency-Key`
   `{order_id}:refund`) and the order ends as `refunded`. The customer gets **409**
   `order_refunded` with this message:
   "We've refunded your payment in full. <Component> ran out before we could make your order. Please try again with a different option."
   `<Component>` is filled from `error.details[0].missing_component`, which is the component the
   refused fresh hold reported as short. `details[]` lists every short component, each with
   `reason: component_ran_out`, `line_item` and `missing_component`. If the fresh hold was refused
   with `store_closed` instead, the message is "We've refunded your payment in full.
   <Store Name> closed before we could make your order.", with `<Store Name>` taken from
   `error.details[0].store_name`. If the request has already returned,
   `GET /v1/orders/{order_id}` shows `refunded` and the app tells the customer. The card is
   refunded once, even if the order is repeated.
4. **Stock service unreachable during the recheck.** The fresh hold follows the normal retry policy
   in the background. If it is still unresolved 5 minutes after the charge, the order is refunded
   with the message "We've refunded your payment in full. We couldn't make your order right now.
   Please try again shortly." (`reason: stock_unreachable`).

**Rule for charged orders (overrides the CLAUDE.md rules 5–8 wording).** CLAUDE.md rules 5–8 set
the "… is unavailable at this store right now." message for orders refused **before** payment.
After a charge, `ServiceBoundary` (Mobile App Checkout) requires that "A customer must never see
'unavailable' after their card has been charged." That hard constraint wins for charged orders, so
the refund messages above say "ran out" or "closed" and **never** "unavailable". The order service
contract enforces this: `OrderRefundedError.message` must not match "unavailable". Uncharged
refusals keep the exact CLAUDE.md wording. Open item: add this exception to CLAUDE.md as a rule 9,
which needs the project owner's approval.

The hold-expiry tests in `tests/contract/test_order_contract.py` cover steps 2 and 3.

## Technical Context

**Language/Version**: Python 3.12 (CI). Local runs on 3.13 also collect cleanly.

**Primary Dependencies** (pinned in `requirements.txt`): fastapi 0.162.2, uvicorn 0.54.0,
httpx 0.28.1. For tests: pytest 9.2.0, PyYAML 6.0.3, jsonschema 4.25.1 and referencing 0.36.2
(response bodies are checked against the OpenAPI schemas as JSON Schema 2020-12).

**Storage**: Stock service has a private SQLite file seeded from snapshot-format rows
(`StockSnapshot` in production; per-test rows in the contract tests). Order service has its own
private SQLite file. Gateway has none. Each service's `create_app(...)` takes a `db_path`.

**Testing**: pytest contract tests in `tests/contract/` (see [Test and CI](#test-and-ci)).

**Target Platform**: Linux containers, one image per service

**Project Type**: Web services (3 microservices)

**Performance Goals**: p95 < 2 s per order (SC-003). Hard 3 s end-to-end budget through the gateway.

**Constraints**:
- Contracted: gateway → order timeout 2.8 s (`contracts/gateway.yaml`).
- Contracted: hold `expires_at` must be 1–600 s ahead, and the order service sends now + 120 s
  (`contracts/stock-service.yaml`).
- Contracted: one same-key retry of the hold call before 503 (`contracts/order-service.yaml`).
- Contracted: the gateway never returns 502/504, and refusals are never 5xx.
- Design targets only, not asserted by the contracts or tests: order → stock 0.6 s per attempt,
  order → payment 1.0 s (research.md R7).

**Scale/Scope**: 4 seeded stores in the contract-test fixtures. The single-worker stock service
serialises reservations, and the contract tests check that 10 concurrent holds for the last unit
give exactly one 201.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | Evidence |
|---|---|---|
| I. Fixed OpenAPI contracts | PASS | `contracts/stock-service.yaml`, `contracts/order-service.yaml` and `contracts/gateway.yaml` (OpenAPI 3.1) are committed before any service code. |
| II. No shared database | PASS | Each service has its own SQLite file and `db_path`. The order service holds only opaque `hold_id`s and no counts. The tests wire services only through HTTP clients. |
| III. Contract tests gate merge | PASS | Required check `contract-tests` runs on push and pull_request. Skips, xfails, assert-free tests and "no tests collected" all fail the run. |
| IV. Inventory semantics | PASS | `available = on_hand − reserved` is computed only in the stock service, and `<= 0` is SOLD OUT. The contract tests check this with hand-derived values. |
| V. Hand-checked tests, string store IDs | PASS | Each test seeds its own rows, with the expected values derived in comments. `SB-09815` keeps its leading zero (tested). |
| VI. Test-first | PASS | The 68 contract tests are committed and failing before any service exists. |
| VII. One container per service | PASS (planned) | 3 Dockerfiles planned. The fake payment app lives in `tests/fakes/` and is never shipped. |
| VIII. No secrets in code | PASS | No credentials anywhere. Services receive their clients and DB paths at construction. |
| IX. Python + FastAPI | PASS | All three services, and the fake payment app. |
| Constraint: snapshot not mutated | PASS | The snapshot only seeds SQLite. |
| Constraint: unknown store → 404 | PASS | `unknown_store` 404 in the stock, order and gateway contracts (tested). |
| Constraint: added complexity justified | JUSTIFIED | SQLite (see Complexity Tracking). |

**Post-design re-check**: still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/001-mobile-order-gateway/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R14, spec conflicts C1–C5
├── data-model.md        # Phase 1: entities, hold and order state machines
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/           # Superseded design drafts; root contracts/*.yaml are authoritative (see Open items)
└── tasks.md             # Phase 2 (/speckit-tasks, not created yet)
```

### Source Code (repository root)

```text
contracts/                             # committed OpenAPI 3.1 contracts (source of truth)
├── stock-service.yaml
├── order-service.yaml
└── gateway.yaml
services/                              # not implemented yet; interface fixed by tests/contract/harness.py
├── stock/
│   ├── Dockerfile
│   └── app/                           # package: __init__.py + main.py
│       ├── main.py                    # create_app(*, db_path, seed_rows) -> FastAPI
│       ├── db.py                      # SQLite schema, seeding, BEGIN IMMEDIATE helper
│       ├── holds.py                   # create/confirm/release/expire, idempotency, tombstones
│       └── catalog.py                 # recipes, name → component, store ID mapping
├── order/
│   ├── Dockerfile
│   └── app/
│       ├── main.py                    # create_app(*, db_path, stock_client, payment_client) -> FastAPI
│       ├── flow.py                    # holding → paying → confirming
│       ├── stock_client.py            # same-key retry, release-by-key
│       ├── payment_client.py          # POST /v1/payments (interface in tests/fakes/payment.py)
│       └── messages.py                # all customer-facing copy
└── gateway/
    ├── Dockerfile
    └── app/main.py                    # create_app(*, order_client) -> FastAPI; proxy, request_id, 503 mapping
tests/
├── conftest.py                        # skip/xfail/assert-free guards; documented-status coverage check
├── fakes/payment.py                   # in-process fake payment provider
└── contract/
    ├── openapi_check.py               # validates real responses against contracts/*.yaml
    ├── harness.py                     # in-process wiring, seed rows, request builders
    ├── test_stock_contract.py
    ├── test_order_contract.py
    └── test_gateway_contract.py
.github/workflows/contract-tests.yml
requirements.txt                       # pinned runtime + test dependencies
pytest.ini
```

**Structure Decision**: One directory per service, each with its own Dockerfile (Constitution VII
and II). The OpenAPI contracts live together in the root `contracts/`, and all contract tests live in
`tests/contract/`, so one run checks every service against its contract. The existing root `app.py`
and `test_app.py` (milk prototype) stay until the stock service replaces them.

## Test and CI

- **Location**: `tests/contract/` holds `test_stock_contract.py`, `test_order_contract.py` and
  `test_gateway_contract.py`. There are 68 contract tests, plus the 6 prototype tests in `test_app.py`.
- **Contract checking**: `openapi_check.py` loads each OpenAPI file (`contracts/stock-service.yaml`,
  `contracts/order-service.yaml`, `contracts/gateway.yaml`). Every real response must:
  - be a documented operation and status,
  - send the documented headers,
  - have a body that validates against the documented schema, including the CLAUDE.md message
    patterns and cross-file `$ref`s.
- **In-process, no network**: services are built with `create_app(...)` and called through
  FastAPI's `TestClient`. Service-to-service calls use `httpx.ASGITransport`, and payment is a fake
  app in `tests/fakes/payment.py`. Timeouts and outages are simulated with `httpx.MockTransport`.
  Nothing outside the job is contacted.
- **Real assertions only**: `tests/conftest.py` reports any skipped test, any xfail, and any test
  that passes without a passing assert as a **failure**. "No tests collected" fails the run (pytest
  exit code 5). When the full suite passes, any documented status (other than 500) that no test
  exercised also fails the run.
- **Expected state now**: collection succeeds, and every contract test fails with
  "<service> service is not implemented yet" until `services/<name>/app/main.py` exists.
- **Workflow**: `.github/workflows/contract-tests.yml` runs on `push` and `pull_request`.
  - Python 3.12, one job whose id and name are exactly `contract-tests`.
  - It installs dependencies with `pip install -r requirements.txt` (all pinned) and runs `pytest`.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| SQLite in the stock service (the constitution prefers re-reading the snapshot) | Holds must change `reserved_quantity` atomically, survive restarts and expire. Without that you get the test-store drift and the last-unit race (SC-004). | Re-reading the CSV can't record reservations. An in-memory dict loses holds on restart. Postgres adds a container. |
| SQLite in the order service | Order-ID idempotency (FR-014) and resuming `hold_unknown` / `payment_unknown` need durable order state. | In-memory state would double-charge or double-reserve on retry after a restart. |
| Background expiry of holds | ServiceBoundary: no hold may stay forever. | Expiry checked only on writes never frees a hold at an idle store. |

## Open items for the owners (not blocking)

1. **Superseded design drafts**: the files under `specs/001-mobile-order-gateway/contracts/`
   contradict `contracts/*.yaml`. For example, they use a `details` object with `line_item_number`
   and `failures[]`, and a stock 422 `unknown_item`. `contracts/*.yaml` supersede them. Delete or
   replace the drafts in a separate cleanup commit.
2. **Other Phase 0/1 docs**: `research.md`, `data-model.md` and `quickstart.md` still describe that
   older error shape and link to the drafts. Bring them in line with `contracts/*.yaml` in the same
   cleanup.
3. **Remaining spec amendments**: C2–C5 in research.md (the snapshot assumption, where validation
   lives, the 503 for an unreachable order service, adopting the US4 codes in CLAUDE.md). C1
   (FR-013) is done; see below.
4. **Not yet covered by contracts or tests**:
   - a confirm that fails for a reason other than an expired hold (the expired-hold case is
     covered above and by contract tests),
   - how a `payment_unknown` order is reconciled,
   - the payment provider's real refund API (the tests use the fake's `POST /v1/refunds`),
   - the per-hop timeouts other than the gateway's 2.8 s,
   - acceptance tests for all 40 sample orders (SC-001).
   Specify them before or with the implementation.
5. **Branch naming**: the spec dir is `001-mobile-order-gateway`, but the work and PR #1 are on
   `002-stock-contracts`.

## Changes after review

- Rejected FR-013's 'no timed hold': Store Inventory requires every hold to expire, so holds now release immediately on failure and expire as a backstop.
  spec.md FR-013 and its clarification were amended to match. `contracts/stock-service.yaml`
  requires `expires_at` on every hold, and `tests/contract/` checks both the immediate release on
  payment decline and the expiry.
- Closed the gap where a hold could expire after a charge but before confirm. The order service now
  rechecks stock with one fresh hold. If that hold is refused, it refunds the charge and returns 409
  `order_refunded`, never "unavailable". `contracts/order-service.yaml` and `contracts/gateway.yaml`
  gained `order_refunded` and the `refunded` order status. See "What the order does if the hold
  expires before confirm".

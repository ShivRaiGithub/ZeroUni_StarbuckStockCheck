# Implementation Plan: Mobile Order Gateway and Pre-Payment Availability Check

**Branch**: `001-mobile-order-gateway` (work currently on git branch `002-stock-contracts`) | **Date**: 2026-11-14 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-mobile-order-gateway/spec.md`. Hard constraints come from `ServiceBoundary` (7 March 2025 boundary review).

## Summary

A mobile order passes through three FastAPI services. The **gateway** only routes `/v1` paths and
translates errors. The **order service** owns orders. The **store stock service** is the sole owner
of `quantity_on_hand`, `reserved_quantity` and `store_open`. The order service makes no stock
decision of its own. It sends the whole order as one `POST /v1/holds` (with `Idempotency-Key` and
`expires_at`) to the stock service, which atomically checks that the store is open, checks every
component against `available = quantity_on_hand - reserved_quantity`, and reserves everything or
nothing. That hold **is** the commit-time check (rules 7–8). Payment is requested only after a 201
hold. A charge confirms the hold; a decline releases it immediately. Refusals (`store_closed`,
`component_unavailable`) are always **HTTP 409**. The only 503s are "try again shortly" cases that
are safe to retry: order service unreachable (from the gateway), and a stock or payment call whose
outcome is unknown (from the order service). See [research.md](research.md) for decisions R1–R14.

### What the order does when the hold call goes wrong

| Hold call | Order does | Caller sees |
|---|---|---|
| **Times out** (0.6 s) | Retries once with the **same** `Idempotency-Key`, so a hold created by the lost call is replayed, not doubled. If it still has no answer: **no payment**, `POST /v1/hold-releases {idempotency_key}` (releases the hold, or tombstones the key so a late create can't reserve), state `hold_unknown`. A client retry with the same `order_id` resumes with key `attempt+1`. | 503 `stock_service_unavailable`, "try again shortly", `Retry-After: 1` |
| **Errors** (5xx / connection refused / bad body) | Same as timeout. | 503 `stock_service_unavailable` |
| Errors (contract 4xx) | 404 → `unknown_store`; 422 → `invalid_order` naming every unrecognised item. Nothing reserved, no payment. | 404 / 400 |
| **Finds stock gone mid-order** (409 `insufficient_stock`) | Nothing was reserved (all or nothing). No payment. Every shortfall is mapped to "Line n, Size Drink: Component is unavailable at this store right now." State `refused` (final; replayed on repeat). | **409** `component_unavailable` with `details.missing_component`, `details.line_item_number`, `details.failures[]` |
| Store closed (409 `store_closed`) | Same as above. Takes precedence over stock problems. | **409** `store_closed` |

After a successful hold, nothing can produce "unavailable": a confirm failure is retried in the
background, and a payment timeout keeps the hold and resolves to `accepted` or `payment_declined`.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: FastAPI, Pydantic v2, uvicorn, httpx (inter-service client with explicit timeouts), python-ulid; dev: pytest, openapi-core (response-vs-contract validation)

**Storage**: Stock service has a private SQLite file seeded from `StockSnapshot` (read-only input). Order service has its own private SQLite file. Gateway has none.

**Testing**: pytest. Contract tests validate every response against the committed OpenAPI files. Services run in-process through `httpx.ASGITransport`, with a fake payment app. No external network.

**Target Platform**: Linux containers, one image per service

**Project Type**: Web services (3 microservices)

**Performance Goals**: p95 < 2 s per order (SC-003). Hard 3 s end-to-end budget through the gateway (R7).

**Constraints**: Timeouts gateway→order 2.8 s; order→stock 0.6 s × 2; order→payment 1.0 s; confirm 0.3 s inline. Holds expire after ≤ 120 s. Never 502/504 from the gateway; refusals never 5xx.

**Scale/Scope**: 3–4 stores in fixtures, 40 sample orders, single-worker stock service (SQLite `BEGIN IMMEDIATE` serialises reservations)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | Evidence |
|---|---|---|
| I. Fixed OpenAPI contracts | PASS | `contracts/{gateway,order-service,stock-service}.openapi.yaml`. They are committed per service before the code. |
| II. No shared database | PASS | Each service has its own SQLite file in its own container. The order service holds only opaque `hold_id`s and no counts. |
| III. Contract tests gate merge | PASS | Required check `contract-tests` runs on push and PR. Skips, xfails and 0-collected all fail (R14). |
| IV. Inventory semantics | PASS | `available = on_hand − reserved`, computed only in the stock service. `<= 0` is SOLD OUT. Reserved units are never sellable. |
| V. Hand-checked tests, string store IDs | PASS | Per-scenario fixtures with written derivations (R13). `store_id` is TEXT and `SB-09815` keeps its zero. |
| VI. Test-first | PASS (process) | Tasks will order contract tests before each endpoint, and CI is expected to go red first. |
| VII. One container per service | PASS | 3 Dockerfiles. The fake payment app is a test double and is not shipped. |
| VIII. No secrets in code | PASS | Service URLs and DB paths come from env vars. There are no credentials. |
| IX. Python + FastAPI | PASS | All three services. |
| Constraint: snapshot not mutated | PASS | The snapshot only seeds SQLite and is never written back. |
| Constraint: unknown store → 404 | PASS | `unknown_store` 404 in the stock and order contracts. |
| Constraint: added complexity justified | JUSTIFIED | SQLite is needed for holds (see Complexity Tracking). |

**Post-design re-check**: still PASS. No new violations came out of Phase 1.

## Project Structure

### Documentation (this feature)

```text
specs/001-mobile-order-gateway/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R14, spec conflicts C1–C5
├── data-model.md        # Phase 1: entities, hold and order state machines
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── gateway.openapi.yaml
│   ├── order-service.openapi.yaml
│   └── stock-service.openapi.yaml
└── tasks.md             # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (repository root)

```text
services/
├── stock/
│   ├── Dockerfile
│   ├── openapi.yaml               # copy of contracts/stock-service.openapi.yaml (source of truth)
│   ├── app/
│   │   ├── main.py                # FastAPI app, routes
│   │   ├── db.py                  # SQLite schema, seed from StockSnapshot, BEGIN IMMEDIATE helper
│   │   ├── holds.py               # create/confirm/release/expire, idempotency, tombstones
│   │   ├── catalog.py             # recipes, name → component, store ID mapping
│   │   └── sweeper.py             # background expiry
│   └── tests/{contract,unit}/ + tests/fixtures/
├── order/
│   ├── Dockerfile
│   ├── openapi.yaml
│   ├── app/
│   │   ├── main.py
│   │   ├── flow.py                # state machine: holding → paying → confirming
│   │   ├── stock_client.py        # httpx, timeouts, same-key retry, release-by-key
│   │   ├── payment_client.py
│   │   ├── messages.py            # all customer-facing copy (one place for Product)
│   │   ├── reconciler.py          # pending confirms, payment_unknown
│   │   └── store.py               # private SQLite: orders, idempotency
│   └── tests/{contract,unit}/
└── gateway/
    ├── Dockerfile
    ├── openapi.yaml
    ├── app/main.py                # proxy, request_id, 503 mapping; no business logic
    └── tests/contract/
tests/
├── conftest.py                    # in-process wiring of 3 services + fake payment; fail on skip
├── fakes/payment.py
└── e2e/                           # 40 sample orders, race test, failure-injection tests
.github/workflows/contract-tests.yml   # job name: contract-tests; push + pull_request; py3.12
docker-compose.yml                     # local only
```

**Structure Decision**: One directory per service, each with its own Dockerfile, OpenAPI file and
tests (Constitution VII and II). The existing root `app.py` and `test_app.py` (milk prototype) move into
`services/stock` as the start of the stock service. Its `/stores/{id}/milk` routes are dropped in
favour of `/v1/stores/{id}/availability`. Cross-service e2e tests live in the root `tests/`.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| SQLite in the stock service (the constitution prefers re-reading the snapshot) | Holds must change `reserved_quantity` atomically, survive restarts and expire. Without that you get the test-store drift and the last-unit race (SC-004). | Re-reading the CSV can't record reservations. An in-memory dict loses holds on restart. Postgres adds a container. |
| SQLite in the order service | Order-ID idempotency (FR-014) and resuming `hold_unknown` / `payment_unknown` need durable order state. | In-memory state would double-charge or double-reserve on retry after a restart. |
| Background sweeper/reconciler tasks | ServiceBoundary: no hold may stay forever. A confirm after a charge must eventually succeed. | Lazy expiry alone never frees a hold at an idle store. |

## Open items for the owners (not blocking Phase 1)

1. **Spec amendments C1–C5** (research.md): FR-013 wording vs hold expiry, the snapshot assumption,
   where validation lives, and the 503 for an unreachable order service. Run `/speckit-specify` or
   `/speckit-clarify` to fold them in.
2. **Hold timeout → 503 vs 409**: this plan returns 503 `stock_service_unavailable`. A 409 would be a
   false "unavailable" and the app wouldn't retry it. If the owners want *every* non-accept to be
   409, change this one mapping (R8).
3. **Branch naming**: the spec dir is `001-mobile-order-gateway` but git is on `002-stock-contracts`.
   Confirm which branch the PR targets so `contract-tests` reports on the right commit.

# Phase 0 Research: Mobile Order Gateway and Pre-Payment Availability Check

All decisions below treat `ServiceBoundary` (7 March 2025 review) as hard constraints. Where the
spec and `ServiceBoundary` disagree, `ServiceBoundary` wins and the conflict is listed in
[§ Conflicts](#conflicts-with-specmd-to-carry-back).

---

## R1. Service split and data ownership

- **Decision**: Three FastAPI services, one container each:
  - **stock service**: the only owner of `quantity_on_hand`, `reserved_quantity`, `store_open`,
    store names, the store-ID mapping and drink recipes. It is the single place that answers "can this
    store make this line item right now" (as `POST /v1/holds`).
  - **order service**: owns orders, order idempotency and the order state machine. It never reads
    stock tables or the snapshot file, and never caches counts or open status.
  - **gateway**: routing and response translation only. It has no storage, does no business
    validation and never decides whether an order is accepted.
- **Rationale**: Store Inventory's hard constraints (sole ownership, no credentials elsewhere, no
  copies treated as truth, one place for the "can make" answer), plus Constitution II and VII.
- **Alternatives rejected**: Order service computing availability from a stock read
  (`GET` then decide). This is forbidden ("callers must not work this out themselves") and it races
  with POS writes. A shared DB or read replica is forbidden outright.

## R2. Stock service storage

- **Decision**: SQLite file private to the stock container, seeded at startup from `StockSnapshot`
  (read-only input, never written back). Every hold, confirm, release and expiry runs in one
  `BEGIN IMMEDIATE` transaction.
- **Rationale**: Holds change `reserved_quantity` and must survive concurrent requests atomically
  (SC-004, rule 8 "never negative"). The current "re-read the CSV per request" approach can't
  record reservations. SQLite needs no network service, so it satisfies Dmitri's "nothing outside
  the job" rule.
- **Alternatives rejected**: In-memory dict with a lock, which loses holds on restart, so the
  `reserved_quantity` drift returns. Postgres, which is an extra container and isn't justified at
  this scale. Writing back to the CSV, which the constitution forbids.

## R3. Commit-time check = hold creation (rules 7, 8; Store Inventory concern on POS drift)

- **Decision**: The order service makes no availability check of its own. The **only** check that
  counts is `POST /v1/holds`. Inside one transaction the stock service:
  1. expires any overdue holds for that store (lazy sweep),
  2. refuses with `store_closed` if the store is closed (this takes precedence over stock, per
     US2 scenario 2),
  3. computes each component's need as Σ(line `quantity` × units per drink) across **all** lines,
  4. checks `quantity_on_hand - reserved_quantity >= need` for every component,
  5. if every component passes, adds `need` to `reserved_quantity` and creates the hold. If any
     fails, it changes nothing and returns every shortfall.
- **Rationale**: This is the "check made when the order is accepted". A value read while browsing
  (or from `GET /v1/stores/{id}/availability`) is advisory only. Because the update is conditional
  and atomic, two orders can never both take the last unit and stock can never go negative.
- **Alternatives rejected**: Check-then-reserve in two calls, which is exactly the stale-check bug
  that rule 7 forbids. Optimistic version columns: same guarantee, more code.

## R4. Units per drink (rule 8)

- **Decision**: The stock service owns a recipe table. v1 uses **1 unit of the chosen milk per drink**
  (none when milk is `None`) and **1 unit of each add-in per drink**, for every size. The need for a
  line is `quantity × units`. The same component on several lines is summed before checking.
- **Rationale**: The snapshot units are abstract "servings". Sample `MO-250307-0028` (4 drinks,
  Brown Sugar Syrup available 1–3) and `MO-250307-0030` (3 drinks) only work with a per-drink
  multiplier.
- **Alternatives**: Size-scaled units are deferred. The recipe table already allows them without a
  contract change.

## R5. Hold lifetime: no hold stays forever

- **Decision**: Every hold is `active → confirmed | released | expired`. The order service sends
  `expires_at` = now + **120 s**. The stock service rejects anything beyond 600 s or in the past, with
  400 `invalid_expiry`. Expiry is enforced in two ways: a lazy sweep at the start of every write
  transaction, and a 5-second background sweeper. Both move the held quantity out of
  `reserved_quantity` exactly once.
- **Rationale**: Store Inventory's hard constraint, plus the drift they saw at test stores. Release
  on payment failure is still **immediate** (FR-013). `expires_at` is only a backstop for crashes
  and lost calls, not a deliberate timed hold.
- **Alternatives rejected**: Holds without expiry, which is the drift bug. A short TTL such as 10 s
  could expire before a slow payment confirms, and that would breach "never unavailable after
  charge".

## R6. Idempotency

- **Decision**:
  - App → order: `order_id` (client-supplied, e.g. `MO-250303-0003`) is the idempotency key. A
    repeat with the same body returns the stored **final** result (`accepted`, a refusal, or
    `payment_declined`) with the original status. A repeat with a different body gets 409
    `order_id_conflict`. Non-final states (see R8) are resumed, not re-run from scratch.
  - Order → stock: `Idempotency-Key: {order_id}:hold:{attempt}`. The stock service stores
    key → (request hash, response). A replay returns the same response without reserving again. The
    same key with a different body gets 409 `idempotency_key_reused`.
  - Order → payment: `Idempotency-Key: {order_id}:pay`.
- **Rationale**: Store Inventory: "a retried call must not hold the same quantity twice". The app
  retries on 503 and on its 8-second timeout, so retries are guaranteed to happen.

## R7. Time budget (3 s end-to-end through the gateway)

| Hop | Per-attempt timeout | Attempts | Worst case |
|---|---|---|---|
| gateway → order | 2.8 s total (0.3 s connect) | 1 | 2.8 s |
| order → stock `POST /v1/holds` | 0.6 s | 2 (same key) | 1.3 s incl. 100 ms backoff |
| order → payment | 1.0 s | 1 | 1.0 s |
| order → stock confirm | 0.3 s inline, then background retry | 1 inline | 0.3 s |

1.3 + 1.0 + 0.3 = 2.6 s, which fits under the gateway's 2.8 s and leaves 0.2 s for
gateway/serialisation. The order service also keeps a **request deadline**: it skips a second
hold attempt if less than 0.6 s + 1.3 s (for payment and confirm) would remain.

## R8. Order flow and failure handling (the core design answer)

```
received ──validate──▶ holding ──hold 201──▶ paying ──charged──▶ confirming ──▶ accepted
   │                     │                     │                     └─confirm fails─▶ accepted (confirm retried in background)
   │ 400/404             │ 409 / 404 / 422     │ declined
   ▼                     ▼                     ▼
 rejected              refused            payment_declined   (hold released immediately)
                         │
                         │ timeout / 5xx / connect error after retries
                         ▼
                   hold_unknown  (release-by-key sent, no payment, 503 to caller, resumable)
```

Payment is requested **only** in the `paying` state, which is reachable only after a 201 from
`POST /v1/holds`. What the order does when the hold call…

| …hold call outcome | Order service action | Response to app (via gateway) |
|---|---|---|
| **201 created** | Request payment (`{order_id}:pay`). Charged → `POST /v1/holds/{id}/confirm` → `accepted`. Declined → `POST /v1/holds/{id}/release` immediately → `payment_declined`. | 201 `accepted` / 402 `payment_declined` |
| **409 `store_closed`** | No hold exists. No payment. State `refused`. | **409** `store_closed`, "<Store Name> is closed right now and can't take mobile orders." |
| **409 `insufficient_stock`** (stock gone mid-order, base/add-in out, short quantity) | No hold exists (all or nothing, nothing reserved). No payment. State `refused`. The message for each shortfall is built from line number, size, drink and component. | **409** `component_unavailable`, with all failing lines in `details.failures` |
| **404 `unknown_store`** / **422 `unknown_item`** | No hold. No payment. State `rejected`. | 404 `unknown_store` / 400 `invalid_order` (naming each unrecognised item) |
| **Times out** (0.6 s, no response) | Retry once with the **same** `Idempotency-Key`. A hold created by the lost first call is replayed, not doubled. If the retry also times out or the budget is gone: **no payment**, send `POST /v1/hold-releases {idempotency_key}` (fire and forget, 0.2 s), state `hold_unknown`. | **503** `stock_service_unavailable`, "We couldn't check stock just now. Please try again shortly.", `Retry-After: 1` |
| **Errors** (5xx, connection refused, malformed body) | Same as timeout: one retry with the same key, then release-by-key, state `hold_unknown`, no payment. | **503** `stock_service_unavailable` |
| **409 `idempotency_key_reused`** | Programming error. Log at ERROR, no payment, release-by-key. | 500 `internal_error` |

**Why a hold timeout is 503 and not 409**: a 409 is a *refusal*, a definite statement that this
order can't be made, and the app won't retry it. After a timeout we don't know anything about
stock, so telling the customer "unavailable" would be false. 503 is safe here: the app retries
automatically, the retry has the same `order_id`, and the order service resumes it from
`hold_unknown` with a fresh hold key (`attempt+1`). The old key has been tombstoned by the
release-by-key (R9), so a late-arriving first hold can't reserve stock. No retry can double-reserve
or double-charge. Stock refusals are **always** 409 and are never mapped to 502/503/504.

**Never "unavailable" after a charge**: the stock check (hold) is finished before payment starts.
A failed confirm is retried in the background every 2 s, with an alert if it is still pending
after 60 s. While the hold is active, confirm can't fail for lack of stock. If the hold has
**expired** (confirm returns 409 `hold_not_active`), the order service makes one fresh hold with
key `{order_id}:rehold:{n}`, which is a new commit-time check. If it succeeds, confirm it and the
order stays `accepted`. If it is refused, refund the charge (`{order_id}:refund`) and end as
`refunded`, returning 409 `order_refunded`, whose message never says "unavailable". If the stock
service can't be reached, retry in the background and refund 5 minutes after the charge.
Superseded wording: "the 120 s TTL leaves lots of headroom" was true only while the order service
stays up.

**Payment call times out** (status unknown): the hold is **not** released, because the card may
have been charged. The order goes to `payment_unknown` and the app gets 503
`payment_status_unknown` ("try again shortly"). A retry with the same `order_id`, or the background
reconciler (within 30 s), re-sends payment with the same `{order_id}:pay` key (the payment provider
is idempotent). Charged → confirm → `accepted`. Not charged → release → `payment_declined`. The
order is never refused for stock after this point.

## R9. Releasing a hold you may not have

- **Decision**: `POST /v1/hold-releases` with `{ "idempotency_key": "..." }`. If a hold exists for
  the key, it's released. If none exists, the stock service records a **tombstone** for the key, and
  any later `POST /v1/holds` with that key returns 409 `hold_released` without reserving. It's
  idempotent and always returns 200.
- **Rationale**: After a timeout the order service doesn't have a `hold_id`. Without a tombstone, a
  slow first request landing after the release would leave an orphan hold, which only `expires_at`
  would clean up.

## R10. Error envelope and refusal body

- **Decision**: `{ "error": { "code", "message", "details" }, "request_id" }`. `code`, `message`
  and `request_id` keep their existing names; `details` is additive (Priya's rule). For stock
  refusals:
  - `error.message` = the rule-5 sentence for the first failure. When there are several failures,
    the sentences are joined with a single space, so today's app (which shows only `message`) still
    shows every problem.
  - `error.details.line_item_number` and `error.details.missing_component` hold the **first**
    failure (CLAUDE.md rules 5–8 and Oren's three fields).
  - `error.details.failures[]` holds **every** failure (spec clarification "all of them") as
    `{ reason, line_item_number, size, drink, missing_component, message }`.
- **Wording ownership**: the stock service returns structured shortfalls only (component names, no
  sentences). All customer copy lives in one template module in the order service (`messages.py`)
  so Product can change it in one place. The exact strings in CLAUDE.md are pinned by contract tests.

## R11. Gateway behaviour

- **Decision**: `POST /v1/orders` and `GET /v1/orders/{order_id}` are proxied to the order service.
  The gateway generates or propagates `X-Request-ID` and puts it in `request_id`. Order-service
  responses (including every 4xx and 409) pass through **unchanged** apart from setting
  `request_id`. Connection error, connect timeout or the 2.8 s read timeout to the order service
  → **503** `order_service_unavailable`, "We can't take orders right now. Please try again shortly.",
  `Retry-After: 1`. The gateway **never** emits 502 or 504. An order-service 5xx it doesn't
  understand → 503 with the same code.
- **Rationale**: FR-015 and the user's instruction. The app retrying 503 is safe because the order
  is idempotent on `order_id`. If the order service had in fact accepted the order before the
  gateway timed out, the retry returns the original `accepted`.

## R12. Store ID format

- **Decision**: The public `store_id` is the string `SB-` + snapshot `store_id` (e.g. `SB-10482`,
  `SB-09815`), and is handled as a string everywhere. The stock service owns the mapping.
- **Rationale**: Sample orders use `SB-10482` while the snapshot uses `10482`. Constitution V
  requires leading zeros to survive.

## R13. Test data vs snapshot

- **Finding**: `Sample_orders.csv` doesn't line up with `StockSnapshot`. `SB-11937` and `SB-20516`
  (Bellevue Square) aren't in the snapshot. Almondmilk at 10482 has 37 available, but the sample
  expects it sold out. Whole/Nonfat/Soy/Coconut milk, Extra Shot and Cinnamon Dolce have no rows.
- **Decision**: Contract and acceptance tests seed the stock service's SQLite with **per-scenario
  fixtures**, worked out by hand from each sample row's `scenario_tag` and expected outcome
  (Constitution V). Each fixture lives in `services/stock/tests/fixtures/`, and its derivation is
  written next to it. The production seed still comes from `StockSnapshot`. A component with no row
  for a store has available = 0.
- **Mid-order scenarios** (`stock_runs_out_mid_order=true`): the test reads availability (1 unit),
  calls `POST /v1/stock-adjustments` (the POS/in-store feed endpoint, owned by the stock service) to
  drop it to 0, then submits the order → 409 `component_unavailable`. `MO-250307-0023` has no
  adjustment and is accepted, with available dropping to 0.

## R14. CI (`contract-tests`)

- **Decision**: `.github/workflows/contract-tests.yml`, triggered `on: [push, pull_request]`, with
  job id **and** name `contract-tests`, `python-version: "3.12"`, running `pytest`. Services run
  in-process through `httpx.ASGITransport`, and the cross-service URLs point at the in-process apps
  plus a fake payment app inside the job. There is no network access.
  - Pytest exit code 5 (no tests collected) already fails the job. `conftest.py` fails the session
    if any test is skipped or xfailed, and `--strict-markers -p no:cacheprovider` is set.
  - Every contract test validates the real response against the committed OpenAPI file with
    `openapi-core`, and asserts concrete status, code and message values.
- **Alternatives rejected**: `docker compose` in CI, which is slower and adds no contract coverage.
  Image builds get their own job, which is **not** the required check. Schemathesis fuzzing could be
  added later as a non-required job.

## Conflicts with spec.md (to carry back)

| # | spec.md says | Hard constraint | Resolution in this plan |
|---|---|---|---|
| C1 | FR-013 "MUST NOT hold stock for a timed period" | ServiceBoundary: every hold must be able to expire on its own | Release stays immediate on payment failure; `expires_at` (120 s) is a crash backstop only. **Amend FR-013** to read "released immediately; holds carry an expiry as a safety backstop". |
| C2 | Assumption "the stock snapshot is the source of stock data for the order service" | Only Store Inventory reads stock data | The order service never reads the snapshot; it calls the stock service. **Amend the assumption.** |
| C3 | FR-002 "the entry point MUST validate…" | Gateway holds no business logic | The *order service* is the validation entry point. The gateway only forwards. Unknown names are reported by the stock service's catalog (422) before anything is reserved. |
| C4 | FR-015 "refuse with a 'try again shortly' message" | The app retries 503 | 503 `order_service_unavailable`. This is intentionally retryable and safe because of `order_id` idempotency. |
| C5 | US4 codes are "proposed, not yet in CLAUDE.md" | — | Adopted as-is in the contracts (`invalid_order` 400, `unknown_store` 404). Add them to CLAUDE.md when approved. |

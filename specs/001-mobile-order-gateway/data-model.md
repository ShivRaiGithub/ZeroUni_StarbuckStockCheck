# Phase 1 Data Model

Each service owns its own entities. No table appears in more than one service. Cross-service
references are **opaque IDs only** (`hold_id`, `order_id`); no service stores another service's
counts or status.

---

## Stock service (SQLite, private to the stock container)

### Store
| Field | Type | Rules |
|---|---|---|
| `store_id` | TEXT PK | Snapshot ID as a string (`"09815"`), never cast to int |
| `public_store_id` | TEXT UNIQUE | `"SB-" + store_id` |
| `store_name` | TEXT | e.g. "Bellevue Square" |
| `store_open` | BOOLEAN | **Owned only here** |

### Component
| Field | Type | Rules |
|---|---|---|
| `component_id` | TEXT PK | e.g. `oat_milk` |
| `component_name` | TEXT UNIQUE | Display name used in refusals, e.g. "Oatmilk" |
| `component_type` | TEXT | `drink_base` \| `add_in` |

### StoreStock (one row per store × component)
| Field | Type | Rules |
|---|---|---|
| `store_id`, `component_id` | composite PK | FK → Store, Component |
| `quantity_on_hand` | INTEGER | Changed by confirm (−q) and by `POST /v1/stock-adjustments` (POS feed) |
| `reserved_quantity` | INTEGER ≥ 0 | +q on hold create, −q on confirm, release or expire |
| `last_updated_at` | TEXT (ISO 8601) | When the row was last written. **Not** proof the value is current. |

Derived: `available = quantity_on_hand - reserved_quantity`. A missing row means available = 0.
Invariant enforced in the hold transaction: no hold may be created that makes `available < 0`.

### Recipe (catalog: "what does a line item need")
| Field | Type | Rules |
|---|---|---|
| `drink_name` | TEXT | e.g. "Caffè Latte" |
| `milk_name` → component | TEXT | "None" means no milk component |
| `add_in_name` → component | TEXT | — |
| `units_per_drink` | INTEGER > 0 | v1 = 1 for milk and every add-in, for all sizes |

Known sizes: `Tall`, `Grande`, `Venti`. Unknown drink, size, milk or add-in → 422 `unknown_item`.

### Hold
| Field | Type | Rules |
|---|---|---|
| `hold_id` | TEXT PK | `hold_` + ULID |
| `idempotency_key` | TEXT UNIQUE | From the `Idempotency-Key` header |
| `request_hash` | TEXT | SHA-256 of the canonical request body |
| `store_id` | TEXT FK | |
| `status` | TEXT | `active` \| `confirmed` \| `released` \| `expired` |
| `expires_at` | TEXT | Required, 1 s – 600 s ahead of now |
| `created_at`, `closed_at` | TEXT | |

### HoldItem
| Field | Type | Rules |
|---|---|---|
| `hold_id`, `component_id` | composite PK | |
| `quantity` | INTEGER > 0 | Σ over all lines of `quantity × units_per_drink` |

### HoldTombstone
| Field | Type | Rules |
|---|---|---|
| `idempotency_key` | TEXT PK | Written by `POST /v1/hold-releases` when no hold exists yet |
| `created_at` | TEXT | Purged after 24 h |

### Hold state transitions

```
             POST /v1/holds (all components available, store open)
   (none) ───────────────────────────────────────────────────────▶ active
                                                                     │
         POST /holds/{id}/confirm   ┌────────────────────────────────┼───────────────────────┐
         on_hand -= q, reserved -= q│     POST /holds/{id}/release   │  now >= expires_at    │
                                    ▼     or /hold-releases          ▼  (lazy + sweeper)     ▼
                               confirmed         released ◀──── reserved -= q ────▶   expired
```
- Each terminal transition changes `reserved_quantity` **exactly once**, in the same transaction
  that changes `status` (`UPDATE … WHERE status='active'`).
- Confirm on `released`/`expired` → 409 `hold_not_active`. Release on `confirmed` → 409
  `hold_not_active`. Repeating the same terminal transition (confirm twice, release twice) → 200
  with the current state.

---

## Order service (SQLite, private to the order container)

### Order
| Field | Type | Rules |
|---|---|---|
| `order_id` | TEXT PK | Client-supplied, e.g. `MO-250303-0003`. This is the idempotency key. |
| `request_hash` | TEXT | Same `order_id` with a different hash → 409 `order_id_conflict` |
| `store_id` | TEXT | Public form `SB-…`, kept as given |
| `submitted_at` | TEXT | From the client |
| `status` | TEXT | See below |
| `hold_attempt` | INTEGER | Starts at 1, and +1 when resuming from `hold_unknown` |
| `hold_id` | TEXT NULL | Opaque reference returned by the stock service |
| `payment_ref` | TEXT NULL | Opaque reference from the payment provider |
| `final_status_code` | INTEGER NULL | Stored HTTP status of the final result |
| `final_body` | TEXT NULL | Stored JSON body, replayed on repeat |
| `confirm_pending` | BOOLEAN | True until the stock confirm succeeds (background retry) |

**Not stored**: any stock count, availability or store open flag.

### OrderLine
| Field | Type | Rules |
|---|---|---|
| `order_id`, `line_item_number` | composite PK | `line_item_number` = 1-based position in `drinks[]` |
| `drink`, `size`, `milk` | TEXT | Required. `milk` may be `"None"`. |
| `add_ins` | JSON array of TEXT | 0..n, order kept, duplicates rejected |
| `quantity` | INTEGER | 1–20 |

### Order state machine

| From | Event | To | Final? | HTTP |
|---|---|---|---|---|
| — | body fails shape validation | `rejected` | yes | 400 `invalid_order` |
| `received` | valid | `holding` | — | — |
| `holding` | hold 404 / 422 | `rejected` | yes | 404 `unknown_store` / 400 `invalid_order` |
| `holding` | hold 409 `store_closed` | `refused` | yes | 409 `store_closed` |
| `holding` | hold 409 `insufficient_stock` | `refused` | yes | 409 `component_unavailable` |
| `holding` | timeout / 5xx after retry | `hold_unknown` | **no** | 503 `stock_service_unavailable` |
| `hold_unknown` | client retry (same `order_id`) | `holding` (attempt+1) | — | — |
| `holding` | hold 201 | `paying` | — | — |
| `paying` | declined | `payment_declined` (hold released) | yes | 402 `payment_declined` |
| `paying` | timeout | `payment_unknown` (hold kept) | **no** | 503 `payment_status_unknown` |
| `payment_unknown` | retry or reconciler: charged / declined | `confirming` / `payment_declined` | — | — |
| `paying`/`confirming` | charged | `accepted` | yes | 201 |

Invariant: no transition into `paying` exists except from a successful hold. So **payment is never
requested for a refused order** (SC-002), and a refusal can never come after a charge.

---

## Gateway

No entities and no storage. Per request it holds only `request_id` (from `X-Request-ID`, or a
generated UUIDv4).

---

## Validation rules (owner in brackets)

| Rule | Owner | Result |
|---|---|---|
| `order_id`, `store_id` present, non-empty strings | order | 400 `invalid_order`, `details.field` |
| `drinks` has ≥ 1 item | order | 400 `invalid_order`, `field=drinks`, "At least one drink is required." |
| `quantity` integer 1–20 | order | 400 `invalid_order` |
| Store exists | stock | 404 `unknown_store`, "Store SB-99999 does not exist." |
| Drink, size, milk and add-in names known | stock | 422 → order maps to 400 `invalid_order`, `details.unrecognised[]` |
| Store open (checked before stock) | stock | 409 `store_closed` |
| Every component available ≥ need | stock | 409 `insufficient_stock` → order maps to 409 `component_unavailable` |

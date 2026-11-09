# Feature Specification: Mobile Order Gateway and Pre-Payment Availability Check

**Feature Branch**: `001-mobile-order-gateway`

**Created**: 2026-11-09

**Status**: Draft

**Input**: User description: "the gateway takes store ID, drinks and add-ins, and the order service checks stock and hours before payment"

## Clarifications

### Session 2026-11-09

- Q: When an accepted order's stock is reserved but payment fails or is abandoned, how long is the stock held? → A: Released immediately; there is no timed hold.
- Q: When a multi-drink order has several problems, does the refusal report only the first or all of them? → A: All of them, every failing line and component in one refusal.
- Q: If the same order is sent twice, is the second copy the same order or a new one? → A: Same order; each order carries a client-supplied unique ID and a repeat returns the original result without reserving again.
- Q: If the gateway cannot reach the order service, is the order refused or accepted for a later check? → A: Refused with a "try again shortly" message; no payment is taken and nothing is reserved.
- Q: What happens when an order names an unrecognised drink, size, milk or add-in? → A: The whole order is rejected as invalid, naming each unrecognised item.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Place an order that can be fulfilled (Priority: P1)

A customer submits a mobile order naming a store and one or more drinks, each with a size, a milk and optional add-ins. The order is checked against the store's opening status and the stock of every component it needs. When everything is available, the order is accepted and the customer is moved on to payment.

**Why this priority**: This is the core purchase path. Without it nothing else has value.

**Independent Test**: Submit an order for an open store whose components are all in stock and confirm it is accepted and handed on to payment.

**Acceptance Scenarios**:

1. **Given** Order `MO-250303-0003`: store `SB-10482` (open), one line, 1 × Venti Pike Place Roast, milk `None`, no add-ins, every component has available stock > 0, **When** the order is submitted, **Then** the outcome is `accepted`, payment is requested once, and no refusal body is returned.
2. **Given** Order `MO-250303-0004`: store `SB-11937` (open), 1 × Venti Iced Caramel Macchiato, 2% Milk, add-ins Vanilla Syrup and Caramel Drizzle, all with available stock > 0, **When** the order is submitted, **Then** the outcome is `accepted` as one order covering the drink and both add-ins.
3. **Given** Order `MO-250307-0023`: store `SB-10482`, 1 × Grande Caffè Latte, Oatmilk, and Oatmilk has exactly 1 unit available, **When** the order is submitted, **Then** the outcome is `accepted` and Oatmilk available drops to 0.
4. **Given** Any accepted order, **When** the order is submitted, **Then** payment is requested only after the hours and stock checks pass, and the payment request count is 0 if either check refuses.

---

### User Story 2 - Be told clearly when the store is closed (Priority: P2)

A customer orders from a store that is currently closed. The order is refused before any payment, with a message naming the store and saying it cannot take mobile orders right now.

**Why this priority**: Prevents customers paying for orders nobody can make.

**Independent Test**: Submit an order for a closed store and confirm it is refused with the store-closed message and no payment step.

**Acceptance Scenarios**:

1. **Given** Order `MO-250303-0001`: store `SB-20516` (Bellevue Square, closed), 1 × Grande Pike Place Roast, milk `None`, no add-ins, **When** the order is submitted, **Then** the response is HTTP 409, error code `store_closed`, message "Bellevue Square is closed right now and can't take mobile orders.", with no payment request and no stock reserved.
2. **Given** Store `SB-20516` is closed and the order also uses Oatmilk, which has available stock 0, **When** the order is submitted, **Then** the response is the same HTTP 409 `store_closed` body as scenario 1, and it contains no `component_unavailable` entry.

---

### User Story 3 - Be told which item is unavailable (Priority: P2)

A customer orders a drink whose milk or add-in is unavailable at the chosen store. The order is refused before payment, naming the line, the drink and the specific unavailable item.

**Why this priority**: Customers need to know what to change so they can reorder, for example with a different milk.

**Independent Test**: Submit an order using a milk with no available stock and confirm the refusal names that milk and the line.

**Acceptance Scenarios**:

1. **Given** Order `MO-250304-0007`: store `SB-10482`, 1 × Grande Caffè Latte, milk Almondmilk (available stock 0), add-in Vanilla Syrup (available > 0), **When** the order is submitted, **Then** the response is HTTP 409, error code `component_unavailable`, `missing_component` `Almondmilk`, message "Line 1, Grande Caffè Latte: Almondmilk is unavailable at this store right now."
2. **Given** Order `MO-250306-0019`: store `SB-10482`, 1 × Grande Iced Caramel Macchiato, 2% Milk, add-ins Vanilla Syrup (available > 0) and Caramel Drizzle (available stock 0), **When** the order is submitted, **Then** the response is HTTP 409, error code `component_unavailable`, `missing_component` `Caramel Drizzle`, message "Line 1, Grande Iced Caramel Macchiato: Caramel Drizzle is unavailable at this store right now." The order is not accepted with the add-in dropped, and Vanilla Syrup is not named.
3. **Given** Order `MO-250307-0028`: store `SB-11937`, 4 × Grande Iced Brown Sugar Oatmilk Shaken Espresso, Oatmilk, add-in Brown Sugar Syrup, where Brown Sugar Syrup available is above 0 but below 4, **When** the order is submitted, **Then** the response is HTTP 409, error code `component_unavailable`, `missing_component` `Brown Sugar Syrup`, message "Line 1, Grande Iced Brown Sugar Oatmilk Shaken Espresso: Brown Sugar Syrup is unavailable at this store right now." Stock is not reduced.
4. **Given** Order `MO-250307-0024`: store `SB-10482`, 1 × Grande Iced Brown Sugar Oatmilk Shaken Espresso, Oatmilk, add-in Brown Sugar Syrup, where Oatmilk has 1 unit available at the first check and 0 at commit, **When** the order is submitted, **Then** the response is HTTP 409, error code `component_unavailable`, `missing_component` `Oatmilk`, message "Line 1, Grande Iced Brown Sugar Oatmilk Shaken Espresso: Oatmilk is unavailable at this store right now."
5. **Given** (Constructed, not in the sample set) store `SB-10482`, line 1 = 1 × Grande Caffè Latte with Almondmilk (available 0), line 2 = 1 × Venti Chai Tea Latte with Oatmilk (available 0), **When** the order is submitted, **Then** the response is a single HTTP 409, error code `component_unavailable` refusal with two entries: line 1 `Almondmilk` with "Line 1, Grande Caffè Latte: Almondmilk is unavailable at this store right now." and line 2 `Oatmilk` with "Line 2, Venti Chai Tea Latte: Oatmilk is unavailable at this store right now."

---

### User Story 4 - Reject malformed orders at the entry point (Priority: P3)

A customer or client sends an order with a missing store, no drinks, an unknown store, or an unrecognised drink or add-in. The order is rejected at the entry point with a clear reason and never reaches the stock and hours check.

**Why this priority**: Protects the downstream check from bad input, but the sample orders do not exercise it.

**Independent Test**: Submit orders with a missing store ID and with an unknown store ID and confirm each is rejected with a specific reason.

**Acceptance Scenarios**:

1. *Proposed codes, not yet in `CLAUDE.md`.* **Given** an order with `store_id` missing, **When** the order is submitted, **Then** the response is HTTP 400, error code `invalid_order`, body field `field` = `store_id`, message "store_id is required.", and the order checks are not run.
2. *Proposed codes, not yet in `CLAUDE.md`.* **Given** store `SB-10482` with an empty drinks list, **When** the order is submitted, **Then** the response is HTTP 400, error code `invalid_order`, `field` = `drinks`, message "At least one drink is required."
3. *Proposed codes, not yet in `CLAUDE.md`.* **Given** store `SB-99999`, which does not exist, with one valid drink, **When** the order is submitted, **Then** the response is HTTP 404, error code `unknown_store`, message "Store SB-99999 does not exist."
4. *Proposed codes, not yet in `CLAUDE.md`.* **Given** store `SB-10482`, 1 × Grande Caffè Latte, 2% Milk, add-in "Vanila Syrup" (misspelled), **When** the order is submitted, **Then** the response is HTTP 400, error code `invalid_order`, `field` = `add_ins`, `unrecognised` = ["Vanila Syrup"], message "Unrecognised add-in: Vanila Syrup.", and the whole order is rejected rather than processed without it.

---

### Edge Cases

- Available stock is on hand minus reserved, so a component with 2 on hand and 1 reserved has 1 available.
- A multi-line order where one line cannot be made is refused as a whole, and the refusal lists every failing line and component.
- Several units of one drink need several units of each component, so the check covers the full quantity.
- Two customers order the last unit of a component at once, so only one may be accepted.
- Store IDs with leading zeros are preserved exactly as entered.
- The order service is down or too slow to answer, so the order is refused without payment (FR-015).
- The same order is sent twice, which returns the original result (FR-014).
- Component stock changes between the check and the commit, which must be caught (User Story 3, scenario 4).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept an order consisting of a store ID and one or more drink lines, each with a drink, size, milk, add-ins (zero or more) and quantity.
- **FR-002**: The entry point MUST validate order shape (store present, at least one drink, known drinks, sizes, milks and add-ins) and reject malformed orders before any stock or hours check. An order with any unrecognised drink, size, milk or add-in MUST be rejected whole, naming each unrecognised item, and MUST NOT be processed with the item dropped.
- **FR-003**: The system MUST check that the store exists and is open before checking stock.
- **FR-004**: The system MUST refuse an order for a closed store with the store-closed message and MUST NOT request payment.
- **FR-005**: The system MUST compute available stock as quantity on hand minus reserved quantity for every component an order needs.
- **FR-006**: The system MUST refuse the whole order, never partially accept it, when any milk or add-in on any line is unavailable or insufficient for the requested quantity.
- **FR-007**: A refusal for stock MUST name the line number, size, drink and the specific unavailable component, and when several lines or components fail it MUST list every one of them in the same refusal.
- **FR-008**: The system MUST re-check stock at commit time so an item that ran out after the first check refuses the order.
- **FR-009**: The system MUST complete the hours and stock checks before any payment is requested or taken.
- **FR-010**: Accepted orders MUST reserve the stock they need so that concurrent orders cannot both take the last unit.
- **FR-011**: Refusals MUST use fixed, documented status and error codes defined in the service contract.
- **FR-012**: The entry point and the order checking MUST be separate services, each owning its own data and talking only through published contracts.
- **FR-013**: Reserved stock MUST be released immediately when payment fails or is cancelled, and the system MUST NOT hold stock for a timed period.
- **FR-014**: Each order MUST carry a client-supplied unique ID, and a repeat submission with the same ID MUST return the original result and MUST NOT reserve stock or request payment again.
- **FR-015**: If the order service cannot be reached or does not answer, the gateway MUST refuse the order with a "try again shortly" message, MUST NOT request payment, and MUST NOT reserve stock.

### Key Entities

- **Order**: A customer's request to one store, with a client-supplied unique ID, a submission time and one or more order lines.
- **Order Line**: One drink with size, milk, add-ins and quantity, identified by its position in the order.
- **Store**: A location with an ID, a name and an open/closed status.
- **Component**: A milk, add-in or base ingredient stocked at a store, with quantity on hand and reserved quantity.
- **Refusal**: The result for an order that cannot be made, with a reason, the missing component where relevant, and a customer-facing message.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the 40 sample orders produce their expected outcome (19 accepted and 21 refused with the expected message).
- **SC-002**: No payment is requested for any refused order.
- **SC-003**: Customers receive an accept or refuse answer within 2 seconds for 95% of orders under normal load.
- **SC-004**: In a test with simultaneous orders for the last unit of a component, exactly one is accepted every time.
- **SC-005**: 100% of refusals name the specific store or component that caused them.

## Assumptions

- Payment itself is out of scope; this feature only guarantees the checks happen before it.
- Prices and totals are supplied or computed elsewhere and are not validated here.
- Store hours are represented as an open/closed status on the store at order time.
- The stock snapshot is the source of stock data for the order service.
- The exact status and error codes are defined in the contract during planning; the sample orders define only the messages and the missing component.
- Customers are already identified by the mobile app, so authentication is outside this feature.

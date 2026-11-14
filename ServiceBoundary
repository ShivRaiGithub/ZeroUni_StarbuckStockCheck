# Service Boundary Review: Asks from the Owning Teams

These are the notes from the 7 March 2025 service boundary review for the Stock-checked mobile order services. Each section records one owning team's hard constraints and open concerns. The owners will hold the store stock service, the order service and the gateway to these asks before they approve plan.md, the three OpenAPI contracts and the CI workflow.

## Store Inventory Systems, Tobias Wren, Keiko Hamasaki

Store Inventory owns the stock data for every store and component. The field names below match the columns in `starbucks_store_stock_snapshot_2025-03-07.csv`. Keiko noted that the feeds update each row's `last_updated_at`. That timestamp marks when a value was last written. It does not mean the value is still true.

**Hard constraints**
- Store Inventory is the only owner of `quantity_on_hand`, `reserved_quantity` and `store_open` for every store and component.
- No other service gets database credentials. No other service reads or writes Store Inventory tables, including read replicas and reporting copies.
- Other services may not keep their own copy of stock counts or store open status and treat it as the truth.
- A hold must not be able to stay forever. Every hold is either confirmed, released, or expires on its own.
- Callers retry stock calls after a timeout. A retried call must not hold the same quantity twice.
- Store Inventory wants one place to answer "can this store make this line item right now". Callers must not work this out themselves from raw quantities.

**Open concerns**
- POS (point-of-sale) sales and the in-store stock feed change `quantity_on_hand` directly. These changes do not go through any API the order service calls. A quantity read while the customer browses the menu can be wrong by checkout. Only a check made when the order is accepted counts.
- At the test stores, `reserved_quantity` has drifted upward. The cause was holds that were never confirmed or released. Store Inventory will not sign off on a design that repeats this.

## API Gateway, Priya Raghunathan

The gateway is shared by every app order call. Priya will review the gateway contract against the existing routes.

**Hard constraints**
- All public paths are versioned under `/v1`.
- The existing error envelope is `{ "error": { "code", "message" }, "request_id" }`. New error responses add fields to this shape. They do not replace it, and they do not rename `code`, `message` or `request_id`.
- The end-to-end budget for an order call through the gateway is 3 seconds. The app gives up at 8 seconds. All time spent in the gateway, the order service and the stock calls counts against the 3 seconds.
- The gateway routes requests and translates responses. It holds no stock or order business logic. It does not decide whether an order is accepted.

**Open concerns**
- The app retries automatically on 502, 503 and 504, and on its own 8-second timeout. The HTTP status returned for a refused order must not be one of these. A sold-out drink that gets retried is still sold out.
- Where the order service passes back a refusal, the gateway should pass the order service's reason through. It should not collapse the refusal into a generic error.

## Mobile App Checkout, Hannah Lindgren, Oren Shapiro

Oren owns the checkout code that reads the refusal response. Hannah owns what the customer is told.

**Hard constraints**
- Checkout today displays `error.message` as-is and reads nothing else. Any refusal must still carry a usable `error.message`, or current app versions will show a blank or broken error.
- To highlight the problem drink, checkout needs three machine-readable fields:
  - a reason code, for example out of stock, store closed or unknown item
  - the line item number the refusal applies to
  - the missing component, for example the milk or the add-in
- Product owns the customer-facing wording. The contract must leave room for the message without hard-coding marketing copy into the services.
- A customer must never see "unavailable" after their card has been charged.

**Open concerns**
- An order can contain several line items. Oren asked whether a refusal can name more than one line item, or only the first that fails.
- A stock check that finishes after the payment step would break the no-charge rule above. The order of stock check and payment needs to be stated explicitly.

## Platform Reliability & CI, Dmitri Volkov

Dmitri runs the GitHub Actions pipelines and the branch protection on the stock service repo.

**Hard constraints**
- The workflow runs on both `push` and `pull_request`.
- The required status check is named `contract-tests`. The name must match exactly, or branch protection treats the check as missing.
- The runner uses Python 3.12. Tests run with pytest.
- Tests may not reach any network service outside the job. That rules out live stock feeds, store systems and third-party APIs. Anything a test calls must be started inside the job.

**Branch protection on `main`, as recorded**

| Rule | Setting |
|---|---|
| Required status check | `contract-tests` |
| Merge while `contract-tests` is failing or missing | Blocked |
| Check must pass on | The pull request's latest commit |
| Draft pull requests | Allowed; the check still runs and reports |

**Open concerns**
- Contract tests are expected to fail until the services exist. The check must still run and report real test results. A broken workflow, a collection error or zero tests found is a failure of the pipeline, not an expected failure.
- Dmitri does not count skipped tests or tests that assert nothing toward coverage.
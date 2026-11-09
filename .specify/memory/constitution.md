<!--
Sync Impact Report
Version change: 1.0.0 → 1.1.0
Modified principles: none renamed; Service Constraints stack bullet tightened to a MUST
Added sections: Principles VI (Test-First), VII (One Container per Service),
  VIII (No Secrets in Code), IX (Python and FastAPI Everywhere)
Removed sections: none
Follow-up TODOs:
- TODO(CONTRACT_TESTS): the repo has pytest tests but no OpenAPI contract tests or committed
  OpenAPI file yet; Principles I and III cannot be enforced until they exist.
- Ratification date set to the date of first adoption (2026-11-09).
-->
# Stock Service Constitution

## Core Principles

### I. Fixed OpenAPI Contracts
Every HTTP endpoint MUST be described by a committed OpenAPI contract, and that contract is the
source of truth. Implementation conforms to the contract, not the reverse. Any change to a path,
parameter, response shape, or status code MUST be made in the contract first and MUST be treated
as a versioned change. Rationale: consumers depend on stable, machine-checkable interfaces.

### II. No Shared Database
Each service MUST own its data and MUST NOT read from or write to another service's database or
data store. Cross-service data access happens only through the other service's published API.
Rationale: shared storage creates hidden coupling that bypasses the contracts in Principle I.

### III. Contract Tests Gate Merge (NON-NEGOTIABLE)
No change may be merged until contract tests pass. Contract tests MUST verify that each endpoint's
actual behaviour matches its OpenAPI contract. A failing or skipped contract test blocks merge;
it MUST NOT be bypassed to meet a deadline.

### IV. Correct Inventory Semantics
Availability is defined as `available = quantity_on_hand - reserved_quantity`. An item is
`SOLD OUT` when `available <= 0` and `IN STOCK` otherwise. Reserved units are promised to orders
and MUST NOT be counted as available for sale. Raw `quantity_on_hand` MUST NOT be reported as
availability. Changes to this rule are a MAJOR amendment of this constitution.

### V. Verified by Hand-Checked Tests
Behaviour MUST be covered by tests whose expected values are derived independently from the
source data (for example, worked out by hand from the stock snapshot), not copied from the
implementation's output. Store IDs MUST be handled as strings to preserve leading zeros.

### VI. Test-First (NON-NEGOTIABLE)
Tests MUST be written before the implementation they cover. The cycle is: write the test, confirm
it fails, implement until it passes, then refactor. Code without a prior failing test MUST NOT be
merged. Rationale: tests written after the fact tend to mirror the implementation's mistakes.

### VII. One Container per Service
Each service MUST be built and deployed as exactly one container, with its own image and
Dockerfile. A container MUST NOT bundle multiple services, and a service MUST NOT be split across
containers. Rationale: this keeps service boundaries (Principle II) physical and independently
deployable.

### VIII. No Secrets in Code
Secrets (API keys, passwords, tokens, connection strings with credentials) MUST NOT appear in
source code, tests, fixtures, container images, or version control. They MUST be supplied at
runtime through environment variables or a secret manager. Any secret committed by mistake MUST be
treated as compromised and rotated.

### IX. Python and FastAPI Everywhere
All services MUST be implemented in Python using FastAPI. A different language or framework
requires a constitutional amendment, not a per-feature exception.

## Service Constraints

- Stack: Python with FastAPI for every service (Principle IX); tests run with pytest.
- Each service ships as a single container (Principle VII).
- The stock snapshot is the data source for this service; it MUST NOT be mutated by the service.
- Unknown stores MUST return 404 rather than an empty success response.
- Simplicity first: re-reading the snapshot per request is acceptable at current scale; added
  complexity (caching, databases) MUST be justified in the relevant plan.

## Development Workflow

- Work flows through Spec Kit: specify → plan → tasks → implement, and each plan MUST include a
  Constitution Check against the principles above.
- Contract changes land before, or together with, the implementation they describe.
- Merge requires: contract tests passing, other tests passing, and a reviewer confirming
  compliance with this constitution.

## Governance

This constitution supersedes other practices for this project. Amendments MUST be documented in
this file with an updated Sync Impact Report, approved by the project owner, and accompanied by a
migration plan when existing code is affected. Versioning follows semantic versioning: MAJOR for
removed or redefined principles, MINOR for added principles or materially expanded guidance,
PATCH for clarifications. Compliance is reviewed on every pull request and at each plan's
Constitution Check; unjustified violations block merge. Runtime guidance lives in `claude.md`
and `README.md`.

**Version**: 1.1.0 | **Ratified**: 2026-11-09 | **Last Amended**: 2026-11-09

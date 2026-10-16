# OPSiB backend assessment: receipt shortfalls, routed tasks, auditable resolution

This is a small FastAPI + PostgreSQL service. It does five things:

1. imports ERP, WMS, HRMS and WFM exports and validates them row by row, without double-counting repeats;
2. maps source identifiers and units onto canonical products and stores, recording the mapping version;
3. detects receipt shortfalls against purchase orders at a frozen evaluation time, with neutral, traceable findings;
4. routes an investigation task to an eligible employee, falls back to the store manager, or queues the task for manual assignment;
5. runs each task through a server-enforced lifecycle with assignment history, audit events, verification and safe retries.

Design notes and the answers to the three design questions are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Real request and response examples are in [docs/API_EXAMPLES.md](docs/API_EXAMPLES.md), and the OpenAPI contract is in [docs/openapi.json](docs/openapi.json). Test evidence is in [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).

## Results on the fixture pack (frozen time 2026-10-16 15:00 IST)

| PO | Outcome | Expected | Accepted | Variance | Task |
|---|---|---|---|---|---|
| PO-501 | SHORTFALL | 100 EA | 92 EA (60 + 32, two GRNs) | 8 EA | HR-102, receiving associate at STORE-014 |
| PO-502 | NO_SHORTFALL | 40 EA | 40 EA | 0 | none |
| PO-503 | NOT_DUE: window closes 17 Oct 18:00 | 50 EA | 20 EA so far | not raised | none |
| PO-504 | NOT_APPLICABLE_CANCELLED | none | none | not raised | none |
| PO-505 | SHORTFALL | 10 BOX = 120 EA | 96 EA | 24 EA | HR-401, store manager fallback at STORE-015 |
| PO-506 | SHORTFALL | 20 EA | 10 EA (a repeated source record counted once) | 10 EA | HR-102 at STORE-014 |

None of these numbers or assignees are hard-coded. They come from the rule and the routing policy applied to the imported files. HR-101 is ACTIVE but OFF_SHIFT, and HR-103 is INACTIVE although the shift export says ON_SHIFT, so neither is selected. Each task stores those reasons.

## Quick start

Requires Python 3.12 and PostgreSQL 16. Docker is optional.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Choose a database:

```bash
# Option A - no Docker: embedded PostgreSQL 16 (pgserver), data in ./.pgdata
python scripts/dev_db.py                 # starts it and writes OPSIB_DATABASE_URL into .env
                                         # (stop it later with: python scripts/dev_db.py --stop)

# Option B - Docker
docker compose up -d db                  # matches the default OPSIB_DATABASE_URL
```

Create the schema, load the fixtures and run detection:

```bash
alembic upgrade head
python -m app.cli demo fixtures/         # imports all six files, runs detection, prints exceptions and tasks
```

Run the API:

```bash
uvicorn app.api.main:create_app --factory --reload
# http://127.0.0.1:8000/docs
```

The same flow over HTTP:

```bash
for f in location_aliases product_aliases employees workforce_status purchase_order_lines goods_receipt_lines; do
  curl -s -F "file=@fixtures/$f.csv" http://127.0.0.1:8000/imports/$f; echo
done
curl -s -X POST http://127.0.0.1:8000/detection/runs
curl -s "http://127.0.0.1:8000/tasks?assignee_id=HR-102"
curl -s -X POST http://127.0.0.1:8000/tasks/<task_id>/acknowledge -H "X-Actor-Id: HR-102" -H "Idempotency-Key: ack-1"
```

## Tests

```bash
python -m pytest -v          # 79 tests, about 50 s
```

The tests start a throwaway embedded PostgreSQL and apply the Alembic migration. To use your own server instead, set `OPSIB_TEST_DATABASE_URL`; the test schema is dropped and recreated. Coverage maps onto the brief's minimum list as follows:

| Brief's minimum test | Tests |
|---|---|
| 1. UOM conversion, partial receipts, deadline boundaries, cancelled lines | `test_unit_rules.py` |
| 2. Fixture pack integration with the six scenario outcomes | `test_detection_integration.py::test_required_scenario_outcomes` and neighbours |
| 3. Idempotency on re-import | `test_reimporting_the_same_files_is_a_no_op`, `test_rerunning_import_and_detection_changes_no_business_result` |
| 4. Identity and assignment: location mapping, off-shift, inactive, fallback manager | `test_unit_mapping_routing.py`, `test_tasks_are_routed_to_eligible_people_with_reasons`, `test_unmapped_employee_location_is_not_used_for_routing` |
| 5. Workflow: invalid transitions, repeated completion, audit history | `test_api_workflow.py` |
| 6. Failure paths: unknown product, conflicting duplicate id, absent eligible assignee | `test_unknown_product_mapping_is_data_quality_not_an_exception`, `test_same_source_id_with_changed_content_is_flagged_not_overwritten`, `test_manual_queue_when_no_eligible_assignee`, and more |

Concurrency is also tested against the real database: four threads import the same file, and four threads run detection at once.

## Configuration

All settings are environment variables with the `OPSIB_` prefix. See `.env.example` and `app/core/config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `OPSIB_DATABASE_URL` | `postgresql+psycopg://opsib:opsib@localhost:5432/opsib` | SQLAlchemy URL |
| `OPSIB_TENANT_ID` | `demo-retailer` | Single tenant for this slice |
| `OPSIB_EVALUATION_TIME` | `2026-10-16T15:00:00+05:30` | Frozen as-of time for every business rule; `system` uses the live clock |
| `OPSIB_AVAILABILITY_MAX_AGE_MINUTES` | `60` | Oldest shift sample that routing will trust |
| `OPSIB_SHORTFALL_TOLERANCE_BASE_UNITS` | `0` | Shortfall tolerance in the base unit |
| `OPSIB_TASK_DUE_HOURS` | `24` | Task due time after detection |
| `OPSIB_AI_PROVIDER` | `disabled` | `claude` turns on the optional summary adapter, which also needs `OPSIB_ANTHROPIC_API_KEY` |

## API

| Method and path | Purpose |
|---|---|
| `POST /imports/{dataset}` | Upload one CSV. Returns counts and the problem rows; 422 if the file itself is unreadable |
| `GET /imports`, `GET /imports/{id}?outcome=` | Import history and row-level outcomes |
| `GET /data-quality` | Unresolved mappings, orphan receipts, open conflicts, rejected rows, lines that could not be evaluated |
| `POST /data-quality/conflicts/{id}/resolve` | Keep the existing fact or accept the incoming one |
| `GET /reconciliation/po-lines` | Read-only canonical view of every PO line at the as-of time |
| `POST /detection/runs`, `GET /detection/runs/{id}` | Run detection; see the outcome of every PO line |
| `GET /exceptions`, `GET /exceptions/{id}` | Findings with evidence, the underlying source records, tasks and events |
| `GET /exceptions/{id}/investigation-summary` | Optional AI summary; falls back to a deterministic one |
| `GET /tasks?assignee_id=&location_id=&status=&queue=`, `GET /tasks/{id}` | Task lists, and one task with its assignment history and events |
| `POST /tasks/{id}/acknowledge`, `/start`, `/block`, `/unblock`, `/complete`, `/verify`, `/reject`, `/reassign` | Validated transitions. `X-Actor-Id` is required; `Idempotency-Key` is optional |

Errors share one shape: `{"error": {"code", "message", "details", "request_id"}}`. Logs are JSON lines that carry ids and counts only, never names or secrets.

## Repository layout

```
app/
  api/          FastAPI app, routes, serialisers, dependencies
  core/         config, frozen clock, errors, JSON logging
  db/           SQLAlchemy models, sessions, advisory locks, typed fact loading
  ingestion/    typed record contracts, per-source adapters, import pipeline, conflict resolution
  mapping/      canonical identity and unit resolution, data-quality report
  detection/    receipt-shortfall rule (pure), explanation, detection run
  routing/      eligibility and routing policy (pure), HRMS/WFM directory
  workflows/    task state machine, task creation, task actions, idempotency keys
  ai/           optional summary provider interface and output validation
migrations/     Alembic (0001_initial)
fixtures/       supplied files, unchanged (SHA-256 below)
tests/          unit, integration and API tests (PostgreSQL)
scripts/        dev_db.py (embedded Postgres), generate_api_docs.py
docs/           architecture, API examples, OpenAPI export, test results
```

## Assumptions and trade-offs

- **Single tenant.** Every table carries `tenant_id` and every unique key includes it, but the tenant comes from configuration, not from authentication.
- **The actor is a header.** `X-Actor-Id` stands in for an authenticated user. Permissions are still enforced on the server from HRMS facts: only the assignee works a task, and only supervisors in scope reassign or verify it.
- **The deadline is exclusive.** A line is actionable only when `as_of > expected_receipt_by`, so a deadline equal to the as-of time is NOT_DUE.
- **What counts as received.** Only `POSTED` receipt lines with `received_at <= as_of` count. Receipts posted after the deadline but before the as-of time still count, because they were received. Unposted and reversed lines appear in the evidence as excluded.
- **Mismatched receipts block evaluation.** A receipt whose product or store resolves differently from its PO line makes that line NOT_EVALUABLE rather than being silently dropped.
- **Over-receipt** is recorded in the run but is not an exception under this rule.
- **Conflicts block, they don't overwrite.** A changed repeat of a source id keeps the existing fact and blocks that record from detection until a person resolves it. A new source id that reuses a business grain cannot be accepted through the API; it must be fixed at the source. Resolving a conflict currently needs only an actor id. A live pilot would restrict it to a data-steward role.
- **Availability must be fresh.** The latest sample at or before the as-of time must be at most 60 minutes old, and the as-of time must fall inside the shift window. HRMS employment status always wins over the shift export.
- **Routing picks deterministically.** Among eligible people the lowest employee id wins. Load balancing is out of scope.
- **Mapping is resolved at read time.** Alias files can be corrected or arrive late without rewriting facts, and each finding stores the mapping version it used.
- **Timestamps.** Every input timestamp must carry an offset; naive values are rejected. The API returns UTC. Business logic uses the frozen clock only; the wall clock is used only for audit `recorded_at` columns.

Intentionally omitted: a frontend, a background job queue (imports and detection run in the request), receipt reversals that reference earlier GRNs, supplier and ASN data, pagination, and soft-deletion or retention policies.

## What would change for a live retailer

None of the following is implemented. It is the hardening plan.

- **Authentication and tenancy.** Use OIDC or JWT, and derive the tenant and actor from the token, never from headers. Add row-level security or a tenant filter enforced in the data layer, and per-tenant integration credentials.
- **Secrets.** Database and API keys come from a secret manager such as AWS Secrets Manager or Vault, rotated, and never kept in `.env`.
- **Migrations.** Run Alembic in the deploy pipeline with backward-compatible migrations, and partition `import_row` and the event tables by time.
- **Ingestion at scale.** Use a job queue (for example Postgres `SKIP LOCKED` or a managed queue) with retries and a dead-letter queue for failed imports. Store raw files in object storage, keyed by SHA-256.
- **Observability.** Add metrics for import outcomes, detection outcomes, data-quality backlog and task SLA breaches. Ship structured logs with request ids, add tracing, and alert on FAILED imports and stale availability feeds.
- **Retention and privacy.** Set retention windows for raw rows and HR data, keep employee PII minimal (names stay only in the HRMS fact), and support data-subject requests.
- **Integrations.** Use per-source adapters with versioned column maps, and add source-specific checks such as expected row counts and control totals.

## AI tooling disclosure

- **Tools used.** Claude Code (Anthropic's Claude) wrote the implementation, tests and documentation in this repository, working from the assessment brief and the fixture pack.
- **How it was checked.** The full suite (79 tests) runs against a real PostgreSQL 16, and the quick start above was re-run from a fresh clone before submission. The fixture hashes below were re-checked at the same time.

## Fixture integrity

The files in `fixtures/` are the supplied files, unchanged. Tests add rows in memory and never edit them.

```
f573fe9ff329f06ea219599118081d049dfd505df4898e98057162f98a1929f8  employees.csv
2772112265c78db68bbdc5bd6099ad6900889610c8eb38a072322a97e2fd01c6  goods_receipt_lines.csv
5a6533471eb48b3e0382a937be9e247fc5b8010a1b5da95b935c5cca16464857  location_aliases.csv
f01841a9add91619448a9ed016593be4282dcb62f2719983f4bf48c8f593d1fa  product_aliases.csv
cf665feac7f6d5886b1537ea2efe184692b156b2a2f2347aeeded80890ddf3cc  purchase_order_lines.csv
bd11e3b90c639c69c84f94806a96f3f5f45592786ff96a5e75d1bafccdc8f401  workforce_status.csv
```

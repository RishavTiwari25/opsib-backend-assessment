# Architecture

## Flow

```
CSV export ──► adapter (column map, source system, identity) ──► typed record (pydantic)
                                                                        │
              import_batch / import_row  ◄── every row + outcome ───────┤
                                                                        ▼
                                     source_record (one per source identity, provenance, content hash)
                                     source_conflict (changed repeat / business-key collision, needs a person)
                                                                        │
              alias facts ──► MappingSnapshot (canonical product/location/base unit, versioned)
                                                                        │
              detection run (frozen as-of time, advisory lock) ──► line_evaluation for every PO line
                                                                        │ SHORTFALL
                                                                        ▼
                     exception_case (business key, evidence, neutral explanation) + exception_event
                                                                        │
              routing policy (HRMS + WFM + HRMS location alias) ──► task + task_assignment + task_event
                                                                        │
                                  server-enforced task workflow ──► verification ──► exception RESOLVED
```

Module boundaries:

| Module | Responsibility | Knows about CSV? | Knows about DB? |
|---|---|---|---|
| `app/ingestion/adapters.py` | Export format → typed record; source system; source identity and business grain | yes | no |
| `app/ingestion/records.py` | Retailer-agnostic record contracts and validation | no | no |
| `app/ingestion/service.py` | Import pipeline, idempotency, conflicts, row outcomes | via adapter | yes |
| `app/mapping/resolver.py` | Canonical identity and unit conversion, never guesses | no | no |
| `app/detection/rules.py` | The shortfall rule, pure and deterministic | no | no |
| `app/detection/service.py` | Runs the rule, persists evaluations, upserts exceptions, creates tasks | no | yes |
| `app/routing/policy.py` | Eligibility, fallback, manual queue, supervision scope; pure | no | no |
| `app/workflows/` | State machine, task actions, idempotency keys | no | yes |
| `app/ai/summarizer.py` | Optional AI summary behind a validated interface | no | no |
| `app/api/` | HTTP: parse, call a service in one transaction, serialise | no | yes |

Adding another retailer's export means writing new adapters. The rule, mapping, routing and workflow code do not change, because they only see the typed records.

## Data model

Three layers are kept apart. Business state is derived from source facts and never edits them.

**Raw input.** `import_batch` holds one row per uploaded file: file SHA-256, adapter, status and counts. `import_row` holds every CSV row as received, with its outcome, errors, source key and links to the fact and conflict.

**Source facts.**
- `source_record` holds one validated record per `(tenant, dataset, source_key)`. It is unique on that identity and also on `(tenant, dataset, business_key)`. It stores the typed payload, a content hash, a version, and the first and last import ids.
- `source_conflict` holds a repeat that changed content, or a new id that reuses an existing business key. It stays OPEN until a person keeps the old content or accepts the new one.

**Business state.**
- `detection_run` stores the as-of time, rule version, mapping version, routing policy version and counts.
- `line_evaluation` stores the outcome of every PO line in every run: SHORTFALL, NO_SHORTFALL, OVER_RECEIPT, NOT_DUE, NOT_APPLICABLE_CANCELLED or NOT_EVALUABLE, with reasons and the quantities.
- `exception_case` has the stable id `uuid5(tenant, business_key)`. The business key is `RECEIPT_SHORTFALL:<source system>:<PO>:<line>`. It stores quantities in the base unit, evidence references, a neutral explanation, an evidence hash and a revision. `exception_event` records DETECTED, EVIDENCE_CHANGED, EVALUATION_BLOCKED, CLEARED_BY_DATA, REOPENED_BY_DATA and RESOLVED.
- `task` is unique on its business key `<exception key>:INVESTIGATE:g<generation>`. A task has either an assignee or a queue, never both.
- `task_assignment` keeps the full assignment history. A partial unique index allows only one open assignment per task.
- `task_event` records every transition and reassignment, with the actor and payload.
- `idempotency_record` stores a client key with its request fingerprint and response.

### Identity: source identity vs business grain vs canonical entity

| Concept | Example | Where |
|---|---|---|
| Field mapping | the `item_code` column is the PO item reference | adapter |
| Source identity | `WMS-GRN506-A-L1` (what the WMS calls this record) | `source_record.source_key` |
| Business grain | `GRN-506-A`, line 1 (what the business calls this receipt line) | `source_record.business_key` |
| Canonical entity | `ERP-SKU-001` (ERP) and `WMS-BRC-001` (WMS) are both `SKU-001` | alias facts → `MappingSnapshot` |

A changed repeat of a source identity is a **conflict**, not an update. A new source identity that reuses an existing business grain is a **collision**, for example a second id for PO-501 line 1. Both are kept out of detection until a person decides.

### Which system is authoritative

| Field | Authority | Notes |
|---|---|---|
| PO line quantity, unit, status, deadline, supplier | ERP | CANCELLED is final for this rule |
| Accepted quantity, receipt time, posting status | WMS | only POSTED lines count |
| Product identity, unit conversion | product alias file (OPSiB mapping) | a unit not declared for that alias is not converted |
| Location identity and region | location alias file | HRMS codes must map before routing |
| Employment status, role, store, manager | HRMS | beats any shift export |
| On-shift / off-shift at a time | WFM sample | must be fresh and inside the shift window |

A missing or ambiguous mapping never turns into a guess. It shows up in `/data-quality`, and the PO line is NOT_EVALUABLE, which is a data-quality problem rather than a business exception.

Mapping is resolved at evaluation time from the current alias facts, and every finding stores the mapping version it used. An alias file that arrives late, or a corrected one, fixes later evaluations without rewriting the facts already loaded.

## Detection rule (RECEIPT_SHORTFALL 1.0.0)

For each PO line fact, the first matching check decides the outcome:

1. An open conflict on the PO line gives NOT_EVALUABLE.
2. A CANCELLED line gives NOT_APPLICABLE_CANCELLED.
3. A product, location or unit that cannot be resolved gives NOT_EVALUABLE.
4. If `as_of <= expected_receipt_by`, the result is NOT_DUE. A deadline exactly at the as-of time has not passed.
5. If any linked receipt is in conflict, unmapped, or resolves to a different product or location, the result is NOT_EVALUABLE.
6. Otherwise the rule compares `expected = ordered × factor` with the sum of POSTED receipts that have `received_at <= as_of`:
   - a variance above the tolerance (zero by default, configurable) gives SHORTFALL;
   - a negative variance gives OVER_RECEIPT, which is recorded but not raised by this rule;
   - anything else gives NO_SHORTFALL.

Grain: receipts are joined to PO lines by `(po_number, po_line_number)` and summed per fact. Facts are already unique per source identity, so a repeated source row is counted once. Receipts that are not counted are kept in the evidence with the reason.

Re-evaluation:
- If the evidence hash changes, the exception's revision goes up and an EVIDENCE_CHANGED event records the before and after values.
- If the line no longer has a shortfall, for example because a late GRN closed the gap, the case becomes CLEARED_BY_DATA and its open tasks are cancelled.
- If the line becomes NOT_EVALUABLE, the case is kept and an EVALUATION_BLOCKED event is written.

## Routing (routing-1.0.0)

| Case | Primary role | Approved fallback | If nobody qualifies |
|---|---|---|---|
| RECEIPT_SHORTFALL | `receiving_associate` at the same canonical store | `store_manager` at the same store | queue `MANUAL_ASSIGNMENT_REQUIRED`, unassigned |

An employee is eligible only if all of these hold:
- HRMS employment status is ACTIVE;
- the HRMS location maps to the task's canonical store;
- the latest WFM sample observed at or before the as-of time is at most 60 minutes old (configurable);
- that sample says ON_SHIFT;
- the as-of time falls inside the shift window.

Ties go to the lowest employee id. A regional employee with no store is never auto-assigned a store task. The routing decision is stored with every candidate considered and the reasons each was rejected, for example `HR-101: AVAILABILITY_OFF_SHIFT` and `HR-103: EMPLOYMENT_STATUS_INACTIVE`. Names are never copied into business state.

## Task lifecycle

```
CREATED ─assign/reassign─► ASSIGNED ─acknowledge─► ACKNOWLEDGED ─start─► IN_PROGRESS ─complete─► COMPLETED
                                                                          │   ▲                       │ (system)
                                                                     block▼   │unblock                ▼
                                                                         BLOCKED            PENDING_VERIFICATION
                                                                                              │verify      │reject
                                                                                              ▼            ▼
                                                                                          VERIFIED      REOPENED ─start─► IN_PROGRESS
any non-terminal ─(system, when the case is cleared by data)─► CANCELLED
```

- The assignee acknowledges, starts, blocks, unblocks and completes. Completion needs a resolution code from a fixed, neutral list and an evidence reference.
- A supervisor reassigns, verifies and rejects. A supervisor is an ACTIVE store manager of that store or regional operations for that region.
- The verifier may not be the person who did the work.
- An exception becomes RESOLVED only when every task that is not cancelled is VERIFIED.
- Reassignment requires a reason. The new assignee must pass the same eligibility checks. It closes the open assignment row, opens a new one, and lands in ASSIGNED.

## Design questions from the brief

### 1. How does a second import or a concurrent worker avoid producing a different result?

- **Uniqueness in the database.** `source_record` is unique on `(tenant, dataset, source_key)` and on `(tenant, dataset, business_key)`. Facts are written with `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. If nothing is returned, the row is compared by content hash: the same hash means DUPLICATE_IN_FILE or ALREADY_LOADED, a different hash opens a conflict. A fact is never inserted twice and never silently overwritten.
- **One transaction per file, under a lock.** Each file is processed in one transaction that first takes `pg_advisory_xact_lock(tenant, "import", dataset)`. Concurrent imports of the same dataset run one after another, and each sees the previous one's committed facts, so row reports are deterministic. A failure rolls the whole file back and the batch is marked FAILED in a separate transaction.
- **Detection is serialised the same way.** Each run is one transaction under `pg_advisory_xact_lock(tenant, "detection")`. Exceptions are unique on `(tenant, business_key)` with id `uuid5(tenant, business_key)`. Tasks are unique on their business key. Both are inserted with `ON CONFLICT DO NOTHING`, so even without the lock two workers cannot create two exceptions or two tasks for one case.
- **Tested.** `test_concurrent_imports_of_the_same_file_store_each_fact_once` and `test_concurrent_detection_runs_create_one_exception_and_one_task_per_case` run four threads against PostgreSQL. `test_rerunning_import_and_detection_changes_no_business_result` checks that a re-run creates no new rows or events.

### 2. The API commits task completion, the client times out and retries. What happens?

- **With an `Idempotency-Key` header.** The response is stored in `idempotency_record` in the same transaction as the state change. The retry takes the same task row lock, finds the record and returns the original response with `Idempotent-Replayed: true`. Nothing is applied twice. Reusing the key for a different request returns 409 `IDEMPOTENCY_KEY_REUSED`. If two requests with one key race on different tasks, the unique constraint on `(tenant, key)` rejects the second insert, and that request is answered from the first one's record.
- **Without a key.** Completion is still safe. The same person sending the same resolution code and evidence reference to an already-completed task gets 200 with `replayed: true` and no new event. A different completion gets 409 `ALREADY_COMPLETED` with the current status.
- **Tested.** `test_retry_after_commit_with_idempotency_key_returns_the_original_response` and `test_repeated_completion_without_key_is_safe` cover both paths.

### 3. Why is the result a receipt shortfall and not a supplier fault?

The data shows only what the ERP ordered and what the WMS accepted and posted. It contains no ASN or dispatch note, no carrier proof of delivery and no invoice. The gap could come from a short dispatch, a transit loss, damage rejected at the dock, goods received but not yet posted, or a counting error. Each finding therefore says what is known, what is unknown, and what evidence would establish the cause. The resolution codes are neutral, and the optional AI summary is rejected if it asserts fault.

## AI boundary

`app/ai/summarizer.py` defines a `SummaryProvider` interface.

- **What the model receives.** It gets only an evidence package: the computed quantities as strings, record reference ids, and the known and unknown statements. It gets no employee data.
- **How output is checked.** The reply must match a schema. Every cited record id must exist in the package. Text that asserts supplier or carrier fault, fraud or theft is rejected.
- **Failure handling.** If the provider is disabled, times out, errors or returns anything invalid, the endpoint returns a deterministic summary and says why.
- **What the model cannot do.** It cannot change quantities, assignment, status or verification. Those code paths never call it.
- **What is tested.** Fake providers cover the valid, invented-record, asserts-fault, bad-schema and timeout cases. A Claude adapter is included as a sketch. It was not run against the live API, and no key was used.

# API examples

Captured from a real run by `scripts/generate_api_docs.py` (fixture pack, frozen time 2026-10-16T15:00:00+05:30). Long lists are shortened with `... n more`. Timestamps are returned in UTC.
The full contract is in [openapi.json](openapi.json); with the server running it is also served at `/docs`.

### Import a dataset (duplicate row reported, not double-counted)

```http
POST /imports/goods_receipt_lines
Content-Type: multipart/form-data  (file=goods_receipt_lines.csv)
```

Response `201`

```json
{
  "import_id": "e2c3d612-5fe1-4c87-9b25-27a1a9837132",
  "dataset": "goods_receipt_lines",
  "adapter": "opsib-fixture-csv-v1",
  "source_system": "WMS",
  "filename": "goods_receipt_lines.csv",
  "file_sha256": "2772112265c78db68bbdc5bd6099ad6900889610c8eb38a072322a97e2fd01c6",
  "status": "COMPLETED",
  "counts": {
    "rows": 7,
    "accepted": 6,
    "already_loaded": 0,
    "duplicate_in_file": 1,
    "rejected": 0,
    "conflict": 0
  },
  "failure_reason": null,
  "started_at": "2026-10-16T17:15:30.838144Z",
  "finished_at": "2026-10-16T17:15:30.872152Z",
  "problem_rows": []
}
```

### Re-import the same file (idempotent)

```http
POST /imports/goods_receipt_lines
Content-Type: multipart/form-data  (file=goods_receipt_lines.csv)
```

Response `201`

```json
{
  "import_id": "967b2ad1-765c-48b5-8044-d54472c7c253",
  "dataset": "goods_receipt_lines",
  "adapter": "opsib-fixture-csv-v1",
  "source_system": "WMS",
  "filename": "goods_receipt_lines.csv",
  "file_sha256": "2772112265c78db68bbdc5bd6099ad6900889610c8eb38a072322a97e2fd01c6",
  "status": "COMPLETED",
  "counts": {
    "rows": 7,
    "accepted": 0,
    "already_loaded": 7,
    "duplicate_in_file": 0,
    "rejected": 0,
    "conflict": 0
  },
  "failure_reason": null,
  "started_at": "2026-10-16T17:15:30.891131Z",
  "finished_at": "2026-10-16T17:15:30.941422Z",
  "problem_rows": []
}
```

### Row-level validation errors (partial success is explicit)

```http
POST /imports/goods_receipt_lines
Content-Type: multipart/form-data  (file=grn_with_bad_row.csv)
```

Response `201`

```json
{
  "import_id": "ecabaacf-40fe-4494-8275-0606ffa2960e",
  "dataset": "goods_receipt_lines",
  "adapter": "opsib-fixture-csv-v1",
  "source_system": "WMS",
  "filename": "grn_with_bad_row.csv",
  "file_sha256": "bb468e094c841f507b97ed96d2416d9af1093478a1907bfd18fbac0224065ef4",
  "status": "COMPLETED_WITH_ERRORS",
  "counts": {
    "rows": 8,
    "accepted": 0,
    "already_loaded": 7,
    "duplicate_in_file": 0,
    "rejected": 1,
    "conflict": 0
  },
  "failure_reason": null,
  "started_at": "2026-10-16T17:15:30.968250Z",
  "finished_at": "2026-10-16T17:15:31.020316Z",
  "problem_rows": [
    {
      "row_number": 8,
      "outcome": "REJECTED",
      "source_key": null,
      "fact_id": null,
      "conflict_id": null,
      "errors": [
        {
          "field": "received_at",
          "value": "2026-10-15T10:00:00",
          "reason": "Value error, timestamp must include an explicit UTC offset, e.g. 2026-10-15T12:00:00+05:30"
        }
      ],
      "raw": {
        "uom": "EA",
        "po_number": "PO-501",
        "product_ref": "WMS-BRC-001",
        "received_at": "2026-10-15T10:00:00",
        "accepted_qty": "5",
        "receipt_line": "1",
        "location_code": "WH-MH-014",
        "po_line_number": "1",
        "posting_status": "POSTED",
        "receipt_number": "GRN-900",
        "source_record_id": "WMS-BAD-1"
      }
    }
  ]
}
```

### Import detail filtered by outcome

```http
GET /imports/e2c3d612-5fe1-4c87-9b25-27a1a9837132?outcome=DUPLICATE_IN_FILE
```

Response `200`

```json
{
  "import_id": "e2c3d612-5fe1-4c87-9b25-27a1a9837132",
  "dataset": "goods_receipt_lines",
  "adapter": "opsib-fixture-csv-v1",
  "source_system": "WMS",
  "filename": "goods_receipt_lines.csv",
  "file_sha256": "2772112265c78db68bbdc5bd6099ad6900889610c8eb38a072322a97e2fd01c6",
  "status": "COMPLETED",
  "counts": {
    "rows": 7,
    "accepted": 6,
    "already_loaded": 0,
    "duplicate_in_file": 1,
    "rejected": 0,
    "conflict": 0
  },
  "failure_reason": null,
  "started_at": "2026-10-16T17:15:30.838144Z",
  "finished_at": "2026-10-16T17:15:30.872152Z",
  "rows": [
    {
      "row_number": 7,
      "outcome": "DUPLICATE_IN_FILE",
      "source_key": "WMS-GRN506-A-L1",
      "fact_id": 37,
      "conflict_id": null,
      "errors": [],
      "raw": {
        "uom": "EA",
        "po_number": "PO-506",
        "product_ref": "WMS-BRC-003",
        "received_at": "2026-10-15T11:00:00+05:30",
        "accepted_qty": "10",
        "receipt_line": "1",
        "location_code": "WH-MH-014",
        "po_line_number": "1",
        "posting_status": "POSTED",
        "receipt_number": "GRN-506-A",
        "source_record_id": "WMS-GRN506-A-L1"
      }
    }
  ]
}
```

### Run detection at the frozen evaluation time

```http
POST /detection/runs
```

Response `201`

```json
{
  "run_id": "953feda2-667c-4e87-a880-3381fd8a5423",
  "rule": {
    "id": "RECEIPT_SHORTFALL",
    "version": "1.0.0"
  },
  "mapping_version": "map-d2d960fb9a49",
  "routing_policy_version": "routing-1.0.0",
  "as_of": "2026-10-16T09:30:00Z",
  "summary": {
    "outcomes": {
      "NOT_DUE": 1,
      "SHORTFALL": 3,
      "NO_SHORTFALL": 1,
      "NOT_APPLICABLE_CANCELLED": 1
    },
    "tasks_created": 3,
    "exceptions_cleared": 0,
    "exceptions_created": 3,
    "exceptions_updated": 0,
    "po_lines_evaluated": 6
  },
  "evaluations": [
    {
      "po_number": "PO-501",
      "po_line_number": 1,
      "outcome": "SHORTFALL",
      "reasons": [],
      "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418",
      "expected_qty": "100",
      "received_qty": "92",
      "variance_qty": "8",
      "unit": "EA"
    },
    {
      "po_number": "PO-502",
      "po_line_number": 1,
      "outcome": "NO_SHORTFALL",
      "reasons": [],
      "exception_id": null,
      "expected_qty": "40",
      "received_qty": "40",
      "variance_qty": "0",
      "unit": "EA"
    },
    {
      "po_number": "PO-503",
      "po_line_number": 1,
      "outcome": "NOT_DUE",
      "reasons": [
        "receiving window still open at the evaluation time"
      ],
      "exception_id": null,
      "expected_qty": "50",
      "received_qty": "20",
      "variance_qty": "30",
      "unit": "EA"
    },
    "... 3 more"
  ]
}
```

### List exceptions

```http
GET /exceptions
```

Response `200`

```json
[
  {
    "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418",
    "business_key": "RECEIPT_SHORTFALL:ERP:PO-501:1",
    "type": "RECEIPT_SHORTFALL",
    "status": "OPEN",
    "po_number": "PO-501",
    "po_line_number": 1,
    "supplier_code": "SUP-07",
    "canonical_product_id": "SKU-001",
    "canonical_location_id": "STORE-014",
    "region_id": "REGION-MH",
    "unit": "EA",
    "expected_qty": "100",
    "received_qty": "92",
    "variance_qty": "8",
    "expected_receipt_by": "2026-10-15T06:30:00Z",
    "detected_as_of": "2026-10-16T09:30:00Z",
    "rule": {
      "id": "RECEIPT_SHORTFALL",
      "version": "1.0.0"
    },
    "mapping_version": "map-d2d960fb9a49",
    "revision": 1
  },
  {
    "exception_id": "d4358539-2d50-56e8-8092-7665724b3384",
    "business_key": "RECEIPT_SHORTFALL:ERP:PO-505:1",
    "type": "RECEIPT_SHORTFALL",
    "status": "OPEN",
    "po_number": "PO-505",
    "po_line_number": 1,
    "supplier_code": "SUP-21",
    "canonical_product_id": "SKU-002",
    "canonical_location_id": "STORE-015",
    "region_id": "REGION-MH",
    "unit": "EA",
    "expected_qty": "120",
    "received_qty": "96",
    "variance_qty": "24",
    "expected_receipt_by": "2026-10-15T09:30:00Z",
    "detected_as_of": "2026-10-16T09:30:00Z",
    "rule": {
      "id": "RECEIPT_SHORTFALL",
      "version": "1.0.0"
    },
    "mapping_version": "map-d2d960fb9a49",
    "revision": 1
  },
  {
    "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
    "business_key": "RECEIPT_SHORTFALL:ERP:PO-506:1",
    "type": "RECEIPT_SHORTFALL",
    "status": "OPEN",
    "po_number": "PO-506",
    "po_line_number": 1,
    "supplier_code": "SUP-22",
    "canonical_product_id": "SKU-003",
    "canonical_location_id": "STORE-014",
    "region_id": "REGION-MH",
    "unit": "EA",
    "expected_qty": "20",
    "received_qty": "10",
    "variance_qty": "10",
    "expected_receipt_by": "2026-10-15T09:30:00Z",
    "detected_as_of": "2026-10-16T09:30:00Z",
    "rule": {
      "id": "RECEIPT_SHORTFALL",
      "version": "1.0.0"
    },
    "mapping_version": "map-d2d960fb9a49",
    "revision": 1
  }
]
```

### Exception with evidence, source records, tasks and events

```http
GET /exceptions/d4358539-2d50-56e8-8092-7665724b3384
```

Response `200`

```json
{
  "exception_id": "d4358539-2d50-56e8-8092-7665724b3384",
  "business_key": "RECEIPT_SHORTFALL:ERP:PO-505:1",
  "type": "RECEIPT_SHORTFALL",
  "status": "OPEN",
  "po_number": "PO-505",
  "po_line_number": 1,
  "supplier_code": "SUP-21",
  "canonical_product_id": "SKU-002",
  "canonical_location_id": "STORE-015",
  "region_id": "REGION-MH",
  "unit": "EA",
  "expected_qty": "120",
  "received_qty": "96",
  "variance_qty": "24",
  "expected_receipt_by": "2026-10-15T09:30:00Z",
  "detected_as_of": "2026-10-16T09:30:00Z",
  "rule": {
    "id": "RECEIPT_SHORTFALL",
    "version": "1.0.0"
  },
  "mapping_version": "map-d2d960fb9a49",
  "revision": 1,
  "explanation": {
    "known": [
      "ERP PO PO-505 line 1 ordered 10 BOX of SKU-002 for STORE-015 = 120 EA in the base unit.",
      "The expected-receipt deadline was 2026-10-15T15:00:00+05:30 and had passed at the evaluation time.",
      "96 EA were accepted on 1 posted WMS receipt line(s): WMS-GRN505-A-L1.",
      "Accepted quantity is 24 EA below the ordered quantity."
    ],
    "unknown": [
      "Whether the supplier dispatched the full quantity: no ASN / dispatch note was supplied.",
      "Whether goods were lost or damaged in transit, or where the discrepancy occurred.",
      "Whether goods arrived but were not yet received or posted in the WMS.",
      "Whether the supplier has invoiced the ordered or the received quantity."
    ],
    "statement": "Receipt shortfall against the purchase order. This records a quantity difference between what was ordered and what was accepted and posted. It does not establish supplier fault, carrier loss or a financial loss.",
    "evidence_needed": [
      "Supplier ASN / dispatch note with dispatched quantity for this PO line.",
      "Carrier proof of delivery showing delivered quantity and any exceptions noted at the dock.",
      "Receiving-dock record or physical count at STORE-015.",
      "... 2 more"
    ]
  },
  "evidence": {
    "rule": {
      "id": "RECEIPT_SHORTFALL",
      "version": "1.0.0"
    },
    "mapping": {
      "version": "map-d2d960fb9a49",
      "product_alias_fact_id": 9,
      "location_alias_fact_id": 4
    },
    "po_line": {
      "dataset": "purchase_order_lines",
      "fact_id": 29,
      "fact_version": 1,
      "source_system": "ERP",
      "first_import_id": "c5b00572-4461-4fee-b721-8877b6104c47",
      "source_record_id": "ERP-PO505-L1",
      "last_seen_import_id": "c5b00572-4461-4fee-b721-8877b6104c47"
    },
    "receipts_counted": [
      {
        "counted": true,
        "dataset": "goods_receipt_lines",
        "fact_id": 36,
        "qty_base": "96",
        "fact_version": 1,
        "source_system": "WMS",
        "excluded_reason": null,
        "first_import_id": "e2c3d612-5fe1-4c87-9b25-27a1a9837132",
        "source_record_id": "WMS-GRN505-A-L1",
        "last_seen_import_id": "ecabaacf-40fe-4494-8275-0606ffa2960e"
      }
    ],
    "receipts_excluded": []
  },
  "source_records": [
    {
      "fact_id": 29,
      "dataset": "purchase_order_lines",
      "source_system": "ERP",
      "source_record_id": "ERP-PO505-L1",
      "version": 1,
      "first_import_id": "c5b00572-4461-4fee-b721-8877b6104c47",
      "payload": {
        "status": "OPEN",
        "item_code": "ERP-BOX-12",
        "po_number": "PO-505",
        "line_number": 1,
        "ordered_qty": "10",
        "ordered_uom": "BOX",
        "location_code": "MUM-015",
        "supplier_code": "SUP-21",
        "source_record_id": "ERP-PO505-L1",
        "expected_receipt_by": "2026-10-15T15:00:00+05:30"
      }
    },
    {
      "fact_id": 36,
      "dataset": "goods_receipt_lines",
      "source_system": "WMS",
      "source_record_id": "WMS-GRN505-A-L1",
      "version": 1,
      "first_import_id": "e2c3d612-5fe1-4c87-9b25-27a1a9837132",
      "payload": {
        "uom": "EA",
        "po_number": "PO-505",
        "product_ref": "WMS-MAT-2002",
        "received_at": "2026-10-15T10:00:00+05:30",
        "accepted_qty": "96",
        "receipt_line": 1,
        "location_code": "WH-MH-015",
        "po_line_number": 1,
        "posting_status": "POSTED",
        "receipt_number": "GRN-505-A",
        "source_record_id": "WMS-GRN505-A-L1"
      }
    }
  ],
  "tasks": [
    {
      "task_id": "e46006d9-a6b0-5ba2-949d-16ddf7585574",
      "exception_id": "d4358539-2d50-56e8-8092-7665724b3384",
      "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
      "status": "ASSIGNED",
      "allowed_actions": [
        "acknowledge",
        "reassign"
      ],
      "canonical_location_id": "STORE-015",
      "assignee_employee_id": "HR-401",
      "assignment_queue": null,
      "due_at": "2026-10-17T09:30:00Z",
      "resolution_code": null,
      "evidence_ref": null,
      "completed_by": null,
      "verified_by": null,
      "version": 1
    }
  ],
  "events": [
    {
      "event_type": "DETECTED",
      "actor": "system:detector",
      "payload": {
        "base_uom": "EA",
        "rule_version": "1.0.0",
        "variance_qty": "24"
      },
      "as_of": "2026-10-16T09:30:00Z",
      "recorded_at": "2026-10-16T17:15:31.057916Z"
    }
  ]
}
```

### Data quality

```http
GET /data-quality
```

Response `200`

```json
{
  "mapping_version": "map-d2d960fb9a49",
  "unresolved_mappings": [],
  "orphan_receipts": [],
  "open_conflicts": [],
  "rejected_rows_latest_imports": [
    {
      "dataset": "goods_receipt_lines",
      "import_id": "ecabaacf-40fe-4494-8275-0606ffa2960e",
      "row_number": 8,
      "errors": [
        {
          "field": "received_at",
          "value": "2026-10-15T10:00:00",
          "reason": "Value error, timestamp must include an explicit UTC offset, e.g. 2026-10-15T12:00:00+05:30"
        }
      ],
      "raw": {
        "uom": "EA",
        "po_number": "PO-501",
        "product_ref": "WMS-BRC-001",
        "received_at": "2026-10-15T10:00:00",
        "accepted_qty": "5",
        "receipt_line": "1",
        "location_code": "WH-MH-014",
        "po_line_number": "1",
        "posting_status": "POSTED",
        "receipt_number": "GRN-900",
        "source_record_id": "WMS-BAD-1"
      }
    }
  ],
  "failed_latest_imports": [],
  "not_evaluable_po_lines_last_run": [],
  "last_detection_run_id": "953feda2-667c-4e87-a880-3381fd8a5423"
}
```

### Tasks for an assignee

```http
GET /tasks?assignee_id=HR-102
```

Response `200`

```json
[
  {
    "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
    "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
    "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
    "status": "ASSIGNED",
    "allowed_actions": [
      "acknowledge",
      "reassign"
    ],
    "canonical_location_id": "STORE-014",
    "assignee_employee_id": "HR-102",
    "assignment_queue": null,
    "due_at": "2026-10-17T09:30:00Z",
    "resolution_code": null,
    "evidence_ref": null,
    "completed_by": null,
    "verified_by": null,
    "version": 1
  },
  {
    "task_id": "dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd",
    "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418",
    "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
    "status": "ASSIGNED",
    "allowed_actions": [
      "acknowledge",
      "reassign"
    ],
    "canonical_location_id": "STORE-014",
    "assignee_employee_id": "HR-102",
    "assignment_queue": null,
    "due_at": "2026-10-17T09:30:00Z",
    "resolution_code": null,
    "evidence_ref": null,
    "completed_by": null,
    "verified_by": null,
    "version": 1
  }
]
```

### Invalid transition is refused

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/complete
X-Actor-Id: HR-102

{
  "resolution_code": "COUNT_ERROR_CORRECTED",
  "evidence_ref": "cycle-count-55"
}
```

Response `409`

```json
{
  "error": {
    "code": "INVALID_TRANSITION",
    "message": "cannot complete a task in status ASSIGNED",
    "details": {
      "status": "ASSIGNED",
      "allowed_actions": [
        "acknowledge",
        "reassign"
      ]
    },
    "request_id": "27a8ee0e-b98c-4a8d-b7a5-9fd834934a2c"
  }
}
```

### Acknowledge

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/acknowledge
X-Actor-Id: HR-102
Idempotency-Key: ack-1
```

Response `200`

```json
{
  "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
  "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "ACKNOWLEDGED",
  "allowed_actions": [
    "reassign",
    "start"
  ],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-102",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00+00:00",
  "resolution_code": null,
  "evidence_ref": null,
  "completed_by": null,
  "verified_by": null,
  "version": 2,
  "replayed": false
}
```

### Start

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/start
X-Actor-Id: HR-102
```

Response `200`

```json
{
  "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
  "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "IN_PROGRESS",
  "allowed_actions": [
    "block",
    "complete",
    "reassign"
  ],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-102",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00+00:00",
  "resolution_code": null,
  "evidence_ref": null,
  "completed_by": null,
  "verified_by": null,
  "version": 3,
  "replayed": false
}
```

### Complete (moves to PENDING_VERIFICATION)

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/complete
X-Actor-Id: HR-102
Idempotency-Key: done-1

{
  "resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER",
  "evidence_ref": "ASN-7781",
  "note": "supplier confirmed short dispatch"
}
```

Response `200`

```json
{
  "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
  "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "PENDING_VERIFICATION",
  "allowed_actions": [
    "reject",
    "verify"
  ],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-102",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00+00:00",
  "resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER",
  "evidence_ref": "ASN-7781",
  "completed_by": "HR-102",
  "verified_by": null,
  "version": 5,
  "replayed": false
}
```

### Client retry after a timeout: same key returns the stored response

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/complete
X-Actor-Id: HR-102
Idempotency-Key: done-1

{
  "resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER",
  "evidence_ref": "ASN-7781",
  "note": "supplier confirmed short dispatch"
}
```

Response `200` (header Idempotent-Replayed: true)

```json
{
  "due_at": "2026-10-17T09:30:00+00:00",
  "status": "PENDING_VERIFICATION",
  "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
  "version": 5,
  "replayed": false,
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "verified_by": null,
  "completed_by": "HR-102",
  "evidence_ref": "ASN-7781",
  "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
  "allowed_actions": [
    "reject",
    "verify"
  ],
  "resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER",
  "assignment_queue": null,
  "assignee_employee_id": "HR-102",
  "canonical_location_id": "STORE-014"
}
```

### Verification by the person who did the work is refused

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/verify
X-Actor-Id: HR-102

{}
```

Response `403`

```json
{
  "error": {
    "code": "ACTOR_ROLE_receiving_associate_NOT_SUPERVISOR",
    "message": "actor may not supervise this task",
    "details": {},
    "request_id": "096ea935-5488-4289-b664-b4b9a9329b3f"
  }
}
```

### Verify by the store manager

```http
POST /tasks/9162f381-73b6-56b7-908c-e49dc46e2f50/verify
X-Actor-Id: HR-201

{
  "note": "ASN checked"
}
```

Response `200`

```json
{
  "task_id": "9162f381-73b6-56b7-908c-e49dc46e2f50",
  "exception_id": "dc22d801-8aa0-54a2-a97c-318849adbe8e",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "VERIFIED",
  "allowed_actions": [],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-102",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00+00:00",
  "resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER",
  "evidence_ref": "ASN-7781",
  "completed_by": "HR-102",
  "verified_by": "HR-201",
  "version": 6,
  "replayed": false
}
```

### Reassign to an ineligible employee is refused with reasons

```http
POST /tasks/dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd/reassign
X-Actor-Id: HR-201

{
  "assignee_employee_id": "HR-103",
  "reason": "cover"
}
```

Response `422`

```json
{
  "error": {
    "code": "ASSIGNEE_NOT_ELIGIBLE",
    "message": "employee HR-103 is not eligible for this task",
    "details": {
      "reasons": [
        "EMPLOYMENT_STATUS_INACTIVE"
      ]
    },
    "request_id": "f6c47e8b-0167-413c-97a3-671fc88291d8"
  }
}
```

### Reassign with a reason

```http
POST /tasks/dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd/reassign
X-Actor-Id: HR-201

{
  "assignee_employee_id": "HR-201",
  "reason": "HR-102 on dock audit"
}
```

Response `200`

```json
{
  "task_id": "dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd",
  "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "ASSIGNED",
  "allowed_actions": [
    "acknowledge",
    "reassign"
  ],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-201",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00+00:00",
  "resolution_code": null,
  "evidence_ref": null,
  "completed_by": null,
  "verified_by": null,
  "version": 2,
  "replayed": false
}
```

### Task history (assignments and events)

```http
GET /tasks/dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd
```

Response `200`

```json
{
  "task_id": "dbfe13a2-e00b-5dcd-b3b0-bd1e676630bd",
  "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418",
  "task_type": "INVESTIGATE_RECEIPT_SHORTFALL",
  "status": "ASSIGNED",
  "allowed_actions": [
    "acknowledge",
    "reassign"
  ],
  "canonical_location_id": "STORE-014",
  "assignee_employee_id": "HR-201",
  "assignment_queue": null,
  "due_at": "2026-10-17T09:30:00Z",
  "resolution_code": null,
  "evidence_ref": null,
  "completed_by": null,
  "verified_by": null,
  "version": 2,
  "assignments": [
    {
      "employee_id": "HR-102",
      "queue": null,
      "stage": "PRIMARY",
      "reason": "primary role receiving_associate at STORE-014: active, on shift, availability fresh; lowest employee id among 1 eligible",
      "policy_version": "routing-1.0.0",
      "decision": {
        "as_of": "2026-10-16T15:00:00+05:30",
        "queue": null,
        "stage": "PRIMARY",
        "reason": "primary role receiving_associate at STORE-014: active, on shift, availability fresh; lowest employee id among 1 eligible",
        "candidates": [
          {
            "role": "receiving_associate",
            "stage": "PRIMARY",
            "reasons": [
              "AVAILABILITY_OFF_SHIFT"
            ],
            "eligible": false,
            "employee_id": "HR-101"
          },
          {
            "role": "receiving_associate",
            "stage": "PRIMARY",
            "reasons": [],
            "eligible": true,
            "employee_id": "HR-102"
          },
          {
            "role": "receiving_associate",
            "stage": "PRIMARY",
            "reasons": [
              "EMPLOYMENT_STATUS_INACTIVE"
            ],
            "eligible": false,
            "employee_id": "HR-103"
          }
        ],
        "location_id": "STORE-014",
        "policy_version": "routing-1.0.0",
        "selected_employee_id": "HR-102"
      },
      "assigned_by": "system:detector",
      "as_of": "2026-10-16T09:30:00Z",
      "ended_as_of": "2026-10-16T09:30:00Z"
    },
    {
      "employee_id": "HR-201",
      "queue": null,
      "stage": "REASSIGNED",
      "reason": "HR-102 on dock audit",
      "policy_version": "routing-1.0.0",
      "decision": {
        "previous": "HR-102",
        "requested_by": "HR-201",
        "checked_reasons": []
      },
      "assigned_by": "HR-201",
      "as_of": "2026-10-16T09:30:00Z",
      "ended_as_of": null
    }
  ],
  "events": [
    {
      "event_type": "CREATED",
      "from_status": null,
      "to_status": "CREATED",
      "actor": "system:detector",
      "payload": {
        "exception_id": "921119a1-3d3a-5984-a2df-bd93e3180418"
      },
      "as_of": "2026-10-16T09:30:00Z",
      "recorded_at": "2026-10-16T17:15:31.057916Z"
    },
    {
      "event_type": "ASSIGNED",
      "from_status": "CREATED",
      "to_status": "ASSIGNED",
      "actor": "system:detector",
      "payload": {
        "stage": "PRIMARY",
        "reason": "primary role receiving_associate at STORE-014: active, on shift, availability fresh; lowest employee id among 1 eligible",
        "employee_id": "HR-102"
      },
      "as_of": "2026-10-16T09:30:00Z",
      "recorded_at": "2026-10-16T17:15:31.057916Z"
    },
    {
      "event_type": "REASSIGN",
      "from_status": "ASSIGNED",
      "to_status": "ASSIGNED",
      "actor": "HR-201",
      "payload": {
        "to": "HR-201",
        "from": "HR-102",
        "reason": "HR-102 on dock audit"
      },
      "as_of": "2026-10-16T09:30:00Z",
      "recorded_at": "2026-10-16T17:15:31.719573Z"
    }
  ]
}
```

### Investigation summary (AI disabled: deterministic fallback)

```http
GET /exceptions/d4358539-2d50-56e8-8092-7665724b3384/investigation-summary
```

Response `200`

```json
{
  "exception_id": "d4358539-2d50-56e8-8092-7665724b3384",
  "source": "deterministic",
  "fallback_reason": "AI provider disabled",
  "authoritative_numbers": {
    "expected_qty": "120",
    "received_qty": "96",
    "variance_qty": "24",
    "base_uom": "EA",
    "product": "SKU-002",
    "location": "STORE-015"
  },
  "summary": "24 EA of SKU-002 at STORE-015 were not accepted against the PO line (96 of 120 EA). The cause is not established by the available data.",
  "missing_context": [
    "Whether the supplier dispatched the full quantity: no ASN / dispatch note was supplied.",
    "Whether goods were lost or damaged in transit, or where the discrepancy occurred.",
    "Whether goods arrived but were not yet received or posted in the WMS.",
    "Whether the supplier has invoiced the ordered or the received quantity."
  ],
  "suggested_steps": [
    "Request the supplier ASN / dispatch note for this PO line.",
    "Check the carrier proof of delivery and dock notes.",
    "Check the WMS for pending or unposted GRNs before contacting the supplier."
  ],
  "cited_record_ids": [
    "ERP-PO505-L1",
    "WMS-GRN505-A-L1"
  ]
}
```

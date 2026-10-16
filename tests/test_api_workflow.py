"""API-level tests: imports, data quality, exceptions, task lifecycle, retries, reassignment and audit."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import LOAD_ORDER, fixture_bytes

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load_and_detect(client):
    for ds in LOAD_ORDER:
        r = client.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", fixture_bytes(ds), "text/csv")})
        assert r.status_code == 201, r.text
    r = client.post("/detection/runs")
    assert r.status_code == 201, r.text
    return r.json()


def _task_for(client, po_number: str) -> dict:
    exc = next(e for e in client.get("/exceptions").json() if e["po_number"] == po_number)
    return client.get(f"/exceptions/{exc['exception_id']}").json()["tasks"][0]


def act(client, task_id, action, actor, body=None, key=None):
    headers = {"X-Actor-Id": actor}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/tasks/{task_id}/{action}", json=body, headers=headers)


COMPLETE = {"resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER", "evidence_ref": "ASN-7781 / supplier email 2026-10-16", "note": "supplier confirmed 8 EA short"}


@pytest.fixture
def loaded(client):
    return _load_and_detect(client)


def test_import_endpoint_reports_partial_success_and_row_errors(client):
    bad = fixture_bytes("purchase_order_lines") + b"ERP-PO900-L1,PO-900,1,SUP-1,MUM-014,ERP-SKU-001,abc,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    r = client.post("/imports/purchase_order_lines", files={"file": ("po.csv", bad, "text/csv")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETED_WITH_ERRORS"
    assert body["counts"] == {"rows": 8, "accepted": 6, "already_loaded": 0, "duplicate_in_file": 1, "rejected": 1, "conflict": 0}
    assert body["problem_rows"][0]["errors"][0]["field"] == "ordered_qty"
    detail = client.get(f"/imports/{body['import_id']}", params={"outcome": "DUPLICATE_IN_FILE"}).json()
    assert [row["row_number"] for row in detail["rows"]] == [7]
    dq = client.get("/data-quality").json()
    assert dq["rejected_rows_latest_imports"][0]["errors"][0]["field"] == "ordered_qty"


def test_unreadable_file_is_422_with_a_visible_failed_import(client):
    r = client.post("/imports/goods_receipt_lines", files={"file": ("x.csv", b"nope\n1\n", "text/csv")})
    assert r.status_code == 422 and r.json()["status"] == "FAILED"
    assert client.get(f"/imports/{r.json()['import_id']}").json()["failure_reason"].startswith("missing required columns")
    assert client.post("/imports/unknown_dataset", files={"file": ("x.csv", b"a\n", "text/csv")}).status_code == 404


def test_detection_run_and_exception_detail(client, loaded):
    outcomes = {e["po_number"]: (e["outcome"], e["variance_qty"]) for e in loaded["evaluations"]}
    assert outcomes["PO-505"] == ("SHORTFALL", "24")
    assert outcomes["PO-503"][0] == "NOT_DUE"
    exc = next(e for e in client.get("/exceptions").json() if e["po_number"] == "PO-505")
    detail = client.get(f"/exceptions/{exc['exception_id']}").json()
    assert {s["source_record_id"] for s in detail["source_records"]} == {"ERP-PO505-L1", "WMS-GRN505-A-L1"}
    assert detail["events"][0]["event_type"] == "DETECTED"
    assert detail["tasks"][0]["assignee_employee_id"] == "HR-401"
    rec = client.get("/reconciliation/po-lines").json()
    assert {line["po_number"]: line["outcome"] for line in rec["po_lines"]}["PO-504"] == "NOT_APPLICABLE_CANCELLED"


def test_list_tasks_by_assignee_and_location(client, loaded):
    mine = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    assert len(mine) == 2 and all(t["status"] == "ASSIGNED" and t["due_at"] for t in mine)
    assert len(client.get("/tasks", params={"location_id": "STORE-015"}).json()) == 1


def test_full_lifecycle_to_verified_resolves_the_exception(client, loaded):
    task = _task_for(client, "PO-501")
    tid = task["task_id"]
    assert act(client, tid, "acknowledge", "HR-102").json()["status"] == "ACKNOWLEDGED"
    assert act(client, tid, "start", "HR-102").json()["status"] == "IN_PROGRESS"
    done = act(client, tid, "complete", "HR-102", COMPLETE).json()
    assert done["status"] == "PENDING_VERIFICATION" and done["resolution_code"] == COMPLETE["resolution_code"]
    assert act(client, tid, "verify", "HR-201", {"note": "checked ASN"}).json()["status"] == "VERIFIED"
    exc = client.get(f"/exceptions/{task['exception_id']}").json()
    assert exc["status"] == "RESOLVED"
    assert exc["events"][-1]["event_type"] == "RESOLVED" and exc["events"][-1]["actor"] == "HR-201"
    history = client.get(f"/tasks/{tid}").json()["events"]
    assert [(e["event_type"], e["to_status"]) for e in history] == [
        ("CREATED", "CREATED"),
        ("ASSIGNED", "ASSIGNED"),
        ("ACKNOWLEDGE", "ACKNOWLEDGED"),
        ("START", "IN_PROGRESS"),
        ("COMPLETE", "COMPLETED"),
        ("SUBMIT_FOR_VERIFICATION", "PENDING_VERIFICATION"),
        ("VERIFY", "VERIFIED"),
    ]
    assert [e["actor"] for e in history][2:] == ["HR-102", "HR-102", "HR-102", "system:workflow", "HR-201"]


def test_invalid_transitions_and_wrong_actor_are_refused(client, loaded):
    tid = _task_for(client, "PO-501")["task_id"]
    r = act(client, tid, "complete", "HR-102", COMPLETE)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_TRANSITION"
    assert r.json()["error"]["details"]["allowed_actions"] == ["acknowledge", "reassign"]
    r = act(client, tid, "acknowledge", "HR-101")
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ASSIGNEE"
    assert client.post(f"/tasks/{tid}/acknowledge").status_code == 422  # no actor header
    assert act(client, tid, "complete", "HR-102", {"resolution_code": "X"}).status_code == 422
    r = client.post(f"/tasks/{tid}/status", json={"status": "VERIFIED"}, headers={"X-Actor-Id": "HR-102"})
    assert r.status_code in (404, 405)  # there is no way to set a status directly


def test_retry_after_commit_with_idempotency_key_returns_the_original_response(client, loaded):
    tid = _task_for(client, "PO-506")["task_id"]
    act(client, tid, "acknowledge", "HR-102")
    act(client, tid, "start", "HR-102")
    first = act(client, tid, "complete", "HR-102", COMPLETE, key="complete-506-1")
    retry = act(client, tid, "complete", "HR-102", COMPLETE, key="complete-506-1")
    assert first.status_code == retry.status_code == 200
    assert retry.headers.get("Idempotent-Replayed") == "true"
    assert retry.json() == first.json()
    events = client.get(f"/tasks/{tid}").json()["events"]
    assert sum(e["event_type"] == "COMPLETE" for e in events) == 1
    reused = act(client, tid, "complete", "HR-102", {**COMPLETE, "evidence_ref": "other"}, key="complete-506-1")
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_repeated_completion_without_key_is_safe(client, loaded):
    tid = _task_for(client, "PO-506")["task_id"]
    act(client, tid, "acknowledge", "HR-102")
    act(client, tid, "start", "HR-102")
    assert act(client, tid, "complete", "HR-102", COMPLETE).json()["replayed"] is False
    again = act(client, tid, "complete", "HR-102", COMPLETE)
    assert again.status_code == 200 and again.json()["replayed"] is True
    different = act(client, tid, "complete", "HR-102", {**COMPLETE, "resolution_code": "COUNT_ERROR_CORRECTED"})
    assert different.status_code == 409 and different.json()["error"]["code"] == "ALREADY_COMPLETED"
    assert sum(e["event_type"] == "COMPLETE" for e in client.get(f"/tasks/{tid}").json()["events"]) == 1


def test_reassignment_rules_and_history(client, loaded):
    tid = _task_for(client, "PO-501")["task_id"]
    r = act(client, tid, "reassign", "HR-102", {"assignee_employee_id": "HR-201", "reason": "busy"})
    assert r.status_code == 403  # a receiver cannot reassign
    r = act(client, tid, "reassign", "HR-401", {"assignee_employee_id": "HR-201", "reason": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACTOR_NOT_MANAGER_OF_THIS_LOCATION"
    r = act(client, tid, "reassign", "HR-201", {"assignee_employee_id": "HR-101", "reason": "x"})
    assert r.status_code == 422 and "AVAILABILITY_OFF_SHIFT" in r.json()["error"]["details"]["reasons"]
    r = act(client, tid, "reassign", "HR-201", {"assignee_employee_id": "HR-103", "reason": "x"})
    assert r.status_code == 422 and "EMPLOYMENT_STATUS_INACTIVE" in r.json()["error"]["details"]["reasons"]
    r = act(client, tid, "reassign", "HR-501", {"assignee_employee_id": "HR-201", "reason": "HR-102 on dock audit"})
    assert r.status_code == 200 and r.json()["assignee_employee_id"] == "HR-201" and r.json()["status"] == "ASSIGNED"
    detail = client.get(f"/tasks/{tid}").json()
    assert [(a["employee_id"], a["stage"], a["ended_as_of"] is None) for a in detail["assignments"]] == [("HR-102", "PRIMARY", False), ("HR-201", "REASSIGNED", True)]
    assert detail["events"][-1]["payload"] == {"from": "HR-102", "to": "HR-201", "reason": "HR-102 on dock audit"}
    assert detail["events"][-1]["actor"] == "HR-501"
    # still one exception and one task
    assert len(client.get("/exceptions").json()) == 3 and len(client.get("/tasks").json()) == 3


def test_verification_requires_an_independent_supervisor_and_can_reopen(client, loaded):
    tid = _task_for(client, "PO-505")["task_id"]  # assigned to store manager HR-401 (fallback)
    for action in ("acknowledge", "start"):
        act(client, tid, action, "HR-401")
    act(client, tid, "complete", "HR-401", COMPLETE)
    r = act(client, tid, "verify", "HR-401", {})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"
    assert act(client, tid, "verify", "HR-201", {}).status_code == 403  # manager of another store
    r = act(client, tid, "reject", "HR-501", {"reason": "evidence reference is not the ASN"})
    assert r.json()["status"] == "REOPENED"
    exc_id = r.json()["exception_id"]
    assert client.get(f"/exceptions/{exc_id}").json()["status"] == "OPEN"
    assert act(client, tid, "start", "HR-401").json()["status"] == "IN_PROGRESS"


def test_block_and_unblock(client, loaded):
    tid = _task_for(client, "PO-506")["task_id"]
    act(client, tid, "acknowledge", "HR-102")
    act(client, tid, "start", "HR-102")
    assert act(client, tid, "block", "HR-102", {"reason": "waiting for supplier ASN"}).json()["status"] == "BLOCKED"
    assert act(client, tid, "complete", "HR-102", COMPLETE).status_code == 409
    assert act(client, tid, "unblock", "HR-102", {"note": "ASN received"}).json()["status"] == "IN_PROGRESS"


def test_manual_queue_task_can_be_assigned_by_a_supervisor(client):
    for ds in LOAD_ORDER:
        content = fixture_bytes(ds)
        if ds == "workforce_status":
            content = content.replace(b"HR-401,ON_SHIFT", b"HR-401,OFF_SHIFT")
        client.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", content, "text/csv")})
    client.post("/detection/runs")
    queued = client.get("/tasks", params={"queue": "MANUAL_ASSIGNMENT_REQUIRED"}).json()
    assert len(queued) == 1 and queued[0]["status"] == "CREATED" and queued[0]["assignee_employee_id"] is None
    # nobody at STORE-015 is eligible, so even a regional supervisor cannot force an ineligible assignee
    r = act(client, queued[0]["task_id"], "reassign", "HR-501", {"assignee_employee_id": "HR-401", "reason": "manager"})
    assert r.status_code == 422 and "AVAILABILITY_OFF_SHIFT" in r.json()["error"]["details"]["reasons"]


def test_errors_are_structured_with_request_id(client):
    r = client.get("/exceptions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "NOT_FOUND" and body["request_id"] == r.headers["X-Request-Id"]

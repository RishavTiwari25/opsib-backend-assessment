"""Integration: load the fixture pack into PostgreSQL, run detection at the frozen time and check the six
required scenario outcomes, provenance, idempotency, concurrency and data-quality paths."""
from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import (
    DetectionRun,
    ExceptionCase,
    ExceptionEvent,
    LineEvaluation,
    SourceConflict,
    Task,
    TaskAssignment,
    TaskEvent,
)
from app.detection.rules import RULE_VERSION
from app.ingestion.conflicts import resolve_conflict
from tests.helpers import (
    TENANT,
    detect,
    fixture_bytes,
    load,
    load_pack,
    replace_rows,
    with_rows,
)


def _outcomes(factory, run_id) -> dict[str, LineEvaluation]:
    with factory() as s:
        return {e.po_number: e for e in s.execute(select(LineEvaluation).where(LineEvaluation.run_id == run_id)).scalars()}


def _exceptions(factory) -> dict[str, ExceptionCase]:
    with factory() as s:
        return {e.po_number: e for e in s.execute(select(ExceptionCase)).scalars()}


def _count(factory, model) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(model)).scalar_one()


def test_required_scenario_outcomes(factory, clock):
    load_pack(factory)
    run = detect(factory, clock)
    out = _outcomes(factory, run.id)
    assert {k: v.outcome for k, v in out.items()} == {
        "PO-501": "SHORTFALL",
        "PO-502": "NO_SHORTFALL",
        "PO-503": "NOT_DUE",
        "PO-504": "NOT_APPLICABLE_CANCELLED",
        "PO-505": "SHORTFALL",
        "PO-506": "SHORTFALL",
    }
    exc = _exceptions(factory)
    assert set(exc) == {"PO-501", "PO-505", "PO-506"}
    got = {k: (e.expected_qty, e.received_qty, e.variance_qty, e.base_uom) for k, e in exc.items()}
    assert got == {
        "PO-501": (Decimal("100"), Decimal("92"), Decimal("8"), "EA"),
        "PO-505": (Decimal("120"), Decimal("96"), Decimal("24"), "EA"),
        "PO-506": (Decimal("20"), Decimal("10"), Decimal("10"), "EA"),
    }
    assert out["PO-503"].details["received_qty"] == "20"  # partial progress visible, nothing raised


def test_finding_is_traceable_to_source_rows_and_versions(factory, clock):
    batches = load_pack(factory)
    run = detect(factory, clock)
    e = _exceptions(factory)["PO-501"]
    assert e.business_key == "RECEIPT_SHORTFALL:ERP:PO-501:1"
    assert (e.rule_version, e.mapping_version, e.first_detection_run_id) == (RULE_VERSION, run.mapping_version, run.id)
    assert e.detected_as_of == clock.now()
    assert e.evidence["po_line"]["source_record_id"] == "ERP-PO501-L1"
    assert e.evidence["po_line"]["first_import_id"] == str(batches["purchase_order_lines"].id)
    assert [r["source_record_id"] for r in e.evidence["receipts_counted"]] == ["WMS-GRN501-A-L1", "WMS-GRN501-B-L1"]
    assert e.evidence["mapping"]["product_alias_fact_id"] and e.evidence["mapping"]["location_alias_fact_id"]
    assert (e.canonical_product_id, e.canonical_location_id, e.supplier_code) == ("SKU-001", "STORE-014", "SUP-07")


def test_explanation_is_neutral_about_cause(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    for e in _exceptions(factory).values():
        text = " ".join([e.explanation["statement"], *e.explanation["known"]]).lower()
        assert "does not establish supplier fault" in text
        assert any("asn" in u.lower() for u in e.explanation["unknown"])
        assert e.explanation["evidence_needed"]


def test_duplicate_receipt_record_is_counted_once(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    e = _exceptions(factory)["PO-506"]
    assert e.received_qty == Decimal("10") and e.variance_qty == Decimal("10")
    assert len(e.evidence["receipts_counted"]) == 1


def test_tasks_are_routed_to_eligible_people_with_reasons(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    with factory() as s:
        tasks = {t.exception_id: t for t in s.execute(select(Task)).scalars()}
        exc = _exceptions(factory)
        assert tasks[exc["PO-501"].id].assignee_employee_id == "HR-102"
        assert tasks[exc["PO-506"].id].assignee_employee_id == "HR-102"
        assert tasks[exc["PO-505"].id].assignee_employee_id == "HR-401"
        a = s.execute(select(TaskAssignment).where(TaskAssignment.task_id == tasks[exc["PO-505"].id].id)).scalar_one()
        assert a.stage == "FALLBACK" and "store_manager" in a.reason
        a14 = s.execute(select(TaskAssignment).where(TaskAssignment.task_id == tasks[exc["PO-501"].id].id)).scalar_one()
        reasons = {c["employee_id"]: c["reasons"] for c in a14.decision["candidates"]}
        assert reasons["HR-101"] == ["AVAILABILITY_OFF_SHIFT"]
        assert reasons["HR-103"] == ["EMPLOYMENT_STATUS_INACTIVE"]
        assert "display_name" not in str(a14.decision)


def test_rerunning_import_and_detection_changes_no_business_result(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    snapshot = lambda: (  # noqa: E731
        sorted((str(e.id), e.status, e.variance_qty, e.revision) for e in _exceptions(factory).values()),
        _count(factory, Task),
        _count(factory, TaskAssignment),
        _count(factory, TaskEvent),
        _count(factory, ExceptionEvent),
    )
    first = snapshot()
    load_pack(factory)
    run2 = detect(factory, clock)
    assert snapshot() == first
    assert run2.summary["exceptions_created"] == 0 and run2.summary["tasks_created"] == 0


def test_concurrent_detection_runs_create_one_exception_and_one_task_per_case(factory, clock):
    load_pack(factory)
    errors: list[BaseException] = []

    def run():
        try:
            detect(factory, clock)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert _count(factory, DetectionRun) == 4
    assert _count(factory, ExceptionCase) == 3
    assert _count(factory, Task) == 3
    with factory() as s:
        detected = s.execute(select(func.count()).select_from(ExceptionEvent).where(ExceptionEvent.event_type == "DETECTED")).scalar_one()
    assert detected == 3


def test_unknown_product_mapping_is_data_quality_not_an_exception(factory, clock):
    load_pack(
        factory,
        overrides={"purchase_order_lines": with_rows("purchase_order_lines", "ERP-PO507-L1,PO-507,1,SUP-30,MUM-014,ERP-SKU-999,5,EA,OPEN,2026-10-15T12:00:00+05:30")},
    )
    run = detect(factory, clock)
    ev = _outcomes(factory, run.id)["PO-507"]
    assert ev.outcome == "NOT_EVALUABLE" and ev.reasons[0].startswith("UNMAPPED_PRODUCT")
    assert "PO-507" not in _exceptions(factory)


def test_conflicting_receipt_blocks_evaluation_until_resolved(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    changed = fixture_bytes("goods_receipt_lines").decode().replace(
        "WMS-GRN506-A-L1,GRN-506-A,1,PO-506,1,WH-MH-014,WMS-BRC-003,10,EA", "WMS-GRN506-A-L1,GRN-506-A,1,PO-506,1,WH-MH-014,WMS-BRC-003,20,EA", 1
    ).encode()
    load(factory, "goods_receipt_lines", changed)
    run = detect(factory, clock)
    ev = _outcomes(factory, run.id)["PO-506"]
    assert ev.outcome == "NOT_EVALUABLE" and ev.reasons[0].startswith("CONFLICTING_RECEIPT")
    exc = _exceptions(factory)["PO-506"]
    assert exc.status == "OPEN" and exc.variance_qty == Decimal("10")  # finding kept, not silently changed
    with factory() as s:
        assert s.execute(select(ExceptionEvent.event_type).where(ExceptionEvent.exception_id == exc.id).order_by(ExceptionEvent.id.desc()).limit(1)).scalar_one() == "EVALUATION_BLOCKED"
        conflict_id = s.execute(select(SourceConflict.id)).scalar_one()
    with factory() as s, s.begin():
        resolve_conflict(s, tenant_id=TENANT, conflict_id=conflict_id, action="KEEP_EXISTING", actor="HR-501", note="WMS confirmed 10")
    run = detect(factory, clock)
    assert _outcomes(factory, run.id)["PO-506"].outcome == "SHORTFALL"


def test_accepting_a_corrected_receipt_updates_the_finding_with_history(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    changed = fixture_bytes("goods_receipt_lines").decode().replace(",PO-501,1,WH-MH-014,WMS-BRC-001,32,EA", ",PO-501,1,WH-MH-014,WMS-BRC-001,36,EA").encode()
    load(factory, "goods_receipt_lines", changed)
    with factory() as s, s.begin():
        cid = s.execute(select(SourceConflict.id)).scalar_one()
        resolve_conflict(s, tenant_id=TENANT, conflict_id=cid, action="ACCEPT_INCOMING", actor="HR-501", note="WMS correction")
    detect(factory, clock)
    e = _exceptions(factory)["PO-501"]
    assert (e.received_qty, e.variance_qty, e.revision) == (Decimal("96"), Decimal("4"), 2)
    with factory() as s:
        ev = s.execute(select(ExceptionEvent).where(ExceptionEvent.exception_id == e.id, ExceptionEvent.event_type == "EVIDENCE_CHANGED")).scalar_one()
    assert ev.payload["before"]["variance"] == "8" and ev.payload["after"]["variance"] == "4"


def test_late_receipt_that_closes_the_gap_clears_the_case_and_cancels_open_tasks(factory, clock):
    load_pack(factory)
    detect(factory, clock)
    load(factory, "goods_receipt_lines", with_rows("goods_receipt_lines", "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,8,EA,2026-10-16T13:00:00+05:30,POSTED"))
    run = detect(factory, clock)
    assert run.summary["exceptions_cleared"] == 1
    e = _exceptions(factory)["PO-501"]
    assert e.status == "CLEARED_BY_DATA"
    with factory() as s:
        assert s.execute(select(Task.status).where(Task.exception_id == e.id)).scalar_one() == "CANCELLED"


def test_manual_queue_when_no_eligible_assignee(factory, clock):
    no_manager = replace_rows("workforce_status", {"HR-401,ON_SHIFT": "HR-401,OFF_SHIFT"})
    load_pack(factory, overrides={"workforce_status": no_manager})
    detect(factory, clock)
    with factory() as s:
        t = s.execute(select(Task).where(Task.canonical_location_id == "STORE-015")).scalar_one()
    assert (t.status, t.assignee_employee_id, t.assignment_queue) == ("CREATED", None, "MANUAL_ASSIGNMENT_REQUIRED")


def test_unmapped_employee_location_is_not_used_for_routing(factory, clock):
    roster = replace_rows("employees", {"HR-102,Dev Shah,ACTIVE,receiving_associate,ST-014": "HR-102,Dev Shah,ACTIVE,receiving_associate,ST-14"})
    load_pack(factory, overrides={"employees": roster})
    detect(factory, clock)
    with factory() as s:
        assignees = set(s.execute(select(Task.assignee_employee_id).where(Task.canonical_location_id == "STORE-014")).scalars())
        reasons = s.execute(select(TaskAssignment.decision).limit(1)).scalar_one()
    assert "HR-102" not in assignees
    assert assignees == {"HR-201"}  # active on-shift store manager fallback at STORE-014
    assert any(c["employee_id"] == "HR-102" and c["reasons"] == ["UNMAPPED_LOCATION_HRMS_ST-14"] for c in reasons["candidates"])

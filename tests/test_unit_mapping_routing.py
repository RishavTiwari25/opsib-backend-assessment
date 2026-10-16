"""Unit tests for identity mapping and employee routing (pure, no database)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.ingestion.records import LocationAliasRecord, ProductAliasRecord
from app.mapping.resolver import AliasFact, MappingSnapshot
from app.routing.policy import (
    MANUAL_QUEUE,
    AvailabilityView,
    EmployeeView,
    can_supervise,
    default_policy,
    latest_availability,
    route_task,
)
from app.workflows.state_machine import Action, next_status

AS_OF = datetime.fromisoformat("2026-10-16T15:00:00+05:30")
POLICY = default_policy(60)


def _p(i, system, ext, canon, src_uom="EA", base="EA", factor="1"):
    rec = ProductAliasRecord(source_system=system, external_item_id=ext, canonical_product_id=canon, source_uom=src_uom, base_uom=base, to_base_factor=Decimal(factor))
    return AliasFact(i, f"{system}|{ext}", f"h{i}", 1), rec


def _l(i, system, ext, canon, region="REGION-MH"):
    rec = LocationAliasRecord(source_system=system, external_location_id=ext, canonical_location_id=canon, region_id=region)
    return AliasFact(i, f"{system}|{ext}", f"h{i}", 1), rec


# ------------------------------------------------------------------ mapping


def test_erp_and_wms_ids_resolve_to_one_canonical_product_with_factor():
    snap = MappingSnapshot.build([_p(1, "ERP", "ERP-BOX-12", "SKU-002", "BOX", "EA", "12"), _p(2, "WMS", "WMS-MAT-2002", "SKU-002")], [])
    erp = snap.resolve_product("ERP", "ERP-BOX-12", "box")
    wms = snap.resolve_product("WMS", "WMS-MAT-2002", "EA")
    assert erp.canonical_product_id == wms.canonical_product_id == "SKU-002"
    assert erp.to_base(Decimal("10")) == Decimal("120")
    assert wms.to_base(Decimal("96")) == Decimal("96")


def test_same_external_id_in_another_system_is_not_reused():
    snap = MappingSnapshot.build([_p(1, "ERP", "X-1", "SKU-1")], [])
    assert snap.resolve_product("WMS", "X-1", "EA").issue == "UNMAPPED_PRODUCT"


def test_inconsistent_base_unit_is_ambiguous_not_guessed():
    snap = MappingSnapshot.build([_p(1, "ERP", "A", "SKU-9", "EA", "EA"), _p(2, "WMS", "B", "SKU-9", "KG", "KG")], [])
    assert snap.resolve_product("ERP", "A", "EA").issue == "AMBIGUOUS_BASE_UOM"


def test_alias_under_open_conflict_is_ambiguous():
    snap = MappingSnapshot.build([_p(1, "ERP", "A", "SKU-1")], [], blocked_product_keys={"ERP|A"})
    assert snap.resolve_product("ERP", "A", "EA").issue == "AMBIGUOUS_PRODUCT_MAPPING_CONFLICT"


def test_location_crosswalk_and_region_ambiguity():
    snap = MappingSnapshot.build([], [_l(1, "ERP", "MUM-014", "STORE-014"), _l(2, "HRMS", "ST-014", "STORE-014")])
    assert snap.resolve_location("HRMS", "ST-014").canonical_location_id == "STORE-014"
    assert snap.resolve_location("HRMS", "ST-999").issue == "UNMAPPED_LOCATION"
    bad = MappingSnapshot.build([], [_l(1, "ERP", "A", "STORE-1", "R1"), _l(2, "WMS", "B", "STORE-1", "R2")])
    assert bad.resolve_location("ERP", "A").issue == "AMBIGUOUS_REGION"


def test_mapping_version_changes_when_alias_content_changes():
    a = MappingSnapshot.build([_p(1, "ERP", "A", "SKU-1")], [])
    b = MappingSnapshot.build([_p(1, "ERP", "A", "SKU-1")], [])
    fact, rec = _p(1, "ERP", "A", "SKU-1")
    c = MappingSnapshot.build([(AliasFact(1, fact.source_key, "different", 2), rec)], [])
    assert a.version == b.version != c.version


# ------------------------------------------------------------------ routing


def emp(eid, role, loc="STORE-014", status="ACTIVE", region="REGION-MH", issue=None):
    return EmployeeView(eid, status, role, loc, issue, region, None)


def avail(eid, status="ON_SHIFT", observed="2026-10-16T14:50:00+05:30", start="2026-10-16T09:00:00+05:30", end="2026-10-16T18:00:00+05:30"):
    return AvailabilityView(eid, status, datetime.fromisoformat(start), datetime.fromisoformat(end), datetime.fromisoformat(observed), f"{eid}|{observed}")


FIXTURE_STAFF = [
    emp("HR-101", "receiving_associate"),
    emp("HR-102", "receiving_associate"),
    emp("HR-103", "receiving_associate", status="INACTIVE"),
    emp("HR-201", "store_manager"),
    emp("HR-401", "store_manager", loc="STORE-015"),
    emp("HR-501", "regional_operations", loc=None),
]
FIXTURE_AVAIL = {
    a.employee_id: a
    for a in [
        avail("HR-101", "OFF_SHIFT", start="2026-10-16T08:00:00+05:30", end="2026-10-16T16:00:00+05:30"),
        avail("HR-102"),
        avail("HR-103"),
        avail("HR-201"),
        avail("HR-401"),
        avail("HR-501"),
    ]
}


def _route(location, staff=FIXTURE_STAFF, availability=FIXTURE_AVAIL):
    return route_task(case_type="RECEIPT_SHORTFALL", location_id=location, employees=staff, availability=availability, as_of=AS_OF, policy=POLICY)


def test_store_014_routes_to_the_on_shift_active_receiver():
    d = _route("STORE-014")
    assert (d.stage, d.employee_id) == ("PRIMARY", "HR-102")
    why = {c.employee_id: c.reasons for c in d.candidates}
    assert why["HR-101"] == ["AVAILABILITY_OFF_SHIFT"]
    assert why["HR-103"] == ["EMPLOYMENT_STATUS_INACTIVE"]  # HRMS wins over the ON_SHIFT export


def test_store_015_falls_back_to_the_store_manager():
    d = _route("STORE-015")
    assert (d.stage, d.employee_id) == ("FALLBACK", "HR-401")


def test_no_eligible_person_goes_to_manual_queue_not_to_someone_ineligible():
    avail_ = {**FIXTURE_AVAIL, "HR-401": avail("HR-401", "OFF_SHIFT")}
    d = _route("STORE-015", availability=avail_)
    assert d.employee_id is None and d.queue == MANUAL_QUEUE and d.stage == "MANUAL_QUEUE"


def test_regional_employee_without_store_is_never_auto_assigned_a_store_task():
    staff = [emp("HR-501", "regional_operations", loc=None)]
    assert _route("STORE-014", staff=staff).queue == MANUAL_QUEUE


def test_stale_availability_sample_is_not_trusted():
    avail_ = {**FIXTURE_AVAIL, "HR-102": avail("HR-102", observed="2026-10-16T13:00:00+05:30")}
    d = _route("STORE-014", availability=avail_)
    assert d.employee_id != "HR-102"
    assert any(r.startswith("AVAILABILITY_STALE") for c in d.candidates if c.employee_id == "HR-102" for r in c.reasons)


def test_outside_shift_window_is_ineligible_even_if_marked_on_shift():
    avail_ = {**FIXTURE_AVAIL, "HR-102": avail("HR-102", end="2026-10-16T14:55:00+05:30")}
    assert _route("STORE-014", availability=avail_).employee_id != "HR-102"


def test_unmapped_hrms_location_makes_employee_ineligible():
    staff = [emp("HR-777", "receiving_associate", loc=None, issue="UNMAPPED_LOCATION_HRMS_ST-777")]
    d = _route("STORE-014", staff=staff, availability={"HR-777": avail("HR-777")})
    assert d.queue == MANUAL_QUEUE
    assert d.candidates[0].reasons == ["UNMAPPED_LOCATION_HRMS_ST-777"]


def test_latest_sample_at_or_before_as_of_wins_and_future_samples_are_ignored():
    samples = [
        avail("HR-102", "OFF_SHIFT", observed="2026-10-16T14:00:00+05:30"),
        avail("HR-102", "ON_SHIFT", observed="2026-10-16T14:50:00+05:30"),
        avail("HR-102", "OFF_SHIFT", observed="2026-10-16T15:30:00+05:30"),
    ]
    assert latest_availability(samples, AS_OF)["HR-102"].status == "ON_SHIFT"


def test_routing_is_deterministic_by_employee_id():
    staff = [emp("HR-109", "receiving_associate"), emp("HR-105", "receiving_associate")]
    a = {e.employee_id: avail(e.employee_id) for e in staff}
    assert _route("STORE-014", staff=staff, availability=a).employee_id == "HR-105"


def test_supervision_scope():
    assert can_supervise(emp("HR-201", "store_manager"), location_id="STORE-014", region_id="REGION-MH", policy=POLICY) is None
    assert can_supervise(emp("HR-201", "store_manager"), location_id="STORE-015", region_id="REGION-MH", policy=POLICY) == "ACTOR_NOT_MANAGER_OF_THIS_LOCATION"
    assert can_supervise(emp("HR-501", "regional_operations", loc=None), location_id="STORE-015", region_id="REGION-MH", policy=POLICY) is None
    assert can_supervise(emp("HR-102", "receiving_associate"), location_id="STORE-014", region_id="REGION-MH", policy=POLICY).startswith("ACTOR_ROLE")


# ------------------------------------------------------------------ state machine


def test_state_machine_rejects_skipping_steps():
    assert next_status("ASSIGNED", Action.COMPLETE) is None
    assert next_status("CREATED", Action.ACKNOWLEDGE) is None
    assert next_status("VERIFIED", Action.REASSIGN) is None
    assert next_status("IN_PROGRESS", Action.COMPLETE) == "COMPLETED"
    assert next_status("PENDING_VERIFICATION", Action.REJECT) == "REOPENED"
    assert next_status("REOPENED", Action.START) == "IN_PROGRESS"


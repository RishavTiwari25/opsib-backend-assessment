"""Builds routing views from HRMS/WFM facts. Employee locations are mapped through the HRMS location alias
before they can be used; names stay in the source record and are not copied into routing decisions."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.facts import load_facts, open_conflict_record_ids
from app.ingestion.records import AvailabilityRecord, EmployeeRecord
from app.mapping.resolver import MappingSnapshot
from app.routing.policy import AvailabilityView, EmployeeView


@dataclass
class Directory:
    employees: dict[str, EmployeeView]
    samples: list[AvailabilityView]
    blocked_employee_ids: set[str]


def load_directory(session: Session, tenant_id: str, mapping: MappingSnapshot) -> Directory:
    blocked = open_conflict_record_ids(session, tenant_id)
    employees: dict[str, EmployeeView] = {}
    blocked_ids: set[str] = set()
    for fact in load_facts(session, tenant_id, "employees"):
        rec: EmployeeRecord = fact.record  # type: ignore[assignment]
        status = rec.employment_status.value
        if fact.record_id in blocked.get("employees", set()):
            # Two versions of an employee record exist; do not route on either until a person decides.
            blocked_ids.add(rec.source_employee_id)
            status = "UNDER_REVIEW"
        location_id = issue = None
        if rec.location_code:
            res = mapping.resolve_location("HRMS", rec.location_code)
            if res.ok:
                location_id = res.canonical_location_id
            else:
                issue = f"{res.issue}_HRMS_{rec.location_code}"
        employees[rec.source_employee_id] = EmployeeView(
            employee_id=rec.source_employee_id,
            employment_status=status,
            role=rec.role,
            canonical_location_id=location_id,
            location_issue=issue,
            region_id=rec.region_id,
            manager_id=rec.manager_employee_id,
        )
    samples = []
    for fact in load_facts(session, tenant_id, "workforce_status"):
        if fact.record_id in blocked.get("workforce_status", set()):
            continue
        a: AvailabilityRecord = fact.record  # type: ignore[assignment]
        samples.append(
            AvailabilityView(
                employee_id=a.source_employee_id,
                status=a.availability_status.value,
                shift_start=a.shift_start,
                shift_end=a.shift_end,
                observed_at=a.observed_at,
                source_key=fact.source_key,
            )
        )
    return Directory(employees, samples, blocked_ids)

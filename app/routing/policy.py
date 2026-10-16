"""Employee eligibility and task routing. Pure functions; the caller supplies facts and the as-of time.

Safety invariants (each one is a test):
  * Employment status wins. INACTIVE / ON_LEAVE / TERMINATED people are never eligible, whatever the
    shift export says.
  * Availability is required and must be fresh: the latest sample observed at or before as-of, no older
    than ``max_availability_age``, status ON_SHIFT, and as-of inside [shift_start, shift_end).
  * Location is compared in canonical form only. An HRMS location that does not map is not "close enough";
    the employee is ineligible and the mapping gap is reported.
  * Regional employees without a store location are not store-scoped and are never auto-assigned store tasks.
  * Fallback is an explicit, approved list of roles per case type. When nobody qualifies the task goes to a
    manual-assignment queue; it is never handed to an ineligible person.
  * Ties are broken by employee id so the same inputs always pick the same person.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

MANUAL_QUEUE = "MANUAL_ASSIGNMENT_REQUIRED"


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    primary_roles: Mapping[str, tuple[str, ...]]
    fallback_roles: Mapping[str, tuple[str, ...]]
    max_availability_age: timedelta
    # Roles allowed to reassign or verify tasks, and the scope they act in.
    supervisor_roles: tuple[str, ...] = ("store_manager", "regional_operations")


def default_policy(max_age_minutes: int = 60) -> RoutingPolicy:
    return RoutingPolicy(
        version="routing-1.0.0",
        primary_roles={"RECEIPT_SHORTFALL": ("receiving_associate",)},
        fallback_roles={"RECEIPT_SHORTFALL": ("store_manager",)},
        max_availability_age=timedelta(minutes=max_age_minutes),
    )


@dataclass(frozen=True)
class EmployeeView:
    employee_id: str
    employment_status: str
    role: str
    canonical_location_id: str | None
    location_issue: str | None  # set when the HRMS location exists but did not map
    region_id: str | None
    manager_id: str | None


@dataclass(frozen=True)
class AvailabilityView:
    employee_id: str
    status: str
    shift_start: datetime
    shift_end: datetime
    observed_at: datetime
    source_key: str


@dataclass
class CandidateCheck:
    employee_id: str
    role: str
    stage: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    policy_version: str
    as_of: datetime
    location_id: str
    stage: str  # PRIMARY | FALLBACK | MANUAL_QUEUE
    employee_id: str | None
    queue: str | None
    reason: str
    candidates: list[CandidateCheck]

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "as_of": self.as_of.isoformat(),
            "location_id": self.location_id,
            "stage": self.stage,
            "selected_employee_id": self.employee_id,
            "queue": self.queue,
            "reason": self.reason,
            "candidates": [c.__dict__ for c in self.candidates],
        }


def latest_availability(samples: Iterable[AvailabilityView], as_of: datetime) -> dict[str, AvailabilityView]:
    """Latest sample per employee observed at or before as-of. Samples from 'the future' are ignored."""
    latest: dict[str, AvailabilityView] = {}
    for s in samples:
        if s.observed_at > as_of:
            continue
        cur = latest.get(s.employee_id)
        if cur is None or s.observed_at > cur.observed_at:
            latest[s.employee_id] = s
    return latest


def eligibility_reasons(
    emp: EmployeeView,
    avail: AvailabilityView | None,
    *,
    location_id: str,
    as_of: datetime,
    policy: RoutingPolicy,
) -> list[str]:
    reasons: list[str] = []
    if emp.employment_status != "ACTIVE":
        reasons.append(f"EMPLOYMENT_STATUS_{emp.employment_status}")
    if emp.canonical_location_id is None:
        reasons.append(emp.location_issue or "NO_STORE_LOCATION")
    elif emp.canonical_location_id != location_id:
        reasons.append(f"DIFFERENT_LOCATION_{emp.canonical_location_id}")
    if avail is None:
        reasons.append("NO_AVAILABILITY_SAMPLE")
    else:
        age = as_of - avail.observed_at
        if age > policy.max_availability_age:
            reasons.append(f"AVAILABILITY_STALE_{int(age.total_seconds() // 60)}_MIN")
        if avail.status != "ON_SHIFT":
            reasons.append(f"AVAILABILITY_{avail.status}")
        if not (avail.shift_start <= as_of < avail.shift_end):
            reasons.append("OUTSIDE_SHIFT_WINDOW")
    return reasons


def route_task(
    *,
    case_type: str,
    location_id: str,
    employees: Sequence[EmployeeView],
    availability: Mapping[str, AvailabilityView],
    as_of: datetime,
    policy: RoutingPolicy,
) -> RoutingDecision:
    stages = (
        ("PRIMARY", policy.primary_roles.get(case_type, ())),
        ("FALLBACK", policy.fallback_roles.get(case_type, ())),
    )
    checks: list[CandidateCheck] = []
    for stage, roles in stages:
        eligible: list[str] = []
        for emp in sorted((e for e in employees if e.role in roles), key=lambda e: e.employee_id):
            reasons = eligibility_reasons(
                emp, availability.get(emp.employee_id), location_id=location_id, as_of=as_of, policy=policy
            )
            # Only record people who are at least in scope for this store, to keep the decision readable.
            if emp.canonical_location_id == location_id or emp.location_issue:
                checks.append(CandidateCheck(emp.employee_id, emp.role, stage, not reasons, reasons))
            if not reasons:
                eligible.append(emp.employee_id)
        if eligible:
            chosen = eligible[0]
            return RoutingDecision(
                policy.version,
                as_of,
                location_id,
                stage,
                chosen,
                None,
                f"{stage.lower()} role {'/'.join(roles)} at {location_id}: active, on shift, availability fresh; "
                f"lowest employee id among {len(eligible)} eligible",
                checks,
            )
    return RoutingDecision(
        policy.version,
        as_of,
        location_id,
        "MANUAL_QUEUE",
        None,
        MANUAL_QUEUE,
        f"no eligible primary or approved fallback role at {location_id}; needs manual assignment",
        checks,
    )


def can_supervise(actor: EmployeeView | None, *, location_id: str, region_id: str | None, policy: RoutingPolicy) -> str | None:
    """Return None if the actor may reassign/verify work at this location, else the reason they may not."""
    if actor is None:
        return "UNKNOWN_ACTOR"
    if actor.employment_status != "ACTIVE":
        return f"ACTOR_EMPLOYMENT_STATUS_{actor.employment_status}"
    if actor.role not in policy.supervisor_roles:
        return f"ACTOR_ROLE_{actor.role}_NOT_SUPERVISOR"
    if actor.role == "store_manager" and actor.canonical_location_id != location_id:
        return "ACTOR_NOT_MANAGER_OF_THIS_LOCATION"
    if actor.role == "regional_operations" and (not region_id or actor.region_id != region_id):
        return "ACTOR_OUTSIDE_REGION"
    return None

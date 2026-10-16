"""Task actions. Each method runs inside the caller's transaction after the task row is locked FOR UPDATE,
so two requests for the same task are applied one at a time and each sees the other's result.

Who may do what
  acknowledge / start / block / unblock / complete : the current assignee, who must still be ACTIVE
  reassign / verify / reject                       : an ACTIVE store_manager of the task's store or an ACTIVE
                                                     regional_operations employee of its region
  verify                                           : additionally must not be the person who completed it

An exception is RESOLVED only when every non-cancelled task is VERIFIED and at least one task was verified,
which means each carries a resolution code, an evidence reference and an independent verifier.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import (
    Conflict,
    Forbidden,
    InvalidTransition,
    NotFound,
    ValidationFailed,
)
from app.db.facts import build_mapping_snapshot
from app.db.models import ExceptionCase, ExceptionEvent, Task, TaskAssignment
from app.routing.directory import Directory, load_directory
from app.routing.policy import (
    RoutingPolicy,
    can_supervise,
    eligibility_reasons,
    latest_availability,
)
from app.workflows.state_machine import (
    Action,
    ResolutionCode,
    allowed_actions,
    next_status,
)
from app.workflows.tasks import add_task_event


@dataclass
class ActionResult:
    task: Task
    replayed: bool = False


class WorkflowService:
    def __init__(self, session: Session, *, tenant_id: str, clock: Clock, policy: RoutingPolicy) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.clock = clock
        self.policy = policy
        self._directory: Directory | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def directory(self) -> Directory:
        if self._directory is None:
            mapping = build_mapping_snapshot(self.session, self.tenant_id)
            self._directory = load_directory(self.session, self.tenant_id, mapping)
        return self._directory

    def lock_task(self, task_id: uuid.UUID) -> Task:
        task = self.session.execute(
            select(Task).where(Task.id == task_id, Task.tenant_id == self.tenant_id).with_for_update()
        ).scalar_one_or_none()
        if task is None:
            raise NotFound(f"task {task_id} not found")
        return task

    def _now(self) -> datetime:
        return self.clock.now()

    def _transition(self, task: Task, action: Action, actor: str, payload: dict | None = None) -> None:
        target = next_status(task.status, action)
        if target is None:
            raise InvalidTransition(
                f"cannot {action.value} a task in status {task.status}",
                details={"status": task.status, "allowed_actions": allowed_actions(task.status)},
            )
        add_task_event(self.session, task, action.value.upper(), task.status, target, actor, self._now(), payload)
        task.status = target
        task.version += 1

    def _require_assignee(self, task: Task, actor: str) -> None:
        if task.assignee_employee_id != actor:
            raise Forbidden(
                "only the current assignee can do this",
                code="NOT_ASSIGNEE",
                details={"assignee_employee_id": task.assignee_employee_id},
            )
        emp = self.directory.employees.get(actor)
        if emp is None or emp.employment_status != "ACTIVE":
            raise Forbidden("assignee is not an active employee", code="ACTOR_NOT_ACTIVE")

    def _require_supervisor(self, task: Task, actor: str) -> None:
        reason = can_supervise(
            self.directory.employees.get(actor), location_id=task.canonical_location_id, region_id=task.region_id, policy=self.policy
        )
        if reason:
            raise Forbidden("actor may not supervise this task", code=reason)

    # ------------------------------------------------------------------ assignee actions

    def acknowledge(self, task: Task, actor: str) -> ActionResult:
        self._require_assignee(task, actor)
        self._transition(task, Action.ACKNOWLEDGE, actor)
        return ActionResult(task)

    def start(self, task: Task, actor: str) -> ActionResult:
        self._require_assignee(task, actor)
        self._transition(task, Action.START, actor)
        return ActionResult(task)

    def block(self, task: Task, actor: str, reason: str) -> ActionResult:
        self._require_assignee(task, actor)
        self._transition(task, Action.BLOCK, actor, {"reason": reason})
        return ActionResult(task)

    def unblock(self, task: Task, actor: str, note: str | None) -> ActionResult:
        self._require_assignee(task, actor)
        self._transition(task, Action.UNBLOCK, actor, {"note": note})
        return ActionResult(task)

    def complete(self, task: Task, actor: str, resolution_code: str, evidence_ref: str, note: str | None) -> ActionResult:
        try:
            code = ResolutionCode(resolution_code)
        except ValueError:
            raise ValidationFailed(
                f"unknown resolution_code {resolution_code}",
                details={"allowed": [c.value for c in ResolutionCode]},
            ) from None
        if not evidence_ref or not evidence_ref.strip():
            raise ValidationFailed("evidence_ref is required to complete a task")

        # A retry of a completion that already committed: same person, same details -> same result, no new effect.
        if task.status in ("COMPLETED", "PENDING_VERIFICATION", "VERIFIED"):
            if task.completed_by == actor and task.resolution_code == code.value and task.evidence_ref == evidence_ref.strip():
                return ActionResult(task, replayed=True)
            raise Conflict(
                "task was already completed with different details",
                code="ALREADY_COMPLETED",
                details={"status": task.status, "resolution_code": task.resolution_code},
            )

        self._require_assignee(task, actor)
        payload = {"resolution_code": code.value, "evidence_ref": evidence_ref.strip(), "note": note}
        self._transition(task, Action.COMPLETE, actor, payload)
        task.resolution_code = code.value
        task.evidence_ref = evidence_ref.strip()
        task.completion_note = note
        task.completed_by = actor
        self._transition(task, Action.SUBMIT_FOR_VERIFICATION, "system:workflow")
        return ActionResult(task)

    # ------------------------------------------------------------------ supervisor actions

    def verify(self, task: Task, actor: str, note: str | None) -> ActionResult:
        self._require_supervisor(task, actor)
        if actor in (task.completed_by, task.assignee_employee_id):
            raise Forbidden("the person who did the work cannot verify it", code="SEGREGATION_OF_DUTIES")
        self._transition(task, Action.VERIFY, actor, {"note": note})
        task.verified_by = actor
        self._maybe_resolve_exception(task, actor)
        return ActionResult(task)

    def reject(self, task: Task, actor: str, reason: str) -> ActionResult:
        self._require_supervisor(task, actor)
        if actor == task.completed_by:
            raise Forbidden("the person who did the work cannot review it", code="SEGREGATION_OF_DUTIES")
        self._transition(task, Action.REJECT, actor, {"reason": reason})
        return ActionResult(task)

    def reassign(self, task: Task, actor: str, new_assignee_id: str, reason: str) -> ActionResult:
        if not reason or not reason.strip():
            raise ValidationFailed("a reason is required to reassign a task")
        self._require_supervisor(task, actor)
        if task.assignee_employee_id == new_assignee_id:
            raise Conflict("task is already assigned to this employee", code="ALREADY_ASSIGNED")
        target = self.directory.employees.get(new_assignee_id)
        if target is None:
            raise ValidationFailed(f"unknown employee {new_assignee_id}", code="UNKNOWN_EMPLOYEE")
        roles = self.policy.primary_roles.get("RECEIPT_SHORTFALL", ()) + self.policy.fallback_roles.get("RECEIPT_SHORTFALL", ())
        availability = latest_availability(self.directory.samples, self._now())
        reasons = eligibility_reasons(
            target, availability.get(new_assignee_id), location_id=task.canonical_location_id, as_of=self._now(), policy=self.policy
        )
        if target.role not in roles:
            reasons.append(f"ROLE_{target.role}_NOT_ALLOWED_FOR_TASK")
        if reasons:
            raise ValidationFailed(
                f"employee {new_assignee_id} is not eligible for this task", code="ASSIGNEE_NOT_ELIGIBLE", details={"reasons": reasons}
            )

        previous = task.assignee_employee_id or task.assignment_queue
        self._transition(task, Action.REASSIGN, actor, {"from": previous, "to": new_assignee_id, "reason": reason.strip()})
        open_row = self.session.execute(
            select(TaskAssignment).where(TaskAssignment.task_id == task.id, TaskAssignment.ended_as_of.is_(None))
        ).scalar_one_or_none()
        if open_row is not None:
            open_row.ended_as_of = self._now()
            self.session.flush()  # close the old row before the partial unique index sees the new one
        self.session.add(
            TaskAssignment(
                task_id=task.id,
                employee_id=new_assignee_id,
                queue=None,
                policy_version=self.policy.version,
                stage="REASSIGNED",
                reason=reason.strip(),
                decision={"checked_reasons": [], "requested_by": actor, "previous": previous},
                assigned_by=actor,
                as_of=self._now(),
            )
        )
        task.assignee_employee_id = new_assignee_id
        task.assignment_queue = None
        return ActionResult(task)

    # ------------------------------------------------------------------ exception resolution

    def _maybe_resolve_exception(self, task: Task, actor: str) -> None:
        exc = self.session.execute(
            select(ExceptionCase).where(ExceptionCase.id == task.exception_id).with_for_update()
        ).scalar_one()
        tasks = self.session.execute(select(Task).where(Task.exception_id == exc.id)).scalars().all()
        live = [t for t in tasks if t.status != "CANCELLED"]
        if exc.status == "OPEN" and live and all(t.status == "VERIFIED" for t in live):
            exc.status = "RESOLVED"
            self.session.add(
                ExceptionEvent(
                    exception_id=exc.id,
                    event_type="RESOLVED",
                    actor=actor,
                    payload={
                        "tasks": [
                            {"task_id": str(t.id), "resolution_code": t.resolution_code, "evidence_ref": t.evidence_ref, "verified_by": t.verified_by}
                            for t in live
                        ]
                    },
                    as_of=self._now(),
                )
            )

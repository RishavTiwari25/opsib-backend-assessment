"""Task creation from an exception. Safe under retries and concurrent detection runs.

The task's business key is ``<exception business key>:INVESTIGATE:g<generation>`` and its id is
uuid5(tenant, business key). Creation is ``INSERT ... ON CONFLICT DO NOTHING`` on the unique business key,
so two workers racing on the same exception create one task; the loser simply finds it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import ExceptionCase, Task, TaskAssignment, TaskEvent
from app.routing.policy import RoutingDecision

TASK_TYPE = "INVESTIGATE_RECEIPT_SHORTFALL"
SYSTEM_ACTOR = "system:detector"


def stable_id(tenant_id: str, business_key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"opsib://{tenant_id}/{business_key}")


def task_business_key(exc: ExceptionCase) -> str:
    return f"{exc.business_key}:INVESTIGATE:g{exc.task_generation}"


def ensure_task(
    session: Session,
    exc: ExceptionCase,
    decision: RoutingDecision,
    *,
    as_of: datetime,
    due_hours: int,
) -> tuple[Task, bool]:
    key = task_business_key(exc)
    task_id = stable_id(exc.tenant_id, key)
    assigned = decision.employee_id is not None
    inserted = session.execute(
        pg_insert(Task)
        .values(
            id=task_id,
            tenant_id=exc.tenant_id,
            exception_id=exc.id,
            business_key=key,
            task_type=TASK_TYPE,
            status="ASSIGNED" if assigned else "CREATED",
            canonical_location_id=exc.canonical_location_id,
            region_id=exc.region_id,
            assignee_employee_id=decision.employee_id,
            assignment_queue=None if assigned else decision.queue,
            due_at=as_of + timedelta(hours=due_hours),
        )
        .on_conflict_do_nothing(constraint="uq_task_business_key")
        .returning(Task.id)
    ).scalar_one_or_none()
    task = session.execute(select(Task).where(Task.id == task_id)).scalar_one()
    if inserted is None:
        return task, False

    session.add(
        TaskAssignment(
            task_id=task.id,
            employee_id=decision.employee_id,
            queue=None if assigned else decision.queue,
            policy_version=decision.policy_version,
            stage=decision.stage,
            reason=decision.reason,
            decision=decision.to_json(),
            assigned_by=SYSTEM_ACTOR,
            as_of=as_of,
        )
    )
    add_task_event(session, task, "CREATED", None, "CREATED", SYSTEM_ACTOR, as_of, {"exception_id": str(exc.id)})
    if assigned:
        add_task_event(
            session,
            task,
            "ASSIGNED",
            "CREATED",
            "ASSIGNED",
            SYSTEM_ACTOR,
            as_of,
            {"employee_id": decision.employee_id, "stage": decision.stage, "reason": decision.reason},
        )
    else:
        add_task_event(
            session, task, "QUEUED_FOR_MANUAL_ASSIGNMENT", "CREATED", "CREATED", SYSTEM_ACTOR, as_of, {"reason": decision.reason}
        )
    return task, True


def add_task_event(
    session: Session,
    task: Task,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    actor: str,
    as_of: datetime,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        TaskEvent(
            task_id=task.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            payload=payload or {},
            as_of=as_of,
        )
    )

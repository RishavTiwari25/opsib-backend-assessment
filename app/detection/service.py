"""Detection run: evaluate every PO line, persist outcomes, upsert exceptions, create and route tasks.

Concurrency and retries
-----------------------
* The whole run is one transaction holding a transaction-scoped advisory lock on (tenant, "detection").
  Two workers therefore never interleave; the second sees the first one's committed exceptions and tasks.
* Independently of the lock, an exception is unique on (tenant, business_key) and its id is
  uuid5(tenant, business_key); a task is unique on (tenant, task business_key). Inserts use
  ON CONFLICT DO NOTHING, so even a missing lock cannot create a second exception or task.
* Re-running with unchanged facts changes nothing except the run log: no new exception revision, no new
  task, no duplicate audit events.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock
from app.db.facts import build_mapping_snapshot, load_facts, open_conflict_record_ids
from app.db.models import (
    DetectionRun,
    ExceptionCase,
    ExceptionEvent,
    LineEvaluation,
    Task,
)
from app.db.session import take_xact_lock
from app.detection.explanation import build_explanation
from app.detection.rules import (
    RULE_ID,
    RULE_VERSION,
    LineResult,
    Outcome,
    evaluate_po_line,
    group_receipts_by_po_line,
)
from app.ingestion.records import POLineRecord
from app.routing.directory import load_directory
from app.routing.policy import RoutingPolicy, latest_availability, route_task
from app.workflows.state_machine import TERMINAL
from app.workflows.tasks import SYSTEM_ACTOR, add_task_event, ensure_task, stable_id

log = logging.getLogger("opsib.detection")


@dataclass(frozen=True)
class DetectionConfig:
    tolerance: Decimal
    task_due_hours: int
    policy: RoutingPolicy


def business_key_for(result: LineResult) -> str:
    po: POLineRecord = result.po_record
    return f"{RULE_ID}:{result.po.source_system}:{po.po_number}:{po.line_number}"


def _q(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def evidence_for(result: LineResult, mapping_version: str) -> dict[str, Any]:
    details = result.details()
    return {
        "po_line": details["po_source"],
        "receipts_counted": [r for r in details["receipts"] if r["counted"]],
        "receipts_excluded": [r for r in details["receipts"] if not r["counted"]],
        "mapping": {
            "version": mapping_version,
            "product_alias_fact_id": result.product_alias_record_id,
            "location_alias_fact_id": result.location_alias_record_id,
        },
        "rule": {"id": RULE_ID, "version": RULE_VERSION},
    }


def evidence_hash(result: LineResult) -> str:
    """Fingerprint of what the finding asserts. Unchanged facts give the same hash on every run."""
    body = {
        "po": [result.po.record_id, result.po.version],
        "product": result.canonical_product_id,
        "location": result.canonical_location_id,
        "expected": _q(result.expected_qty),
        "received": _q(result.received_qty),
        "counted": sorted([u.fact.record_id, u.fact.version] for u in result.receipts if u.counted),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def run_detection(factory: sessionmaker[Session], *, tenant_id: str, clock: Clock, config: DetectionConfig) -> DetectionRun:
    as_of = clock.now()
    with factory() as session, session.begin():
        take_xact_lock(session, tenant_id, "detection")
        blocked = open_conflict_record_ids(session, tenant_id)
        mapping = build_mapping_snapshot(session, tenant_id, blocked)
        pos = load_facts(session, tenant_id, "purchase_order_lines")
        receipts_by_line = group_receipts_by_po_line(load_facts(session, tenant_id, "goods_receipt_lines"))
        directory = load_directory(session, tenant_id, mapping)
        availability = latest_availability(directory.samples, as_of)

        run = DetectionRun(
            tenant_id=tenant_id,
            rule_id=RULE_ID,
            rule_version=RULE_VERSION,
            mapping_version=mapping.version,
            routing_policy_version=config.policy.version,
            as_of=as_of,
        )
        session.add(run)
        session.flush()

        counts: dict[str, int] = {}
        changes: dict[str, int] = {"exceptions_created": 0, "exceptions_updated": 0, "exceptions_cleared": 0, "tasks_created": 0}
        for po in pos:
            rec: POLineRecord = po.record  # type: ignore[assignment]
            result = evaluate_po_line(
                po,
                receipts_by_line.get((rec.po_number, rec.line_number), []),
                mapping,
                as_of=as_of,
                tolerance=config.tolerance,
                conflicted_po_ids=blocked.get("purchase_order_lines", set()),
                conflicted_receipt_ids=blocked.get("goods_receipt_lines", set()),
            )
            counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
            exc = _apply_result(session, run, result, mapping.version, as_of, config, directory, availability, changes)
            session.add(
                LineEvaluation(
                    run_id=run.id,
                    po_record_id=po.record_id,
                    po_number=rec.po_number,
                    po_line_number=rec.line_number,
                    outcome=result.outcome.value,
                    reasons=result.reasons,
                    details=result.details(),
                    exception_id=exc.id if exc else None,
                )
            )
        run.summary = {"outcomes": counts, **changes, "po_lines_evaluated": len(pos)}
    log.info("detection.finished", extra={"run_id": str(run.id), "as_of": as_of.isoformat(), **run.summary})
    return run


def _apply_result(
    session: Session,
    run: DetectionRun,
    result: LineResult,
    mapping_version: str,
    as_of: datetime,
    config: DetectionConfig,
    directory: Any,
    availability: Any,
    changes: dict[str, int],
) -> ExceptionCase | None:
    key = business_key_for(result)
    exc = session.execute(
        select(ExceptionCase).where(ExceptionCase.tenant_id == run.tenant_id, ExceptionCase.business_key == key).with_for_update()
    ).scalar_one_or_none()

    if result.outcome == Outcome.SHORTFALL:
        if exc is None:
            exc, created = _create_exception(session, run, result, key, mapping_version, as_of)
            changes["exceptions_created"] += int(created)
            if _route_and_create_task(session, exc, as_of, config, directory, availability):
                changes["tasks_created"] += 1
            return exc
        new_hash = evidence_hash(result)
        exc.last_detection_run_id = run.id
        if exc.status == "CLEARED_BY_DATA":
            _refresh(exc, result, mapping_version, new_hash)
            exc.status = "OPEN"
            exc.task_generation += 1
            _exception_event(session, exc, "REOPENED_BY_DATA", run, as_of, {"variance_qty": _q(result.variance_qty)})
            changes["exceptions_updated"] += 1
            if _route_and_create_task(session, exc, as_of, config, directory, availability):
                changes["tasks_created"] += 1
        elif exc.evidence_hash != new_hash:
            before = {"expected": _q(exc.expected_qty), "received": _q(exc.received_qty), "variance": _q(exc.variance_qty)}
            _refresh(exc, result, mapping_version, new_hash)
            _exception_event(
                session,
                exc,
                "EVIDENCE_CHANGED",
                run,
                as_of,
                {"before": before, "after": {"expected": _q(result.expected_qty), "received": _q(result.received_qty), "variance": _q(result.variance_qty)}},
            )
            changes["exceptions_updated"] += 1
        return exc

    if exc is None:
        return None
    exc.last_detection_run_id = run.id
    if result.outcome in (Outcome.NO_SHORTFALL, Outcome.OVER_RECEIPT, Outcome.NOT_APPLICABLE_CANCELLED) and exc.status == "OPEN":
        exc.status = "CLEARED_BY_DATA"
        _exception_event(session, exc, "CLEARED_BY_DATA", run, as_of, {"outcome": result.outcome.value, "reasons": result.reasons})
        for task in session.execute(select(Task).where(Task.exception_id == exc.id).with_for_update()).scalars():
            if task.status not in TERMINAL:
                add_task_event(session, task, "CANCELLED", task.status, "CANCELLED", SYSTEM_ACTOR, as_of, {"reason": "exception cleared by newer data"})
                task.status = "CANCELLED"
                task.version += 1
        changes["exceptions_cleared"] += 1
    elif result.outcome == Outcome.NOT_EVALUABLE and exc.status == "OPEN":
        last = exc.events[-1] if exc.events else None
        if not (last and last.event_type == "EVALUATION_BLOCKED" and last.payload.get("reasons") == result.reasons):
            _exception_event(session, exc, "EVALUATION_BLOCKED", run, as_of, {"reasons": result.reasons})
    return exc


def _create_exception(
    session: Session, run: DetectionRun, result: LineResult, key: str, mapping_version: str, as_of: datetime
) -> tuple[ExceptionCase, bool]:
    po: POLineRecord = result.po_record
    exc_id = stable_id(run.tenant_id, key)
    inserted = session.execute(
        pg_insert(ExceptionCase)
        .values(
            id=exc_id,
            tenant_id=run.tenant_id,
            case_type=RULE_ID,
            business_key=key,
            status="OPEN",
            rule_id=RULE_ID,
            rule_version=RULE_VERSION,
            mapping_version=mapping_version,
            po_number=po.po_number,
            po_line_number=po.line_number,
            supplier_code=po.supplier_code,
            canonical_product_id=result.canonical_product_id,
            canonical_location_id=result.canonical_location_id,
            region_id=result.region_id,
            base_uom=result.base_uom,
            expected_qty=result.expected_qty,
            received_qty=result.received_qty,
            variance_qty=result.variance_qty,
            expected_receipt_by=po.expected_receipt_by,
            detected_as_of=as_of,
            evidence=evidence_for(result, mapping_version),
            explanation=build_explanation(result),
            evidence_hash=evidence_hash(result),
            first_detection_run_id=run.id,
            last_detection_run_id=run.id,
        )
        .on_conflict_do_nothing(constraint="uq_exception_business_key")
        .returning(ExceptionCase.id)
    ).scalar_one_or_none()
    exc = session.execute(select(ExceptionCase).where(ExceptionCase.id == exc_id).with_for_update()).scalar_one()
    if inserted is None:
        # Another writer created it first (only possible without the run lock); do not log a second DETECTED.
        return exc, False
    _exception_event(
        session,
        exc,
        "DETECTED",
        run,
        as_of,
        {"variance_qty": _q(result.variance_qty), "base_uom": result.base_uom, "rule_version": RULE_VERSION},
    )
    return exc, True


def _refresh(exc: ExceptionCase, result: LineResult, mapping_version: str, new_hash: str) -> None:
    exc.expected_qty = result.expected_qty  # type: ignore[assignment]
    exc.received_qty = result.received_qty  # type: ignore[assignment]
    exc.variance_qty = result.variance_qty  # type: ignore[assignment]
    exc.mapping_version = mapping_version
    exc.evidence = evidence_for(result, mapping_version)
    exc.explanation = build_explanation(result)
    exc.evidence_hash = new_hash
    exc.revision += 1


def _route_and_create_task(
    session: Session, exc: ExceptionCase, as_of: datetime, config: DetectionConfig, directory: Any, availability: Any
) -> bool:
    decision = route_task(
        case_type=exc.case_type,
        location_id=exc.canonical_location_id,
        employees=list(directory.employees.values()),
        availability=availability,
        as_of=as_of,
        policy=config.policy,
    )
    _, created = ensure_task(session, exc, decision, as_of=as_of, due_hours=config.task_due_hours)
    return created


def _exception_event(
    session: Session, exc: ExceptionCase, event_type: str, run: DetectionRun, as_of: datetime, payload: dict[str, Any]
) -> None:
    event = ExceptionEvent(
        exception_id=exc.id,
        event_type=event_type,
        actor=SYSTEM_ACTOR,
        detection_run_id=run.id,
        payload=payload,
        as_of=as_of,
    )
    session.add(event)
    exc.events.append(event)

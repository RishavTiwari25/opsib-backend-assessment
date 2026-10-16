"""Persistence model.

Three layers are kept apart on purpose:

* Raw input      - ``import_batch`` + ``import_row``: every CSV row exactly as received, with its outcome.
* Source facts   - ``source_record``: one validated, typed record per source identity, with provenance.
                   ``source_conflict`` holds changed or colliding repeats that need a human decision.
* Business state - ``detection_run``/``line_evaluation``, ``exception_case``/``exception_event`` and
                   ``task``/``task_assignment``/``task_event``. Business state is derived from source facts
                   and never edits them.

Canonical identity (product, location, base unit) is resolved from the alias facts at evaluation time,
so a late alias import fixes earlier rows without rewriting them. Every finding stores the mapping
version it used.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

QTY = Numeric(18, 6)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _ts(nullable: bool = False) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


# --------------------------------------------------------------------------- raw input


class ImportBatch(Base):
    __tablename__ = "import_batch"
    __table_args__ = (
        CheckConstraint(
            "status in ('PROCESSING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED')", name="ck_import_batch_status"
        ),
        Index("ix_import_batch_tenant_dataset", "tenant_id", "dataset"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    adapter: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    already_loaded_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_in_file_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = _ts(nullable=True)

    rows: Mapped[list[ImportRow]] = relationship(back_populates="batch", order_by="ImportRow.row_number")


class ImportRow(Base):
    __tablename__ = "import_row"
    __table_args__ = (
        UniqueConstraint("import_id", "row_number", name="uq_import_row_position"),
        CheckConstraint(
            "outcome in ('ACCEPTED','ALREADY_LOADED','DUPLICATE_IN_FILE','REJECTED','CONFLICT')",
            name="ck_import_row_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batch.id", ondelete="CASCADE"))
    row_number: Mapped[int] = mapped_column(Integer)  # 1-based data row, header excluded
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(32))
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    source_key: Mapped[str | None] = mapped_column(String(255))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    conflict_id: Mapped[int | None] = mapped_column(ForeignKey("source_conflict.id"))

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


# --------------------------------------------------------------------------- source facts


class SourceRecord(Base):
    """One validated record per (tenant, dataset, source identity). Identical repeats never add rows."""

    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset", "source_key", name="uq_source_record_identity"),
        UniqueConstraint("tenant_id", "dataset", "business_key", name="uq_source_record_business_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    source_key: Mapped[str] = mapped_column(String(255))
    business_key: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    first_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batch.id"))
    last_seen_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SourceConflict(Base):
    """A repeat of a known identity with different content, or a new identity reusing a business key."""

    __tablename__ = "source_conflict"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "dataset", "source_key", "kind", "incoming_content_hash", name="uq_source_conflict_once"
        ),
        CheckConstraint("kind in ('CHANGED_CONTENT','BUSINESS_KEY_COLLISION')", name="ck_source_conflict_kind"),
        CheckConstraint(
            "status in ('OPEN','RESOLVED_KEPT_EXISTING','RESOLVED_ACCEPTED_INCOMING')", name="ck_source_conflict_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    existing_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    existing_content_hash: Mapped[str] = mapped_column(String(64))
    incoming_content_hash: Mapped[str] = mapped_column(String(64))
    incoming_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    first_import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batch.id"))
    first_row_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    replaced_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- detection


class DetectionRun(Base):
    __tablename__ = "detection_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(32))
    mapping_version: Mapped[str] = mapped_column(String(64))
    routing_policy_version: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    evaluations: Mapped[list[LineEvaluation]] = relationship(order_by="LineEvaluation.id")


class LineEvaluation(Base):
    """Outcome for every PO line in a run, including lines that raised nothing or could not be evaluated."""

    __tablename__ = "line_evaluation"
    __table_args__ = (UniqueConstraint("run_id", "po_record_id", name="uq_line_evaluation_once_per_run"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detection_run.id", ondelete="CASCADE"))
    po_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))
    reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    exception_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exception_case.id"))


class ExceptionCase(Base):
    """A business case. Its id is uuid5(tenant, business_key), so it is stable across re-runs and rebuilds."""

    __tablename__ = "exception_case"
    __table_args__ = (
        UniqueConstraint("tenant_id", "business_key", name="uq_exception_business_key"),
        CheckConstraint("status in ('OPEN','RESOLVED','CLEARED_BY_DATA')", name="ck_exception_status"),
        CheckConstraint("variance_qty > 0", name="ck_exception_positive_variance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    case_type: Mapped[str] = mapped_column(String(64))
    business_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(32))
    mapping_version: Mapped[str] = mapped_column(String(64))
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(64))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    base_uom: Mapped[str] = mapped_column(String(16))
    expected_qty: Mapped[Decimal] = mapped_column(QTY)
    received_qty: Mapped[Decimal] = mapped_column(QTY)
    variance_qty: Mapped[Decimal] = mapped_column(QTY)
    expected_receipt_by: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    task_generation: Mapped[int] = mapped_column(Integer, default=1)
    first_detection_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detection_run.id"))
    last_detection_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("detection_run.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    events: Mapped[list[ExceptionEvent]] = relationship(order_by="ExceptionEvent.id")
    tasks: Mapped[list[Task]] = relationship(order_by="Task.created_at")


class ExceptionEvent(Base):
    __tablename__ = "exception_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    exception_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exception_case.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(64))
    detection_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("detection_run.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- tasks


TASK_STATUSES = (
    "CREATED",
    "ASSIGNED",
    "ACKNOWLEDGED",
    "IN_PROGRESS",
    "BLOCKED",
    "COMPLETED",
    "PENDING_VERIFICATION",
    "VERIFIED",
    "REOPENED",
    "CANCELLED",
)


class Task(Base):
    """Work a person performs for an exception. Reassignment changes the assignee, never the exception."""

    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "business_key", name="uq_task_business_key"),
        CheckConstraint("status in (" + ",".join(f"'{s}'" for s in TASK_STATUSES) + ")", name="ck_task_status"),
        CheckConstraint(
            "(assignee_employee_id is null) or (assignment_queue is null)", name="ck_task_single_owner"
        ),
        Index("ix_task_assignee", "tenant_id", "assignee_employee_id"),
        Index("ix_task_location", "tenant_id", "canonical_location_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    exception_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exception_case.id"))
    business_key: Mapped[str] = mapped_column(String(255))
    task_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    assignee_employee_id: Mapped[str | None] = mapped_column(String(64))
    assignment_queue: Mapped[str | None] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(512))
    completion_note: Mapped[str | None] = mapped_column(Text)
    completed_by: Mapped[str | None] = mapped_column(String(64))
    verified_by: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    assignments: Mapped[list[TaskAssignment]] = relationship(order_by="TaskAssignment.id")
    events: Mapped[list[TaskEvent]] = relationship(order_by="TaskEvent.id")


class TaskAssignment(Base):
    """Assignment history. Exactly one open row (ended_as_of is null) per task at any time."""

    __tablename__ = "task_assignment"
    __table_args__ = (
        Index(
            "uq_task_assignment_one_open",
            "task_id",
            unique=True,
            postgresql_where="ended_as_of is null",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"))
    employee_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))  # PRIMARY | FALLBACK | MANUAL_QUEUE | REASSIGNED
    reason: Mapped[str] = mapped_column(Text)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB)
    assigned_by: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_as_of: Mapped[datetime | None] = _ts(nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskEvent(Base):
    __tablename__ = "task_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyRecord(Base):
    """Stored response for a client-supplied Idempotency-Key, written in the same transaction as the effect."""

    __tablename__ = "idempotency_record"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

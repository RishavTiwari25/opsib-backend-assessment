"""HTTP routes. Thin: parse, call a service inside one transaction, serialise."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.ai.summarizer import build_package, summarize
from app.api.deps import Container, get_actor, get_container
from app.api.serializers import exception_brief, import_row, import_summary, task_view
from app.core.errors import NotFound, ValidationFailed
from app.db.facts import build_mapping_snapshot, load_facts, open_conflict_record_ids
from app.db.models import (
    DetectionRun,
    ExceptionCase,
    ImportBatch,
    ImportRow,
    LineEvaluation,
    SourceRecord,
    Task,
)
from app.detection.rules import evaluate_po_line, group_receipts_by_po_line
from app.detection.service import run_detection
from app.ingestion.adapters import ADAPTERS
from app.ingestion.conflicts import resolve_conflict
from app.ingestion.service import import_dataset
from app.mapping.data_quality import data_quality_report
from app.workflows import idempotency
from app.workflows.service import WorkflowService

router = APIRouter()
Ctr = Annotated[Container, Depends(get_container)]
Actor = Annotated[str, Depends(get_actor)]


@router.get("/health", tags=["ops"])
def health(c: Ctr) -> dict[str, Any]:
    return {"status": "ok", "tenant_id": c.tenant_id, "evaluation_time": c.clock.now().isoformat()}


# --------------------------------------------------------------------------- imports


@router.post("/imports/{dataset}", status_code=201, tags=["imports"])
def create_import(dataset: str, c: Ctr, response: Response, file: UploadFile = File(...)) -> dict[str, Any]:
    """Validate and load one CSV. Row-level problems never fail the whole import; they are reported per row."""
    if dataset not in ADAPTERS:
        raise NotFound(f"unknown dataset '{dataset}'", details={"known": sorted(ADAPTERS)})
    content = file.file.read()
    batch = import_dataset(c.factory, tenant_id=c.tenant_id, dataset=dataset, filename=file.filename or dataset, content=content)
    if batch.status == "FAILED":
        response.status_code = 422
    with c.factory() as s:
        rows = s.execute(
            select(ImportRow).where(ImportRow.import_id == batch.id, ImportRow.outcome.in_(("REJECTED", "CONFLICT"))).order_by(ImportRow.row_number)
        ).scalars().all()
        return {**import_summary(batch), "problem_rows": [import_row(r) for r in rows]}


@router.get("/imports", tags=["imports"])
def list_imports(c: Ctr, dataset: str | None = None) -> list[dict[str, Any]]:
    with c.factory() as s:
        stmt = select(ImportBatch).where(ImportBatch.tenant_id == c.tenant_id).order_by(ImportBatch.started_at.desc())
        if dataset:
            stmt = stmt.where(ImportBatch.dataset == dataset)
        return [import_summary(b) for b in s.execute(stmt).scalars()]


@router.get("/imports/{import_id}", tags=["imports"])
def get_import(import_id: uuid.UUID, c: Ctr, outcome: str | None = Query(default=None, description="Filter rows by outcome")) -> dict[str, Any]:
    with c.factory() as s:
        batch = s.get(ImportBatch, import_id)
        if batch is None or batch.tenant_id != c.tenant_id:
            raise NotFound(f"import {import_id} not found")
        stmt = select(ImportRow).where(ImportRow.import_id == import_id).order_by(ImportRow.row_number)
        if outcome:
            stmt = stmt.where(ImportRow.outcome == outcome.upper())
        return {**import_summary(batch), "rows": [import_row(r) for r in s.execute(stmt).scalars()]}


# --------------------------------------------------------------------------- data quality


@router.get("/data-quality", tags=["data-quality"])
def get_data_quality(c: Ctr) -> dict[str, Any]:
    with c.factory() as s:
        return data_quality_report(s, c.tenant_id)


class ResolveConflictBody(BaseModel):
    action: str = Field(description="KEEP_EXISTING or ACCEPT_INCOMING")
    note: str | None = None


@router.post("/data-quality/conflicts/{conflict_id}/resolve", tags=["data-quality"])
def post_resolve_conflict(conflict_id: int, body: ResolveConflictBody, c: Ctr, actor: Actor) -> dict[str, Any]:
    with c.factory() as s, s.begin():
        conflict = resolve_conflict(s, tenant_id=c.tenant_id, conflict_id=conflict_id, action=body.action.upper(), actor=actor, note=body.note)
        return {"conflict_id": conflict.id, "status": conflict.status, "resolved_by": conflict.resolved_by}


# --------------------------------------------------------------------------- reconciliation & detection


@router.get("/reconciliation/po-lines", tags=["detection"])
def reconcile_po_lines(c: Ctr) -> dict[str, Any]:
    """Read-only canonical view of every PO line at the evaluation time. Persists nothing."""
    as_of = c.clock.now()
    with c.factory() as s:
        blocked = open_conflict_record_ids(s, c.tenant_id)
        mapping = build_mapping_snapshot(s, c.tenant_id, blocked)
        receipts = group_receipts_by_po_line(load_facts(s, c.tenant_id, "goods_receipt_lines"))
        lines = []
        for po in load_facts(s, c.tenant_id, "purchase_order_lines"):
            r = evaluate_po_line(
                po,
                receipts.get((po.record.po_number, po.record.line_number), []),  # type: ignore[attr-defined]
                mapping,
                as_of=as_of,
                tolerance=c.settings.shortfall_tolerance_base_units,
                conflicted_po_ids=blocked.get("purchase_order_lines", set()),
                conflicted_receipt_ids=blocked.get("goods_receipt_lines", set()),
            )
            lines.append({"po_number": po.record.po_number, "line_number": po.record.line_number, "status": po.record.status.value, "outcome": r.outcome.value, "reasons": r.reasons, **r.details()})  # type: ignore[attr-defined]
        return {"as_of": as_of.isoformat(), "mapping_version": mapping.version, "po_lines": lines}


@router.post("/detection/runs", status_code=201, tags=["detection"])
def create_detection_run(c: Ctr) -> dict[str, Any]:
    run = run_detection(c.factory, tenant_id=c.tenant_id, clock=c.clock, config=c.detection_config())
    return get_detection_run(run.id, c)


@router.get("/detection/runs/{run_id}", tags=["detection"])
def get_detection_run(run_id: uuid.UUID, c: Ctr) -> dict[str, Any]:
    with c.factory() as s:
        run = s.get(DetectionRun, run_id)
        if run is None or run.tenant_id != c.tenant_id:
            raise NotFound(f"detection run {run_id} not found")
        evals = s.execute(select(LineEvaluation).where(LineEvaluation.run_id == run_id).order_by(LineEvaluation.po_number, LineEvaluation.po_line_number)).scalars()
        return {
            "run_id": str(run.id),
            "rule": {"id": run.rule_id, "version": run.rule_version},
            "mapping_version": run.mapping_version,
            "routing_policy_version": run.routing_policy_version,
            "as_of": run.as_of,
            "summary": run.summary,
            "evaluations": [
                {
                    "po_number": e.po_number,
                    "po_line_number": e.po_line_number,
                    "outcome": e.outcome,
                    "reasons": e.reasons,
                    "exception_id": str(e.exception_id) if e.exception_id else None,
                    "expected_qty": e.details.get("expected_qty"),
                    "received_qty": e.details.get("received_qty"),
                    "variance_qty": e.details.get("variance_qty"),
                    "unit": e.details.get("base_uom"),
                }
                for e in evals
            ],
        }


# --------------------------------------------------------------------------- exceptions


@router.get("/exceptions", tags=["exceptions"])
def list_exceptions(c: Ctr, status: str | None = None, location_id: str | None = None) -> list[dict[str, Any]]:
    with c.factory() as s:
        stmt = select(ExceptionCase).where(ExceptionCase.tenant_id == c.tenant_id).order_by(ExceptionCase.po_number, ExceptionCase.po_line_number)
        if status:
            stmt = stmt.where(ExceptionCase.status == status.upper())
        if location_id:
            stmt = stmt.where(ExceptionCase.canonical_location_id == location_id)
        return [exception_brief(e) for e in s.execute(stmt).scalars()]


@router.get("/exceptions/{exception_id}", tags=["exceptions"])
def get_exception(exception_id: uuid.UUID, c: Ctr) -> dict[str, Any]:
    with c.factory() as s:
        e = s.execute(
            select(ExceptionCase)
            .where(ExceptionCase.id == exception_id, ExceptionCase.tenant_id == c.tenant_id)
            .options(selectinload(ExceptionCase.events), selectinload(ExceptionCase.tasks))
        ).scalar_one_or_none()
        if e is None:
            raise NotFound(f"exception {exception_id} not found")
        fact_ids = [e.evidence["po_line"]["fact_id"]] + [r["fact_id"] for r in e.evidence["receipts_counted"] + e.evidence["receipts_excluded"]]
        sources = s.execute(select(SourceRecord).where(SourceRecord.id.in_(fact_ids)).order_by(SourceRecord.id)).scalars()
        return {
            **exception_brief(e),
            "explanation": e.explanation,
            "evidence": e.evidence,
            "source_records": [
                {"fact_id": r.id, "dataset": r.dataset, "source_system": r.source_system, "source_record_id": r.source_key, "version": r.version, "first_import_id": str(r.first_import_id), "payload": r.payload}
                for r in sources
            ],
            "tasks": [task_view(t) for t in e.tasks],
            "events": [{"event_type": ev.event_type, "actor": ev.actor, "payload": ev.payload, "as_of": ev.as_of, "recorded_at": ev.recorded_at} for ev in e.events],
        }


@router.get("/exceptions/{exception_id}/investigation-summary", tags=["exceptions"])
def get_investigation_summary(exception_id: uuid.UUID, c: Ctr) -> dict[str, Any]:
    with c.factory() as s:
        e = s.get(ExceptionCase, exception_id)
        if e is None or e.tenant_id != c.tenant_id:
            raise NotFound(f"exception {exception_id} not found")
        package = build_package(e)
    result = summarize(c.ai_provider, package, timeout_s=c.settings.ai_timeout_seconds)
    return {
        "exception_id": str(exception_id),
        "source": result.source,
        "fallback_reason": result.fallback_reason,
        "authoritative_numbers": package.computed,
        **result.summary.model_dump(),
    }


# --------------------------------------------------------------------------- tasks


@router.get("/tasks", tags=["tasks"])
def list_tasks(c: Ctr, assignee_id: str | None = None, location_id: str | None = None, status: str | None = None, queue: str | None = None) -> list[dict[str, Any]]:
    with c.factory() as s:
        stmt = select(Task).where(Task.tenant_id == c.tenant_id).order_by(Task.due_at, Task.id)
        if assignee_id:
            stmt = stmt.where(Task.assignee_employee_id == assignee_id)
        if location_id:
            stmt = stmt.where(Task.canonical_location_id == location_id)
        if status:
            stmt = stmt.where(Task.status == status.upper())
        if queue:
            stmt = stmt.where(Task.assignment_queue == queue)
        return [task_view(t) for t in s.execute(stmt).scalars()]


@router.get("/tasks/{task_id}", tags=["tasks"])
def get_task(task_id: uuid.UUID, c: Ctr) -> dict[str, Any]:
    with c.factory() as s:
        t = s.execute(
            select(Task).where(Task.id == task_id, Task.tenant_id == c.tenant_id).options(selectinload(Task.assignments), selectinload(Task.events))
        ).scalar_one_or_none()
        if t is None:
            raise NotFound(f"task {task_id} not found")
        return task_view(t, with_history=True)


class NoteBody(BaseModel):
    note: str | None = None


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1)


class CompleteBody(BaseModel):
    resolution_code: str
    evidence_ref: str = Field(min_length=1, description="Reference to the supporting document, photo or system record")
    note: str | None = None


class ReassignBody(BaseModel):
    assignee_employee_id: str
    reason: str = Field(min_length=1)


def _run_action(
    c: Container,
    request: Request,
    response: Response,
    task_id: uuid.UUID,
    actor: str,
    idem_key: str | None,
    body: BaseModel | None,
    action: str,
) -> dict[str, Any]:
    payload = body.model_dump() if body else {}
    fp = idempotency.fingerprint(request.method, request.url.path, actor, payload)
    for _attempt in range(2):
        try:
            with c.factory() as s, s.begin():
                svc = WorkflowService(s, tenant_id=c.tenant_id, clock=c.clock, policy=c.policy)
                task = svc.lock_task(task_id)
                if idem_key:
                    rec = idempotency.find(s, c.tenant_id, idem_key, fp)
                    if rec is not None:
                        response.status_code = rec.response_status
                        response.headers["Idempotent-Replayed"] = "true"
                        return rec.response_body
                result = _dispatch(svc, task, actor, action, payload)
                s.flush()
                out = jsonable_encoder({**task_view(result.task), "replayed": result.replayed})
                if idem_key:
                    idempotency.save(s, c.tenant_id, idem_key, fp, 200, out)
                if result.replayed:
                    response.headers["Idempotent-Replayed"] = "true"
                return out
        except IntegrityError:
            # A concurrent request committed the same Idempotency-Key first; answer from its record.
            if not idem_key:
                raise
            continue
    raise ValidationFailed("could not apply the request; retry with the same Idempotency-Key", code="RETRY")


def _dispatch(svc: WorkflowService, task: Task, actor: str, action: str, p: dict[str, Any]):
    if action == "acknowledge":
        return svc.acknowledge(task, actor)
    if action == "start":
        return svc.start(task, actor)
    if action == "block":
        return svc.block(task, actor, p["reason"])
    if action == "unblock":
        return svc.unblock(task, actor, p.get("note"))
    if action == "complete":
        return svc.complete(task, actor, p["resolution_code"], p["evidence_ref"], p.get("note"))
    if action == "verify":
        return svc.verify(task, actor, p.get("note"))
    if action == "reject":
        return svc.reject(task, actor, p["reason"])
    if action == "reassign":
        return svc.reassign(task, actor, p["assignee_employee_id"], p["reason"])
    raise NotFound(f"unknown action {action}")


IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", description="Client-generated key; retries with the same key are safe")]


@router.post("/tasks/{task_id}/acknowledge", tags=["tasks"])
def acknowledge(task_id: uuid.UUID, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, None, "acknowledge")


@router.post("/tasks/{task_id}/start", tags=["tasks"])
def start(task_id: uuid.UUID, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, None, "start")


@router.post("/tasks/{task_id}/block", tags=["tasks"])
def block(task_id: uuid.UUID, body: ReasonBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "block")


@router.post("/tasks/{task_id}/unblock", tags=["tasks"])
def unblock(task_id: uuid.UUID, body: NoteBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "unblock")


@router.post("/tasks/{task_id}/complete", tags=["tasks"])
def complete(task_id: uuid.UUID, body: CompleteBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "complete")


@router.post("/tasks/{task_id}/verify", tags=["tasks"])
def verify(task_id: uuid.UUID, body: NoteBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "verify")


@router.post("/tasks/{task_id}/reject", tags=["tasks"])
def reject(task_id: uuid.UUID, body: ReasonBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "reject")


@router.post("/tasks/{task_id}/reassign", tags=["tasks"])
def reassign(task_id: uuid.UUID, body: ReassignBody, c: Ctr, actor: Actor, request: Request, response: Response, idem: IdemKey = None) -> dict[str, Any]:
    return _run_action(c, request, response, task_id, actor, idem, body, "reassign")


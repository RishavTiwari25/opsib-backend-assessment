"""Import pipeline: CSV bytes -> import_batch/import_row (raw) -> source_record (validated facts).

Transaction design
------------------
1. The batch row is created and committed first (status PROCESSING) so a crashed import stays visible.
2. All rows of one file are then processed in ONE transaction that first takes a transaction-scoped
   advisory lock on (tenant, dataset). Concurrent imports of the same dataset therefore run one after
   the other and each sees the previous one's committed facts.
3. Facts are written with ``INSERT ... ON CONFLICT DO NOTHING`` against the unique constraints on
   (tenant, dataset, source_key) and (tenant, dataset, business_key). Even without the lock, two writers
   cannot create two facts for one identity; the lock just makes per-row reporting deterministic.
4. Any unexpected error rolls the row transaction back completely (no half-loaded file) and the batch is
   marked FAILED in a separate transaction with the reason.

Row outcomes
------------
ACCEPTED           new source identity, stored as a fact
DUPLICATE_IN_FILE  identical repeat of an identity first seen earlier in this same file
ALREADY_LOADED     identical repeat of a fact loaded by an earlier import (re-import is a no-op)
CONFLICT           same identity with different content, or a new identity reusing an existing business
                   key; the existing fact is kept and a source_conflict is opened for review
REJECTED           failed validation; the row and the reasons are stored, nothing else changes
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ImportBatch, ImportRow, SourceConflict, SourceRecord
from app.db.session import take_xact_lock
from app.ingestion.adapters import DatasetAdapter, get_adapter

log = logging.getLogger("opsib.ingestion")


# --------------------------------------------------------------------------- hashing


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return text if "." not in text else text.rstrip("0").rstrip(".")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    return value


def content_hash(record: BaseModel) -> str:
    """Hash of business content. 100 == 100.0 and 12:00+05:30 == 06:30Z, so formatting noise is not a change."""
    canonical = json.dumps(_normalise(dict(record)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def stored_payload(record: BaseModel) -> dict[str, Any]:
    return record.model_dump(mode="json")


# --------------------------------------------------------------------------- results


@dataclass
class RowResult:
    row_number: int
    outcome: str
    source_key: str | None
    errors: list[dict[str, Any]]
    conflict_id: int | None = None


class ImportFileError(Exception):
    """The file as a whole cannot be read (encoding, missing columns, unknown dataset)."""


def _format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    out = []
    for err in exc.errors(include_url=False):
        field = ".".join(str(p) for p in err.get("loc", ())) or "(row)"
        out.append({"field": field, "reason": err.get("msg", "invalid"), "value": err.get("input")})
    return out


def parse_csv(content: bytes, adapter: DatasetAdapter) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError(f"file is not valid UTF-8: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in adapter.required_columns if c not in header]
    if missing:
        raise ImportFileError(f"missing required columns: {', '.join(missing)}")
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v if v is not None else "") for k, v in raw.items()})
    return rows


# --------------------------------------------------------------------------- pipeline


def import_dataset(
    factory: sessionmaker[Session],
    *,
    tenant_id: str,
    dataset: str,
    filename: str,
    content: bytes,
) -> ImportBatch:
    adapter = get_adapter(dataset)
    sha = hashlib.sha256(content).hexdigest()

    with factory() as session, session.begin():
        batch = ImportBatch(
            tenant_id=tenant_id,
            dataset=dataset,
            adapter=adapter.adapter_id,
            source_system=adapter.source_system,
            filename=filename,
            file_sha256=sha,
            status="PROCESSING",
        )
        session.add(batch)
    batch_id = batch.id
    log.info("import.started", extra={"import_id": str(batch_id), "dataset": dataset, "sha256": sha})

    try:
        rows = parse_csv(content, adapter)
    except ImportFileError as exc:
        return _mark_failed(factory, batch_id, str(exc))

    try:
        with factory() as session, session.begin():
            take_xact_lock(session, tenant_id, "import", dataset)
            results = [
                _process_row(session, adapter, tenant_id, batch_id, n, raw) for n, raw in enumerate(rows, start=1)
            ]
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            counts = {o: sum(1 for r in results if r.outcome == o) for o in _OUTCOMES}
            batch.row_count = len(results)
            batch.accepted_count = counts["ACCEPTED"]
            batch.already_loaded_count = counts["ALREADY_LOADED"]
            batch.duplicate_in_file_count = counts["DUPLICATE_IN_FILE"]
            batch.rejected_count = counts["REJECTED"]
            batch.conflict_count = counts["CONFLICT"]
            batch.status = "COMPLETED_WITH_ERRORS" if counts["REJECTED"] or counts["CONFLICT"] else "COMPLETED"
            batch.finished_at = datetime.now(UTC)
    except Exception as exc:  # noqa: BLE001 - recorded on the batch, then surfaced
        log.exception("import.failed", extra={"import_id": str(batch_id)})
        return _mark_failed(factory, batch_id, f"{type(exc).__name__}: {exc}")

    log.info(
        "import.finished",
        extra={"import_id": str(batch_id), "status": batch.status, **{k.lower(): v for k, v in counts.items()}},
    )
    return batch


_OUTCOMES = ("ACCEPTED", "ALREADY_LOADED", "DUPLICATE_IN_FILE", "REJECTED", "CONFLICT")


def _mark_failed(factory: sessionmaker[Session], batch_id: uuid.UUID, reason: str) -> ImportBatch:
    with factory() as session, session.begin():
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        batch.status = "FAILED"
        batch.failure_reason = reason
        batch.finished_at = datetime.now(UTC)
    return batch


def _process_row(
    session: Session,
    adapter: DatasetAdapter,
    tenant_id: str,
    batch_id: uuid.UUID,
    row_number: int,
    raw: dict[str, str],
) -> RowResult:
    extra = sorted(set(raw) - set(adapter.required_columns))
    try:
        record = adapter.record_model.model_validate(adapter.to_fields(raw))
    except ValidationError as exc:
        result = RowResult(row_number, "REJECTED", None, _format_validation_errors(exc))
        _store_row(session, batch_id, raw, result, None)
        return result

    source_key = adapter.source_key(record)
    business_key = adapter.business_key(record)
    digest = content_hash(record)
    warnings = [{"field": c, "reason": "column not used by this adapter; ignored"} for c in extra]

    inserted_id = session.execute(
        pg_insert(SourceRecord)
        .values(
            tenant_id=tenant_id,
            dataset=adapter.dataset,
            source_system=adapter.source_system,
            source_key=source_key,
            business_key=business_key,
            content_hash=digest,
            payload=stored_payload(record),
            first_import_id=batch_id,
            last_seen_import_id=batch_id,
        )
        .on_conflict_do_nothing()
        .returning(SourceRecord.id)
    ).scalar_one_or_none()

    if inserted_id is not None:
        result = RowResult(row_number, "ACCEPTED", source_key, warnings)
        _store_row(session, batch_id, raw, result, inserted_id)
        return result

    existing = session.execute(
        select(SourceRecord).where(
            SourceRecord.tenant_id == tenant_id,
            SourceRecord.dataset == adapter.dataset,
            SourceRecord.source_key == source_key,
        )
    ).scalar_one_or_none()

    if existing is not None and existing.content_hash == digest:
        outcome = "DUPLICATE_IN_FILE" if existing.first_import_id == batch_id else "ALREADY_LOADED"
        existing.last_seen_import_id = batch_id
        result = RowResult(row_number, outcome, source_key, warnings)
        _store_row(session, batch_id, raw, result, existing.id)
        return result

    if existing is not None:
        kind, target = "CHANGED_CONTENT", existing
        reason = "same source identity was loaded before with different content; existing fact kept"
    else:
        target = session.execute(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id,
                SourceRecord.dataset == adapter.dataset,
                SourceRecord.business_key == business_key,
            )
        ).scalar_one()
        kind = "BUSINESS_KEY_COLLISION"
        reason = f"business key {business_key} already belongs to source identity {target.source_key}"

    conflict_id = _open_conflict(session, tenant_id, adapter, source_key, kind, target, digest, record, batch_id, row_number)
    result = RowResult(
        row_number,
        "CONFLICT",
        source_key,
        [{"field": "(row)", "reason": reason, "conflict_id": conflict_id}] + warnings,
        conflict_id,
    )
    _store_row(session, batch_id, raw, result, target.id)
    return result


def _open_conflict(
    session: Session,
    tenant_id: str,
    adapter: DatasetAdapter,
    source_key: str,
    kind: str,
    target: SourceRecord,
    digest: str,
    record: BaseModel,
    batch_id: uuid.UUID,
    row_number: int,
) -> int:
    session.execute(
        pg_insert(SourceConflict)
        .values(
            tenant_id=tenant_id,
            dataset=adapter.dataset,
            source_key=source_key,
            kind=kind,
            existing_record_id=target.id,
            existing_content_hash=target.content_hash,
            incoming_content_hash=digest,
            incoming_payload=stored_payload(record),
            first_import_id=batch_id,
            first_row_number=row_number,
            status="OPEN",
        )
        .on_conflict_do_nothing(constraint="uq_source_conflict_once")
    )
    return session.execute(
        select(SourceConflict.id).where(
            SourceConflict.tenant_id == tenant_id,
            SourceConflict.dataset == adapter.dataset,
            SourceConflict.source_key == source_key,
            SourceConflict.kind == kind,
            SourceConflict.incoming_content_hash == digest,
        )
    ).scalar_one()


def _store_row(
    session: Session,
    batch_id: uuid.UUID,
    raw: dict[str, str],
    result: RowResult,
    source_record_id: int | None,
) -> None:
    session.add(
        ImportRow(
            import_id=batch_id,
            row_number=result.row_number,
            raw=raw,
            outcome=result.outcome,
            errors=json.loads(json.dumps(result.errors, default=str)),
            source_key=result.source_key,
            source_record_id=source_record_id,
            conflict_id=result.conflict_id,
        )
    )

"""Human resolution of source conflicts.

Policy for a known source identity that arrives again with different content:
  * the existing fact stays authoritative and the record is marked under review (blocked from detection
    and routing) until someone decides;
  * KEEP_EXISTING closes the conflict; later repeats of that exact incoming content stay closed;
  * ACCEPT_INCOMING replaces the fact's payload, bumps its version and keeps the replaced payload on the
    conflict row, so the change is auditable. Findings computed from the old version keep their evidence.
A business-key collision (a new source id reusing an existing PO line or GRN line) cannot be accepted here:
it has to be fixed in the source system, so only KEEP_EXISTING is allowed.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.models import SourceConflict, SourceRecord


def resolve_conflict(session: Session, *, tenant_id: str, conflict_id: int, action: str, actor: str, note: str | None) -> SourceConflict:
    conflict = session.execute(
        select(SourceConflict).where(SourceConflict.id == conflict_id, SourceConflict.tenant_id == tenant_id).with_for_update()
    ).scalar_one_or_none()
    if conflict is None:
        raise NotFound(f"conflict {conflict_id} not found")
    if conflict.status != "OPEN":
        raise Conflict(f"conflict is already {conflict.status}", code="CONFLICT_ALREADY_RESOLVED")
    if action not in ("KEEP_EXISTING", "ACCEPT_INCOMING"):
        raise ValidationFailed("action must be KEEP_EXISTING or ACCEPT_INCOMING")

    if action == "ACCEPT_INCOMING":
        if conflict.kind != "CHANGED_CONTENT":
            raise ValidationFailed("a business-key collision must be corrected in the source system", code="CANNOT_ACCEPT_COLLISION")
        record = session.execute(select(SourceRecord).where(SourceRecord.id == conflict.existing_record_id).with_for_update()).scalar_one()
        if record.content_hash != conflict.existing_content_hash:
            raise Conflict("the fact changed since this conflict was raised; re-import and review again", code="STALE_CONFLICT")
        conflict.replaced_payload = record.payload
        record.payload = conflict.incoming_payload
        record.content_hash = conflict.incoming_content_hash
        record.version += 1
        record.updated_at = datetime.now(UTC)
        conflict.status = "RESOLVED_ACCEPTED_INCOMING"
    else:
        conflict.status = "RESOLVED_KEPT_EXISTING"
    conflict.resolved_by = actor
    conflict.resolution_note = note
    conflict.resolved_at = datetime.now(UTC)
    return conflict

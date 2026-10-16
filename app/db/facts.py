"""Read access to source facts as typed records. The only place that turns JSONB payloads back into models."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SourceConflict, SourceRecord
from app.ingestion.adapters import get_adapter
from app.ingestion.records import LocationAliasRecord, ProductAliasRecord
from app.mapping.resolver import AliasFact, MappingSnapshot

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class Fact(Generic[M]):
    record_id: int
    source_system: str
    source_key: str
    content_hash: str
    version: int
    first_import_id: str
    last_seen_import_id: str
    record: M

    def ref(self, dataset: str) -> dict[str, object]:
        """Provenance reference used in evidence packages and API responses."""
        return {
            "dataset": dataset,
            "source_system": self.source_system,
            "source_record_id": self.source_key,
            "fact_id": self.record_id,
            "fact_version": self.version,
            "first_import_id": self.first_import_id,
            "last_seen_import_id": self.last_seen_import_id,
        }


def load_facts(session: Session, tenant_id: str, dataset: str) -> list[Fact]:
    model = get_adapter(dataset).record_model
    rows = session.execute(
        select(SourceRecord)
        .where(SourceRecord.tenant_id == tenant_id, SourceRecord.dataset == dataset)
        .order_by(SourceRecord.id)
    ).scalars()
    return [
        Fact(
            record_id=r.id,
            source_system=r.source_system,
            source_key=r.source_key,
            content_hash=r.content_hash,
            version=r.version,
            first_import_id=str(r.first_import_id),
            last_seen_import_id=str(r.last_seen_import_id),
            record=model.model_validate(r.payload),
        )
        for r in rows
    ]


def open_conflict_record_ids(session: Session, tenant_id: str) -> dict[str, set[int]]:
    """Fact ids (per dataset) that have an unresolved conflict and must not be used as settled evidence."""
    out: dict[str, set[int]] = defaultdict(set)
    rows = session.execute(
        select(SourceConflict.dataset, SourceConflict.existing_record_id).where(
            SourceConflict.tenant_id == tenant_id, SourceConflict.status == "OPEN"
        )
    )
    for dataset, record_id in rows:
        out[dataset].add(record_id)
    return out


def build_mapping_snapshot(session: Session, tenant_id: str, blocked: dict[str, set[int]] | None = None) -> MappingSnapshot:
    blocked = blocked if blocked is not None else open_conflict_record_ids(session, tenant_id)
    products = load_facts(session, tenant_id, "product_aliases")
    locations = load_facts(session, tenant_id, "location_aliases")

    def alias(f: Fact) -> AliasFact:
        return AliasFact(f.record_id, f.source_key, f.content_hash, f.version)

    return MappingSnapshot.build(
        [(alias(f), f.record) for f in products if isinstance(f.record, ProductAliasRecord)],
        [(alias(f), f.record) for f in locations if isinstance(f.record, LocationAliasRecord)],
        blocked_product_keys={f.source_key for f in products if f.record_id in blocked.get("product_aliases", set())},
        blocked_location_keys={f.source_key for f in locations if f.record_id in blocked.get("location_aliases", set())},
    )

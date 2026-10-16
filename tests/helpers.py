"""Shared test helpers. Fixture files are read, never modified; extra scenarios are built as new CSV text."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock
from app.db.facts import Fact
from app.detection.service import DetectionConfig, run_detection
from app.ingestion.adapters import get_adapter
from app.ingestion.service import import_dataset
from app.routing.policy import default_policy

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TENANT = "test-retailer"
LOAD_ORDER = ("location_aliases", "product_aliases", "employees", "workforce_status", "purchase_order_lines", "goods_receipt_lines")


def fixture_bytes(dataset: str) -> bytes:
    return (FIXTURES / f"{dataset}.csv").read_bytes()


def load_pack(factory: sessionmaker[Session], overrides: dict[str, bytes] | None = None) -> dict:
    overrides = overrides or {}
    out = {}
    for ds in LOAD_ORDER:
        content = overrides.get(ds, fixture_bytes(ds))
        out[ds] = import_dataset(factory, tenant_id=TENANT, dataset=ds, filename=f"{ds}.csv", content=content)
    return out


def load(factory: sessionmaker[Session], dataset: str, content: bytes, filename: str = "extra.csv"):
    return import_dataset(factory, tenant_id=TENANT, dataset=dataset, filename=filename, content=content)


def with_rows(dataset: str, *rows: str, base: bytes | None = None) -> bytes:
    """Fixture file plus extra data rows (as CSV lines). The fixture file on disk is untouched."""
    text = (base if base is not None else fixture_bytes(dataset)).decode("utf-8-sig").rstrip("\n")
    return (text + "\n" + "\n".join(rows) + "\n").encode()


def replace_rows(dataset: str, mapping: dict[str, str]) -> bytes:
    """Fixture file with whole lines swapped (old line -> new line)."""
    text = fixture_bytes(dataset).decode("utf-8-sig")
    for old, new in mapping.items():
        assert old in text, old
        text = text.replace(old, new)
    return text.encode()


def detect(factory: sessionmaker[Session], clock: Clock, *, tolerance: Decimal = Decimal("0")):
    return run_detection(
        factory,
        tenant_id=TENANT,
        clock=clock,
        config=DetectionConfig(tolerance=tolerance, task_due_hours=24, policy=default_policy(60)),
    )


def fact(dataset: str, record_id: int, **fields) -> Fact:
    """In-memory fact for pure unit tests (no database)."""
    adapter = get_adapter(dataset)
    rec = adapter.record_model.model_validate(fields)
    return Fact(
        record_id=record_id,
        source_system=adapter.source_system,
        source_key=adapter.source_key(rec),
        content_hash="h",
        version=1,
        first_import_id="imp",
        last_seen_import_id="imp",
        record=rec,
    )

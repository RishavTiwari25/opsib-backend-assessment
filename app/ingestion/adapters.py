"""Source adapters: retailer export format -> typed source record.

An adapter owns everything source-specific: column names, which system is authoritative for the dataset,
how the source identity and business grain are formed. Supporting another retailer's export means adding
adapters here; the detector, router and workflow code do not change.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.ingestion.records import (
    AvailabilityRecord,
    EmployeeRecord,
    LocationAliasRecord,
    POLineRecord,
    ProductAliasRecord,
    ReceiptLineRecord,
)


@dataclass(frozen=True)
class DatasetAdapter:
    dataset: str
    adapter_id: str
    source_system: str  # system that is authoritative for this dataset
    record_model: type[BaseModel]
    required_columns: tuple[str, ...]
    # Source identity: what the source system calls "the same record".
    source_key: Callable[[Any], str]
    # Business grain: what the business calls "the same thing". Two identities may not share it.
    business_key: Callable[[Any], str]
    column_map: Mapping[str, str] | None = None  # export column -> record field, when they differ

    def to_fields(self, row: Mapping[str, str]) -> dict[str, Any]:
        mapping = self.column_map or {c: c for c in self.required_columns}
        return {field: row.get(column) for column, field in mapping.items()}


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


FIXTURE_V1 = "opsib-fixture-csv-v1"

ADAPTERS: dict[str, DatasetAdapter] = {
    "purchase_order_lines": DatasetAdapter(
        dataset="purchase_order_lines",
        adapter_id=FIXTURE_V1,
        source_system="ERP",
        record_model=POLineRecord,
        required_columns=(
            "source_record_id",
            "po_number",
            "line_number",
            "supplier_code",
            "location_code",
            "item_code",
            "ordered_qty",
            "ordered_uom",
            "status",
            "expected_receipt_by",
        ),
        source_key=lambda r: r.source_record_id,
        business_key=lambda r: f"{r.po_number}|{r.line_number}",
    ),
    "goods_receipt_lines": DatasetAdapter(
        dataset="goods_receipt_lines",
        adapter_id=FIXTURE_V1,
        source_system="WMS",
        record_model=ReceiptLineRecord,
        required_columns=(
            "source_record_id",
            "receipt_number",
            "receipt_line",
            "po_number",
            "po_line_number",
            "location_code",
            "product_ref",
            "accepted_qty",
            "uom",
            "received_at",
            "posting_status",
        ),
        source_key=lambda r: r.source_record_id,
        business_key=lambda r: f"{r.receipt_number}|{r.receipt_line}",
    ),
    "product_aliases": DatasetAdapter(
        dataset="product_aliases",
        adapter_id=FIXTURE_V1,
        source_system="OPSIB_MAPPING",
        record_model=ProductAliasRecord,
        required_columns=(
            "source_system",
            "external_item_id",
            "canonical_product_id",
            "source_uom",
            "base_uom",
            "to_base_factor",
        ),
        source_key=lambda r: f"{r.source_system.value}|{r.external_item_id}",
        business_key=lambda r: f"{r.source_system.value}|{r.external_item_id}",
    ),
    "location_aliases": DatasetAdapter(
        dataset="location_aliases",
        adapter_id=FIXTURE_V1,
        source_system="OPSIB_MAPPING",
        record_model=LocationAliasRecord,
        required_columns=("source_system", "external_location_id", "canonical_location_id", "region_id"),
        source_key=lambda r: f"{r.source_system.value}|{r.external_location_id}",
        business_key=lambda r: f"{r.source_system.value}|{r.external_location_id}",
    ),
    "employees": DatasetAdapter(
        dataset="employees",
        adapter_id=FIXTURE_V1,
        source_system="HRMS",
        record_model=EmployeeRecord,
        required_columns=(
            "source_employee_id",
            "display_name",
            "employment_status",
            "role",
            "location_code",
            "manager_employee_id",
            "region_id",
        ),
        source_key=lambda r: r.source_employee_id,
        business_key=lambda r: r.source_employee_id,
    ),
    "workforce_status": DatasetAdapter(
        dataset="workforce_status",
        adapter_id=FIXTURE_V1,
        source_system="WFM",
        record_model=AvailabilityRecord,
        required_columns=("source_employee_id", "availability_status", "shift_start", "shift_end", "observed_at"),
        # A sample is identified by employee + observation instant (normalised to UTC).
        source_key=lambda r: f"{r.source_employee_id}|{_utc_iso(r.observed_at)}",
        business_key=lambda r: f"{r.source_employee_id}|{_utc_iso(r.observed_at)}",
    ),
}


def get_adapter(dataset: str) -> DatasetAdapter:
    try:
        return ADAPTERS[dataset]
    except KeyError as exc:
        raise KeyError(f"unknown dataset '{dataset}'. Known: {', '.join(sorted(ADAPTERS))}") from exc

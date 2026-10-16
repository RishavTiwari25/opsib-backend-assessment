"""Typed source-record contracts.

These are retailer-agnostic. A retailer-specific adapter (see ``adapters.py``) turns an export row into
one of these models; the detector and router only ever see these models, never CSV columns.

Validation rules worth calling out:
* every timestamp must carry an explicit UTC offset; naive timestamps are rejected, not guessed;
* quantities are Decimals, never floats;
* status-like fields are closed vocabularies, so an unknown status is a visible rejection.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _require_tz(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit UTC offset, e.g. 2026-10-15T12:00:00+05:30")
    return value


AwareDatetime = Annotated[datetime, AfterValidator(_require_tz)]
Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Uom = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=16)]
OptionalCode = Annotated[str | None, AfterValidator(lambda v: (v.strip() or None) if isinstance(v, str) else v)]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class POStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class PostingStatus(str, Enum):
    POSTED = "POSTED"
    PENDING = "PENDING"
    REVERSED = "REVERSED"


class EmploymentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ON_LEAVE = "ON_LEAVE"
    TERMINATED = "TERMINATED"


class AvailabilityStatus(str, Enum):
    ON_SHIFT = "ON_SHIFT"
    OFF_SHIFT = "OFF_SHIFT"
    ON_BREAK = "ON_BREAK"
    ABSENT = "ABSENT"


class SourceSystem(str, Enum):
    ERP = "ERP"
    WMS = "WMS"
    HRMS = "HRMS"


class POLineRecord(_Record):
    source_record_id: Code
    po_number: Code
    line_number: int = Field(gt=0)
    supplier_code: Code
    location_code: Code
    item_code: Code
    ordered_qty: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    ordered_uom: Uom
    status: POStatus
    expected_receipt_by: AwareDatetime


class ReceiptLineRecord(_Record):
    source_record_id: Code
    receipt_number: Code
    receipt_line: int = Field(gt=0)
    po_number: Code
    po_line_number: int = Field(gt=0)
    location_code: Code
    product_ref: Code
    accepted_qty: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    uom: Uom
    received_at: AwareDatetime
    posting_status: PostingStatus


class ProductAliasRecord(_Record):
    source_system: SourceSystem
    external_item_id: Code
    canonical_product_id: Code
    source_uom: Uom
    base_uom: Uom
    to_base_factor: Decimal = Field(gt=0, max_digits=18, decimal_places=6)

    @model_validator(mode="after")
    def _same_unit_means_factor_one(self) -> ProductAliasRecord:
        if self.source_uom == self.base_uom and self.to_base_factor != 1:
            raise ValueError("source_uom equals base_uom, so to_base_factor must be 1")
        return self


class LocationAliasRecord(_Record):
    source_system: SourceSystem
    external_location_id: Code
    canonical_location_id: Code
    region_id: Code


class EmployeeRecord(_Record):
    source_employee_id: Code
    display_name: OptionalCode = None  # kept in the source record only; never copied into business state or logs
    employment_status: EmploymentStatus
    role: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=64)]
    location_code: OptionalCode = None
    manager_employee_id: OptionalCode = None
    region_id: OptionalCode = None

    @model_validator(mode="after")
    def _needs_some_scope(self) -> EmployeeRecord:
        if not self.location_code and not self.region_id:
            raise ValueError("employee needs a location_code or a region_id to have an organisational scope")
        return self


class AvailabilityRecord(_Record):
    source_employee_id: Code
    availability_status: AvailabilityStatus
    shift_start: AwareDatetime
    shift_end: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _shift_order(self) -> AvailabilityRecord:
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self

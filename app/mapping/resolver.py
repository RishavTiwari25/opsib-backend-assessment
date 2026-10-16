"""Canonical identity resolution.

Field mapping (column -> field) lives in the adapters. This module answers a different question:
*which canonical entity does a source identifier refer to, and in which unit?* It never guesses:

* no alias for (source_system, external_id)          -> UNMAPPED_*
* alias blocked by an open source conflict            -> AMBIGUOUS_* (two versions of the truth)
* aliases of one canonical product disagree on base   -> AMBIGUOUS_BASE_UOM
* record unit differs from the alias' declared unit   -> UOM_MISMATCH (we do not convert unknown units)
* aliases of one canonical location disagree on region-> AMBIGUOUS_REGION

The snapshot is immutable and carries a ``version`` (hash of the alias facts it was built from). Every
finding records that version, so a reader can tell which mapping produced it.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.ingestion.records import LocationAliasRecord, ProductAliasRecord


@dataclass(frozen=True)
class AliasFact:
    record_id: int
    source_key: str
    content_hash: str
    version: int


@dataclass(frozen=True)
class ProductResolution:
    ok: bool
    issue: str | None = None
    canonical_product_id: str | None = None
    base_uom: str | None = None
    factor: Decimal | None = None
    alias_record_id: int | None = None

    def to_base(self, qty: Decimal) -> Decimal:
        if not self.ok or self.factor is None:
            raise ValueError("cannot convert a quantity through an unresolved product mapping")
        return qty * self.factor


@dataclass(frozen=True)
class LocationResolution:
    ok: bool
    issue: str | None = None
    canonical_location_id: str | None = None
    region_id: str | None = None
    alias_record_id: int | None = None


@dataclass
class MappingSnapshot:
    products: dict[tuple[str, str], tuple[AliasFact, ProductAliasRecord]] = field(default_factory=dict)
    locations: dict[tuple[str, str], tuple[AliasFact, LocationAliasRecord]] = field(default_factory=dict)
    blocked_product_keys: set[str] = field(default_factory=set)
    blocked_location_keys: set[str] = field(default_factory=set)
    version: str = "map-empty"
    _base_uoms: dict[str, set[str]] = field(default_factory=dict)
    _regions: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        product_aliases: Iterable[tuple[AliasFact, ProductAliasRecord]],
        location_aliases: Iterable[tuple[AliasFact, LocationAliasRecord]],
        *,
        blocked_product_keys: Iterable[str] = (),
        blocked_location_keys: Iterable[str] = (),
    ) -> MappingSnapshot:
        snap = cls(
            blocked_product_keys=set(blocked_product_keys),
            blocked_location_keys=set(blocked_location_keys),
        )
        base_uoms: dict[str, set[str]] = defaultdict(set)
        regions: dict[str, set[str]] = defaultdict(set)
        fingerprint = []
        for fact, rec in product_aliases:
            snap.products[(rec.source_system.value, rec.external_item_id)] = (fact, rec)
            base_uoms[rec.canonical_product_id].add(rec.base_uom)
            fingerprint.append(f"P:{fact.source_key}:{fact.content_hash}:{fact.version}")
        for fact, rec in location_aliases:
            snap.locations[(rec.source_system.value, rec.external_location_id)] = (fact, rec)
            regions[rec.canonical_location_id].add(rec.region_id)
            fingerprint.append(f"L:{fact.source_key}:{fact.content_hash}:{fact.version}")
        fingerprint += [f"BP:{k}" for k in snap.blocked_product_keys] + [f"BL:{k}" for k in snap.blocked_location_keys]
        snap._base_uoms = dict(base_uoms)
        snap._regions = dict(regions)
        digest = hashlib.sha256("\n".join(sorted(fingerprint)).encode()).hexdigest()[:12]
        snap.version = f"map-{digest}"
        return snap

    def resolve_product(self, source_system: str, external_id: str, uom: str) -> ProductResolution:
        key = f"{source_system}|{external_id}"
        if key in self.blocked_product_keys:
            return ProductResolution(False, "AMBIGUOUS_PRODUCT_MAPPING_CONFLICT")
        hit = self.products.get((source_system, external_id))
        if hit is None:
            return ProductResolution(False, "UNMAPPED_PRODUCT")
        fact, rec = hit
        if len(self._base_uoms.get(rec.canonical_product_id, set())) != 1:
            return ProductResolution(False, "AMBIGUOUS_BASE_UOM", alias_record_id=fact.record_id)
        if uom.upper() != rec.source_uom:
            return ProductResolution(False, "UOM_MISMATCH", alias_record_id=fact.record_id)
        return ProductResolution(
            True,
            canonical_product_id=rec.canonical_product_id,
            base_uom=rec.base_uom,
            factor=rec.to_base_factor,
            alias_record_id=fact.record_id,
        )

    def resolve_location(self, source_system: str, external_id: str | None) -> LocationResolution:
        if not external_id:
            return LocationResolution(False, "NO_LOCATION")
        key = f"{source_system}|{external_id}"
        if key in self.blocked_location_keys:
            return LocationResolution(False, "AMBIGUOUS_LOCATION_MAPPING_CONFLICT")
        hit = self.locations.get((source_system, external_id))
        if hit is None:
            return LocationResolution(False, "UNMAPPED_LOCATION")
        fact, rec = hit
        if len(self._regions.get(rec.canonical_location_id, set())) != 1:
            return LocationResolution(False, "AMBIGUOUS_REGION", alias_record_id=fact.record_id)
        return LocationResolution(
            True,
            canonical_location_id=rec.canonical_location_id,
            region_id=rec.region_id,
            alias_record_id=fact.record_id,
        )

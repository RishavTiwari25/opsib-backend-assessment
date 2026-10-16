"""Optional AI investigation summary - a bounded, replaceable helper, never a source of truth.

The detector has already computed every quantity and chosen every assignee. The model only receives a
minimal evidence package (no employee data) and may explain it, list missing context and suggest next
steps. Its output is schema-validated, and every record id it cites must exist in the package. If the
provider is disabled, times out, errors or returns anything invalid, the API returns the deterministic
summary instead and says why. The model cannot change quantities, assignment, status or verification.

``ClaudeSummarizer`` is an adapter sketch for the Anthropic Messages API. It is NOT exercised by the tests
or against the live API in this submission (no key was used); tests use fake providers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.db.models import ExceptionCase


class EvidencePackage(BaseModel):
    case_id: str
    case_type: str
    rule_version: str
    computed: dict[str, str]  # authoritative numbers from the detector, as strings
    records: list[dict[str, Any]]  # each has a "ref_id"
    known: list[str]
    unknown: list[str]

    @property
    def ref_ids(self) -> set[str]:
        return {r["ref_id"] for r in self.records}


class InvestigationSummary(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    missing_context: list[str] = Field(default_factory=list, max_length=10)
    suggested_steps: list[str] = Field(default_factory=list, max_length=10)
    cited_record_ids: list[str] = Field(default_factory=list, max_length=50)


class SummaryProvider(Protocol):
    name: str

    def summarize(self, package: EvidencePackage, *, timeout_s: float) -> dict[str, Any]: ...


class ProviderUnavailable(Exception):
    pass


# Phrases that would assert a cause the evidence cannot support.
_UNSUPPORTED_CLAIMS = re.compile(r"\b(supplier|vendor|carrier)\s+(is|was)\s+(at fault|responsible|to blame)\b|\b(fraud|theft|stole)\b", re.I)


def build_package(exc: ExceptionCase) -> EvidencePackage:
    records = [{"ref_id": exc.evidence["po_line"]["source_record_id"], "kind": "po_line", "dataset": "purchase_order_lines"}]
    for r in exc.evidence.get("receipts_counted", []) + exc.evidence.get("receipts_excluded", []):
        records.append(
            {"ref_id": r["source_record_id"], "kind": "receipt_line", "counted": r["counted"], "excluded_reason": r.get("excluded_reason")}
        )
    return EvidencePackage(
        case_id=str(exc.id),
        case_type=exc.case_type,
        rule_version=exc.rule_version,
        computed={
            "expected_qty": format(exc.expected_qty.normalize(), "f"),
            "received_qty": format(exc.received_qty.normalize(), "f"),
            "variance_qty": format(exc.variance_qty.normalize(), "f"),
            "base_uom": exc.base_uom,
            "product": exc.canonical_product_id,
            "location": exc.canonical_location_id,
        },
        records=records,
        known=exc.explanation.get("known", []),
        unknown=exc.explanation.get("unknown", []),
    )


def validate_output(raw: Any, package: EvidencePackage) -> InvestigationSummary:
    summary = InvestigationSummary.model_validate(raw)
    unknown_refs = set(summary.cited_record_ids) - package.ref_ids
    if unknown_refs:
        raise ValueError(f"cited record ids not in the evidence package: {sorted(unknown_refs)}")
    text = " ".join([summary.summary, *summary.missing_context, *summary.suggested_steps])
    if _UNSUPPORTED_CLAIMS.search(text):
        raise ValueError("summary asserts a cause that the evidence does not support")
    return summary


def deterministic_summary(package: EvidencePackage) -> InvestigationSummary:
    c = package.computed
    return InvestigationSummary(
        summary=(
            f"{c['variance_qty']} {c['base_uom']} of {c['product']} at {c['location']} were not accepted against the PO line "
            f"({c['received_qty']} of {c['expected_qty']} {c['base_uom']}). The cause is not established by the available data."
        ),
        missing_context=package.unknown,
        suggested_steps=[
            "Request the supplier ASN / dispatch note for this PO line.",
            "Check the carrier proof of delivery and dock notes.",
            "Check the WMS for pending or unposted GRNs before contacting the supplier.",
        ],
        cited_record_ids=sorted(package.ref_ids),
    )


@dataclass
class SummaryResult:
    source: str  # "ai:<provider>" or "deterministic"
    summary: InvestigationSummary
    fallback_reason: str | None = None


def summarize(provider: SummaryProvider | None, package: EvidencePackage, *, timeout_s: float) -> SummaryResult:
    if provider is None:
        return SummaryResult("deterministic", deterministic_summary(package), "AI provider disabled")
    try:
        raw = provider.summarize(package, timeout_s=timeout_s)
        return SummaryResult(f"ai:{provider.name}", validate_output(raw, package))
    except (ProviderUnavailable, httpx.HTTPError, TimeoutError) as exc:
        reason = f"provider unavailable: {type(exc).__name__}"
    except (ValidationError, ValueError, TypeError) as exc:
        reason = f"provider output rejected: {str(exc)[:200]}"
    return SummaryResult("deterministic", deterministic_summary(package), reason)


class ClaudeSummarizer:
    """Adapter sketch (untested against the live API). Sends only the evidence package."""

    name = "claude"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def summarize(self, package: EvidencePackage, *, timeout_s: float) -> dict[str, Any]:
        prompt = (
            "You help a retail operations analyst investigate a receipt shortfall. Use ONLY the evidence below. "
            "Do not recompute or change any quantity. Do not assert supplier, carrier or employee fault. "
            "Cite only ref_id values that appear in the evidence. Reply with JSON only, matching: "
            '{"summary": str, "missing_context": [str], "suggested_steps": [str], "cited_record_ids": [str]}.\n\n'
            + package.model_dump_json()
        )
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self._model, "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        text = "".join(block.get("text", "") for block in resp.json().get("content", []) if block.get("type") == "text")
        return json.loads(text)

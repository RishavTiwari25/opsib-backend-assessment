"""The optional AI summary may explain evidence but never becomes a source of truth. These tests use fake
providers; no network call and no API key are involved."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.helpers import LOAD_ORDER, fixture_bytes


class Fake:
    name = "fake"

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.packages = []

    def summarize(self, package, *, timeout_s):
        self.packages.append(package)
        return self.behaviour(package)


def _client_with(settings, provider):
    app = create_app(settings, ai_provider=provider)
    return TestClient(app), app


def _exception_id(tc):
    for ds in LOAD_ORDER:
        tc.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", fixture_bytes(ds), "text/csv")})
    tc.post("/detection/runs")
    return next(e for e in tc.get("/exceptions").json() if e["po_number"] == "PO-501")["exception_id"]


def _timeout(_):
    raise TimeoutError("slow")


@pytest.mark.parametrize(
    ("behaviour", "expected_source", "reason_fragment"),
    [
        (lambda p: {"summary": "8 EA not accepted; cause unknown.", "missing_context": ["ASN"], "suggested_steps": ["Ask for ASN"], "cited_record_ids": ["ERP-PO501-L1"]}, "ai:fake", None),
        (lambda p: {"summary": "x", "cited_record_ids": ["INVENTED-123"]}, "deterministic", "not in the evidence package"),
        (lambda p: {"summary": "The supplier was at fault for the shortage."}, "deterministic", "asserts a cause"),
        (lambda p: {"text": "free-form"}, "deterministic", "output rejected"),
        (_timeout, "deterministic", "unavailable"),
    ],
    ids=["valid", "invented-record", "asserts-fault", "bad-schema", "timeout"],
)
def test_ai_output_is_validated_and_fails_safe(settings, factory, behaviour, expected_source, reason_fragment):
    provider = Fake(behaviour)
    tc, app = _client_with(settings, provider)
    with tc:
        exc_id = _exception_id(tc)
        before = tc.get(f"/exceptions/{exc_id}").json()
        r = tc.get(f"/exceptions/{exc_id}/investigation-summary")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == expected_source
        if reason_fragment:
            assert reason_fragment in body["fallback_reason"]
        assert body["authoritative_numbers"]["variance_qty"] == "8"
        # the model never changes business state
        after = tc.get(f"/exceptions/{exc_id}").json()
        assert (after["variance_qty"], after["status"], after["tasks"]) == (before["variance_qty"], before["status"], before["tasks"])
    app.state.container.engine.dispose()
    package = provider.packages[0]
    assert "HR-" not in package.model_dump_json()  # no employee data leaves the service


def test_disabled_provider_returns_deterministic_summary(client):
    exc_id = _exception_id(client)
    body = client.get(f"/exceptions/{exc_id}/investigation-summary").json()
    assert body["source"] == "deterministic" and body["fallback_reason"] == "AI provider disabled"
    assert set(body["cited_record_ids"]) == {"ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"}

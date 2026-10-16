"""Regenerate docs/openapi.json and docs/API_EXAMPLES.md from a real run against a throwaway PostgreSQL.

    python scripts/generate_api_docs.py

Every example below is an actual request/response captured from the running app, not hand-written.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pgserver  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.core.config import Settings  # noqa: E402

DOCS = ROOT / "docs"
ORDER = ("location_aliases", "product_aliases", "employees", "workforce_status", "purchase_order_lines", "goods_receipt_lines")


def trim(obj, depth=0):
    """Shorten long lists so the examples stay readable."""
    if isinstance(obj, list) and len(obj) > 4 and depth > 0:
        return [trim(x, depth + 1) for x in obj[:3]] + [f"... {len(obj) - 3} more"]
    if isinstance(obj, list):
        return [trim(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in obj.items()}
    return obj


def main() -> None:
    srv = pgserver.get_server(Path(tempfile.mkdtemp(prefix="opsib-docs-pg-")), cleanup_mode="stop")
    url = srv.get_uri().replace("postgresql://", "postgresql+psycopg://")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    app = create_app(Settings(database_url=url, log_level="WARNING"))
    DOCS.mkdir(exist_ok=True)
    (DOCS / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))

    sections: list[str] = []

    def show(title, method, path, *, headers=None, body=None, files=None, params=None, note=None):
        with TestClient(app) as tc:
            r = tc.request(method, path, headers=headers, json=body, files=files, params=params)
        req = f"{method} {path}"
        if params:
            req += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        lines = [f"### {title}", "", "```http", req]
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        if files:
            lines.append("Content-Type: multipart/form-data  (file=" + files["file"][0] + ")")
        if body is not None:
            lines += ["", json.dumps(body, indent=2)]
        lines += ["```", "", f"Response `{r.status_code}`" + (f" ({note})" if note else ""), "", "```json", json.dumps(trim(r.json()), indent=2, default=str), "```", ""]
        sections.append("\n".join(lines))
        return r.json()

    for ds in ORDER[:-1]:
        with TestClient(app) as tc:
            tc.post(f"/imports/{ds}", files={"file": (f"{ds}.csv", (ROOT / "fixtures" / f"{ds}.csv").read_bytes(), "text/csv")})
    grn = (ROOT / "fixtures" / "goods_receipt_lines.csv").read_bytes()
    imp = show("Import a dataset (duplicate row reported, not double-counted)", "POST", "/imports/goods_receipt_lines", files={"file": ("goods_receipt_lines.csv", grn, "text/csv")})
    show("Re-import the same file (idempotent)", "POST", "/imports/goods_receipt_lines", files={"file": ("goods_receipt_lines.csv", grn, "text/csv")})
    bad = grn + b"WMS-BAD-1,GRN-900,1,PO-501,1,WH-MH-014,WMS-BRC-001,5,EA,2026-10-15T10:00:00,POSTED\n"
    show("Row-level validation errors (partial success is explicit)", "POST", "/imports/goods_receipt_lines", files={"file": ("grn_with_bad_row.csv", bad, "text/csv")})
    show("Import detail filtered by outcome", "GET", f"/imports/{imp['import_id']}", params={"outcome": "DUPLICATE_IN_FILE"})
    run = show("Run detection at the frozen evaluation time", "POST", "/detection/runs")
    exc_id = next(e["exception_id"] for e in run["evaluations"] if e["po_number"] == "PO-505")
    show("List exceptions", "GET", "/exceptions")
    show("Exception with evidence, source records, tasks and events", "GET", f"/exceptions/{exc_id}")
    show("Data quality", "GET", "/data-quality")
    tasks = show("Tasks for an assignee", "GET", "/tasks", params={"assignee_id": "HR-102"})
    tid = tasks[0]["task_id"]
    show("Invalid transition is refused", "POST", f"/tasks/{tid}/complete", headers={"X-Actor-Id": "HR-102"}, body={"resolution_code": "COUNT_ERROR_CORRECTED", "evidence_ref": "cycle-count-55"})
    show("Acknowledge", "POST", f"/tasks/{tid}/acknowledge", headers={"X-Actor-Id": "HR-102", "Idempotency-Key": "ack-1"})
    show("Start", "POST", f"/tasks/{tid}/start", headers={"X-Actor-Id": "HR-102"})
    complete = {"resolution_code": "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER", "evidence_ref": "ASN-7781", "note": "supplier confirmed short dispatch"}
    show("Complete (moves to PENDING_VERIFICATION)", "POST", f"/tasks/{tid}/complete", headers={"X-Actor-Id": "HR-102", "Idempotency-Key": "done-1"}, body=complete)
    show("Client retry after a timeout: same key returns the stored response", "POST", f"/tasks/{tid}/complete", headers={"X-Actor-Id": "HR-102", "Idempotency-Key": "done-1"}, body=complete, note="header Idempotent-Replayed: true")
    show("Verification by the person who did the work is refused", "POST", f"/tasks/{tid}/verify", headers={"X-Actor-Id": "HR-102"}, body={})
    show("Verify by the store manager", "POST", f"/tasks/{tid}/verify", headers={"X-Actor-Id": "HR-201"}, body={"note": "ASN checked"})
    other = tasks[1]["task_id"]
    show("Reassign to an ineligible employee is refused with reasons", "POST", f"/tasks/{other}/reassign", headers={"X-Actor-Id": "HR-201"}, body={"assignee_employee_id": "HR-103", "reason": "cover"})
    show("Reassign with a reason", "POST", f"/tasks/{other}/reassign", headers={"X-Actor-Id": "HR-201"}, body={"assignee_employee_id": "HR-201", "reason": "HR-102 on dock audit"})
    show("Task history (assignments and events)", "GET", f"/tasks/{other}")
    show("Investigation summary (AI disabled: deterministic fallback)", "GET", f"/exceptions/{exc_id}/investigation-summary")

    header = (
        "# API examples\n\nCaptured from a real run by `scripts/generate_api_docs.py` (fixture pack, frozen time "
        "2026-10-16T15:00:00+05:30). Long lists are shortened with `... n more`. Timestamps are returned in UTC.\n"
        "The full contract is in [openapi.json](openapi.json); with the server running it is also served at `/docs`.\n\n"
    )
    (DOCS / "API_EXAMPLES.md").write_text(header + "\n".join(sections), encoding="utf-8")
    app.state.container.engine.dispose()
    srv.cleanup()
    print("wrote docs/openapi.json and docs/API_EXAMPLES.md")


if __name__ == "__main__":
    main()

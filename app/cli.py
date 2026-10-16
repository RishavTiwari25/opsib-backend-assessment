"""Command line entry points.

    python -m app.cli load fixtures/        # import all six datasets in dependency order
    python -m app.cli detect                # run the receipt-shortfall detector at the configured evaluation time
    python -m app.cli demo fixtures/        # load + detect + print exceptions and tasks
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.api.deps import build_container
from app.api.serializers import exception_brief, import_summary, task_view
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import ExceptionCase, Task
from app.detection.service import run_detection
from app.ingestion.service import import_dataset

LOAD_ORDER = ("location_aliases", "product_aliases", "employees", "workforce_status", "purchase_order_lines", "goods_receipt_lines")


def load(directory: Path) -> list[dict]:
    c = build_container(get_settings())
    out = []
    for dataset in LOAD_ORDER:
        path = directory / f"{dataset}.csv"
        batch = import_dataset(c.factory, tenant_id=c.tenant_id, dataset=dataset, filename=path.name, content=path.read_bytes())
        out.append(import_summary(batch))
    return out


def detect() -> dict:
    c = build_container(get_settings())
    run = run_detection(c.factory, tenant_id=c.tenant_id, clock=c.clock, config=c.detection_config())
    return {"run_id": str(run.id), "as_of": run.as_of.isoformat(), "summary": run.summary}


def snapshot() -> dict:
    c = build_container(get_settings())
    with c.factory() as s:
        return {
            "exceptions": [exception_brief(e) for e in s.execute(select(ExceptionCase).order_by(ExceptionCase.po_number)).scalars()],
            "tasks": [task_view(t) for t in s.execute(select(Task).order_by(Task.canonical_location_id, Task.id)).scalars()],
        }


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING")
    parser = argparse.ArgumentParser(prog="opsib")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_load = sub.add_parser("load")
    p_load.add_argument("directory", type=Path)
    sub.add_parser("detect")
    p_demo = sub.add_parser("demo")
    p_demo.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "load":
        result: object = load(args.directory)
    elif args.cmd == "detect":
        result = detect()
    else:
        result = {"imports": load(args.directory), "detection": detect(), **snapshot()}
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

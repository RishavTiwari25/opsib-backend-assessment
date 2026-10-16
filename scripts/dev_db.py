"""Start a local PostgreSQL 16 without Docker (embedded binaries from the `pgserver` package).

    python scripts/dev_db.py            # starts the server and writes OPSIB_DATABASE_URL into ./.env
    python scripts/dev_db.py --stop     # stops it

Data lives in ./.pgdata. The server keeps running after this script exits.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".pgdata"
ENV = ROOT / ".env"


def stop() -> None:
    pg_ctl = Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_ctl"
    subprocess.run([str(pg_ctl), "stop", "-D", str(DATA), "-m", "fast"], check=False)


def start() -> str:
    srv = pgserver.get_server(DATA, cleanup_mode=None)
    if "1 row" not in srv.psql("select 1 from pg_database where datname = 'opsib';"):
        srv.psql("create database opsib;")
    uri = srv.get_uri().replace("postgresql://", "postgresql+psycopg://").rsplit("/", 1)[0] + "/opsib"
    # The embedded server picks a free port on each start, so keep .env in step with it.
    kept = [line for line in (ENV.read_text().splitlines() if ENV.exists() else []) if not line.startswith("OPSIB_DATABASE_URL=")]
    ENV.write_text("\n".join([f"OPSIB_DATABASE_URL={uri}", *kept]) + "\n")
    return uri


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop()
    else:
        print(start())

"""Test infrastructure.

Tests run against real PostgreSQL, because the guarantees under test (unique constraints, ON CONFLICT,
advisory locks, row locks, partial unique indexes) are database behaviour. By default an embedded
PostgreSQL 16 is started in a temporary directory via `pgserver`; set OPSIB_TEST_DATABASE_URL to use your own
(e.g. the docker-compose service). The schema is created with the Alembic migration, not create_all, so the
migration itself is tested.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.models import Base
from app.db.session import make_engine, make_session_factory

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
FROZEN = datetime.fromisoformat("2026-10-16T15:00:00+05:30")
TENANT = "test-retailer"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.getenv("OPSIB_TEST_DATABASE_URL")
    server = None
    if not url:
        import pgserver

        server = pgserver.get_server(Path(tempfile.mkdtemp(prefix="opsib-test-pg-")), cleanup_mode="stop")
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("drop schema public cascade; create schema public;"))
    engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    yield url
    if server is not None:
        server.cleanup()


@pytest.fixture(scope="session")
def engine(database_url: str):
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine) -> Iterator[sessionmaker[Session]]:
    yield make_session_factory(engine)
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"truncate {tables} restart identity cascade"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FROZEN)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, tenant_id=TENANT, evaluation_time=FROZEN.isoformat(), log_level="WARNING")


@pytest.fixture
def client(settings: Settings, factory) -> Iterator[TestClient]:
    from app.api.main import create_app

    app = create_app(settings)
    with TestClient(app) as tc:
        yield tc
    app.state.container.engine.dispose()

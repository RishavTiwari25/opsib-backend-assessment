"""Engine and session factory."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def advisory_lock_key(*parts: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def take_xact_lock(session: Session, *parts: str) -> None:
    """Serialise writers for one scope until the current transaction ends."""
    session.execute(text("select pg_advisory_xact_lock(:k)"), {"k": advisory_lock_key(*parts)})


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()

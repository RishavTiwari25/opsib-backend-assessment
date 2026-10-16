"""Idempotency-Key support for task actions.

Design question: the API commits a completion but the client times out and retries.

* With an ``Idempotency-Key`` header: the response is stored in ``idempotency_record`` in the SAME
  transaction as the state change. A retry with the same key and the same request finds the record and gets
  the original response back (header ``Idempotent-Replayed: true``); nothing is applied twice. The same key
  with a different request body or path is rejected with 409, because it is a client bug, not a retry.
  Two concurrent requests with one key are serialised by the task row lock; if they target different tasks,
  the unique constraint on (tenant, key) makes the second insert fail and that request is answered from
  the first one's record.
* Without a key: completion is also semantically idempotent. Re-sending the same completion (same actor,
  resolution code and evidence reference) to a task that is already completed returns 200 with the current
  state and ``replayed: true``; a different completion returns 409 ALREADY_COMPLETED. Either way the client
  learns the true state and no second event is written.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.db.models import IdempotencyRecord


def fingerprint(method: str, path: str, actor: str, body: dict[str, Any] | None) -> str:
    canonical = json.dumps({"m": method, "p": path, "a": actor, "b": body or {}}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def find(session: Session, tenant_id: str, key: str, fp: str) -> IdempotencyRecord | None:
    rec = session.execute(
        select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == tenant_id, IdempotencyRecord.idempotency_key == key)
    ).scalar_one_or_none()
    if rec is not None and rec.request_fingerprint != fp:
        raise Conflict(
            "Idempotency-Key was already used for a different request",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return rec


def save(session: Session, tenant_id: str, key: str, fp: str, status: int, body: dict[str, Any]) -> None:
    session.add(
        IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=key,
            request_fingerprint=fp,
            response_status=status,
            response_body=jsonable_encoder(body),
        )
    )

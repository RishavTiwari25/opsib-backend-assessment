"""Clock abstraction. Business rules take the evaluation time from here, never from datetime.now()."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def clock_from_setting(value: str) -> Clock:
    if value.strip().lower() == "system":
        return SystemClock()
    return FrozenClock(datetime.fromisoformat(value))

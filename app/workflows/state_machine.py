"""Task lifecycle. The server owns every transition; clients call actions, never set a status.

    CREATED --assign--> ASSIGNED --acknowledge--> ACKNOWLEDGED --start--> IN_PROGRESS
    IN_PROGRESS --block--> BLOCKED --unblock--> IN_PROGRESS
    IN_PROGRESS --complete--> COMPLETED --(system, same transaction)--> PENDING_VERIFICATION
    PENDING_VERIFICATION --verify--> VERIFIED                 (terminal)
    PENDING_VERIFICATION --reject--> REOPENED --start--> IN_PROGRESS
    {CREATED, ASSIGNED, ACKNOWLEDGED, IN_PROGRESS, BLOCKED, REOPENED} --reassign--> ASSIGNED
    any non-terminal --cancel (system only)--> CANCELLED      (terminal)

Reassignment always lands in ASSIGNED so the new owner has to acknowledge the work themselves.
"""
from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    ASSIGN = "assign"
    ACKNOWLEDGE = "acknowledge"
    START = "start"
    BLOCK = "block"
    UNBLOCK = "unblock"
    COMPLETE = "complete"
    SUBMIT_FOR_VERIFICATION = "submit_for_verification"
    VERIFY = "verify"
    REJECT = "reject"
    REASSIGN = "reassign"
    CANCEL = "cancel"


TERMINAL = frozenset({"VERIFIED", "CANCELLED"})
REASSIGNABLE = frozenset({"CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS", "BLOCKED", "REOPENED"})

TRANSITIONS: dict[tuple[str, Action], str] = {
    ("CREATED", Action.ASSIGN): "ASSIGNED",
    ("ASSIGNED", Action.ACKNOWLEDGE): "ACKNOWLEDGED",
    ("ACKNOWLEDGED", Action.START): "IN_PROGRESS",
    ("REOPENED", Action.START): "IN_PROGRESS",
    ("IN_PROGRESS", Action.BLOCK): "BLOCKED",
    ("BLOCKED", Action.UNBLOCK): "IN_PROGRESS",
    ("IN_PROGRESS", Action.COMPLETE): "COMPLETED",
    ("COMPLETED", Action.SUBMIT_FOR_VERIFICATION): "PENDING_VERIFICATION",
    ("PENDING_VERIFICATION", Action.VERIFY): "VERIFIED",
    ("PENDING_VERIFICATION", Action.REJECT): "REOPENED",
    **{(s, Action.REASSIGN): "ASSIGNED" for s in REASSIGNABLE},
    **{
        (s, Action.CANCEL): "CANCELLED"
        for s in ("CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS", "BLOCKED", "COMPLETED", "PENDING_VERIFICATION", "REOPENED")
    },
}


def next_status(current: str, action: Action) -> str | None:
    return TRANSITIONS.get((current, action))


def allowed_actions(current: str) -> list[str]:
    return sorted(a.value for (s, a) in TRANSITIONS if s == current and a not in (Action.CANCEL, Action.SUBMIT_FOR_VERIFICATION, Action.ASSIGN))


class ResolutionCode(str, Enum):
    """Neutral outcomes. None of them asserts fault; each needs an evidence reference."""

    RECEIPT_POSTING_CORRECTED = "RECEIPT_POSTING_CORRECTED"
    SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER = "SHORT_DELIVERY_CONFIRMED_BY_SUPPLIER"
    CARRIER_DISCREPANCY_RECORDED = "CARRIER_DISCREPANCY_RECORDED"
    DAMAGED_GOODS_REJECTED_AT_DOCK = "DAMAGED_GOODS_REJECTED_AT_DOCK"
    COUNT_ERROR_CORRECTED = "COUNT_ERROR_CORRECTED"
    BACKORDER_AGREED = "BACKORDER_AGREED"
    CAUSE_UNDETERMINED_ESCALATED = "CAUSE_UNDETERMINED_ESCALATED"

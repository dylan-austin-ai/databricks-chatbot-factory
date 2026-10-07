"""Bot lifecycle state machine (LCY-1..7).

    draft -> provisioning -> testing -> pending_approval -> live
    live <-> paused ; live/paused/testing -> archived -> deleted (soft)

Going live needs a passing eval gate (LCY-3, EVG-1) and Reviewer approval (LCY-4).
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    PROVISION_FAILED = "provision_failed"
    TESTING = "testing"
    PENDING_APPROVAL = "pending_approval"
    LIVE = "live"
    PAUSED = "paused"
    BUDGET_PAUSED = "budget_paused"     # CST-8: owner chose "pause at budget"; resumes on the 1st
    ARCHIVED = "archived"
    DELETED = "deleted"                 # soft delete: stops serving, data kept for the retention window
    PURGED = "purged"                   # data removed; audit, logs and traces kept


TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT: {State.PROVISIONING, State.DELETED},
    State.PROVISIONING: {State.TESTING, State.PROVISION_FAILED},
    State.PROVISION_FAILED: {State.PROVISIONING, State.DELETED},
    State.TESTING: {State.PENDING_APPROVAL, State.PROVISIONING, State.ARCHIVED},
    State.PENDING_APPROVAL: {State.LIVE, State.TESTING},
    State.LIVE: {State.PAUSED, State.BUDGET_PAUSED, State.ARCHIVED, State.TESTING},  # TESTING: first smoke test failed
    State.PAUSED: {State.LIVE, State.ARCHIVED},
    State.BUDGET_PAUSED: {State.LIVE, State.PAUSED, State.ARCHIVED},
    State.ARCHIVED: {State.TESTING, State.DELETED},
    State.DELETED: {State.ARCHIVED, State.PURGED},   # restorable until purged
    State.PURGED: set(),
}

# Bot changes after go-live don't change the bot's state: they are staged as a
# candidate release, tested, approved and promoted while the live release keeps serving.


class TransitionError(ValueError):
    pass


def check_transition(current: str, target: str) -> None:
    """Go-live quality is enforced by the eval gate (EVG-1) and the promote job; this only
    guards the state machine."""
    if State(target) not in TRANSITIONS[State(current)]:
        raise TransitionError(f"A chatbot can't move from {current} to {target}.")

"""Who owns the live browser session, right now.

Exactly one owner at a time. Automation holds the lease with an epoch number; every automated
action checks it still holds the lease *at that epoch* before touching the page. Handing control
to a human and getting it back bumps the epoch, so nothing captured before the handoff can act
after it.

    AUTOMATION ─request─▶ AWAITING_HUMAN ─take─▶ HUMAN ─release─▶ VERIFYING ─verified─▶ AUTOMATION
         │                      │                  │                  │
         └──────────────────────┴─────abort────────┴──────────────────┴──▶ ABORTED
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum


class Owner(StrEnum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    VERIFYING = "verifying"
    ABORTED = "aborted"


_ALLOWED = {
    Owner.AUTOMATION: {Owner.AWAITING_HUMAN, Owner.ABORTED},
    # → verifying: released without ever taking control; → automation: request withdrawn
    Owner.AWAITING_HUMAN: {Owner.HUMAN, Owner.VERIFYING, Owner.ABORTED, Owner.AUTOMATION},
    Owner.HUMAN: {Owner.VERIFYING, Owner.ABORTED},
    Owner.VERIFYING: {Owner.AUTOMATION, Owner.AWAITING_HUMAN, Owner.ABORTED},
    Owner.ABORTED: set(),
}


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseEvent:
    at: str
    frm: Owner
    to: Owner
    by: str
    reason: str
    epoch: int


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")


@dataclass
class Lease:
    owner: Owner = Owner.AUTOMATION
    epoch: int = 1
    holder: str = "automation"
    log: list[LeaseEvent] = field(default_factory=list)

    def transition(self, to: Owner, by: str, reason: str) -> int:
        if to not in _ALLOWED[self.owner]:
            raise LeaseError(f"illegal lease transition {self.owner} → {to}")
        if to == Owner.AUTOMATION:
            self.epoch += 1
        self.log.append(LeaseEvent(_now(), self.owner, to, by, reason, self.epoch))
        self.owner, self.holder = to, by
        return self.epoch

    def assert_automation(self, epoch: int) -> None:
        """Called before every automated action."""
        if self.owner != Owner.AUTOMATION or self.epoch != epoch:
            raise LeaseError(
                f"automation does not hold the session (owner={self.owner}, epoch={self.epoch}, mine={epoch})")

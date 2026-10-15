"""Pure logic to confirm that a command reached the car, using the car's reported state.

After a command, the car (via the ESP32) should report the same charging on/off and amps.
If it does not within `timeout_s`, re-send; after `max_attempts` sends, trust the car.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

from .controller import Command


@dataclass(frozen=True)
class CarState:
    charging: bool
    amps: int


@dataclass(frozen=True)
class Pending:
    cmd: Command
    sent_at: float
    attempts: int = 1


class Action(Enum):
    WAIT = "wait"          # not confirmed yet, still within the timeout
    CONFIRMED = "confirmed"
    RESEND = "resend"
    ADOPT = "adopt"        # give up re-sending: the car's reported state becomes the truth
    UNKNOWN = "unknown"    # give up, and the car reports nothing to adopt


def matches(cmd: Command, state: CarState) -> bool:
    # Amps only matter while charging: a stopped car keeps its last setpoint.
    return state.charging == cmd.charging and (not cmd.charging or state.amps == cmd.amps)


def reconcile(pending: Pending, observed: Optional[CarState], now: float,
              timeout_s: float, max_attempts: int) -> tuple[Action, Optional[Pending]]:
    """Next step for an unconfirmed command, and what stays pending afterwards."""
    if observed is not None and matches(pending.cmd, observed):
        return Action.CONFIRMED, None
    if now - pending.sent_at < timeout_s:
        return Action.WAIT, pending
    if pending.attempts < max_attempts:
        return Action.RESEND, replace(pending, sent_at=now, attempts=pending.attempts + 1)
    if observed is None:
        return Action.UNKNOWN, None
    return Action.ADOPT, None

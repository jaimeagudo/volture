"""Pure logic about the car's reported state: did it obey, did someone else change it, can it charge.

After a command, the car (via the ESP32) should report the same charging on/off and amps.
If it does not within `timeout_s`, re-send; after `max_attempts` sends, trust the car.
A change that is not what Volture asked for came from outside (Tesla app, the car itself):
the Tesla app has priority, see Controller.follow_user.
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
    plugged: Optional[bool] = None        # charge cable connected; None: not reported
    battery_pct: Optional[float] = None   # state of charge
    limit_pct: Optional[float] = None     # the car's charge limit (set in the car / Tesla app)


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


def same_drive(a: CarState, b: CarState) -> bool:
    """Same charging on/off and, while charging, same amps (battery % etc. ignored)."""
    return a.charging == b.charging and (not a.charging or a.amps == b.amps)


def external_change(pending: Optional["Pending"], last: Optional[CarState],
                    observed: Optional[CarState]) -> bool:
    """The car changed, and not to what Volture asked for: someone else changed it."""
    if observed is None or last is None or same_drive(observed, last):
        return False
    return pending is None or not matches(pending.cmd, observed)


def can_charge(state: Optional[CarState]) -> bool:
    """False when starting is pointless: cable unplugged or battery at its charge limit.

    Unknown values do not block: the car itself refuses what it cannot do.
    """
    if state is None:
        return True
    if state.plugged is False:
        return False
    if state.battery_pct is not None and state.limit_pct is not None:
        return state.battery_pct < state.limit_pct
    return True


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

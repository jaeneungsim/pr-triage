"""AI spending limits, counted by the app itself from the token usage of each call.

AWS Budgets updates a few times a day, so it is only the backstop. These limits act before
the call: reserve the worst case, call, then settle with the real cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .params import Config, Limits, Model


class UsageStore(Protocol):
    def reserve_daily(self, day: str, limit: int) -> bool: ...
    def release_daily(self, day: str) -> None: ...
    def reserve_monthly(self, month: str, amount_micro: int, threshold_micro: int) -> bool: ...
    def settle_monthly(self, month: str, reserved_micro: int, actual_micro: int, *, called: bool) -> None: ...


@dataclass(frozen=True)
class Reservation:
    day: str
    month: str
    reserved_micro: int


def cost_micro(model: Model, input_tokens: int, output_tokens: int) -> int:
    """USD per million tokens equals micro-dollars per token."""
    return math.ceil(input_tokens * model.input_usd_per_mtok + output_tokens * model.output_usd_per_mtok)


def max_call_micro(model: Model, limits: Limits) -> int:
    return cost_micro(model, limits.max_input_tokens, limits.max_output_tokens)


def reserve(usage: UsageStore, cfg: Config, now: datetime) -> tuple[Reservation | None, str | None]:
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if not usage.reserve_daily(day, cfg.limits.daily_reviews):
        return None, f"the daily limit of {cfg.limits.daily_reviews} AI reviews is reached"
    amount = max_call_micro(cfg.model, cfg.limits)
    threshold = int(cfg.limits.monthly_usd * 1_000_000) - amount
    if threshold < 0 or not usage.reserve_monthly(month, amount, threshold):
        usage.release_daily(day)
        return None, f"the monthly AI budget of ${cfg.limits.monthly_usd:.2f} is reached"
    return Reservation(day, month, amount), None


def settle(usage: UsageStore, reservation: Reservation, actual_micro: int) -> None:
    usage.settle_monthly(reservation.month, reservation.reserved_micro, actual_micro, called=True)


def release(usage: UsageStore, reservation: Reservation) -> None:
    usage.settle_monthly(reservation.month, reservation.reserved_micro, 0, called=False)
    usage.release_daily(reservation.day)

from datetime import UTC, datetime

from pr_triage import caps
from pr_triage.params import Config, Limits, Model

MODEL = Model(id="m", input_usd_per_mtok=1.0, output_usd_per_mtok=5.0)


class FakeUsage:
    def __init__(self):
        self.daily: dict[str, int] = {}
        self.committed: dict[str, int] = {}
        self.spent: dict[str, int] = {}

    def reserve_daily(self, day, limit):
        if self.daily.get(day, 0) >= limit:
            return False
        self.daily[day] = self.daily.get(day, 0) + 1
        return True

    def release_daily(self, day):
        self.daily[day] -= 1

    def reserve_monthly(self, month, amount_micro, threshold_micro):
        if self.committed.get(month, 0) > threshold_micro:
            return False
        self.committed[month] = self.committed.get(month, 0) + amount_micro
        return True

    def settle_monthly(self, month, reserved_micro, actual_micro, *, called):
        self.committed[month] += actual_micro - reserved_micro
        self.spent[month] = self.spent.get(month, 0) + actual_micro


def config(**limits):
    return Config(True, "v3", "advisory", Limits(**limits), MODEL, ("me",))


NOW = datetime(2026, 10, 11, 9, tzinfo=UTC)


def test_cost_is_micro_dollars():
    assert caps.cost_micro(MODEL, 12_000, 1_500) == 19_500  # $0.0195
    assert caps.max_call_micro(MODEL, Limits()) == 45_000


def test_daily_limit():
    usage = FakeUsage()
    cfg = config(daily_reviews=2)
    assert caps.reserve(usage, cfg, NOW)[0] is not None
    assert caps.reserve(usage, cfg, NOW)[0] is not None
    reservation, reason = caps.reserve(usage, cfg, NOW)
    assert reservation is None and "daily limit of 2" in reason


def test_monthly_limit_reserves_the_worst_case_then_settles():
    usage = FakeUsage()
    cfg = config(monthly_usd=0.10)  # room for two worst-case calls of $0.045
    first, _ = caps.reserve(usage, cfg, NOW)
    caps.settle(usage, first, 20_000)
    assert usage.committed["2026-10"] == 20_000
    second, _ = caps.reserve(usage, cfg, NOW)
    third, reason = caps.reserve(usage, cfg, NOW)
    assert second is not None and third is None and "monthly AI budget" in reason
    assert usage.daily["2026-10-11"] == 2  # the refused call gave its daily slot back


def test_release_returns_everything():
    usage = FakeUsage()
    reservation, _ = caps.reserve(usage, config(), NOW)
    caps.release(usage, reservation)
    assert usage.committed["2026-10"] == 0 and usage.daily["2026-10-11"] == 0

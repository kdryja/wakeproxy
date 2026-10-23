"""Fakes for the tests: a clock, a machine, and plugins that drive it."""
import asyncio

from wakeproxy import config
from wakeproxy.core import Controller
from wakeproxy.history import History
from wakeproxy.plugins import Check, PowerOff, PowerOn, Source


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Machine:
    """A pretend machine: on/off, and a record of what was done to it."""

    def __init__(self, clock, boot_seconds=60, lost=0):
        self.clock, self.boot_seconds, self.lost = clock, boot_seconds, lost
        self.on, self.booting_since, self.calls = False, None, []

    def wake(self):
        """`lost` wakes never arrive; one that arrives while booting or on changes nothing."""
        self.calls.append("wake")
        if self.lost:
            self.lost -= 1
        elif self.booting_since is None and not self.on:
            self.booting_since = self.clock()

    def up(self):
        if self.booting_since is not None and self.clock() >= self.booting_since + self.boot_seconds:
            self.on, self.booting_since = True, None
        return self.on


MACHINES = {}


class FakeWake(PowerOn):
    type = "fake"

    def __init__(self, ctx, *, machine="m", fail=False, fail_first=0, repeat_safe=False):
        super().__init__(ctx)
        self.machine, self.fail, self.fail_first = MACHINES[machine], fail, fail_first
        self.repeat_safe = repeat_safe

    async def power_on(self):
        if self.fail or self.fail_first:
            self.fail_first = max(0, self.fail_first - 1)
            raise RuntimeError("no route to host")
        self.machine.wake()


class FakeShutdown(PowerOff):
    type = "fake"

    def __init__(self, ctx, *, machine="m"):
        super().__init__(ctx)
        self.machine = MACHINES[machine]

    async def power_off(self, force):
        self.machine.calls.append("shutdown-force" if force else "shutdown")
        self.machine.on = False


class FakeCheck(Check):
    """Each check moves the clock a little, so waits on it always end."""
    type = "fake"

    def __init__(self, ctx, *, machine="m", step=5):
        super().__init__(ctx)
        self.machine, self.step = MACHINES[machine], step

    async def check(self):
        self.machine.clock.advance(self.step)
        return self.machine.up()


class Objector(Source):
    """Holds off the shutdown once, without grace (like windows_session)."""
    type = "objector"
    wakes = False

    def __init__(self, ctx, *, times=1):
        super().__init__(ctx)
        self.times = times
        self.asked = 0

    async def before_shutdown(self):
        self.asked += 1
        if self.asked <= self.times:
            self.force_shutdown = True
            self.ctx.hold("busy", "someone is there", until=self.ctx.now() + 120, grace=False)


def make(extra=None, clock=None, boot_seconds=60, history=None, lost=0, **machine):
    """A Controller on fake plugins, with its clock and machine."""
    clock = clock or Clock()
    MACHINES["m"] = m = Machine(clock, boot_seconds, lost)
    data = {
        "machine": {"name": "TestPC", "idle_minutes": 10, "wake_timeout": 180,
                    "wake": {"type": "helpers:FakeWake", **machine.pop("wake", {})},
                    "shutdown": {"type": "helpers:FakeShutdown"},
                    "up_check": [{"type": "helpers:FakeCheck"}], **machine},
        "history": {"path": ":memory:"},
    }
    for k, v in (extra or {}).items():
        data[k] = v
    cfg = config.parse(data)
    ctl = Controller(cfg, history or History(":memory:", clock), clock)
    ctl.build_plugins()
    ctl.lock = asyncio.Lock()
    ctl.wake_poll = 0
    ctl.wake_gap = 0
    return ctl, clock, m


def kinds(ctl):
    return [e["kind"] for e in reversed(ctl.history.events(0))]

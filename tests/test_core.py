"""Holds, the idle timer, waking and shutting down, with a fake clock and machine."""
import asyncio

from helpers import kinds, make
from wakeproxy.history import History


def run(coro):
    return asyncio.run(coro)


async def boot(ctl, m):
    """Wake the machine and let the monitor see it on."""
    assert await ctl.wake({"client": "test"})
    await ctl.monitor_tick(0)
    assert ctl.state["pc_up"]


def test_wake_marks_the_machine_as_managed():
    ctl, clock, m = make()

    async def go():
        await ctl.monitor_tick(0)                   # first look: off
        trigger = {"client": "test", "path": "/x"}
        assert await ctl.wake(trigger)
        assert trigger["woke"] == 1 and m.calls == ["wake"]
        assert ctl.state["woken_by_me"]
        await ctl.monitor_tick(0)
    run(go())
    assert kinds(ctl) == ["proxy_start", "wake", "wake_ok", "pc_on"]
    assert ctl.history.events(0)[0]["by"] == "wakeproxy"


def test_no_wake_when_already_up():
    ctl, clock, m = make()
    m.on = True
    assert run(ctl.wake({"client": "test"}))
    assert m.calls == [] and not ctl.state["woken_by_me"]


def test_wake_that_never_answers_fails():
    ctl, clock, m = make(boot_seconds=10_000)
    assert not run(ctl.wake({"client": "test"}))
    assert kinds(ctl)[-1] == "wake_failed"
    assert ctl.state["waking"] is None


def test_wake_plugin_error_is_recorded():
    ctl, clock, m = make(boot_seconds=10_000, wake={"fail": True})
    assert not run(ctl.wake({"client": "test"}))
    wake = [e for e in ctl.history.events(0) if e["kind"] == "wake"][0]
    assert wake["wol_sent"] is False and "no route to host" in wake["error"]


def test_repeat_safe_wake_is_sent_three_times():
    ctl, clock, m = make(wake={"repeat_safe": True}, boot_seconds=20)   # up before the 30 s resend
    assert run(ctl.wake({"client": "test"}))
    assert m.calls == ["wake"] * 3
    wake, ok = [e for e in ctl.history.events(0) if e["kind"] in ("wake", "wake_ok")][::-1]
    assert wake["packets"] == 3 and wake["wol_sent"] is True and ok["packets"] == 3


def test_lost_packets_are_sent_again_while_waiting():
    # The three packets at the start are lost; the one sent 30 s later wakes the machine.
    ctl, clock, m = make(wake={"repeat_safe": True}, lost=3, boot_seconds=20)
    assert run(ctl.wake({"client": "test"}))
    assert m.calls == ["wake"] * 4
    ok = [e for e in ctl.history.events(0) if e["kind"] == "wake_ok"][0]
    assert ok["packets"] == 4 and ok["seconds"] < 180


def test_wake_that_is_not_repeat_safe_is_sent_once():
    # A smart plug toggle or a script: one send, never repeated, even if nothing happens.
    ctl, clock, m = make(lost=1)
    assert not run(ctl.wake({"client": "test"}))
    assert m.calls == ["wake"]


def test_repeats_can_be_set_for_any_wake():
    ctl, clock, m = make(wake_packets=2, wake_resend=0, lost=2)
    assert not run(ctl.wake({"client": "test"}))
    assert m.calls == ["wake"] * 2


def test_resend_can_be_switched_off_for_a_repeat_safe_wake():
    ctl, clock, m = make(wake={"repeat_safe": True}, wake_resend=0, lost=3)
    assert not run(ctl.wake({"client": "test"}))
    assert m.calls == ["wake"] * 3


def test_one_send_that_works_is_enough():
    ctl, clock, m = make(wake={"repeat_safe": True, "fail_first": 2})
    assert run(ctl.wake({"client": "test"}))
    wake = [e for e in ctl.history.events(0) if e["kind"] == "wake"][0]
    assert wake["wol_sent"] is True and wake["error"] is None and wake["packets"] == 1


def test_idle_shutdown_after_the_timer():
    ctl, clock, m = make()

    async def go():
        await boot(ctl, m)
        await ctl.watchdog_tick()
        assert "shutdown" not in m.calls
        clock.advance(ctl.cfg.idle_seconds + 1)
        await ctl.watchdog_tick()
    run(go())
    assert m.calls[-1] == "shutdown"
    assert ctl.history.events(0)[0]["reason"] == "idle for 10 minutes"
    assert not ctl.state["woken_by_me"] and ctl.state["shutting_down"]


def test_a_hold_keeps_it_on_and_its_end_restarts_the_timer():
    ctl, clock, m = make()

    async def go():
        await boot(ctl, m)
        ctl.hold("job", 1, "transcoding")
        clock.advance(3 * ctl.cfg.idle_seconds)
        await ctl.watchdog_tick()
        assert "shutdown" not in m.calls
        assert ctl.status()["timer"]["deadline"] is None     # held: no countdown
        ctl.release("job", 1)
        clock.advance(ctl.cfg.idle_seconds - 5)
        await ctl.watchdog_tick()
        assert "shutdown" not in m.calls                     # timer restarted at the release
        clock.advance(10)
        await ctl.watchdog_tick()
    run(go())
    assert m.calls[-1] == "shutdown"


def test_timed_hold_expires():
    ctl, clock, m = make()

    async def go():
        await boot(ctl, m)
        ctl.hold("api", "x", "backup", until=clock() + 300)
        clock.advance(299)
        await ctl.watchdog_tick()
        assert ctl.active_holds()
        clock.advance(2)
        await ctl.watchdog_tick()                    # expired: released, timer restarts
        assert not ctl.active_holds()
        assert "shutdown" not in m.calls
    run(go())


def test_a_source_can_object_before_shutdown_and_force_it_later():
    ctl, clock, m = make(extra={"sources": [{"type": "helpers:Objector"}]})
    src = ctl.sources[0]

    async def go():
        await boot(ctl, m)
        clock.advance(ctl.cfg.idle_seconds + 1)
        await ctl.watchdog_tick()                    # objector holds it once
        assert src.asked == 1 and "shutdown" not in m.calls
        clock.advance(121)                           # its hold ran out, no grace: asked again now
        await ctl.watchdog_tick()
    run(go())
    assert src.asked == 2
    assert m.calls[-1] == "shutdown-force"           # it said someone is signed in


def test_never_shuts_down_a_machine_it_did_not_wake():
    ctl, clock, m = make()
    m.on = True

    async def go():
        await ctl.monitor_tick(0)
        clock.advance(10 * ctl.cfg.idle_seconds)
        await ctl.watchdog_tick()
    run(go())
    assert m.calls == []


def test_timer_override_is_replaced_by_later_activity():
    ctl, clock, m = make()

    async def go():
        await boot(ctl, m)
        ctl.state["override"] = clock() + 60         # "off in 1 min" from the status page
        ctl.hold("r", 1, "request")
        ctl.release("r", 1)                          # activity pushes past the override
        assert ctl.state["override"] is None
        ctl.state["override"] = clock() + 3600       # a later override stays
        ctl.touch()
        assert ctl.state["override"] == clock() + 3600 - 0
    run(go())


def test_machine_turned_off_outside_is_recorded_and_tells_sources():
    seen = []
    ctl, clock, m = make(extra={"sources": [{"type": "helpers:Objector"}]})
    ctl.sources[0].on_machine_off = seen.append

    async def go():
        await boot(ctl, m)
        clock.advance(3600)
        m.on = False
        misses = await ctl.monitor_tick(0)
        assert ctl.state["pc_up"]                    # one miss is a hiccup
        await ctl.monitor_tick(misses)
        assert not ctl.state["pc_up"]
    run(go())
    assert seen == ["outside wakeproxy"]
    assert kinds(ctl)[-1] == "pc_off" and not ctl.state["woken_by_me"]


def test_state_survives_a_restart(tmp_path):
    db = str(tmp_path / "w.db")
    ctl, clock, m = make(history=History(db))
    ctl.history.clock = clock

    async def go():
        await boot(ctl, m)
        ctl.hold("api", "x", "backup", until=clock() + 3600, persist=True)
        ctl.state["stores"]["Tdarr"] = {"blocked": {"sig": "1|x"}}
        ctl.save_state()
    run(go())
    ctl2, _, _ = make(clock=clock, history=History(db))
    ctl2.load_state()
    assert ctl2.state["woken_by_me"]
    assert [h["label"] for h in ctl2.active_holds()] == ["backup"]
    assert ctl2.state["stores"]["Tdarr"]["blocked"]["sig"] == "1|x"


def test_status_lists_holds_and_sources():
    ctl, clock, m = make(extra={"sources": [{"type": "helpers:Objector", "name": "Desk"}]})
    ctl.hold("localai", 7, "app → /v1/chat/completions", detail="llama")
    st = ctl.status()
    assert st["machine"]["name"] == "TestPC"
    assert st["holds"][0]["source"] == "localai" and st["holds"][0]["detail"] == "llama"
    assert st["config"]["wake_sources"] == []        # the objector does not wake
    assert st["sources"] == [{"name": "Desk", "type": "objector", "wakes": False, "route": None,
                             "summary": "objector"}]

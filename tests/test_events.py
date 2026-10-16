"""Every event the core writes gets its own text (none fall through to the raw fallback)."""
import asyncio

from helpers import make
from wakeproxy import events


def test_core_events_all_have_text():
    ctl, clock, m = make(extra={"sources": [{"type": "helpers:Objector"}]})

    async def go():
        await ctl.monitor_tick(0)                                # proxy_start
        await ctl.wake({"client": "app", "route": "llm", "path": "/v1/chat"})   # wake, wake_ok
        await ctl.monitor_tick(0)                                # pc_on
        ctl.sources[0].ctx.event("something happened", "warn")   # note
        clock.advance(ctl.cfg.idle_seconds + 1)
        await ctl.watchdog_tick()                                # objector holds
        clock.advance(121)
        await ctl.watchdog_tick()                                # shutdown
        await ctl.monitor_tick(0)
        await ctl.monitor_tick(1)                                # pc_off
    asyncio.run(go())
    evs = events.with_text(ctl.history.events(0), "TestPC")
    seen = {e["kind"] for e in evs}
    assert {"proxy_start", "wake", "wake_ok", "pc_on", "note", "shutdown", "pc_off"} <= seen
    for e in evs:
        assert e["label"] and e["text"] and ": " not in e["text"][:3], e
    by_kind = {e["kind"]: e for e in evs}
    assert by_kind["wake"]["text"] == "Wake requested by app → llm → /v1/chat"
    assert by_kind["note"]["label"] == "objector" and by_kind["note"]["level"] == "warn"
    assert by_kind["shutdown"]["text"].endswith("(forced)")


def test_unknown_kind_still_shows_something():
    label, level, text = events.describe({"ts": 1, "kind": "mystery", "x": 1}, "PC")
    assert label == "mystery" and text == "x: 1"


def test_durations():
    assert events.dur(5) == "5 s" and events.dur(90) == "1 min 30 s" and events.dur(3720) == "1 h 02 min"

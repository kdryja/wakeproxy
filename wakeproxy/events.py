"""Text for history events, so the status page needs no knowledge of event kinds.

Each event gets a `label` (a short tag), a `level` ("on", "warn", "bad", "acc" or "") and a
`text`. Events from plugins (ctx.event) are "note" events: their source is the label.
"""
from .plugins import at


def dur(sec):
    sec = max(0, round(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s" if s else f"{m} min"
    return f"{s} s"


def trigger(t):
    """{"client", "route", "path"} as "client → route → path"."""
    return " → ".join(str(t[k]) for k in ("client", "route", "path") if (t or {}).get(k))


def describe(e, machine):
    k, by = e["kind"], f" · {e['by']}" if e.get("by") else ""
    if k == "wake":
        if e.get("pc_was_up"):
            return "wake", "warn", f"A service was not answering; wake sent anyway ({machine} was already on)"
        text = f"Wake requested by {trigger(e.get('trigger')) or 'a request'}"
        if e.get("wol_sent") is False:
            text += " — sending the wake failed" + (f": {e['error']}" if e.get("error") else "")
        return "wake", "warn", text
    if k == "wake_ok":
        return "ready", "on", f"Ready after {dur(e.get('seconds'))} ({e.get('route')})"
    if k == "wake_failed":
        return "wake failed", "bad", f"Did not answer within {dur(e.get('seconds'))} ({e.get('route')})"
    if k == "pc_on":
        return "on", "on", f"{machine} came on ({e.get('by')})"
    if k == "pc_off":
        return "off", "", f"{machine} went off ({e.get('by')})"
    if k == "shutdown":
        text = ("Shutdown sent: " if e.get("ok") else "Shutdown failed: ") + str(e.get("reason"))
        text += (" (forced)" if e.get("forced") else "") + by + (f" — {e['error']}" if e.get("error") else "")
        return "shutdown", "acc" if e.get("ok") else "bad", text
    if k == "shutdown_incomplete":
        return "stuck", "bad", f"Shutdown was sent but {machine} was still on {e.get('minutes')} min later"
    if k == "timer":
        what = {"extend": f"Timer extended by {e.get('minutes'):g} min",
                "shorten": f"Timer shortened by {e.get('minutes'):g} min",
                "set": f"Timer set to {e.get('minutes'):g} min",
                "reset": "Timer back to the idle timer"}.get(e.get("action"), "Timer changed")
        return "timer", "acc", f"{what} → off at {at(e['deadline'])}{by}" if e.get("deadline") else what + by
    if k == "auto_off":
        return "auto-off", "acc", ("Auto-off started" if e.get("on") else "Auto-off cancelled (keep on)") + by
    if k == "note":
        return e.get("source") or "note", "warn" if e.get("level") == "warn" else "", str(e.get("text"))
    if k == "proxy_start":
        state = "on" if e.get("pc_up") else "off"
        return "restart", "", f"Wakeproxy started; {machine} was {state}" + (
            ", timer restored" if e.get("pc_up") and e.get("managed") else "")
    if k == "shutdown_deferred":          # written by versions before 1.0
        return "held off", "warn", f"Shutdown held off: {e.get('reason')}"
    return k, "", ", ".join(f"{a}: {b}" for a, b in e.items() if a not in ("ts", "kind"))


def with_text(events, machine):
    for e in events:
        e["label"], e["level"], e["text"] = describe(e, machine)
    return events

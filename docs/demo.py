"""A demo of the status page with made-up data, for the README screenshot.

    python docs/demo.py > demo.html

writes the page with a week of invented history and a busy moment (a chat request and a Tdarr
job keeping the machine on), all produced by wakeproxy's own code: the status and history come
from the real endpoints, with fake plugins and a fake clock. Open demo.html in a browser, or take
a screenshot of it (1200 px wide).
"""
import asyncio
import base64
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from wakeproxy import config, web  # noqa: E402
from wakeproxy.core import Controller  # noqa: E402
from wakeproxy.history import History  # noqa: E402
from wakeproxy.plugins import Check, PowerOff, PowerOn, Source  # noqa: E402


class DemoWake(PowerOn):
    type = "demo"

    def describe(self):
        return "Wake-on-LAN to aa:bb:cc:dd:ee:ff"


class DemoShutdown(PowerOff):
    type = "demo"

    def describe(self):
        return "command over SSH to user@workstation.lan"


class DemoCheck(Check):
    type = "demo"


class DemoTdarr(Source):
    type = "demo_tdarr"
    title = "Tdarr"

    def status(self):
        return {"summary": "2 queued (2 transcode, 0 health check) · node Workstation connected", "state": "on",
                "rows": [{"text": "The Grand Budapest Hotel (2014).mkv", "detail": "Transcode (hevc_nvenc)",
                          "right": "transcodegpu · 63%", "state": "on"}]}


class DemoSession(Source):
    type = "demo_session"
    title = "Windows session"
    wakes = False

    def status(self):
        return {"summary": "checks for someone signed in before each shutdown"}


CONFIG = {
    "machine": {"name": "Workstation", "wake": {"type": "__main__:DemoWake"},
                "shutdown": {"type": "__main__:DemoShutdown"}, "up_check": [{"type": "__main__:DemoCheck"}]},
    "routes": [
        {"name": "ollama", "upstream": "http://workstation.lan:11434", "health": "/api/tags",
         "work": {"paths": ["/api/chat", "/api/generate", "/v1/chat/completions"]}},
        {"name": "whisper", "prefix": "/whisper", "upstream": "http://workstation.lan:9000", "health": "/docs"},
    ],
    "sources": [{"type": "__main__:DemoTdarr"}, {"type": "__main__:DemoSession"}],
    "history": {"path": ":memory:"},
}

CLIENTS = [("open-webui", "ollama", "/api/chat", "llama3.1:8b"), ("n8n", "ollama", "/v1/chat/completions", "qwen3:14b"),
           ("bazarr", "whisper", "/asr", None), ("karakeep", "ollama", "/api/generate", "llama3.1:8b")]


def invent_week(ctl, clock, now):
    """Sessions over the past week: woken for a request, a Tdarr queue or a backup; idle shutdown."""
    rnd = random.Random(7)
    h = ctl.history
    day0 = now - now % 86400 - 6 * 86400
    for day in range(7):
        starts = sorted(rnd.sample(range(7, 23), rnd.randint(1, 3)))
        for hour in starts:
            t = day0 + day * 86400 + hour * 3600 + rnd.randint(0, 3000)
            if t > now - 3 * 3600:
                continue
            kind = rnd.choice(["request", "request", "tdarr", "backup"])
            clock.t = t
            if kind == "request":
                client, route, path, model = rnd.choice(CLIENTS)
                trigger = {"client": client, "route": route, "path": path}
            elif kind == "tdarr":
                trigger = {"client": "Tdarr", "path": f"{rnd.randint(1, 6)} files queued"}
            else:
                trigger = {"client": "nas", "route": "backup", "path": "nightly backup"}
            h.event("wake", trigger=trigger, pc_was_up=False, wol_sent=True, error=None)
            boot = rnd.randint(75, 110)
            clock.t += boot
            h.event("pc_on", by="wakeproxy")
            h.event("wake_ok", seconds=boot, route=trigger.get("route") or "Workstation")
            for _ in range(rnd.randint(1, 12)):
                client, route, path, model = rnd.choice(CLIENTS)
                clock.t += rnd.randint(20, 400)
                rec = {"start": clock.t, "client": client, "route": route, "method": "POST", "path": path,
                       "model": model, "status": 200 if rnd.random() > 0.04 else 500, "error": None, "woke": 0}
                rec["id"] = h.start_request(rec)
                clock.t += rnd.randint(2, 90)
                h.finish_request(rec)
            clock.t += 600
            h.event("shutdown", reason="idle for 10 minutes", forced=False, by=None, ok=True, error=None)
            clock.t += 70
            h.event("pc_off", by="wakeproxy")
    clock.t = now


async def build():
    now = time.time()

    class Clock:
        t = now

        def __call__(self):
            return self.t

    clock = Clock()
    cfg = config.parse(CONFIG)
    ctl = Controller(cfg, History(":memory:", clock), clock)
    ctl.build_plugins()
    invent_week(ctl, clock, now)
    # Right now: woken 25 minutes ago for a chat; a chat request and a Tdarr job keep it on.
    clock.t = now - 1500
    ctl.history.event("wake", trigger={"client": "open-webui", "route": "ollama", "path": "/api/chat"},
                      pc_was_up=False, wol_sent=True, error=None)
    clock.t += 88
    ctl.history.event("pc_on", by="wakeproxy")
    ctl.history.event("wake_ok", seconds=88, route="ollama")
    clock.t = now
    ctl.state.update(pc_up=True, woken_by_me=True, last_activity=now - 120)
    ctl.services = {"ollama": {"up": True, "checked": now}, "whisper": {"up": True, "checked": now}}
    ctl.hold("ollama", 1, "open-webui → /api/chat", detail="llama3.1:8b")["since"] = now - 14
    ctl.hold("Tdarr", "work", "1 job running", detail="The Grand Budapest Hotel (2014).mkv")["since"] = now - 1300

    client = TestClient(TestServer(web.make_app(ctl)))
    await client.start_server()
    status = await (await client.get("/api/status")).json()
    history = await (await client.get("/api/history?days=7")).json()
    page = await (await client.get("/")).text()
    logo = await (await client.get("/logo.svg")).read()
    await client.close()
    return status, history, page, logo


def main():
    status, history, page, logo = asyncio.run(build())
    page = page.replace('src="logo.svg"', 'src="data:image/svg+xml;base64,%s"' % base64.b64encode(logo).decode())
    page = page.replace('href="logo.svg"', 'href="data:image/svg+xml;base64,%s"' % base64.b64encode(logo).decode())
    mock = ("<script>const STATUS = %s, HISTORY = %s;\n"
            "window.fetch = async (u) => ({ok: true, status: 200, "
            "json: async () => (String(u).startsWith('api/history') ? HISTORY : STATUS)});</script>\n"
            % (json.dumps(status), json.dumps(history)))
    sys.stdout.write(page.replace("<script>\nconst $", mock + "<script>\nconst $", 1))


if __name__ == "__main__":
    main()

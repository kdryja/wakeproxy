"""The proxy and the hold API, against a fake upstream service."""
import asyncio

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from helpers import make
from wakeproxy import proxy


class Upstream:
    def __init__(self, machine):
        self.machine, self.seen = machine, []

    def app(self):
        async def any_path(request):
            self.machine.clock.advance(1)            # so a wait for it always ends
            if not self.machine.up():
                return web.Response(status=502)
            self.seen.append((request.method, request.path, await request.read()))
            if request.path == "/v1/models":
                return web.json_response({"data": [{"id": "m1"}]})
            return web.json_response({"ok": True, "path": request.path})
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", any_path)
        return app


async def setup(**extra):
    ctl, clock, m = make(boot_seconds=10)
    up = Upstream(m)
    upstream = TestServer(up.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")
    from wakeproxy import config
    ctl.cfg.routes = [
        config.Route(name="llm", upstream=base, health="/v1/models", work_paths=["/v1/chat"],
                     cache_health=True, offline_status=200, offline_body={"status": "ok"}),
        config.Route(name="asr", prefix="/asr", upstream=base, health="/docs"),
    ]
    ctl.route_by_name = {r.name: r for r in ctl.cfg.routes}
    for k, v in extra.items():
        setattr(ctl.cfg, k, v)
    ctl.http = ClientSession()
    client = TestClient(TestServer(proxy.make_app(ctl)))
    await client.start_server()
    return ctl, clock, m, up, upstream, client


async def teardown(ctl, upstream, client):
    await client.close()
    await upstream.close()
    await ctl.http.close()


def test_work_request_wakes_holds_and_is_logged():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        held_during = []
        orig = ctl.wake

        async def wake(trigger, ready=None):
            held_during.extend(h["label"] for h in ctl.active_holds())
            return await orig(trigger, ready)
        ctl.wake = wake
        r = await client.post("/v1/chat/completions", json={"model": "m1", "messages": []})
        assert r.status == 200 and (await r.json())["path"] == "/v1/chat/completions"
        assert m.calls == ["wake"]
        assert held_during and held_during[0].endswith("→ /v1/chat/completions")
        assert ctl.active_holds() == []              # released when the request finished
        [req] = ctl.history.requests(0)
        assert req["model"] == "m1" and req["status"] == 200 and req["woke"] == 1 and req["route"] == "llm"
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_probes_never_wake_and_get_the_offline_answer():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        r = await client.get("/v1/models")
        assert r.status == 200 and await r.json() == {"status": "ok"}    # nothing cached yet
        m.on = True
        assert await ctl.route_alive(ctl.route_by_name["llm"])          # caches the model list
        m.on = False
        r = await client.get("/v1/models")
        assert (await r.json())["data"][0]["id"] == "m1"
        r = await client.get("/asr/docs")
        assert r.status == 503 and (await r.json())["status"] == "offline"
        r = await client.post("/v1/other")                              # not a work path
        assert r.status == 200 and m.calls == []
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_prefix_is_stripped_and_any_post_is_work():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        r = await client.post("/asr/asr?task=transcribe", data=b"audio")
        assert r.status == 200
        assert up.seen[-1] == ("POST", "/asr", b"audio")
        assert ctl.history.requests(0)[0]["route"] == "asr"
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_hold_api():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        r = await client.post("/_wakeproxy/hold/backup", json={"minutes": 30, "reason": "nightly backup", "wake": False})
        assert r.status == 200
        [h] = ctl.active_holds()
        assert h["source"] == "backup" and h["label"] == "nightly backup" and h["until"] == clock() + 1800
        r = await client.post("/_wakeproxy/hold/backup", json={"minutes": 0})
        assert r.status == 400
        r = await client.post("/_wakeproxy/hold/llm", json={"minutes": 5})
        assert r.status == 400                       # a route's name
        r = await client.delete("/_wakeproxy/hold/backup")
        assert (await r.json()) == {"released": 1} and ctl.active_holds() == []
        r = await client.post("/_wakeproxy/hold/job", json={"minutes": 5, "ready": "llm"})
        assert r.status == 200 and (await r.json())["up"] and m.calls == ["wake"]
        r = await client.get("/_wakeproxy/status")
        assert (await r.json())["holds"][0]["source"] == "job"
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_hold_api_token():
    async def go():
        ctl, clock, m, up, upstream, client = await setup(hold_token="s3cret")
        r = await client.post("/_wakeproxy/hold/x", json={"minutes": 5, "wake": False})
        assert r.status == 401
        r = await client.post("/_wakeproxy/hold/x", json={"minutes": 5, "wake": False},
                              headers={"Authorization": "Bearer s3cret"})
        assert r.status == 200
        await teardown(ctl, upstream, client)
    asyncio.run(go())

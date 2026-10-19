"""The proxy and the hold API, against a fake upstream service."""
import asyncio
import gzip

import pytest
from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from helpers import make
from wakeproxy import proxy


class Upstream:
    def __init__(self, machine):
        self.machine, self.seen = machine, []
        self.client_has_first_chunk = asyncio.Event()

    def app(self):
        async def any_path(request):
            self.machine.clock.advance(1)            # so a wait for it always ends
            if not self.machine.up():
                return web.Response(status=502)
            self.seen.append((request.method, request.path, await request.read()))
            if request.path == "/v1/models":
                return web.json_response({"data": [{"id": "m1"}]})
            return web.json_response({"ok": True, "path": request.path})
        async def ws(request):
            if not self.machine.up():
                return web.Response(status=502)
            sock = web.WebSocketResponse()
            await sock.prepare(request)
            await sock.send_json({"hello": request.query.get("clientId")})
            async for msg in sock:
                if msg.type == WSMsgType.TEXT and msg.data == "close":
                    await sock.close()
                elif msg.type == WSMsgType.TEXT:
                    await sock.send_str("echo:" + msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await sock.send_bytes(msg.data[::-1])
            return sock

        async def stream(request):
            # The second chunk is only sent once the client has the first: a proxy that waits for
            # the whole body would never deliver it, and the wait below would time out.
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b"data: one\n\n")
            try:
                await asyncio.wait_for(self.client_has_first_chunk.wait(), 2)
                await resp.write(b"data: two\n\n")
            except asyncio.TimeoutError:
                await resp.write(b"data: buffered\n\n")
            return resp

        async def redirect(request):
            return web.Response(status=302, headers={"Location": "/elsewhere"})

        async def cookies(request):
            resp = web.Response(text="ok")
            resp.set_cookie("a", "1")
            resp.set_cookie("b", "2")
            return resp

        async def gzipped(request):
            return web.Response(body=gzip.compress(b"hello " * 100),
                                headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"})

        async def page(request):
            body = b"<html><head><title>App</title></head><body>hi</body></html>"
            if "gzip" in request.headers.get("Accept-Encoding", ""):
                return web.Response(body=gzip.compress(body), headers={"Content-Encoding": "gzip",
                                                                       "Content-Type": "text/html"})
            return web.Response(body=body, content_type="text/html")

        app = web.Application()
        app.router.add_get("/page", page)
        app.router.add_get("/stream", stream)
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/cookies", cookies)
        app.router.add_get("/gzip", gzipped)
        app.router.add_get("/ws", ws)
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
        config.Route(name="comfy", prefix="/comfy", upstream=base, health="/docs", work_websockets=True),
        config.Route(name="app", prefix=None, hosts=["app.example"], upstream=base, health="/docs",
                     wake_page=True, work_websockets=True, widget=True),
        config.Route(name="plain", prefix=None, hosts=["plain.example"], upstream=base, health="/docs"),
    ]
    ctl.route_by_name = {r.name: r for r in ctl.cfg.routes}
    ctl.route_by_host = {h: r for r in ctl.cfg.routes for h in r.hosts}
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


async def until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def test_work_websocket_wakes_and_holds_until_closed():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        ws = await client.ws_connect("/comfy/ws?clientId=abc")
        assert m.calls == ["wake"]
        assert await ws.receive_json() == {"hello": "abc"}       # prefix stripped, query kept
        await ws.send_str("hi")
        assert await ws.receive_str() == "echo:hi"
        await ws.send_bytes(b"abc")
        assert await ws.receive_bytes() == b"cba"
        [h] = ctl.active_holds()
        assert h["source"] == "comfy" and h["label"].endswith("→ /ws (websocket)")
        await ws.close()
        await until(lambda: not ctl.active_holds())
        [req] = ctl.history.requests(0)
        assert req["method"] == "WS" and req["status"] == 101 and req["woke"] == 1
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_upstream_closing_the_websocket_closes_the_client():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        ws = await client.ws_connect("/comfy/ws")
        await ws.receive_json()
        await ws.send_str("close")                                # the service ends the connection
        msg = await ws.receive()
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        await until(lambda: not ctl.active_holds())
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_other_websockets_never_wake_or_hold():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        with pytest.raises(WSServerHandshakeError) as e:
            await client.ws_connect("/asr/ws")                    # machine off: the offline answer
        assert e.value.status == 503 and m.calls == []
        m.on = True
        ws = await client.ws_connect("/asr/ws")
        assert await ws.receive_json() == {"hello": None}
        assert ctl.active_holds() == [] and ctl.history.requests(0) == []
        await ws.close()
        with pytest.raises(WSServerHandshakeError) as e:
            await client.ws_connect("/asr/not-a-websocket")       # upstream answers without upgrading
        assert e.value.status == 502
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_responses_are_streamed():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        r = await client.get("/asr/stream")
        assert r.headers["Content-Type"] == "text/event-stream"
        assert await r.content.readuntil(b"\n\n") == b"data: one\n\n"
        up.client_has_first_chunk.set()
        assert await r.content.read() == b"data: two\n\n"
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_redirects_cookies_and_compression_pass_through():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        r = await client.get("/asr/redirect", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/elsewhere"      # not followed
        r = await client.get("/asr/cookies")
        assert sorted(c.split("=")[0] for c in r.headers.getall("Set-Cookie")) == ["a", "b"]
        r = await client.get("/asr/gzip")
        assert r.headers["Content-Encoding"] == "gzip" and await r.text() == "hello " * 100
        r = await client.get("/asr/page", skip_auto_headers=["Accept-Encoding"])   # client accepts no gzip
        assert "Content-Encoding" not in r.headers and await r.text() == "<html><head><title>App</title></head><body>hi</body></html>"
        await teardown(ctl, upstream, client)
    asyncio.run(go())


APP = {"Host": "app.example"}
PAGE = {"Host": "app.example", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_hostname_route_keeps_the_path():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        r = await client.get("/api/thing?x=1", headers=APP)
        assert (await r.json())["path"] == "/api/thing"                  # not stripped, not "llm"
        assert up.seen[-1][1] == "/api/thing"
        r = await client.get("/asr/docs", headers=APP)                    # a prefix means nothing here
        assert up.seen[-1][1] == "/asr/docs"
        r = await client.post("/v1/chat", headers={"Host": "other.example"}, json={})
        assert ctl.history.requests(0)[0]["route"] == "llm"               # other hosts: by path as before
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_wake_page_for_browsers_while_off():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        r = await client.get("/", headers=PAGE)
        text = await r.text()
        assert r.status == 503 and r.headers["Content-Type"].startswith("text/html")
        assert "Wake TestPC" in text and 'ROUTE = "app"' in text and m.calls == []
        r = await client.get("/api/thing", headers=APP)                   # not a page: the usual answer
        assert r.status == 503 and (await r.json())["status"] == "offline"
        r = await client.get("/", headers={"Accept": "text/html"})        # by path: no wake page
        assert r.status == 200 and (await r.json())["status"] == "ok"
        r = await client.get("/_wakeproxy/page/logo.svg", headers=APP)
        assert r.status == 200 and "svg" in r.headers["Content-Type"]
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_wake_page_state_and_button():
    async def go():
        ctl, clock, m, up, upstream, client = await setup(user_header="X-User")
        ctl.history.event("wake_ok", seconds=80, route="app")
        ctl.history.event("wake_ok", seconds=100, route="app")
        r = await client.get("/_wakeproxy/page/state?route=app", headers=APP)
        st = await r.json()
        assert st["up"] is False and st["service_up"] is False and st["typical_wake"] == 90
        r = await client.get("/_wakeproxy/page/state?route=llm", headers=APP)
        assert r.status == 404                                            # no wake page on that route
        woken = []
        ctl.wake_soon = lambda trigger, ready=None: woken.append((trigger, ready))
        r = await client.post("/_wakeproxy/page/wake?route=app", headers={**APP, "X-User": "alice"})
        assert r.status == 200
        assert woken == [({"client": "alice", "route": "app", "path": "wake page"}, "app")]
        m.on = True
        ctl.state["pc_up"] = True
        st = await (await client.get("/_wakeproxy/page/state?route=app", headers=APP)).json()
        assert st["service_up"] is True
        await client.post("/_wakeproxy/page/wake?route=app", headers=APP)
        assert len(woken) == 1                                            # already up: no wake
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_hostname_websockets_never_hold():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        ws = await client.ws_connect("/ws?clientId=page", headers=APP)
        assert await ws.receive_json() == {"hello": "page"}
        assert ctl.active_holds() == [] and ctl.history.requests(0) == []
        await ws.close()
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_fresh_background_check_saves_a_health_check_per_request():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        ctl.state["pc_up"] = True
        ctl.services["app"] = {"up": True, "checked": clock()}
        for _ in range(3):
            await client.get("/asset.js", headers=APP)
        assert [p for _, p, _ in up.seen] == ["/asset.js"] * 3        # no /docs health checks
        clock.advance(60)                                               # stale: checked again
        await client.get("/asset.js", headers=APP)
        assert [p for _, p, _ in up.seen][-2:] == ["/docs", "/asset.js"]
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_widget_is_added_to_pages_only():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        m.on = True
        r = await client.get("/page", headers={**PAGE, "Accept-Encoding": "gzip"})
        body = await r.text()
        assert "Content-Encoding" not in r.headers                    # asked uncompressed, to add it
        assert body.index('src="/_wakeproxy/page/widget.js?route=app&amp;position=bottom-right"') < body.index("</head>")
        r = await client.get("/page", headers={**APP, "Accept": "application/json", "Accept-Encoding": "gzip"})
        assert r.headers["Content-Encoding"] == "gzip" and "widget.js" not in await r.text()
        r = await client.get("/page", headers={"Host": "plain.example", "Accept": "text/html"})
        assert "widget.js" not in await r.text()                      # route without the widget
        r = await client.get("/_wakeproxy/page/widget.js", headers=APP)
        assert r.status == 200 and "javascript" in r.headers["Content-Type"] and "attachShadow" in await r.text()
        await teardown(ctl, upstream, client)
    asyncio.run(go())


def test_widget_extend_and_presence():
    async def go():
        ctl, clock, m, up, upstream, client = await setup()
        r = await client.post("/_wakeproxy/page/presence?route=app", headers=APP)
        assert r.status == 200 and ctl.active_holds() == []           # machine off: nothing held
        m.on = True
        ctl.state["pc_up"] = True
        r = await client.post("/_wakeproxy/page/extend?route=app&minutes=30", headers=APP)
        assert r.status == 409                                        # not woken by wakeproxy: no timer
        ctl.state["woken_by_me"] = True
        ctl.touch()
        before = ctl.deadline()
        st = await (await client.post("/_wakeproxy/page/extend?route=app&minutes=30", headers=APP)).json()
        assert ctl.deadline() == before + 1800 and st["deadline"] == ctl.deadline() and st["managed"]
        r = await client.post("/_wakeproxy/page/extend?route=app&minutes=999", headers=APP)
        assert r.status == 400
        st = await (await client.post("/_wakeproxy/page/presence?route=app", headers=APP)).json()
        [h] = ctl.active_holds()
        assert (h["source"], h["key"]) == ("app", "presence") and 178 <= h["until"] - clock() <= 180
        assert st["deadline"] is None and st["holds"][0]["key"] == "presence"
        clock.advance(181)
        ctl.expire_holds()
        # presence ended: the idle timer starts over, and the +30 min given earlier still counts
        assert ctl.active_holds() == [] and ctl.state["last_activity"] == clock()
        assert ctl.deadline() == ctl.state["override"] > clock() + ctl.cfg.idle_seconds
        r = await client.post("/_wakeproxy/page/presence?route=plain", headers=APP)
        assert r.status == 404
        await teardown(ctl, upstream, client)
    asyncio.run(go())

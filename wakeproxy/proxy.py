"""The reverse proxy in front of the machine's services, and the hold API.

A request that does work (by its route's `work` rule) holds the machine on while it runs, and
wakes it first if needed. Everything else is passed through while the machine is up; while it is
off, the route's `while_off` answer is returned, and the machine is never woken for it.

Websockets are passed through: messages are copied both ways until either side closes. With the
route's `work.websockets`, opening one counts as work: it wakes the machine and holds it until the
connection closes.

A route can also answer whole hostnames (`hosts`), for web apps that use absolute paths: the
reverse proxy in front sends those hostnames here, and the path is forwarded as it is. With
`while_off.wake_page`, a browser asking for a page while the machine is off gets a page with a
Wake button instead of the offline answer; it reloads itself once the service answers. Websockets
opened through a hostname never count as work: browser pages keep theirs open for as long as the
tab is.

Hold API (same port): POST /_wakeproxy/hold/<name> {"minutes", "reason", "wake", "ready"} keeps the
machine on, DELETE /_wakeproxy/hold/<name> ends it, GET /_wakeproxy/status shows the state.
"""
import asyncio
import html
import json
import logging
import socket
import statistics
from importlib import resources

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, WSServerHandshakeError, web

log = logging.getLogger("wakeproxy")

# Connection-level headers are not copied: the body is re-sent whole (a chunked upload passed on
# with a complete body is rejected by some servers, uvicorn among them).
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "expect",
               "te", "upgrade"}
# Answers are streamed back as they arrive (token streams, large files), with the upstream's own
# headers. The body is passed on undecoded, so Content-Encoding and Content-Length stay valid.
RESPONSE_HOP_HEADERS = {"transfer-encoding", "connection", "keep-alive", "te", "upgrade",
                        "proxy-connection"}

STATIC = resources.files("wakeproxy").joinpath("static")
_client_names = {}
UPSTREAM = web.AppKey("upstream", ClientSession)


async def client_name(ip):
    """A readable name for the caller: Docker's DNS answers reverse lookups with
    "<container>.<network>", so this is usually the calling container's name."""
    if ip not in _client_names:
        try:
            host = (await asyncio.to_thread(socket.gethostbyaddr, ip))[0]
            _client_names[ip] = host.split(".")[0]
        except Exception:
            _client_names[ip] = ip
    return _client_names[ip]


def model_of(request, body):
    """The "model" field of an OpenAI-style request (JSON or multipart), for the request log."""
    try:
        if request.content_type == "application/json":
            return str(json.loads(body).get("model"))[:100]
        i = body.find(b'name="model"')
        if i >= 0:
            j = body.find(b"\r\n\r\n", i)
            k = body.find(b"\r\n", j + 4)
            if 0 <= j < i + 200 and k > j:
                return body[j + 4:k][:100].decode(errors="replace")
    except Exception:
        pass
    return None


def is_websocket(request):
    return request.headers.get("Upgrade", "").lower() == "websocket"


async def relay(src, dst):
    async for msg in src:              # ends when src closes
        if msg.type == WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await dst.send_bytes(msg.data)


async def pipe(a, b):
    """Copy messages between two websockets until either side closes, then close both."""
    tasks = [asyncio.create_task(relay(a, b)), asyncio.create_task(relay(b, a))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await a.close()
        await b.close()


def wants_page(request):
    """A browser navigating to a page (not a script, image or API call)."""
    return request.method == "GET" and "text/html" in request.headers.get("Accept", "")


def is_work(route, method, path):
    return method in route.work_methods and (
        route.work_paths is None or any(path.startswith(p) for p in route.work_paths))


def make_app(ctl):
    cfg = ctl.cfg

    def offline(route, request, path):
        """The route's answer while the machine is off."""
        cached = ctl.cached_health(route) if route.cache_health else None
        if cached and request.method == "GET" and path == route.health:
            return web.Response(text=cached["body"], content_type=cached["content_type"])
        if route.offline_body is None:
            return web.json_response({"status": "offline", "machine": cfg.name},
                                     status=route.offline_status)
        if isinstance(route.offline_body, str):
            return web.Response(text=route.offline_body, status=route.offline_status)
        return web.json_response(route.offline_body, status=route.offline_status)

    async def start_record(request, route, method, path):
        rec = {"start": ctl.clock(), "client": await client_name(request.remote), "route": route.name,
               "method": method, "path": path, "model": None, "status": None,
               "error": None, "woke": 0}
        rec["id"] = ctl.history.start_request(rec)
        return rec

    def wake_page(route):
        page = STATIC.joinpath("wake.html").read_text()
        for key, value in (("MACHINE", cfg.name), ("SERVICE", route.name), ("ROUTE", route.name)):
            page = page.replace("{{" + key + "}}", html.escape(value))
        return web.Response(text=page, status=503, content_type="text/html",
                            headers={"Cache-Control": "no-store", "Retry-After": "10"})

    async def alive(route):
        """Whether the service answers. The background check (every 15 s) is trusted while it is
        fresh, so a web app's many requests don't each cost an extra check against the machine."""
        seen = ctl.services.get(route.name)
        if ctl.state["pc_up"] and seen and seen["up"] and ctl.clock() - seen["checked"] < 30:
            return True
        return await ctl.route_alive(route)

    def page_route(request):
        route = ctl.route_by_name.get(request.query.get("route", ""))
        return route if route and route.wake_page else None

    async def page_state(route):
        st = ctl.state
        waits = [e["seconds"] for e in ctl.history.events(ctl.clock() - 30 * 86400)
                 if e["kind"] == "wake_ok" and isinstance(e.get("seconds"), (int, float))]
        return {"machine": cfg.name, "route": route.name, "now": ctl.clock(),
                "up": bool(st["pc_up"]), "service_up": bool(st["pc_up"]) and await ctl.route_alive(route),
                "waking": st["waking"]["since"] if st["waking"] else None,
                "shutting_down": st["shutting_down"] is not None,
                "typical_wake": round(statistics.median(waits)) if waits else None}

    async def api_page_state(request):
        route = page_route(request)
        if not route:
            return web.json_response({"error": "no such route with a wake page"}, status=404)
        return web.json_response(await page_state(route), headers={"Cache-Control": "no-store"})

    async def api_page_wake(request):
        """The Wake button. Anyone who can reach the hostname can press it, so keep that behind
        the authenticating reverse proxy, as for the app itself."""
        route = page_route(request)
        if not route:
            return web.json_response({"error": "no such route with a wake page"}, status=404)
        state = await page_state(route)
        if not state["service_up"] and not state["shutting_down"]:
            who = (cfg.user_header and request.headers.get(cfg.user_header)) or await client_name(request.remote)
            ctl.wake_soon({"client": who, "route": route.name, "path": "wake page"}, ready=route.name)
            state = await page_state(route)
        return web.json_response(state)

    async def page_logo(request):
        return web.Response(text=STATIC.joinpath("logo.svg").read_text(), content_type="image/svg+xml",
                            headers={"Cache-Control": "max-age=86400"})

    async def websocket(request, route, path, by_host):
        work = route.work_websockets and not by_host
        rec = await start_record(request, route, "WS", path) if work else None
        try:
            if rec:
                ctl.hold(route.name, rec["id"], f"{rec['client']} → {path} (websocket)")
                if not await ctl.wake(rec, ready=route.name):
                    rec["status"], rec["error"] = 503, f"{cfg.name} failed to wake"
                    return web.json_response({"error": {"message": f"{cfg.name} failed to wake"}}, status=503)
            elif not await alive(route):
                return offline(route, request, path)
            # aiohttp makes its own handshake headers
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in HOP_HEADERS and not k.lower().startswith("sec-websocket-")}
            protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
                         if p.strip()]
            query = f"?{request.query_string}" if request.query_string else ""
            url = "ws" + route.upstream[len("http"):] + path + query     # http -> ws, https -> wss
            async with ClientSession(cookie_jar=DummyCookieJar()) as s:
                try:
                    up = await s.ws_connect(url, headers=headers, protocols=protocols, max_msg_size=0)
                except WSServerHandshakeError as e:
                    if rec:
                        rec["status"] = e.status
                    return web.Response(status=e.status if e.status >= 400 else 502, text=e.message or "")
                async with up:
                    down = web.WebSocketResponse(protocols=[up.protocol] if up.protocol else (),
                                                 max_msg_size=0)
                    await down.prepare(request)
                    if rec:
                        rec["status"] = 101
                    await pipe(down, up)
                    return down
        except Exception as e:
            if rec and not rec["error"]:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            if rec:
                ctl.release(route.name, rec["id"])
                ctl.history.finish_request(rec)

    async def handle(request):
        route, path, by_host = ctl.match(request.host, request.path)
        if route is None:
            return web.json_response({"error": "no route for this path"}, status=404)
        if is_websocket(request):
            return await websocket(request, route, path, by_host)
        work = is_work(route, request.method, path)
        rec = await start_record(request, route, request.method, path) if work else None
        try:
            if work:
                ctl.hold(route.name, rec["id"], f"{rec['client']} → {path}")
                # Take the whole upload before waking. A client whose upload stalls for the
                # length of a boot may give up; one that has sent everything just waits for the
                # answer, with its (usually much longer) read timeout.
                try:
                    body = await request.read()
                except ConnectionResetError:
                    rec["error"] = "client disconnected during upload"
                    return web.Response(status=400)
                rec["model"] = model_of(request, body)
                ctl.hold(route.name, rec["id"], f"{rec['client']} → {path}", detail=rec["model"])
                if not await ctl.wake(rec, ready=route.name):
                    rec["status"], rec["error"] = 503, f"{cfg.name} failed to wake"
                    return web.json_response({"error": {"message": f"{cfg.name} failed to wake"}}, status=503)
            else:
                # never wake for probes
                if not await alive(route):
                    if by_host and route.wake_page and wants_page(request):
                        return wake_page(route)
                    return offline(route, request, path)
                try:
                    body = await request.read()
                except ConnectionResetError:
                    return web.Response(status=400)
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
            query = f"?{request.query_string}" if request.query_string else ""
            async with request.app[UPSTREAM].request(
                    request.method, f"{route.upstream}{path}{query}", data=body, headers=headers,
                    allow_redirects=False, timeout=ClientTimeout(total=route.timeout)) as r:
                if rec:
                    rec["status"] = r.status
                resp = web.StreamResponse(status=r.status, reason=r.reason)
                for k, v in r.headers.items():                # items(): repeated headers (Set-Cookie) too
                    if k.lower() not in RESPONSE_HOP_HEADERS:
                        resp.headers.add(k, v)
                await resp.prepare(request)
                try:
                    async for chunk in r.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                except ConnectionResetError:
                    if rec:
                        rec["error"] = "client disconnected"
                return resp
        except Exception as e:
            if rec and not rec["error"]:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            if rec:
                ctl.release(route.name, rec["id"])
                ctl.history.finish_request(rec)

    def authorized(request):
        return not cfg.hold_token or request.headers.get("Authorization") == f"Bearer {cfg.hold_token}"

    async def api_hold(request):
        """POST {"minutes": 60, "reason": "...", "wake": true, "ready": "<route>"}: keep the machine
        on for that long (post again to renew), waking it first unless "wake" is false. Answers
        once the machine (or the route in "ready") is up. DELETE ends the hold."""
        if not authorized(request):
            return web.json_response({"error": "missing or wrong bearer token"}, status=401)
        name = request.match_info["name"][:40]
        if name in ctl.route_by_name or name in {s.name for s in ctl.sources}:
            return web.json_response({"error": f"'{name}' is the name of a route or source"}, status=400)
        if request.method == "DELETE":
            return web.json_response({"released": ctl.release(name)})
        try:
            body = await request.json() if request.can_read_body else {}
            minutes = float(body.get("minutes", 60))
        except Exception:
            return web.json_response({"error": "expected JSON with a number in \"minutes\""}, status=400)
        if not 1 <= minutes <= 24 * 60:
            return web.json_response({"error": "minutes must be between 1 and 1440"}, status=400)
        ready = body.get("ready")
        if ready is not None and ready not in ctl.route_by_name:
            return web.json_response({"error": "ready must be one of: " + ", ".join(ctl.route_by_name)},
                                     status=400)
        client = await client_name(request.remote)
        reason = str(body.get("reason") or "held through the API")[:200]
        until = ctl.clock() + minutes * 60
        ctl.hold(name, "api", reason, detail=f"from {client}", until=until, persist=True)
        up = bool(ctl.state["pc_up"])
        if body.get("wake", True):
            up = await ctl.wake({"client": client, "route": name, "path": reason}, ready)
            if not up:
                return web.json_response({"error": f"{cfg.name} failed to wake", "until": until}, status=503)
        return web.json_response({"until": until, "up": up})

    async def api_status(request):
        if not authorized(request):
            return web.json_response({"error": "missing or wrong bearer token"}, status=401)
        return web.json_response(ctl.status())

    async def open_upstream(app):
        # One session for all forwarding. No cookie jar: cookies belong to each client, and
        # no decompression: bodies are passed on as the service sent them.
        app[UPSTREAM] = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False)

    async def close_upstream(app):
        await app[UPSTREAM].close()

    app = web.Application(client_max_size=cfg.max_upload_mb * 1024 * 1024)
    app.on_startup.append(open_upstream)
    app.on_cleanup.append(close_upstream)
    app.router.add_route("POST", "/_wakeproxy/hold/{name}", api_hold)
    app.router.add_route("DELETE", "/_wakeproxy/hold/{name}", api_hold)
    app.router.add_get("/_wakeproxy/status", api_status)
    app.router.add_get("/_wakeproxy/page/state", api_page_state)
    app.router.add_post("/_wakeproxy/page/wake", api_page_wake)
    app.router.add_get("/_wakeproxy/page/logo.svg", page_logo)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app

"""The status page: state, holds, sources and history, with timer and power controls.

It has no login of its own. Put it behind a reverse proxy that authenticates, and list that proxy
in `status_page.trusted_proxies`: other callers are refused (127.0.0.1 is always allowed). With
the list empty, anyone who can reach the port can use it.
"""
from importlib import resources

from aiohttp import web

from . import events as event_text

STATIC = resources.files("wakeproxy").joinpath("static")


def make_app(ctl):
    cfg = ctl.cfg

    @web.middleware
    async def guard(request, handler):
        if cfg.trusted_proxies and request.remote not in cfg.trusted_proxies and request.remote != "127.0.0.1":
            return web.json_response({"error": "only reachable through the reverse proxy"}, status=403)
        if request.method == "POST" and request.content_type != "application/json":
            return web.json_response({"error": "JSON only"}, status=415)
        return await handler(request)

    def user(request):
        return (cfg.user_header and request.headers.get(cfg.user_header)) or "status page"

    def answer():
        return web.json_response(ctl.status())

    async def index(request):
        return web.Response(text=STATIC.joinpath("index.html").read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def logo(request):
        return web.Response(text=STATIC.joinpath("logo.svg").read_text(), content_type="image/svg+xml",
                            headers={"Cache-Control": "max-age=86400"})

    async def status(request):
        return answer()

    async def history(request):
        days = max(1, min(int(request.query.get("days", "7")), 365))
        now = ctl.clock()
        since = now - days * 86400
        h = ctl.history
        requests = h.requests(since)
        for r in requests:      # the path as the client sent it, with the route's prefix
            route = ctl.route_by_name.get(r["route"])
            r["full_path"] = (route.prefix if route else "") + (r["path"] or "")
        return web.json_response({"since": since, "now": now,
                                  "events": event_text.with_text(h.events(since), cfg.name),
                                  "requests": requests, "by_client": h.by_client(since),
                                  "sessions": h.sessions(since, now)})

    async def timer(request):
        body = await request.json()
        action, minutes = body.get("action"), float(body.get("minutes") or 0)
        st, now = ctl.state, ctl.clock()
        if not st["pc_up"]:
            return web.json_response({"error": f"{cfg.name} is off"}, status=409)
        if not 0 <= minutes <= 24 * 60:
            return web.json_response({"error": "minutes must be between 0 and 1440"}, status=400)
        if action == "set":
            st["woken_by_me"] = True       # setting a timer also puts the machine under management
            st["override"] = now + max(minutes, 1) * 60
        elif not st["woken_by_me"]:
            return web.json_response({"error": "no timer is running"}, status=409)
        elif action == "extend":
            st["override"] = max(ctl.deadline(), now) + minutes * 60
        elif action == "shorten":
            st["override"] = max(now + 60, ctl.deadline() - minutes * 60)
        elif action == "reset":
            st["override"] = None
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        ctl.save_state()
        ctl.history.event("timer", action=action, minutes=minutes, by=user(request), deadline=ctl.deadline())
        return answer()

    async def auto_off(request):
        on = bool((await request.json()).get("auto_off"))
        if on and not ctl.state["pc_up"]:
            return web.json_response({"error": f"{cfg.name} is off"}, status=409)
        ctl.state.update(woken_by_me=on, override=None)
        if on:
            ctl.state["last_activity"] = ctl.clock()
        ctl.save_state()
        ctl.history.event("auto_off", on=on, by=user(request))
        return answer()

    async def shutdown(request):
        body = await request.json()
        if not ctl.state["pc_up"]:
            return web.json_response({"error": f"{cfg.name} is off"}, status=409)
        held = ctl.active_holds()
        if held and not body.get("force"):
            return web.json_response({"error": "kept on by: " + "; ".join(
                f"{h['source']}: {h['label']}" for h in held)}, status=409)
        error = await ctl.shutdown("shut down from the status page", force=bool(body.get("force")),
                                   by=user(request))
        if error:
            return web.json_response({"error": error}, status=502)
        return answer()

    async def wake(request):
        if ctl.state["pc_up"]:
            return web.json_response({"error": f"{cfg.name} is already on"}, status=409)
        ctl.wake_soon({"client": user(request), "route": "status page", "path": ""})
        return answer()

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/logo.svg", logo)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/timer", timer)
    app.router.add_post("/api/auto-off", auto_off)
    app.router.add_post("/api/shutdown", shutdown)
    app.router.add_post("/api/wake", wake)
    return app

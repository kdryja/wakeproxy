"""The controller: holds, the idle timer, waking and shutting down, on/off tracking.

Everything that needs the machine takes a *hold*. The machine stays on while any hold is active,
and is shut down `idle_minutes` after the last one ends, but only if wakeproxy woke it (or a timer
was set on the status page). Taking a hold while the machine is off wakes it first.
"""
import asyncio
import logging
import time

from aiohttp import ClientSession

from . import plugins
from .config import ConfigError

log = logging.getLogger("wakeproxy")


class PluginContext:
    """What a plugin sees of wakeproxy. Sources use all of it; the others mostly `http`."""

    def __init__(self, ctl, name):
        self.ctl, self.name = ctl, name

    http = property(lambda self: self.ctl.http)                    # shared aiohttp ClientSession
    pc_up = property(lambda self: bool(self.ctl.state["pc_up"]))
    managed = property(lambda self: self.ctl.state["woken_by_me"])  # wakeproxy will shut it down
    waking = property(lambda self: self.ctl.state["waking"] is not None)
    shutting_down = property(lambda self: self.ctl.state["shutting_down"] is not None)
    store = property(lambda self: self.ctl.state["stores"].setdefault(self.name, {}))  # kept across restarts
    idle_seconds = property(lambda self: self.ctl.cfg.idle_seconds)

    def now(self):
        return self.ctl.clock()

    def hold(self, key, label, detail=None, until=None, grace=True):
        """Keep the machine on. Calling it again with the same key updates the hold."""
        return self.ctl.hold(self.name, key, label, detail, until, grace)

    def release(self, key=None):
        """End one hold of this source, or all of them."""
        return self.ctl.release(self.name, key)

    def wake(self, text, ready=None):
        """Wake the machine in the background; `ready`: a route name to wait for."""
        self.ctl.wake_soon({"client": self.name, "route": None, "path": text}, ready)

    def event(self, text, level="info"):
        """A line in the history ("info" or "warn")."""
        self.ctl.history.event("note", source=self.name, text=text, level=level)

    def save(self):
        """Store `store` now (it is also stored whenever other state changes)."""
        self.ctl.save_state()


class Controller:
    wake_poll = 3.0          # seconds between readiness checks during a wake
    monitor_interval = 15.0  # seconds between on/off checks
    watchdog_interval = 15.0

    def __init__(self, cfg, history, clock=time.time):
        self.cfg, self.history, self.clock = cfg, history, clock
        self.state = {"woken_by_me": False, "last_activity": 0.0,
                      # shutdown time set on the status page; replaces the idle deadline until
                      # activity pushes the idle deadline past it
                      "override": None,
                      "pc_up": None,            # None until the first check
                      "waking": None,           # {"since", "trigger"} while a wake is running
                      "last_wol": 0.0,
                      "shutting_down": None,    # when the shutdown was sent
                      "stores": {},             # source name -> its saved dict
                      "started": clock()}
        self.holds = {}       # (source, key) -> {"source", "key", "label", "detail", "since", "until", "grace", "persist"}
        self.services = {}    # route name -> {"up", "checked"}
        self.health_cache = {}
        self.route_by_name = {r.name: r for r in cfg.routes}
        self.route_by_host = {h: r for r in cfg.routes for h in r.hosts}
        self.http = None
        self.lock = None
        self.tasks = set()
        self.power_on = self.power_off = None
        self.checks, self.sources = [], []

    # --- plugins ---------------------------------------------------------------------------

    def build_plugins(self):
        """Create every configured plugin; raises ConfigError for a bad entry."""
        registry = plugins.builtin_plugins()
        registry.update(plugins.load_plugin_paths(self.cfg.plugin_paths))
        self.power_on = self._make(registry, "power_on", self.cfg.power_on)
        self.power_off = self._make(registry, "power_off", self.cfg.power_off)
        self.checks = [self._make(registry, "check", c) for c in self.cfg.checks]
        self.sources = []
        for c in self.cfg.sources:
            if c.route is not None and c.route not in self.route_by_name:
                raise ConfigError(f"{c.where}.route: no route named '{c.route}'")
            src = self._make(registry, "source", c)
            src.route = c.route
            self.sources.append(src)
        names = [s.name for s in self.sources]
        if len(set(names)) != len(names):
            raise ConfigError("config.sources: two sources have the same name; give one a 'name'")
        if set(names) & set(self.route_by_name):
            raise ConfigError("config.sources: a source has the same name as a route")

    def _make(self, registry, kind, conf):
        try:
            cls = plugins.resolve(kind, conf.type, registry)
        except (LookupError, ImportError, AttributeError, TypeError) as e:
            raise ConfigError(f"{conf.where}: {e}")
        name = conf.name or getattr(cls, "title", None) or cls.type
        try:
            return cls(PluginContext(self, name), **conf.options)
        except TypeError as e:
            raise ConfigError(f"{conf.where} ({conf.type}): {e}")
        except ValueError as e:
            raise ConfigError(f"{conf.where} ({conf.type}): {e}")

    # --- holds and the idle timer -----------------------------------------------------------

    def hold(self, source, key, label, detail=None, until=None, grace=True, persist=False):
        """Keep the machine on. `until`: the hold ends by itself then. `grace`: when it ends,
        the idle timer starts over (False: the timer carries on where it was, so whatever took
        the hold is asked again right away). `persist`: survives a restart."""
        k = (source, str(key))
        h = self.holds.get(k)
        if h is None:
            h = self.holds[k] = {"source": source, "key": str(key), "since": self.clock()}
        h.update(label=label, detail=detail, until=until, grace=grace, persist=persist)
        if persist:
            self.save_state()
        return h

    def release(self, source, key=None):
        gone = [k for k in self.holds if k[0] == source and (key is None or k[1] == str(key))]
        for k in gone:
            h = self.holds.pop(k)
            if h["grace"]:
                self.touch()
            elif h["persist"]:
                self.save_state()
        return len(gone)

    def active_holds(self):
        now = self.clock()
        return sorted((h for h in self.holds.values() if not h["until"] or h["until"] > now),
                      key=lambda h: h["since"])

    def expire_holds(self):
        now = self.clock()
        for h in [h for h in self.holds.values() if h["until"] and h["until"] <= now]:
            self.release(h["source"], h["key"])

    def touch(self):
        """Activity: the idle timer starts over."""
        now = self.clock()
        self.state["last_activity"] = now
        if self.state["override"] and self.state["override"] < now + self.cfg.idle_seconds:
            self.state["override"] = None
        self.save_state()

    def deadline(self):
        return self.state["override"] or self.state["last_activity"] + self.cfg.idle_seconds

    def save_state(self):
        keep = {k: self.state[k] for k in ("woken_by_me", "last_activity", "override", "stores")}
        keep["holds"] = [h for h in self.holds.values() if h["persist"]]
        self.history.put("state", keep)

    def load_state(self):
        # Survive restarts: otherwise a restart forgets that wakeproxy woke the machine, and it
        # is never shut down.
        saved = self.history.get("state") or {}
        for h in saved.pop("holds", []):
            if not h["until"] or h["until"] > self.clock():
                self.holds[(h["source"], h["key"])] = h
        for k in ("woken_by_me", "last_activity", "override", "stores"):
            if k in saved:
                self.state[k] = saved[k]
        if self.state["woken_by_me"]:   # give a running machine at least 5 minutes after a restart
            self.state["last_activity"] = max(self.state["last_activity"],
                                              self.clock() - self.cfg.idle_seconds + 300)

    # --- the machine ------------------------------------------------------------------------

    def match(self, host, path):
        """The route for a request and the path to forward, and whether it was picked by hostname.
        A hostname match comes first and keeps the path as it is (for web apps with absolute
        paths); otherwise the path prefix decides and is stripped."""
        route = self.route_by_host.get((host or "").split(":")[0].lower())
        if route:
            return route, path, True
        route = self.route_for(path)
        if route is None:
            return None, path, False
        return route, path[len(route.prefix):] or "/", False

    def route_for(self, path):
        default = None
        for r in self.cfg.routes:
            if r.prefix is None:
                continue
            if not r.prefix:
                default = r
            elif path == r.prefix or path.startswith(r.prefix + "/"):
                return r
        return default

    async def route_alive(self, route):
        try:
            async with self.http.get(route.upstream + route.health, timeout=3) as r:
                if route.cache_health and r.status == 200:
                    body = await r.read()
                    entry = {"body": body.decode(errors="replace"), "content_type": r.content_type}
                    if self.health_cache.get(route.name) != entry:
                        self.health_cache[route.name] = entry
                        self.history.put("health:" + route.name, entry)
                return r.status < 500
        except Exception:
            return False

    def cached_health(self, route):
        if route.name not in self.health_cache:
            self.health_cache[route.name] = self.history.get("health:" + route.name)
        return self.health_cache[route.name]

    async def machine_up(self):
        for c in self.checks:
            try:
                if await c.check():
                    return True
            except Exception:
                log.debug("check %s failed", c.type, exc_info=True)
        return False

    async def ready(self, route_name):
        route = self.route_by_name.get(route_name) if route_name else None
        return await (self.route_alive(route) if route else self.machine_up())

    async def wake(self, trigger, ready=None):
        """Wake the machine if needed and wait until it answers: the route named `ready`, or the
        up checks. `trigger` ({"client", "route", "path"}) goes into the history and gets
        "woke": 1 when a wake was sent. Returns whether it is ready."""
        async with self.lock:                  # one waker at a time
            if await self.ready(ready):
                return True
            started = self.clock()
            info = {k: trigger.get(k) for k in ("client", "route", "path")}
            log.info("waking %s for %s", self.cfg.name, info)
            self.state["waking"] = {"since": started, "trigger": info}
            trigger["woke"] = 1
            was_up = bool(self.state["pc_up"])
            error = None
            try:
                await self.power_on.power_on()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:300]
                log.warning("wake failed: %s", error)
            self.history.event("wake", trigger=info, pc_was_up=was_up, wol_sent=error is None, error=error)
            # A machine that was already on (only a service was down) was not woken by us:
            # don't shut it down later.
            if not was_up:
                self.state["woken_by_me"] = True
                self.state["last_wol"] = started
                self.touch()
            try:
                while self.clock() < started + self.cfg.wake_timeout:
                    await asyncio.sleep(self.wake_poll)
                    if await self.ready(ready):
                        log.info("%s is up", ready or self.cfg.name)
                        self.history.event("wake_ok", seconds=round(self.clock() - started),
                                           route=ready or self.cfg.name)
                        self.touch()
                        return True
                log.warning("%s did not answer within %ss", ready or self.cfg.name, self.cfg.wake_timeout)
                self.history.event("wake_failed", seconds=self.cfg.wake_timeout, route=ready or self.cfg.name)
                return False
            finally:
                self.state["waking"] = None

    def wake_soon(self, trigger, ready=None):
        if not self.state["waking"]:
            self.spawn(self.wake(trigger, ready))

    async def shutdown(self, reason, force=False, by=None):
        log.warning("shutting %s down (%s)", self.cfg.name, reason)
        error = None
        try:
            await self.power_off.power_off(force)
        except Exception as e:
            error = f"{e}"[:300] or type(e).__name__
        self.history.event("shutdown", reason=reason, forced=force, by=by, ok=error is None, error=error)
        if error is None:
            self.state.update(woken_by_me=False, override=None, shutting_down=self.clock())
        else:
            log.warning("shutdown failed: %s", error)
            self.state["override"] = None
            self.state["last_activity"] = self.clock() - self.cfg.idle_seconds + 300   # retry in 5 min
        self.save_state()
        return error

    # --- loops ------------------------------------------------------------------------------

    async def watchdog_tick(self):
        self.expire_holds()
        st = self.state
        if not st["woken_by_me"] or not st["pc_up"] or st["shutting_down"] or st["waking"]:
            return
        if self.active_holds() or self.clock() < self.deadline():
            return
        for src in self.sources:              # last chance to object, e.g. someone at the machine
            await src.before_shutdown()
        if self.active_holds():
            return
        force = any(src.force_shutdown for src in self.sources)
        if st["override"]:
            await self.shutdown("timer set on the status page ran out", force=force)
        else:
            await self.shutdown("idle for %s minutes" % round(self.cfg.idle_seconds / 60), force=force)

    async def monitor_tick(self, misses):
        """Tracks whether the machine and its services are up and records on/off changes.
        Returns the number of failed checks in a row."""
        now = self.clock()
        up = await self.machine_up()
        misses = 0 if up else misses + 1
        was = self.state["pc_up"]
        # two misses in a row before calling it off, so a hiccup doesn't count
        new = up or (False if was is None or misses >= 2 else was)
        if was is None:
            self.history.event("proxy_start", pc_up=new, managed=self.state["woken_by_me"])
            if not new:
                self.state["woken_by_me"], self.state["override"] = False, None
                self.save_state()
        elif new and not was:
            by = ("wakeproxy" if now - self.state["last_wol"] < self.cfg.wake_timeout + 120
                  else "outside wakeproxy")
            self.history.event("pc_on", by=by)
        elif was and not new:
            by = ("wakeproxy" if self.state["shutting_down"] and now - self.state["shutting_down"] < 900
                  else "outside wakeproxy")
            self.history.event("pc_off", by=by)
            for src in self.sources:
                try:
                    src.on_machine_off(by)
                except Exception:
                    log.exception("source %s", src.name)
            self.state.update(woken_by_me=False, override=None, shutting_down=None)
            self.save_state()
        self.state["pc_up"] = new

        if self.state["shutting_down"] and new and now - self.state["shutting_down"] > 600:
            self.history.event("shutdown_incomplete", minutes=10)
            self.state["shutting_down"] = None

        for route in self.cfg.routes:
            self.services[route.name] = {"up": new and await self.route_alive(route), "checked": now}
        return misses

    async def _loop(self, what, interval, step):
        while True:
            try:
                await step()
            except Exception:
                log.exception(what)
            await asyncio.sleep(interval() if callable(interval) else interval)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        self.lock = asyncio.Lock()
        self.http = ClientSession()
        self.load_state()
        misses = 0

        async def monitor():
            nonlocal misses
            misses = await self.monitor_tick(misses)

        async def prune():
            self.history.prune(self.cfg.request_days, self.cfg.event_days)

        self.spawn(self._loop("watchdog", self.watchdog_interval, self.watchdog_tick))
        self.spawn(self._loop("monitor", self.monitor_interval, monitor))
        self.spawn(self._loop("prune", 3600, prune))
        for src in self.sources:
            self.spawn(self._loop("source " + src.name, src.interval, src.poll))

    async def stop(self):
        for t in list(self.tasks):
            t.cancel()
        for p in [self.power_on, self.power_off, *self.checks, *self.sources]:
            try:
                await p.close()
            except Exception:
                pass
        if self.http:
            await self.http.close()

    # --- status -----------------------------------------------------------------------------

    def status(self):
        managed, held = self.state["woken_by_me"], self.active_holds()
        return {
            "now": self.clock(),
            "started": self.state["started"],
            "machine": {"name": self.cfg.name, "wake": self.power_on.describe(),
                        "shutdown": self.power_off.describe()},
            "pc": {"up": self.state["pc_up"], "waking": self.state["waking"],
                   "shutting_down": self.state["shutting_down"]},
            "managed": managed,
            "timer": {"deadline": self.deadline() if managed and not held else None,
                      "override": self.state["override"] if managed else None,
                      "last_activity": self.state["last_activity"] or None},
            "holds": [{k: h[k] for k in ("source", "label", "detail", "since", "until")} for h in held],
            "services": [{"name": r.name, "upstream": r.upstream, "hosts": r.hosts,
                          **self.services.get(r.name, {"up": None, "checked": None})}
                         for r in self.cfg.routes],
            "sources": [{"name": s.name, "type": s.type, "wakes": s.wakes, "route": s.route, **s.status()}
                        for s in self.sources],
            "config": {"idle_seconds": self.cfg.idle_seconds, "wake_timeout": self.cfg.wake_timeout,
                       "history_days": self.cfg.request_days,
                       "wake_sources": [s.name for s in self.sources if getattr(s, "wakes", True)]},
        }

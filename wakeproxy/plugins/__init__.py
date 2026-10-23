"""Plugins: how wakeproxy turns the machine on and off, checks it, and learns what needs it.

There are four kinds. Each plugin is a class with `kind` and `type` attributes; `type` is the
name used in the config file. Built-in plugins live in this package (one module per topic); your
own can be loaded from any importable module with `type: "package.module:ClassName"`, or from a
directory listed in `plugin_paths` in the config.

    power_on   async power_on()               start the machine (Wake-on-LAN, an HTTP call, ...)
    power_off  async power_off(force)          shut it down (a command over SSH, ...)
    check      async check() -> bool           is the machine up?
    source     async poll(), before_shutdown(), on_machine_off(by), status()
               something that can need the machine: it takes and releases holds and asks for wakes

Options from the config are passed to the constructor as keyword arguments, after the context:
`def __init__(self, ctx, *, url, token, timeout=10)`. A missing or unknown option is reported as
a config error at startup. The context (`self.ctx`, see wakeproxy.core.PluginContext) gives
access to the shared HTTP session and, for sources, to holds, wakes, events and stored state.
"""
import importlib
import inspect
import pkgutil
import sys

KINDS = ("power_on", "power_off", "check", "source")


def at(ts):
    """A time inside text for the status page (hold labels and details, notes, events): the page
    shows it as a clock time in the viewer's time zone."""
    return "{time:%d}" % ts


class Plugin:
    kind = None
    type = None

    def __init__(self, ctx):
        self.ctx = ctx

    def describe(self):
        """A few words for the status page and history, e.g. "Wake-on-LAN via Home Assistant"."""
        return self.type

    async def close(self):
        """Called on shutdown of wakeproxy."""


class PowerOn(Plugin):
    kind = "power_on"
    # True when sending it again while the machine boots (or is on) does no harm, like a
    # Wake-on-LAN packet. wakeproxy then repeats it by default (machine.wake_packets and
    # machine.wake_resend); a smart plug toggle or a script must not be.
    repeat_safe = False

    async def power_on(self):
        """Start the machine. Raise an exception if the request could not be sent."""
        raise NotImplementedError


class PowerOff(Plugin):
    kind = "power_off"

    async def power_off(self, force):
        """Shut the machine down. `force`: close running programs without asking (someone is
        signed in). Raise an exception on failure; its text ends up in the history."""
        raise NotImplementedError


class Check(Plugin):
    kind = "check"

    async def check(self):
        """True if the machine is up. Should answer within a few seconds."""
        raise NotImplementedError


class Source(Plugin):
    kind = "source"
    title = None            # display name (default: the type); a config entry's `name` overrides it
    interval = 60           # seconds between poll() calls
    wakes = True            # can wake the machine (listed on the status page as a wake source)
    force_shutdown = False  # set True when shutting down needs `force` (someone is signed in)
    route = None            # set from the config entry's `route`: shown on that route's line

    @property
    def name(self):
        return self.ctx.name

    async def poll(self):
        """Called every `interval` seconds: take or release holds, ask for a wake."""

    async def before_shutdown(self):
        """Called when the idle timer has run out, just before shutting down. Taking a hold
        here stops the shutdown."""

    def on_machine_off(self, by):
        """The machine went off; `by` is "wakeproxy" or "outside wakeproxy"."""

    def status(self):
        """This source's line on the status page, listed with the routes:
        {"summary": "one line", "state": "on" | "bad" | None,
         "rows": [{"text", "detail", "right", "state"}],   # optional details below it
         "note": "optional remark"}"""
        return {"summary": self.describe()}


def builtin_plugins():
    """{(kind, type): class} for the plugins in this package."""
    found = {}
    for info in pkgutil.iter_modules(__path__, __name__ + "."):
        found.update(plugins_in(importlib.import_module(info.name)))
    return found


def plugins_in(module):
    return {(obj.kind, obj.type): obj for obj in vars(module).values()
            if inspect.isclass(obj) and issubclass(obj, Plugin) and obj.kind and obj.type
            and obj.__module__ == module.__name__}


def load_plugin_paths(paths):
    """Make the .py files in these directories available as plugins."""
    found = {}
    for path in paths:
        sys.path.insert(0, path)
        for info in pkgutil.iter_modules([path]):
            found.update(plugins_in(importlib.import_module(info.name)))
    return found


def resolve(kind, type_name, registry):
    """The class for a config entry: a built-in or plugin_paths type, or "module:Class"."""
    if ":" in type_name:
        module, _, attr = type_name.partition(":")
        cls = getattr(importlib.import_module(module), attr)
        if not (inspect.isclass(cls) and issubclass(cls, Plugin) and cls.kind == kind):
            raise TypeError(f"{type_name} is not a {kind} plugin")
        return cls
    if (kind, type_name) not in registry:
        names = sorted(t for k, t in registry if k == kind)
        raise LookupError(f"unknown {kind} type '{type_name}' (available: {', '.join(names)})")
    return registry[(kind, type_name)]

"""Reads and checks the YAML config file (see config.example.yaml for every setting).

${NAME} in any string is replaced by the environment variable NAME, so secrets can stay in the
environment; ${NAME:-default} gives a default. Unknown settings, missing required ones and wrong
types are reported with their place in the file, before anything starts.
"""
import os
import re
from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def substitute(value, where="config"):
    if isinstance(value, str):
        def replace(m):
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"{where}: environment variable {name} is not set")
        return _VAR.sub(replace, value)
    if isinstance(value, list):
        return [substitute(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: substitute(v, f"{where}.{k}") for k, v in value.items()}
    return value


@dataclass
class PluginConf:
    type: str
    options: dict
    where: str              # place in the config file, for error messages
    name: str = None        # sources: shown on the status page (defaults to the plugin's name)
    route: str = None       # sources: shown on this route's line of the status page


@dataclass
class Route:
    name: str
    upstream: str
    prefix: str = ""                  # "" = everything no other route takes
    health: str = "/"                 # GET this to see whether the service is up
    timeout: float = 600              # seconds a forwarded request may take
    work_methods: list = field(default_factory=lambda: ["POST"])
    work_paths: list = None           # path prefixes that do work; None = every path
    work_websockets: bool = False     # an open websocket wakes the machine and holds it until closed
    offline_status: int = 503         # answer to other requests while the machine is off
    offline_body: object = None
    cache_health: bool = False        # while off, answer GET <health> with its last response


@dataclass
class Config:
    name: str
    idle_seconds: int
    wake_timeout: int
    power_on: PluginConf
    power_off: PluginConf
    checks: list
    routes: list
    sources: list
    proxy_port: int = 8009
    max_upload_mb: int = 1024
    hold_token: str = None
    ui_port: int = 8010
    trusted_proxies: list = field(default_factory=list)
    user_header: str = None
    db_path: str = "/data/wakeproxy.db"
    request_days: int = 90
    event_days: int = 365
    plugin_paths: list = field(default_factory=list)
    log_level: str = "INFO"


class _Section:
    """Reads one mapping, remembering which keys were used, to report unknown ones."""

    def __init__(self, data, where):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping")
        self.data, self.where, self.used = data, where, set()

    def get(self, key, kind, default=..., check=None):
        self.used.add(key)
        if key not in self.data or self.data[key] is None:
            if default is ...:
                raise ConfigError(f"{self.where}.{key}: required")
            return default
        value = self.data[key]
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            names = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
            raise ConfigError(f"{self.where}.{key}: expected {names}, got {value!r}")
        if check and not check(value):
            raise ConfigError(f"{self.where}.{key}: invalid value {value!r}")
        return value

    def section(self, key, required=False):
        self.used.add(key)
        if required and key not in self.data:
            raise ConfigError(f"{self.where}.{key}: required")
        return _Section(self.data.get(key), f"{self.where}.{key}")

    def done(self):
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ConfigError(f"{self.where}: unknown setting{'s' * (len(unknown) > 1)} "
                              f"{', '.join(unknown)}")


def _plugin(data, where, named=False):
    if not isinstance(data, dict) or "type" not in data:
        raise ConfigError(f"{where}: expected a mapping with a 'type'")
    options = dict(data)
    type_name = options.pop("type")
    name = options.pop("name", None) if named else None
    route = options.pop("route", None) if named else None
    if not isinstance(type_name, str):
        raise ConfigError(f"{where}.type: expected a string")
    if route is not None and not isinstance(route, str):
        raise ConfigError(f"{where}.route: expected a string")
    return PluginConf(type_name, options, where, name, route)


def _route(data, where):
    s = _Section(data, where)
    work = s.section("work")
    off = s.section("while_off")
    paths = work.get("paths", list, None)
    route = Route(
        name=s.get("name", str),
        upstream=s.get("upstream", str).rstrip("/"),
        prefix=s.get("prefix", str, "").rstrip("/"),
        health=s.get("health", str, "/"),
        timeout=s.get("timeout", float, 600.0, lambda v: v > 0),
        work_methods=[m.upper() for m in work.get("methods", list, ["POST"])],
        work_paths=paths,
        work_websockets=work.get("websockets", bool, False),
        offline_status=off.get("status", int, 503),
        offline_body=off.get("body", (dict, list, str), None),
        cache_health=off.get("cache_health", bool, False),
    )
    if route.prefix and not route.prefix.startswith("/"):
        raise ConfigError(f"{where}.prefix: must start with /")
    work.done()
    off.done()
    s.done()
    return route


def parse(data):
    data = substitute(data)
    root = _Section(data, "config")
    machine = root.section("machine", required=True)
    checks = machine.get("up_check", list)
    if not checks:
        raise ConfigError("config.machine.up_check: needs at least one check")
    routes = [_route(r, f"config.routes[{i}]") for i, r in enumerate(root.get("routes", list, []))]
    names = [r.name for r in routes]
    if len(set(names)) != len(names):
        raise ConfigError("config.routes: route names must be unique")
    if len({r.prefix for r in routes}) != len(routes):
        raise ConfigError("config.routes: two routes have the same prefix")
    proxy = root.section("proxy")
    ui = root.section("status_page")
    history = root.section("history")
    cfg = Config(
        name=machine.get("name", str),
        idle_seconds=int(machine.get("idle_minutes", (int, float), 10, lambda v: v > 0) * 60),
        wake_timeout=machine.get("wake_timeout", int, 180, lambda v: v > 0),
        power_on=_plugin(machine.get("wake", dict), "config.machine.wake"),
        power_off=_plugin(machine.get("shutdown", dict), "config.machine.shutdown"),
        checks=[_plugin(c, f"config.machine.up_check[{i}]") for i, c in enumerate(checks)],
        routes=routes,
        sources=[_plugin(c, f"config.sources[{i}]", named=True)
                 for i, c in enumerate(root.get("sources", list, []))],
        proxy_port=proxy.get("port", int, 8009),
        max_upload_mb=proxy.get("max_upload_mb", int, 1024),
        hold_token=proxy.get("hold_token", str, None) or None,
        ui_port=ui.get("port", int, 8010),
        trusted_proxies=ui.get("trusted_proxies", list, []),
        user_header=ui.get("user_header", str, None),
        db_path=history.get("path", str, "/data/wakeproxy.db"),
        request_days=history.get("request_days", int, 90),
        event_days=history.get("event_days", int, 365),
        plugin_paths=root.get("plugin_paths", list, []),
        log_level=root.get("log_level", str, "INFO").upper(),
    )
    for s in (machine, proxy, ui, history, root):
        s.done()
    return cfg


def load(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file {path} not found")
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}")
    return parse(data)

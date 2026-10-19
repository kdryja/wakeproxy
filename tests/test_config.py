import os
from pathlib import Path

import pytest
import yaml

from wakeproxy import config
from wakeproxy.core import Controller
from wakeproxy.history import History

ROOT = Path(__file__).resolve().parent.parent


def minimal(**overrides):
    data = {"machine": {"name": "PC", "wake": {"type": "wol", "mac": "aa:bb:cc:dd:ee:ff"},
                        "shutdown": {"type": "command", "command": "true"},
                        "up_check": [{"type": "tcp", "host": "pc", "port": 22}]}}
    data.update(overrides)
    return data


def build(data):
    cfg = config.parse(data)
    ctl = Controller(cfg, History(":memory:"))
    ctl.build_plugins()
    return ctl


def test_example_config_loads_and_builds(monkeypatch):
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    ctl = build(data)
    assert ctl.cfg.name == "MyPC"
    assert [r.name for r in ctl.cfg.routes] == ["ollama", "whisper", "comfyui"]
    assert [s.name for s in ctl.sources] == ["Tdarr", "ComfyUI queue", "Windows session"]
    assert ctl.cfg.idle_seconds == 600


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("WP_MAC", "11:22:33:44:55:66")
    data = minimal()
    data["machine"]["wake"]["mac"] = "${WP_MAC}"
    data["machine"]["name"] = "${WP_UNSET:-Fallback}"
    cfg = config.parse(data)
    assert cfg.power_on.options["mac"] == "11:22:33:44:55:66"
    assert cfg.name == "Fallback"


def test_missing_env_var_is_an_error():
    data = minimal()
    data["machine"]["wake"]["mac"] = "${WP_DEFINITELY_NOT_SET}"
    with pytest.raises(config.ConfigError, match=r"config\.machine\.wake\.mac: environment variable WP_DEFINITELY_NOT_SET"):
        config.parse(data)


def test_unknown_setting_names_its_place():
    data = minimal(proxy={"port": 1, "prot": 2})
    with pytest.raises(config.ConfigError, match=r"config\.proxy: unknown setting prot"):
        config.parse(data)


def test_missing_required_setting():
    data = minimal()
    del data["machine"]["up_check"]
    with pytest.raises(config.ConfigError, match=r"config\.machine\.up_check: required"):
        config.parse(data)


def test_wrong_type():
    data = minimal()
    data["machine"]["idle_minutes"] = "ten"
    with pytest.raises(config.ConfigError, match=r"idle_minutes: expected"):
        config.parse(data)


def test_unknown_plugin_type_lists_the_choices():
    data = minimal()
    data["machine"]["wake"] = {"type": "telepathy"}
    with pytest.raises(config.ConfigError, match=r"unknown power_on type 'telepathy' \(available: .*wol"):
        build(data)


def test_bad_plugin_option_names_its_place():
    data = minimal(sources=[{"type": "tdarr", "url": "http://t", "nod": "PC"}])
    with pytest.raises(config.ConfigError, match=r"config\.sources\[0\] \(tdarr\): .*nod"):
        build(data)


def test_bad_mac():
    data = minimal()
    data["machine"]["wake"]["mac"] = "nope"
    with pytest.raises(config.ConfigError, match="not a MAC address"):
        build(data)


def test_duplicate_source_names():
    t = {"type": "tdarr", "url": "http://t", "node": "PC"}
    with pytest.raises(config.ConfigError, match="same name"):
        build(minimal(sources=[t, t]))
    build(minimal(sources=[t, {**t, "name": "Tdarr 2"}]))


def test_routes():
    data = minimal(routes=[
        {"name": "llm", "upstream": "http://pc:1/", "work": {"paths": ["/v1/chat"]}},
        {"name": "asr", "prefix": "/asr", "upstream": "http://pc:2", "work": {"methods": ["post", "put"]}},
    ])
    ctl = build(data)
    assert ctl.route_for("/asr/x").name == "asr"
    assert ctl.route_for("/asrx").name == "llm"
    assert ctl.route_for("/v1/models").name == "llm"
    assert ctl.cfg.routes[0].upstream == "http://pc:1"
    assert ctl.cfg.routes[1].work_methods == ["POST", "PUT"]
    assert not ctl.cfg.routes[0].work_websockets


def test_route_websockets():
    ctl = build(minimal(routes=[{"name": "c", "upstream": "http://pc:3", "work": {"websockets": True}}]))
    assert ctl.cfg.routes[0].work_websockets
    with pytest.raises(config.ConfigError, match=r"routes\[0\]\.work\.websockets: expected bool"):
        build(minimal(routes=[{"name": "c", "upstream": "http://pc:3", "work": {"websockets": "yes"}}]))


def test_source_on_a_route():
    routes = [{"name": "comfyui", "prefix": "/c", "upstream": "http://pc:3"}]
    ctl = build(minimal(routes=routes, sources=[{"type": "comfyui", "url": "http://pc:3", "route": "comfyui"}]))
    assert ctl.sources[0].route == "comfyui"
    assert ctl.status()["sources"][0]["route"] == "comfyui"
    with pytest.raises(config.ConfigError, match=r"sources\[0\]\.route: no route named 'other'"):
        build(minimal(routes=routes, sources=[{"type": "comfyui", "url": "http://pc:3", "route": "other"}]))


def test_routes_by_hostname():
    ctl = build(minimal(routes=[
        {"name": "llm", "upstream": "http://pc:1"},
        {"name": "app", "upstream": "http://pc:2", "hosts": ["App.Example.com"], "while_off": {"wake_page": True}},
        {"name": "both", "prefix": "/b", "upstream": "http://pc:3", "hosts": ["b.example.com"]},
    ]))
    app = ctl.route_by_name["app"]
    assert app.prefix is None and app.hosts == ["app.example.com"] and app.wake_page
    assert ctl.match("app.example.com:443", "/x/y") == (app, "/x/y", True)
    assert ctl.match("elsewhere", "/x")[0].name == "llm"              # a hostname route is never the default
    assert ctl.match("b.example.com", "/b/z")[1] == "/b/z"            # by hostname: path kept
    assert ctl.match("other", "/b/z")[1] == "/z"                      # by prefix: stripped
    with pytest.raises(config.ConfigError, match="same hostname"):
        build(minimal(routes=[{"name": "a", "upstream": "http://pc:1", "hosts": ["x.example"]},
                              {"name": "b", "upstream": "http://pc:2", "hosts": ["X.example"]}]))
    with pytest.raises(config.ConfigError, match=r"hosts: expected hostnames"):
        build(minimal(routes=[{"name": "a", "upstream": "http://pc:1", "hosts": ["http://x/"]}]))


def test_browser_settings():
    ctl = build(minimal(routes=[{"name": "a", "upstream": "http://pc:1", "hosts": ["a.example"],
                                 "browser": {"widget": True, "position": "bottom-center", "presence_minutes": 5}}],
                        status_page={"public_url": "https://wake.example"}))
    r = ctl.cfg.routes[0]
    assert r.widget and r.widget_position == "bottom-center" and r.presence_minutes == 5
    assert ctl.cfg.status_url == "https://wake.example"
    with pytest.raises(config.ConfigError, match=r"browser.position: invalid"):
        build(minimal(routes=[{"name": "a", "upstream": "http://pc:1", "browser": {"position": "middle"}}]))


def test_route_without_default_gives_none():
    ctl = build(minimal(routes=[{"name": "asr", "prefix": "/asr", "upstream": "http://pc:2"}]))
    assert ctl.route_for("/other") is None


def test_plugin_paths(tmp_path):
    (tmp_path / "myplugins.py").write_text(
        "from wakeproxy.plugins import Source\n"
        "class Mine(Source):\n    type = 'mine'\n    def __init__(self, ctx, *, level):\n"
        "        super().__init__(ctx); self.level = level\n")
    ctl = build(minimal(plugin_paths=[str(tmp_path)], sources=[{"type": "mine", "level": 3, "name": "My source"}]))
    assert ctl.sources[0].level == 3 and ctl.sources[0].name == "My source"


def test_check_command_runs(tmp_path, monkeypatch):
    import subprocess, sys
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(minimal()))
    out = subprocess.run([sys.executable, "-m", "wakeproxy", "--config", str(path), "--check"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "config OK" in out.stdout
    path.write_text(yaml.safe_dump(minimal(bogus=1)))
    out = subprocess.run([sys.executable, "-m", "wakeproxy", "--config", str(path), "--check"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 2 and "unknown setting bogus" in out.stderr

"""python -m wakeproxy [--config PATH] [--check]"""
import argparse
import asyncio
import logging
import os
import sys

from aiohttp import web

from . import config, proxy, web as status_page
from .core import Controller
from .history import History

log = logging.getLogger("wakeproxy")


async def serve(ctl):
    await ctl.start()
    runners = []
    for app, port, access_log in ((proxy.make_app(ctl), ctl.cfg.proxy_port, True),
                                  (status_page.make_app(ctl), ctl.cfg.ui_port, False)):
        runner = web.AppRunner(app, **({} if access_log else {"access_log": None}))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("proxy on %s, status page on %s; machine %s; sources: %s", ctl.cfg.proxy_port,
             ctl.cfg.ui_port, ctl.cfg.name, ", ".join(s.name for s in ctl.sources) or "none")
    try:
        await asyncio.Event().wait()
    finally:
        for r in runners:
            await r.cleanup()
        await ctl.stop()


def main():
    ap = argparse.ArgumentParser(prog="wakeproxy", description=__doc__)
    ap.add_argument("--config", default=os.environ.get("WAKEPROXY_CONFIG", "/config/config.yaml"))
    ap.add_argument("--check", action="store_true", help="check the config and plugins, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = config.load(args.config)
        logging.getLogger().setLevel(cfg.log_level)
        history = History(":memory:" if args.check else cfg.db_path)
        ctl = Controller(cfg, history)
        ctl.build_plugins()
    except config.ConfigError as e:
        log.error("config error: %s", e)
        sys.exit(2)
    if args.check:
        print(f"config OK: machine {cfg.name}, {len(cfg.routes)} routes, "
              f"{len(ctl.sources)} sources ({', '.join(s.name for s in ctl.sources) or 'none'})")
        return
    asyncio.run(serve(ctl))


if __name__ == "__main__":
    main()

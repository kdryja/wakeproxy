"""The Tdarr source against a fake Tdarr server."""
import asyncio

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from helpers import make


class FakeTdarr:
    def __init__(self):
        self.queue, self.health, self.workers = [], [], []
        self.connected, self.paused = True, False

    def app(self):
        async def tables(request):
            table = (await request.json())["data"]["opts"]["table"]
            rows = {"table1": self.queue, "table4": self.health}.get(table, [])
            return web.json_response({"array": [{"_id": f} for f in rows], "totalCount": len(rows)})

        async def nodes(request):
            if not self.connected:
                return web.json_response({})
            return web.json_response({"abc": {"nodeName": "PC", "workers": {
                str(i): {"workerType": "transcodegpu", "file": "/media/" + f, "percentage": 40, "status": "ffmpeg"}
                for i, f in enumerate(self.workers)}}})

        async def cruddb(request):
            data = (await request.json())["data"]
            if data["collection"] == "NodeJSONDB":
                return web.json_response({"_id": "PC", "nodePaused": self.paused})
            return web.json_response({"_id": "globalsettings", "pauseAllNodes": False})

        app = web.Application()
        app.router.add_post("/api/v2/client/status-tables", tables)
        app.router.add_get("/api/v2/get-nodes", nodes)
        app.router.add_post("/api/v2/cruddb", cruddb)
        return app


async def setup(fake):
    server = TestServer(fake.app())
    await server.start_server()
    ctl, clock, m = make(extra={"sources": [{"type": "tdarr", "url": str(server.make_url("")), "node": "PC"}]})
    ctl.http = ClientSession()
    wakes = []
    ctl.wake_soon = lambda trigger, ready=None: wakes.append(trigger)
    return server, ctl, clock, m, ctl.sources[0], wakes


async def teardown(server, ctl):
    await ctl.http.close()
    await server.close()


def test_queued_work_wakes_the_machine():
    async def go():
        fake = FakeTdarr()
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = False
        await src.poll()
        assert wakes == []
        fake.queue = ["/media/a.mkv", "/media/b.mkv"]
        await src.poll()
        assert wakes == [{"client": "Tdarr", "route": None, "path": "2 files queued"}]
        await teardown(server, ctl)
    asyncio.run(go())


def test_paused_node_does_not_wake():
    async def go():
        fake = FakeTdarr()
        fake.queue, fake.paused = ["/media/a.mkv"], True
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = False
        await src.poll()
        assert wakes == [] and "paused" in src.status()["note"]
        await teardown(server, ctl)
    asyncio.run(go())


def test_running_jobs_hold_the_machine_on():
    async def go():
        fake = FakeTdarr()
        fake.workers = ["a.mkv"]
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = True
        await src.poll()
        [h] = ctl.active_holds()
        assert h["source"] == "Tdarr" and h["label"] == "1 job running" and h["detail"] == "a.mkv"
        fake.workers = []
        await src.poll()
        assert ctl.active_holds() == []
        rows = src.status()["rows"]
        assert rows[0]["text"] == "Queue empty" and "connected" in rows[0]["right"]
        await teardown(server, ctl)
    asyncio.run(go())


def test_work_the_node_never_takes_is_given_up_until_the_queue_changes():
    async def go():
        fake = FakeTdarr()
        fake.queue = ["/media/stuck.mkv"]
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = True
        await src.poll()
        assert ctl.active_holds()[0]["label"] == "1 file queued, waiting for the node"
        clock.advance(15 * 60 + 1)
        await src.poll()
        assert ctl.active_holds() == []
        assert "took none of it" in ctl.state["stores"]["Tdarr"]["blocked"]["reason"]
        ctl.state["pc_up"] = False
        await src.poll()
        assert wakes == []                           # blocked: no wake for the same queue
        fake.queue = ["/media/stuck.mkv", "/media/new.mkv"]
        await src.poll()
        assert "blocked" not in ctl.state["stores"]["Tdarr"]
        assert len(wakes) == 1
        await teardown(server, ctl)
    asyncio.run(go())


def test_turned_off_by_hand_with_work_queued_blocks_the_wake():
    async def go():
        fake = FakeTdarr()
        fake.queue = ["/media/a.mkv"]
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = True
        await src.poll()
        src.on_machine_off("outside wakeproxy")
        ctl.state["pc_up"] = False
        await src.poll()
        assert wakes == [] and "turned off outside" in src.status()["note"]
        await teardown(server, ctl)
    asyncio.run(go())


def test_unreachable_server_releases_and_reports():
    async def go():
        fake = FakeTdarr()
        fake.workers = ["a.mkv"]
        server, ctl, clock, m, src, wakes = await setup(fake)
        ctl.state["pc_up"] = True
        await src.poll()
        assert ctl.active_holds()
        await server.close()
        await src.poll()
        assert ctl.active_holds() == [] and src.status()["rows"][0]["state"] == "bad"
        await ctl.http.close()
    asyncio.run(go())

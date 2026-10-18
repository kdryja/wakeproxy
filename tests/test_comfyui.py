"""The ComfyUI source against a fake ComfyUI server."""
import asyncio

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from helpers import make


class FakeComfyUI:
    def __init__(self):
        self.running, self.pending, self.freed, self.up = 0, 0, [], True

    def app(self):
        async def queue(request):
            if not self.up:
                return web.Response(status=502)
            return web.json_response({"queue_running": [[i, "id"] for i in range(self.running)],
                                      "queue_pending": [[i, "id"] for i in range(self.pending)]})

        async def free(request):
            self.freed.append(await request.json())
            return web.json_response({})

        app = web.Application()
        app.router.add_get("/queue", queue)
        app.router.add_post("/free", free)
        return app


async def setup(fake, **options):
    server = TestServer(fake.app())
    await server.start_server()
    ctl, clock, m = make(extra={"sources": [{"type": "comfyui", "url": str(server.make_url("")), **options}]})
    ctl.http = ClientSession()
    ctl.state["pc_up"] = True
    return server, ctl, clock, ctl.sources[0]


async def teardown(server, ctl):
    await ctl.http.close()
    await server.close()


def test_jobs_hold_the_machine_and_never_wake_it():
    async def go():
        fake = FakeComfyUI()
        server, ctl, clock, src = await setup(fake)
        assert src.wakes is False
        fake.running, fake.pending = 1, 2
        await src.poll()
        [h] = ctl.active_holds()
        assert h["source"] == "ComfyUI queue" and h["label"] == "1 running, 2 waiting"
        assert src.status()["state"] == "on"
        fake.running = fake.pending = 0
        await src.poll()
        assert ctl.active_holds() == [] and src.status()["summary"] == "queue empty"
        ctl.state["pc_up"] = False
        await src.poll()
        assert src.status()["summary"] == "machine off"
        await teardown(server, ctl)
    asyncio.run(go())


def test_unreachable_comfyui_releases_and_shows_the_error():
    async def go():
        fake = FakeComfyUI()
        server, ctl, clock, src = await setup(fake)
        fake.running = 1
        await src.poll()
        assert ctl.active_holds()
        fake.up = False
        await src.poll()
        assert ctl.active_holds() == []
        assert src.status() == {"summary": "waiting for ComfyUI to answer"}    # e.g. still starting
        clock.advance(300)
        await src.poll()
        assert src.status()["state"] == "bad" and "502" in src.status()["note"]
        fake.up = True
        await src.poll()
        assert src.status()["summary"] == "1 running" and ctl.active_holds()
        await teardown(server, ctl)
    asyncio.run(go())


def test_models_are_freed_once_after_the_queue_empties():
    async def go():
        fake = FakeComfyUI()
        server, ctl, clock, src = await setup(fake, free_after_minutes=5)
        fake.running = 1
        await src.poll()
        fake.running = 0
        await src.poll()                          # idle from now
        clock.advance(240)
        await src.poll()
        assert fake.freed == [] and "Unloading the models" in src.status()["note"]
        clock.advance(60)
        await src.poll()
        assert fake.freed == [{"unload_models": True, "free_memory": True}]
        assert "Models unloaded" in src.status()["note"]
        clock.advance(600)
        await src.poll()
        assert len(fake.freed) == 1               # once per idle period
        fake.running = 1
        await src.poll()
        fake.running = 0
        await src.poll()
        clock.advance(300)
        await src.poll()
        assert len(fake.freed) == 2
        await teardown(server, ctl)
    asyncio.run(go())


def test_models_stay_loaded_by_default():
    async def go():
        fake = FakeComfyUI()
        server, ctl, clock, src = await setup(fake)
        await src.poll()
        clock.advance(86400)
        await src.poll()
        assert fake.freed == [] and src.status()["note"] is None
        await teardown(server, ctl)
    asyncio.run(go())


def test_nothing_is_checked_while_shutting_down():
    async def go():
        fake = FakeComfyUI()
        server, ctl, clock, src = await setup(fake)
        fake.running = 1
        await src.poll()
        ctl.state["shutting_down"] = clock()
        fake.up = False
        await src.poll()
        assert ctl.active_holds() == [] and src.status() == {"summary": "machine off"}
        await teardown(server, ctl)
    asyncio.run(go())

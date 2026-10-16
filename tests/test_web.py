"""The status page's own routes."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from helpers import make
from wakeproxy import web


def test_page_logo_and_status():
    async def go():
        ctl, clock, m = make()
        client = TestClient(TestServer(web.make_app(ctl)))
        await client.start_server()
        r = await client.get("/")
        page = await r.text()
        assert r.status == 200 and 'href="logo.svg"' in page and 'id="machineName"' in page
        r = await client.get("/logo.svg")
        assert r.status == 200 and r.content_type == "image/svg+xml" and "<svg" in await r.text()
        r = await client.get("/api/status")
        assert (await r.json())["machine"]["name"] == "TestPC"
        r = await client.post("/api/wake", data="x")
        assert r.status == 415                       # JSON only
        r = await client.get("/api/history?days=1")
        assert r.status == 200 and "events" in await r.json()
        await client.close()
    asyncio.run(go())

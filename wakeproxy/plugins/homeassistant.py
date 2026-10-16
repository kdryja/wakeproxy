"""Power control through any Home Assistant service: wake_on_lan.send_magic_packet (handy when
wakeproxy runs in Docker and cannot send broadcasts itself), a smart plug, a script."""
from . import PowerOff, PowerOn


class HomeAssistant:
    def __init__(self, ctx, url, token, timeout):
        self.ctx, self.url, self.token, self.timeout = ctx, url.rstrip("/"), token, timeout

    async def call(self, service, data):
        domain, _, name = service.partition(".")
        if not name:
            raise ValueError(f"service must look like domain.name, got {service!r}")
        async with self.ctx.http.post(f"{self.url}/api/services/{domain}/{name}", json=data or {},
                                      headers={"Authorization": f"Bearer {self.token}"},
                                      timeout=self.timeout) as r:
            if r.status >= 400:
                raise RuntimeError(f"Home Assistant answered {r.status}: {(await r.text())[:200]}")


class HomeAssistantWake(PowerOn):
    type = "homeassistant"

    def __init__(self, ctx, *, url, token, service="wake_on_lan.send_magic_packet", data=None, timeout=10):
        super().__init__(ctx)
        self.ha, self.service, self.data = HomeAssistant(ctx, url, token, timeout), service, data

    def describe(self):
        if self.service == "wake_on_lan.send_magic_packet":
            return "Wake-on-LAN via Home Assistant"
        return f"Home Assistant {self.service}"

    async def power_on(self):
        await self.ha.call(self.service, self.data)


class HomeAssistantShutdown(PowerOff):
    """`force_service`/`force_data` (optional) are used when the shutdown must close programs."""
    type = "homeassistant"

    def __init__(self, ctx, *, url, token, service, data=None, force_service=None, force_data=None, timeout=10):
        super().__init__(ctx)
        self.ha = HomeAssistant(ctx, url, token, timeout)
        self.service, self.data = service, data
        self.force_service, self.force_data = force_service or service, force_data if force_data is not None else data

    def describe(self):
        return f"Home Assistant {self.service}"

    async def power_off(self, force):
        if force:
            await self.ha.call(self.force_service, self.force_data)
        else:
            await self.ha.call(self.service, self.data)

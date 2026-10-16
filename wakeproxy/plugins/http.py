"""Power control and checks through plain HTTP calls (a smart plug, a script's webhook, ...)."""
from . import Check, PowerOff, PowerOn


async def call(ctx, method, url, headers, body, timeout):
    async with ctx.http.request(method, url, headers=headers or {}, json=body, timeout=timeout) as r:
        if r.status >= 400:
            raise RuntimeError(f"{method} {url} answered {r.status}: {(await r.text())[:200]}")


class HttpWake(PowerOn):
    """Sends an HTTP request to wake the machine."""
    type = "http"

    def __init__(self, ctx, *, url, method="POST", headers=None, body=None, timeout=10):
        super().__init__(ctx)
        self.url, self.method, self.headers, self.body, self.timeout = url, method.upper(), headers, body, timeout

    def describe(self):
        return f"HTTP {self.method} {self.url.split('?')[0]}"

    async def power_on(self):
        await call(self.ctx, self.method, self.url, self.headers, self.body, self.timeout)


class HttpShutdown(PowerOff):
    """Sends an HTTP request to shut the machine down; `force_body` (optional) when forced."""
    type = "http"

    def __init__(self, ctx, *, url, method="POST", headers=None, body=None, force_body=None, timeout=10):
        super().__init__(ctx)
        self.url, self.method, self.headers, self.timeout = url, method.upper(), headers, timeout
        self.body, self.force_body = body, force_body

    def describe(self):
        return f"HTTP {self.method} {self.url.split('?')[0]}"

    async def power_off(self, force):
        body = self.force_body if force and self.force_body is not None else self.body
        await call(self.ctx, self.method, self.url, self.headers, body, self.timeout)


class HttpCheck(Check):
    """Up when a URL answers. By default any status below 500 counts; `status` lists exact ones."""
    type = "http"

    def __init__(self, ctx, *, url, status=None, timeout=3):
        super().__init__(ctx)
        self.url, self.status, self.timeout = url, status, timeout

    def describe(self):
        return f"{self.url} answers"

    async def check(self):
        try:
            async with self.ctx.http.get(self.url, timeout=self.timeout) as r:
                return r.status in self.status if self.status else r.status < 500
        except Exception:
            return False

"""ComfyUI (https://www.comfy.org): jobs in ComfyUI's queue keep the machine on.

For ComfyUI running on the machine:
    - type: comfyui
      url: http://mypc.lan:8188
      free_after_minutes: 0      # optional: unload ComfyUI's models this long after the queue
                                 # empties, so the GPU is free for other programs (0: never)

The queue lives in ComfyUI, so this source can't see work while the machine is off and never
wakes it. Send ComfyUI's clients through a route to wake it; one with `work.websockets` for clients
that open a websocket per job (Open WebUI). A client that keeps a websocket open all the time
(SwarmUI, ComfyUI's own page) needs a route without it, or it would hold the machine on for good;
this source then keeps the machine on while its jobs run.
"""
from . import Source, at


class ComfyUI(Source):
    type = "comfyui"
    title = "ComfyUI"
    interval = 15
    wakes = False

    def __init__(self, ctx, *, url, free_after_minutes=0):
        super().__init__(ctx)
        self.url, self.free_after = url.rstrip("/"), free_after_minutes * 60
        self.running = self.pending = 0
        self.error = self.checked = self.idle_since = self.freed_at = self.free_error = None

    async def poll(self):
        ctx, now = self.ctx, self.ctx.now()
        if not ctx.pc_up:
            ctx.release()
            self.running = self.pending = 0
            self.error = self.checked = self.idle_since = None
            return
        try:
            async with ctx.http.get(self.url + "/queue", timeout=10) as r:
                r.raise_for_status()
                q = await r.json(content_type=None)
        except Exception as e:
            self.error, self.checked = f"{type(e).__name__}: {e}"[:200], now
            ctx.release()
            return
        self.error, self.checked = None, now
        self.running, self.pending = len(q.get("queue_running") or []), len(q.get("queue_pending") or [])
        if self.running or self.pending:
            ctx.hold("work", self.summary())
            self.idle_since = self.freed_at = None
            return
        ctx.release()
        self.idle_since = self.idle_since or now
        if self.free_after and not self.freed_at and now - self.idle_since >= self.free_after:
            self.freed_at, self.free_error = now, None      # once per idle period, even if it fails
            try:
                # unload_models: out of VRAM; free_memory: out of RAM too
                async with ctx.http.post(self.url + "/free", json={"unload_models": True, "free_memory": True},
                                         timeout=10) as r:
                    r.raise_for_status()
            except Exception as e:
                self.free_error = f"{type(e).__name__}: {e}"[:200]

    def summary(self):
        parts = []
        if self.running:
            parts.append("%d running" % self.running)
        if self.pending:
            parts.append("%d waiting" % self.pending)
        return ", ".join(parts) or "queue empty"

    def status(self):
        if not self.ctx.pc_up:
            return {"summary": "machine off"}
        if self.error:
            return {"summary": "can't reach ComfyUI", "state": "bad", "note": self.error}
        if not self.checked:
            return {"summary": "checking…"}
        note = None
        if self.free_error:
            note = "Unloading the models failed at %s: %s" % (at(self.freed_at), self.free_error)
        elif self.freed_at:
            note = "Models unloaded at %s." % at(self.freed_at)
        elif self.free_after and self.idle_since:
            note = "Unloading the models at %s unless a job comes in." % at(self.idle_since + self.free_after)
        return {"summary": self.summary(), "state": "on" if self.running else None, "note": note}

"""Tdarr (https://tdarr.io): work in Tdarr's queue wakes the machine; a busy node keeps it on.

For a Tdarr server that runs elsewhere and a worker node on the machine:
    - type: tdarr
      url: http://tdarr:8265     # the server's web port
      node: MyPC                 # the node's name in Tdarr
      stuck_minutes: 15          # optional
      settle_minutes: 0          # optional: wait this long before waking

Pausing the node (or all nodes) in Tdarr stops the wakes, and so does a node schedule that allows
no workers in the current hour. Wakes for a queue are called off when the node took none of it
for stuck_minutes, or when the machine was turned off by hand while work was queued; any change
to the queue turns them back on.
"""
import os
import time
import zlib

from . import Source, at


def plural(n, word):
    return "%d %s%s" % (n, word, "s" * (n != 1))


class Tdarr(Source):
    type = "tdarr"
    title = "Tdarr"

    def __init__(self, ctx, *, url, node, stuck_minutes=15, settle_minutes=0):
        super().__init__(ctx)
        self.url, self.node = url.rstrip("/"), node
        self.stuck, self.settle = stuck_minutes * 60, settle_minutes * 60
        self.last, self.error, self.checked = {}, None, None
        self.idle_since = self.pending_since = None

    async def _post(self, path, data):
        async with self.ctx.http.post(self.url + path, json={"data": data}, timeout=10) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def fetch(self):
        out, ids = {}, []
        for key, table in (("transcode", "table1"), ("health", "table4")):    # the two queues
            d = await self._post("/api/v2/client/status-tables", {
                "start": 0, "pageSize": 200, "filters": [], "sorts": [], "opts": {"table": table}})
            out[key] = d.get("totalCount") or 0
            ids += [table + ":" + x["_id"] for x in d.get("array", [])]
        out["queued"] = out["transcode"] + out["health"]
        out["sig"] = "%d|%08x" % (out["queued"], zlib.crc32("\n".join(sorted(ids)).encode()))

        async with self.ctx.http.get(self.url + "/api/v2/get-nodes", timeout=10) as r:
            nodes = await r.json(content_type=None) or {}
        node = next((n for n in nodes.values() if n.get("nodeName") == self.node), None)
        out["connected"] = node is not None
        out["jobs"] = [{"type": w.get("workerType"), "file": os.path.basename(w.get("file") or ""),
                        "percentage": w.get("percentage"), "step": w.get("status")}
                       for w in (node or {}).get("workers", {}).values()]
        out["workers"] = len(out["jobs"])

        # The node's settings are on the server even while the node is off.
        settings = await self._post("/api/v2/cruddb", {
            "collection": "NodeJSONDB", "mode": "getById", "docID": self.node}) or {}
        glob = await self._post("/api/v2/cruddb", {
            "collection": "SettingsGlobalJSONDB", "mode": "getById", "docID": "globalsettings"}) or {}
        out["paused"] = None
        if settings.get("nodePaused"):
            out["paused"] = "the node is paused in Tdarr"
        elif glob.get("pauseAllNodes"):
            out["paused"] = "all nodes are paused in Tdarr"
        elif settings.get("scheduleEnabled") and not glob.get("ignoreSchedules"):
            hour = "%02d-" % time.localtime().tm_hour
            slot = next((x for x in settings.get("schedule") or []
                         if x.get("_id", "").startswith(hour)), None)
            if slot and not any(v for k, v in slot.items() if k != "_id"):
                out["paused"] = "the node's schedule allows no workers this hour"
        out["work"] = out["queued"] > 0 and not out["paused"]
        return out

    def block(self, reason):
        sig = self.last.get("sig")
        if sig and (self.ctx.store.get("blocked") or {}).get("sig") != sig:
            self.ctx.store["blocked"] = {"sig": sig, "reason": reason, "since": self.ctx.now()}
            self.ctx.save()
            self.ctx.event("No more wakes for this queue until it changes: " + reason, "warn")

    async def poll(self):
        ctx, now = self.ctx, self.ctx.now()
        try:
            q = await self.fetch()
        except Exception as e:
            self.error, self.checked = f"{type(e).__name__}: {e}"[:200], now
            ctx.release()
            return
        self.last, self.error, self.checked = q, None, now
        blocked = ctx.store.get("blocked")
        if blocked and blocked["sig"] != q["sig"]:
            ctx.store.pop("blocked")                       # the queue changed: wakes are back on
            ctx.save()
            blocked = None
        if q["workers"] or not ctx.pc_up:
            self.idle_since = None

        if not ctx.pc_up:
            ctx.release()
            if not q["work"] or blocked or ctx.waking or ctx.shutting_down:
                self.pending_since = None
                return
            self.pending_since = self.pending_since or now
            if now - self.pending_since >= self.settle:
                self.pending_since = None
                ctx.wake(plural(q["queued"], "file") + " queued")
            return

        self.pending_since = None
        if q["workers"]:
            ctx.hold("work", plural(q["workers"], "job") + " running",
                     detail="; ".join(j["file"] for j in q["jobs"]))
        elif q["work"] and not blocked:
            # Work is queued but no worker has it yet: the node may still be starting. Wait for it,
            # but not forever, or a file the node never takes would keep the machine on for good.
            self.idle_since = self.idle_since or now
            if now - self.idle_since < self.stuck:
                ctx.hold("work", plural(q["queued"], "file") + " queued, waiting for the node")
            else:
                ctx.release()
                self.block("work was queued for %d minutes but the node took none of it"
                           % (self.stuck // 60))
        else:
            ctx.release()

    def on_machine_off(self, by):
        if by == "outside wakeproxy" and self.last.get("work"):
            self.block("the machine was turned off outside wakeproxy")

    def status(self):
        if self.error:
            return {"summary": "can't reach the Tdarr server", "state": "bad", "note": self.error}
        if not self.checked:
            return {"summary": "checking…"}
        q = self.last
        queue = ("%d queued (%d transcode, %d health check)" % (q["queued"], q["transcode"], q["health"])
                 if q["queued"] else "queue empty")
        node = "node %s %s" % (self.node, "connected" if q["connected"] else "offline")
        rows = []
        for j in q["jobs"]:
            pct = j["percentage"]
            rows.append({"text": j["file"], "detail": j["step"] or "", "state": "on",
                         "right": (j["type"] or "") + (" · %d%%" % pct if pct else "")})
        blocked, note = self.ctx.store.get("blocked"), None
        if blocked:
            note = ("Won't wake the machine for this queue: %s (%s). Any change to the queue turns wakes "
                    "back on." % (blocked["reason"], at(blocked["since"])))
        elif q["paused"]:
            note = "Won't wake the machine: %s." % q["paused"]
        elif self.pending_since:
            note = "Waking the machine at %s unless the queue empties first." % at(self.pending_since + self.settle)
        elif self.idle_since:
            note = ("Waiting for the node to take the queued work; gives up at %s."
                    % at(self.idle_since + self.stuck))
        return {"summary": queue + " · " + node, "state": "on" if q["connected"] else None,
                "rows": rows, "note": note}


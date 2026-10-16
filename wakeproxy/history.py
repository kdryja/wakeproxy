"""SQLite storage: the event log, the request log, and small saved values (state, caches)."""
import json
import sqlite3
import time


class History:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, start REAL, end REAL, client TEXT,
            route TEXT, method TEXT, path TEXT, model TEXT, status INTEGER, error TEXT, woke INTEGER);
        CREATE INDEX IF NOT EXISTS requests_start ON requests(start);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)

    # --- events ---------------------------------------------------------------------------

    def event(self, kind, **detail):
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (self.clock(), kind, json.dumps(detail)))

    def events(self, since, limit=1000):
        return [{"ts": ts, "kind": kind, **json.loads(detail)} for ts, kind, detail in self.db.execute(
            "SELECT ts, kind, detail FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit))]

    def sessions(self, since, until):
        """Periods the machine was on between since and until, from the on/off events."""
        row = self.db.execute("SELECT ts, kind, detail FROM events WHERE ts < ? AND kind IN "
                              "('pc_on', 'pc_off', 'proxy_start') ORDER BY ts DESC LIMIT 1",
                              (since,)).fetchone()
        cur = None
        if row and (row[1] == "pc_on" or (row[1] == "proxy_start" and json.loads(row[2]).get("pc_up"))):
            cur = {"start": since, "by": json.loads(row[2]).get("by", "before this period"), "clipped": True}
        out = []
        for ts, kind, detail in self.db.execute(
                "SELECT ts, kind, detail FROM events WHERE ts >= ? AND ts < ? AND kind IN "
                "('pc_on', 'pc_off', 'proxy_start', 'shutdown') ORDER BY ts", (since, until)):
            d = json.loads(detail)
            if kind == "pc_on" and cur is None:
                cur = {"start": ts, "by": d.get("by")}
            elif kind == "proxy_start" and d.get("pc_up") and cur is None:
                cur = {"start": ts, "by": "unknown (wakeproxy restarted)"}
            elif kind == "shutdown" and cur is not None and d.get("ok"):
                cur["reason"] = d.get("reason")
            elif kind in ("pc_off", "proxy_start") and cur is not None and not d.get("pc_up"):
                cur.update(end=ts, off_by=d.get("by", "unknown (wakeproxy restarted)"))
                out.append(cur)
                cur = None
        if cur is not None:
            cur["end"] = None
            out.append(cur)
        return out

    # --- requests -------------------------------------------------------------------------

    def start_request(self, rec):
        return self.db.execute(
            "INSERT INTO requests (start, client, route, method, path) VALUES (?, ?, ?, ?, ?)",
            (rec["start"], rec["client"], rec["route"], rec["method"], rec["path"])).lastrowid

    def finish_request(self, rec):
        self.db.execute("UPDATE requests SET end = ?, model = ?, status = ?, error = ?, woke = ? WHERE id = ?",
                        (self.clock(), rec["model"], rec["status"], rec["error"], rec["woke"], rec["id"]))

    def requests(self, since, limit=500):
        cols = ("id", "start", "end", "client", "route", "method", "path", "model", "status", "error", "woke")
        return [dict(zip(cols, row)) for row in self.db.execute(
            f"SELECT {', '.join(cols)} FROM requests WHERE start >= ? ORDER BY start DESC LIMIT ?",
            (since, limit))]

    def by_client(self, since):
        return [dict(zip(("client", "route", "count", "errors", "seconds"), row)) for row in self.db.execute(
            "SELECT client, route, COUNT(*), SUM(error IS NOT NULL OR status >= 400), SUM(end - start) "
            "FROM requests WHERE start >= ? GROUP BY client, route ORDER BY 3 DESC", (since,))]

    def prune(self, request_days, event_days):
        now = self.clock()
        self.db.execute("DELETE FROM requests WHERE start < ?", (now - request_days * 86400,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (now - event_days * 86400,))

    # --- saved values ---------------------------------------------------------------------

    def get(self, key, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

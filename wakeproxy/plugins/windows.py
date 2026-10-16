"""Windows: don't shut down under someone who is using the machine."""
import asyncio
import time

from . import Source
from .ssh import Ssh


class WindowsSession(Source):
    """A signed-in, unlocked Windows session holds off the shutdown (checked again every idle
    period), for `max_hours` at most; then the shutdown goes ahead anyway. A signed-in but locked
    session does not hold it off, but makes the shutdown forced (/f), so open programs cannot stop
    it. Checked only when the machine is about to be shut down, over SSH.

    The default commands suit Windows' own OpenSSH server; through WSL give the full paths,
    e.g. /mnt/c/Windows/System32/query.exe session."""
    type = "windows_session"
    title = "Windows session"
    wakes = False

    def __init__(self, ctx, *, target, key=None, port=22, known_hosts=None, max_hours=3,
                 session_command="query session",
                 lock_command='tasklist /FI "IMAGENAME eq LogonUI.exe"'):
        super().__init__(ctx)
        self.ssh = Ssh(target, key, port, known_hosts)
        self.max_seconds = max_hours * 3600
        self.session_command, self.lock_command = session_command, lock_command
        self.since = None          # when an unlocked session was first seen holding off

    async def signed_in(self):
        try:
            code, out, _ = await self.ssh.run(self.session_command, 15)
            return "Active" in out and "console" in out.lower()
        except Exception:
            return True            # unsure: rather not shut down

    async def locked(self):
        """Windows is at the lock or sign-in screen (LogonUI.exe running)."""
        try:
            code, out, _ = await self.ssh.run(self.lock_command, 15)
            return "LogonUI.exe" in out
        except Exception:
            return False           # unsure: assume someone is there

    async def before_shutdown(self):
        ctx = self.ctx
        self.force_shutdown = False
        if not await self.signed_in():
            self.since = None
            return
        self.force_shutdown = True
        if await self.locked():
            await asyncio.sleep(10)          # LogonUI can flash up during UAC prompts and sign-in
            if await self.locked():
                self.since = None
                return
        now = ctx.now()
        if self.since is None:
            self.since = now
            ctx.event("Shutdown held off: someone is signed in and the screen is unlocked "
                      f"(shuts down anyway at {hhmm(now + self.max_seconds)})")
        if now - self.since > self.max_seconds:
            ctx.event(f"Unlocked for over {self.max_seconds / 3600:g} h: shutting down anyway", "warn")
            self.since = None
            return
        idle = ctx.idle_seconds
        # No grace: when the hold ends the timer has already run out, so this check runs again.
        ctx.hold("session", "Someone is signed in and the screen is unlocked",
                 detail=f"checked every {idle // 60} min; shuts down anyway at {hhmm(self.since + self.max_seconds)}",
                 until=min(now + idle, self.since + self.max_seconds), grace=False)

    async def poll(self):
        if not self.ctx.pc_up or not self.ctx.managed:
            self.since = None
            self.ctx.release()


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))

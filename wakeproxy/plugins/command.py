"""Power control and checks through shell commands run inside wakeproxy's container."""
import asyncio

from . import Check, PowerOff, PowerOn


async def run(command, timeout):
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"timed out after {timeout} s: {command}")
    return proc.returncode, out.decode(errors="replace").strip()


async def run_ok(command, timeout):
    code, out = await run(command, timeout)
    if code:
        raise RuntimeError(out[-300:] or f"exit code {code}")


class CommandWake(PowerOn):
    type = "command"

    def __init__(self, ctx, *, command, timeout=30):
        super().__init__(ctx)
        self.command, self.timeout = command, timeout

    def describe(self):
        return "command"

    async def power_on(self):
        await run_ok(self.command, self.timeout)


class CommandShutdown(PowerOff):
    """`force_command` (optional) is used when the shutdown must close running programs."""
    type = "command"

    def __init__(self, ctx, *, command, force_command=None, timeout=30):
        super().__init__(ctx)
        self.command, self.force_command, self.timeout = command, force_command, timeout

    def describe(self):
        return "command"

    async def power_off(self, force):
        await run_ok(self.force_command if force and self.force_command else self.command, self.timeout)


class CommandCheck(Check):
    """Up when the command exits with 0."""
    type = "command"

    def __init__(self, ctx, *, command, timeout=5):
        super().__init__(ctx)
        self.command, self.timeout = command, timeout

    async def check(self):
        try:
            return (await run(self.command, self.timeout))[0] == 0
        except Exception:
            return False

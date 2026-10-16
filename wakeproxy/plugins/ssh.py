"""Shutting down (and checking) over SSH."""
import asyncio

from . import Check, PowerOff


class Ssh:
    """Runs commands on the machine over SSH with a key. `known_hosts`: a file to remember the
    machine's host key in (first contact is accepted, a changed key is refused); without it the
    key is accepted every time wakeproxy restarts."""

    def __init__(self, target, key=None, port=22, known_hosts=None, connect_timeout=10):
        self.target = target
        self.options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
                        "-o", "StrictHostKeyChecking=accept-new", "-p", str(port)]
        if key:
            self.options += ["-i", key]
        if known_hosts:
            self.options += ["-o", f"UserKnownHostsFile={known_hosts}"]

    async def run(self, command, timeout=30):
        """(exit code, output) of `command` on the machine."""
        proc = await asyncio.create_subprocess_exec(
            "ssh", *self.options, self.target, command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"ssh timed out after {timeout} s")
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class SshShutdown(PowerOff):
    """Runs a shutdown command over SSH. Defaults suit Windows reached through its own OpenSSH
    server; through WSL, give the full paths (/mnt/c/Windows/System32/shutdown.exe ...)."""
    type = "ssh"

    def __init__(self, ctx, *, target, key=None, port=22, known_hosts=None,
                 command="shutdown /s /t 20", force_command="shutdown /s /f /t 20", timeout=30):
        super().__init__(ctx)
        self.ssh = Ssh(target, key, port, known_hosts)
        self.target, self.command, self.force_command, self.timeout = target, command, force_command, timeout

    def describe(self):
        return f"command over SSH to {self.target}"

    async def power_off(self, force):
        code, out, err = await self.ssh.run(self.force_command if force else self.command, self.timeout)
        if code:
            raise RuntimeError((err or out).strip()[:300] or f"exit code {code}")


class SshCheck(Check):
    """Up when a command over SSH succeeds (default: just logging in)."""
    type = "ssh"

    def __init__(self, ctx, *, target, key=None, port=22, known_hosts=None, command="true", timeout=10):
        super().__init__(ctx)
        self.ssh = Ssh(target, key, port, known_hosts, connect_timeout=min(timeout, 5))
        self.command, self.timeout = command, timeout

    async def check(self):
        try:
            return (await self.ssh.run(self.command, self.timeout))[0] == 0
        except Exception:
            return False

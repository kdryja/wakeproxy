"""Wake-on-LAN and TCP port checks."""
import asyncio
import re
import socket

from . import Check, PowerOn


class WakeOnLan(PowerOn):
    """Sends a Wake-on-LAN magic packet. The packet is a broadcast, so it only reaches machines on
    the same network segment: in Docker this needs `network_mode: host` (or use the homeassistant
    plugin, which sends it from Home Assistant)."""
    type = "wol"

    def __init__(self, ctx, *, mac, broadcast="255.255.255.255", port=9):
        super().__init__(ctx)
        digits = re.sub(r"[^0-9a-fA-F]", "", mac)
        if len(digits) != 12:
            raise ValueError(f"not a MAC address: {mac!r}")
        self.mac, self.packet = mac, b"\xff" * 6 + bytes.fromhex(digits) * 16
        self.broadcast, self.port = broadcast, port

    def describe(self):
        return f"Wake-on-LAN to {self.mac}"

    async def power_on(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(self.packet, (self.broadcast, self.port))


class TcpCheck(Check):
    """Up when a TCP port accepts connections (e.g. SSH on 22)."""
    type = "tcp"

    def __init__(self, ctx, *, host, port, timeout=2):
        super().__init__(ctx)
        self.host, self.port, self.timeout = host, port, timeout

    def describe(self):
        return f"{self.host}:{self.port} accepts connections"

    async def check(self):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
            w.close()
            return True
        except Exception:
            return False

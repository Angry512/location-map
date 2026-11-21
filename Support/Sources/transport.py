"""Own one raw IPv6 tunnel and its unprivileged TCP stack."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import struct

from tcp_stack import UserspaceTun, UserspaceDialPlane


class TunnelNetwork:
    def __init__(self, raw):
        self.raw = raw
        self.tun = None
        self.plane = None
        self.tasks = []
        self.closed = asyncio.Event()
        self._closing = False
        self._close_task = None

    async def open(self):
        # Reject malformed handshake addresses before the stack sees them.
        client = ipaddress.IPv6Address(self.raw.client_address)
        peer = ipaddress.IPv6Address(self.raw.server_address)
        if client.is_unspecified or client.is_multicast or peer.is_unspecified or peer.is_multicast:
            raise ValueError('Invalid tunnel address')
        self.tun = UserspaceTun()
        self.tun.addr = str(client)
        self.tun.mtu = min(max(int(self.raw.mtu), 1280), 16000)
        try:
            await self.tun.up()
            self.tun.set_peer(str(peer))
            self.plane = UserspaceDialPlane(self.tun, str(peer))
            await self.plane.__aenter__()
            self.tasks = [asyncio.create_task(self._receive(), name='tunnel-ipv6-receive'),
                          asyncio.create_task(self._send(), name='tunnel-ipv6-send')]
            return self
        except BaseException:
            await self.aclose()
            raise

    async def _receive(self):
        try:
            while True:
                header = await self.raw.reader.readexactly(40)
                if header[0] >> 4 != 6:
                    raise ValueError('Invalid tunnel packet')
                length = struct.unpack('!H', header[4:6])[0]
                if length == 0 or length + 40 > 65535:
                    raise ValueError('Invalid tunnel packet size')
                body = await self.raw.reader.readexactly(length)
                self.tun.write(header + body)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.closed.set()

    async def _send(self):
        try:
            while True:
                packet = await self.tun.async_read()
                if not packet or packet[0] >> 4 != 6:
                    raise ValueError('Invalid outgoing tunnel packet')
                self.raw.writer.write(packet)
                await self.raw.writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.closed.set()

    async def dial(self, host, port):
        if self._closing or self.closed.is_set() or self.plane is None:
            raise ConnectionError('Device tunnel is closed')
        if str(host) != str(ipaddress.IPv6Address(self.raw.server_address)):
            raise ValueError('Only this device tunnel can be dialed')
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('Invalid device port')
        return await asyncio.wait_for(self.plane.dial(host, port), 20)

    async def aclose(self):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # A Stop followed immediately by engine shutdown must finish closing
            # the stack and retained pairing socket before cancellation escapes.
            await asyncio.shield(self._close_task)
            raise

    async def _cleanup(self):
        self.closed.set()
        if self.plane is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.plane.__aexit__(None, None, None), 2)
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.tun is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.tun.close(), 2)
        with suppress(Exception):
            await asyncio.wait_for(self.raw.aclose(), 2)


class DeviceSession:
    def __init__(self, network, location=None, asset_directory=None):
        self.network = network
        self.location = location
        self.asset_directory = asset_directory

    @classmethod
    async def open_link(cls, pairing_path, asset_directory=None):
        """Authenticate and hold the local link without setting a location or mounting a DDI."""
        from remote_pair import open_tunnel
        raw = await open_tunnel(pairing_path)
        network = TunnelNetwork(raw)
        try:
            await network.open()
            return cls(network, asset_directory=asset_directory)
        except BaseException:
            await network.aclose()
            raise

    @classmethod
    async def connect(cls, pairing_path, asset_directory=None):
        session = await cls.open_link(pairing_path, asset_directory)
        try:
            await session.prepare_location()
            return session
        except BaseException:
            await session.aclose()
            raise

    @property
    def link_alive(self):
        return not self.network.closed.is_set()

    async def prepare_location(self):
        if self.location is not None:
            return
        from services import connect_location, DeveloperServiceUnavailable
        raw = self.network.raw
        args = self.network.dial, raw.server_address, raw.rsd_port
        try:
            self.location = await connect_location(*args)
        except DeveloperServiceUnavailable:
            from pathlib import Path
            from ddi import prepare_ddi
            assets = self.asset_directory or Path(__file__).resolve().parent / 'ddi'
            await prepare_ddi(*args, assets)
        else:
            return

        # The image can be installed before a new RSD connection sees the
        # location service. Re-discover it on the same authenticated tunnel.
        for delay in (0, .5, 1, 2, 4):
            if delay:
                await asyncio.sleep(delay)
            try:
                self.location = await connect_location(*args)
                return
            except DeveloperServiceUnavailable:
                if not self.link_alive:
                    raise
        raise DeveloperServiceUnavailable('The developer image is installed but the location service is unavailable.')

    async def set(self, lat, lon):
        if self.network.closed.is_set():
            raise ConnectionError('Device connection was lost')
        await self.prepare_location()
        await self.location.set(lat, lon)

    async def clear(self):
        if self.network.closed.is_set():
            raise ConnectionError('Device connection was lost')
        await self.prepare_location()
        await self.location.clear()

    async def clear_if_open(self):
        """Try to clear an existing service without starting image recovery."""
        if self.location is None or not self.link_alive:
            return False
        await self.location.clear()
        return True

    async def aclose(self):
        if self.location is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.location.aclose(), 2)
        await self.network.aclose()

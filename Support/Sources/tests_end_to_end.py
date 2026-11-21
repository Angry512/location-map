"""Actual RemotePairing/TLS -> IPv6/PyTCP -> RSD/DTX -> Set/Stop pipeline.

Only the outer endpoint is redirected to a generated-key localhost peer and the
iOS audio boundary is replaced in the Controller test. Pairing, TLS, raw packet
transport, userspace TCP, DeviceSession, discovery, DTX, and Controller are real.
No phone, real pairing record, Apple TSS, or location side effects are involved.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import ipaddress
from pathlib import Path
import plistlib
import ssl
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).with_name("vendor")))
import remote_pair
import services
from core import Controller
from transport import DeviceSession
from tests_remote_pair import MockPeer, HOST_KEY, HOST_ID, DEVICE_KEY, DEVICE_ID, raw, frame, receive
from tests_services import MockServices
from tests_transport import checksum, pseudo, segment, CLIENT, DEVICE, SYN, ACK, FIN, RST


@dataclass
class ForwardConnection:
    source_port: int
    destination_port: int
    receive_next: int
    send_next: int
    initial_send: int
    reader: object
    writer: object
    pump: object = None
    client_fin: bool = False
    peer_fin: bool = False


class ForwardingTCPPeer:
    """RFC checksum/sequence peer forwarding two virtual services to stdlib TCP."""

    def __init__(self, ports):
        self.ports = set(ports)
        self.connections = {}
        self.errors = []
        self.wire_bytes = {port: [0, 0] for port in ports}
        self.validated_packets = 0
        self.fin_count = 0
        self.closed = asyncio.Event()
        self.outer_writer = None

    def send(self, connection, flags, data=b"", *, sequence=None, syn=False):
        self.outer_writer.write(segment(connection.destination_port, connection.source_port,
            connection.send_next if sequence is None else sequence,
            connection.receive_next, flags, data, syn=syn, window_scale=None))

    async def pump(self, connection):
        try:
            while True:
                payload = await connection.reader.read(1220)
                if not payload:
                    if not connection.peer_fin:
                        self.send(connection, FIN | ACK)
                        connection.send_next = (connection.send_next + 1) & 0xffffffff
                        connection.peer_fin = True
                        await self.outer_writer.drain()
                    break
                self.wire_bytes[connection.destination_port][1] += len(payload)
                self.send(connection, ACK | 8, payload)
                connection.send_next = (connection.send_next + len(payload)) & 0xffffffff
                await self.outer_writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.errors.append(exc)

    async def serve(self, reader, writer):
        self.outer_writer = writer
        try:
            while True:
                header = await reader.readexactly(40)
                assert header[0] >> 4 == 6
                size = int.from_bytes(header[4:6], "big")
                assert 0 < size <= 65535 - 40
                body = await reader.readexactly(size)
                if header[6] != 6:
                    continue
                assert size + 40 <= 1500
                source, destination = (str(ipaddress.IPv6Address(header[n:n + 16])) for n in (8, 24))
                assert source == CLIENT and destination == DEVICE
                assert checksum(pseudo(source, destination, size) + body) == 0
                self.validated_packets += 1
                sport, dport, seq, _, offset, flags, _, _, _ = struct.unpack("!HHIIBBHHH", body[:20])
                header_size = (offset >> 4) * 4
                assert 20 <= header_size <= len(body)
                payload = body[header_size:]
                assert len(payload) <= 1220
                assert dport in self.ports
                key = (sport, dport)
                connection = self.connections.get(key)
                if flags & SYN:
                    if connection is None:
                        app_reader, app_writer = await asyncio.open_connection("127.0.0.1", dport)
                        initial = 100000 + len(self.connections) * 1000000
                        connection = ForwardConnection(sport, dport, (seq + 1) & 0xffffffff,
                                                       initial + 1, initial, app_reader, app_writer)
                        self.connections[key] = connection
                        connection.pump = asyncio.create_task(self.pump(connection))
                    self.send(connection, SYN | ACK, sequence=connection.initial_send, syn=True)
                    await writer.drain()
                    continue
                assert connection is not None
                if flags & RST:
                    connection.writer.close()
                    connection.pump.cancel()
                    continue
                if payload:
                    if seq == connection.receive_next:
                        connection.receive_next = (connection.receive_next + len(payload)) & 0xffffffff
                        self.wire_bytes[dport][0] += len(payload)
                        connection.writer.write(payload)
                        self.send(connection, ACK)
                        await connection.writer.drain()
                    else:
                        self.send(connection, ACK)
                if flags & FIN:
                    if not connection.client_fin and ((seq + len(payload)) & 0xffffffff) == connection.receive_next:
                        connection.receive_next = (connection.receive_next + 1) & 0xffffffff
                        connection.client_fin = True
                        self.fin_count += 1
                        if connection.writer.can_write_eof():
                            connection.writer.write_eof()
                        else:
                            connection.writer.close()
                    self.send(connection, ACK)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            for connection in self.connections.values():
                connection.writer.close()
                connection.pump.cancel()
            if self.connections:
                await asyncio.gather(*(c.pump for c in self.connections.values()), return_exceptions=True)
            self.closed.set()
            writer.close()


class FullPeer(MockPeer):
    def __init__(self, app_services):
        super().__init__()
        self.app_services = app_services
        self.forwarder = None

    async def tls(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.append(writer)
        try:
            assert await receive(reader, b"CDTunnel") == {"type": "clientHandshakeRequest", "mtu": 16000}
            writer.write(frame({"type": "serverHandshakeResponse", "clientParameters": {
                "address": CLIENT, "netmask": "ffff:ffff:ffff:ffff::", "mtu": 1500},
                "serverAddress": DEVICE, "serverRSDPort": self.app_services.rsd_port}, b"CDTunnel"))
            await writer.drain()
            self.forwarder = ForwardingTCPPeer((self.app_services.rsd_port, self.app_services.dvt_port))
            await self.forwarder.serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            writer.close()
            self.tasks.discard(task)


async def until(predicate, seconds=6):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError("Pipeline state did not arrive before the test deadline")


@unittest.skipUnless(getattr(ssl, "HAS_PSK", False), "Python 3.13/OpenSSL PSK required")
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "generated-test-record.plist"
        self.path.write_bytes(plistlib.dumps({
            "identifier": HOST_ID.decode(), "private_key": b"h" * 32,
            "public_key": raw(HOST_KEY.public_key()), "peer_identifier": DEVICE_ID.decode(),
            "peer_public_key": raw(DEVICE_KEY.public_key()), "alt_irk": b"i" * 16}))
        self.app_services = await MockServices().start()
        self.peer = await FullPeer(self.app_services).start()
        actual_open = remote_pair.open_tunnel
        async def local_endpoint(path):
            # Redirect the PC's absent LocalDevVPN endpoint only. All cryptography,
            # frame parsing, TLS, and production client code still execute.
            return await actual_open(path, "127.0.0.1", self.peer.port)
        self.endpoint = mock.patch.object(remote_pair, "open_tunnel", new=local_endpoint)
        self.endpoint.start()
        self.session = None
        self.controller = None

    async def asyncTearDown(self):
        if self.controller:
            await asyncio.to_thread(self.controller.close)
        if self.session:
            await self.session.aclose()
        self.endpoint.stop()
        await asyncio.wait_for(self.peer.close(), 3)
        await asyncio.wait_for(self.app_services.aclose(), 3)
        self.directory.cleanup()

    def assert_full_wire_pipeline(self):
        self.assertTrue(self.peer.host_proof_received)
        self.assertFalse(self.peer.errors, repr(self.peer.errors))
        self.assertIsNotNone(self.peer.forwarder)
        self.assertFalse(self.peer.forwarder.errors, repr(self.peer.forwarder.errors))
        self.assertGreater(self.peer.forwarder.validated_packets, 8)
        for counts in self.peer.forwarder.wire_bytes.values():
            self.assertGreater(counts[0], 0)
            self.assertGreater(counts[1], 0)

    async def test_actual_crypto_tcp_rsd_dtx_set_and_stop(self):
        self.session = await asyncio.wait_for(DeviceSession.connect(self.path), 8)
        self.assertTrue(self.session.network.raw.peer_authenticated)
        cipher = self.session.network.raw.writer.get_extra_info("ssl_object").cipher()[0]
        self.assertEqual(cipher, "PSK-AES256-CBC-SHA384")
        await asyncio.wait_for(self.session.set(38.9, -77.0), 5)
        await asyncio.wait_for(self.session.set(-33.8, 151.2), 5)
        await asyncio.wait_for(self.session.clear(), 5)
        self.assertEqual(self.app_services.events, [
            (services.SET_SELECTOR, [38.9, -77.0]),
            (services.SET_SELECTOR, [-33.8, 151.2]),
            (services.CLEAR_SELECTOR, [])])
        self.assert_full_wire_pipeline()
        await self.session.aclose()
        await asyncio.wait_for(self.peer.control_closed.wait(), 3)
        await asyncio.wait_for(self.peer.forwarder.closed.wait(), 3)
        self.assertTrue(self.session.network.closed.is_set())

    async def test_controller_freshness_and_stop_through_entire_wire_pipeline(self):
        class NativeAudioBoundary:
            def __init__(self): self.playing = False
            def start(self): self.playing = True
            def stop(self): self.playing = False
            def status(self): return {"playing": self.playing}
        # This runs the real DeviceSession and PyTCP stack on the Controller's
        # dedicated event loop; fixture servers stay on this test's event loop.
        async def connect_generated_fixture(path):
            try:
                return await DeviceSession.connect(path)
            except Exception as error:
                # This fixture uses generated public test identities only.
                print('PIPELINE_FAILURE', type(error).__name__, str(error), flush=True)
                raise
        self.controller = Controller(self.path, Path(self.directory.name) / "state.json",
                                     session_factory=connect_generated_fixture,
                                     audio_factory=NativeAudioBoundary).start()
        self.controller.set(51.5, -.12)
        await until(lambda: self.controller.snapshot()["state"] in ("active", "error"))
        state = self.controller.snapshot()
        self.assertEqual(state["state"], "active", state["message"])
        self.assertTrue(state["fresh"])
        self.assertEqual(state["confirmation_source"], "developer_service_ack")
        self.controller.stop()
        await until(lambda: self.controller.snapshot()["state"] in ("idle", "error"))
        self.assertEqual(self.controller.snapshot()["state"], "idle")
        self.assertFalse(self.controller.snapshot()["fresh"])
        self.assertFalse(self.controller.snapshot()["requires_stop"])
        self.assertEqual(self.app_services.events, [
            (services.SET_SELECTOR, [51.5, -.12]), (services.CLEAR_SELECTOR, [])])
        self.assert_full_wire_pipeline()
        await asyncio.to_thread(self.controller.close)
        self.assertFalse(self.controller._thread.is_alive())
        await asyncio.wait_for(self.peer.control_closed.wait(), 3)

    async def test_prepared_link_is_reused_without_sending_an_early_location(self):
        from tests_core import Audio

        async def reject_cold_connection(_):
            raise ConnectionError('Fresh connection unavailable after cellular data resumed')

        self.controller = Controller(self.path, Path(self.directory.name) / 'state.json',
                                     session_factory=reject_cold_connection,
                                     link_factory=DeviceSession.open_link,
                                     audio_factory=Audio).start()
        self.controller.set_foreground(True)
        await until(lambda: self.controller.snapshot()['link_ready'])
        probe = self.controller.probe_location_service()
        self.assertTrue(await asyncio.wait_for(asyncio.wrap_future(probe), 8))
        self.assertEqual(self.app_services.events, [])
        self.assertFalse(self.controller.snapshot()['requires_stop'])
        for point in ((51.5, -.12), (51.6, -.13)):
            self.controller.set(*point)
            await until(lambda: self.controller.snapshot()['state'] in ('active', 'error'))
            self.assertEqual(self.controller.snapshot()['state'], 'active')
            self.controller.stop()
            await until(lambda: self.controller.snapshot()['state'] in ('idle', 'error'))
            self.assertEqual(self.controller.snapshot()['state'], 'idle')
        self.assertEqual(self.app_services.events, [
            (services.SET_SELECTOR, [51.5, -.12]), (services.CLEAR_SELECTOR, []),
            (services.SET_SELECTOR, [51.6, -.13]), (services.CLEAR_SELECTOR, [])])
        self.assertFalse(self.peer.control_closed.is_set())
        await asyncio.to_thread(self.controller.close)
        await asyncio.wait_for(self.peer.control_closed.wait(), 3)


if __name__ == "__main__":
    unittest.main()

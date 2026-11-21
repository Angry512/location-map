"""Loopback protocol test of setup with the real bundled HouseArrest and AFC clients."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import plistlib
import struct
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pymobiledevice3.exceptions import ConnectionTerminatedError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.service_connection import ServiceConnection

import setup_phone as setup


HEADER = struct.Struct('<8sQQQQ')
NUMBER = struct.Struct('<Q')
MAGIC = b'CFA6LPAA'
STATUS, DATA = 1, 2
REMOVE, MKDIR, STAT = 8, 9, 10
OPEN, OPENED, READ, WRITE, CLOSE, RENAME = 13, 14, 15, 16, 20, 24
SUCCESS, NOT_FOUND = 0, 8


def decode_path(payload):
    assert payload.endswith(b'\0')
    return payload[:-1].decode('utf-8')


def status(code):
    return STATUS, NUMBER.pack(code)


class WirePeer:
    """Independent in-memory device side. No usbmux, real phone or Apple account."""

    def __init__(self, interrupt_path):
        self.files = {
            str(setup.PROFILE): b'echo original startup\n',
            '/Documents/keep.txt': b'unrelated data',
            str(setup.RECEIPT): json.dumps({'version': 1, 'nonce': 'old', 'status': 'ready'}).encode(),
        }
        self.directories = {'/Documents'}
        self.interrupt_path = interrupt_path
        self.interrupts = 0
        self.receipt_reads = 0
        self.requests = []
        self.commits = []
        self.errors = []
        self.tasks = set()

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        handles = {}
        try:
            size = struct.unpack('>I', await reader.readexactly(4))[0]
            request = plistlib.loads(await reader.readexactly(size))
            assert request == {'Command': 'VendDocuments', 'Identifier': setup.ASHELL}, request
            self.requests.append(request)
            reply = plistlib.dumps({'Status': 'Complete'})
            writer.write(struct.pack('>I', len(reply)) + reply)
            await writer.drain()

            while True:
                magic, length, first_length, packet, operation = HEADER.unpack(await reader.readexactly(HEADER.size))
                assert magic == MAGIC and HEADER.size <= first_length <= length <= 5 * 1024**2
                payload = await reader.readexactly(length - HEADER.size)
                reply = self.dispatch(handles, operation, payload)
                if reply is None:  # Deliberate cable-drop boundary, after a previous chunk persisted.
                    return
                opcode, data = reply
                size = HEADER.size + len(data)
                writer.write(HEADER.pack(MAGIC, size, size, packet, opcode) + data)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    def dispatch(self, handles, operation, payload):
        if operation == STAT:
            path = decode_path(payload)
            if path in self.directories:
                kind, size = 'S_IFDIR', 0
            elif path in self.files:
                kind, size = 'S_IFREG', len(self.files[path])
            else:
                return status(NOT_FOUND)
            fields = ('st_ifmt', kind, 'st_size', str(size), 'st_blocks', '0', 'st_nlink', '1',
                      'st_mtime', '1700000000000000000', 'st_birthtime', '1700000000000000000')
            return DATA, ('\0'.join(fields) + '\0').encode()
        if operation == MKDIR:
            self.directories.add(decode_path(payload))
        elif operation == OPEN:
            mode = NUMBER.unpack_from(payload)[0]
            path = decode_path(payload[NUMBER.size:])
            assert mode in (1, 3)
            if mode == 3:
                self.files[path] = b''
            if path not in self.files:
                return status(NOT_FOUND)
            handle = max(handles, default=0) + 1
            handles[handle] = (path, 0)
            return OPENED, NUMBER.pack(handle)
        elif operation == WRITE:
            handle = NUMBER.unpack_from(payload)[0]
            path, _ = handles[handle]
            if path == self.interrupt_path and self.files[path] and self.interrupts == 0:
                self.interrupts += 1
                return None
            self.files[path] += payload[NUMBER.size:]
        elif operation == READ:
            handle, size = struct.unpack('<QQ', payload)
            path, position = handles[handle]
            handles[handle] = (path, position + size)
            return DATA, self.files[path][position:position + size]
        elif operation == CLOSE:
            path, _ = handles.pop(NUMBER.unpack(payload)[0])
            if path == str(setup.RECEIPT):
                self.receipt_reads += 1
                self.files[path] = json.dumps({'version': 1, 'nonce': self.files[str(setup.CHALLENGE)].decode(),
                                               'status': 'ready'}).encode()
        elif operation == RENAME:
            source, target, terminator = payload.decode().split('\0')
            assert not terminator
            self.files[target] = self.files.pop(source)
            self.commits.append(target)
        elif operation == REMOVE:
            if self.files.pop(decode_path(payload), None) is None:
                return status(NOT_FOUND)
        else:
            raise AssertionError(f'Unexpected AFC opcode {operation}')
        return status(SUCCESS)


class InstallerWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_transfer_resumes_and_rejects_stale_receipt(self):
        target = setup.APP / 'vendor/multi-chunk.bin'
        pending = target.with_name('.multi-chunk.bin.location-map-pending')
        peer = WirePeer(str(pending))
        server = await asyncio.start_server(peer.accept, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        lockdown = Mock(spec=LockdownClient)

        async def service(name, **kwargs):
            self.assertEqual(name, 'com.apple.mobile.house_arrest')
            return await ServiceConnection.create_using_tcp('127.0.0.1', port, keep_alive=False)

        @asynccontextmanager
        async def connected_phone(*args):
            yield lockdown

        lockdown.start_lockdown_service = AsyncMock(side_effect=service)
        payload = setup.read_payload(setup.KIT / 'Phone/Location-Map-source.zip')
        launcher = setup.read_launcher(setup.KIT)
        files = {name: payload[name] for name in ('startup_profile.py', 'phone_location.py')}
        files['vendor/multi-chunk.bin'] = b'A' * (4 * 1024**2) + b'second chunk'
        key = Ed25519PrivateKey.generate()
        pairing = plistlib.dumps({'identifier': 'wire-test', 'private_key': key.private_bytes_raw(),
                                   'public_key': key.public_key().public_bytes_raw()})
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda _, context: unhandled.append(context['message']))
        try:
            with (patch.object(setup, 'ready_phone', connected_phone), patch.dict(setup.os.environ),
                  patch.object(setup, 'generate_pairing', AsyncMock(return_value=pairing)) as generate):
                with self.assertRaises(ConnectionTerminatedError):
                    await asyncio.wait_for(setup.install(files, launcher, None), 10)
                self.assertEqual(peer.interrupts, 1)
                self.assertEqual(len(peer.files[str(pending)]), 4 * 1024**2)
                self.assertNotIn(str(target), peer.files)
                self.assertEqual(peer.files[str(setup.PROFILE)], b'echo original startup\n')
                generate.assert_not_awaited()

                await asyncio.wait_for(setup.install(files, launcher, None), 10)
                generate.assert_awaited_once()
            self.assertEqual(len(peer.requests), 2)
            self.assertEqual(peer.files[str(target)], files['vendor/multi-chunk.bin'])
            self.assertNotIn(str(pending), peer.files)
            self.assertEqual(peer.files[str(setup.PAIRING)], pairing)
            self.assertEqual(peer.files[str(setup.APP / setup.SHORTCUT)], launcher)
            self.assertEqual(peer.files['/Documents/keep.txt'], b'unrelated data')
            self.assertTrue(peer.files[str(setup.PROFILE)].startswith(b'echo original startup\n'))
            self.assertEqual(peer.commits[-2:], [str(setup.PROFILE), str(setup.CHALLENGE)])
            self.assertEqual(peer.receipt_reads, 2)
            self.assertFalse(peer.errors, peer.errors)
        finally:
            try:
                server.close()
                await server.wait_closed()
                if peer.tasks:
                    await asyncio.wait_for(asyncio.gather(*peer.tasks), 2)
            finally:
                loop.set_exception_handler(previous_handler)
        self.assertEqual(unhandled, [])


if __name__ == '__main__':
    unittest.main()

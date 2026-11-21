"""Offline regression tests for the USB preview's release and staging gates."""
import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pymobiledevice3.exceptions import AfcFileNotFoundError, UserDeniedPairingError
from pymobiledevice3.services.afc import AfcError

import setup_phone as setup
from cryptex_assets import CryptexSetupError, _download
from editor_launcher import ACTION_TYPE, EditorSetupError, _discover, _write_actions, launcher_items


class FakeAfc:
    def __init__(self):
        self.files = {str(setup.PROFILE): b'echo keep my startup\n'}
        self.dirs = {str(setup.DOCUMENTS)}
        self.links = set()
        self.commits = []
        self.handles = {}
        self.next_handle = 0
        self.fail_path = None
        self.corrupt_path = None

    async def stat(self, path):
        if path in self.links:
            return {'st_ifmt': 'S_IFLNK', 'st_size': 0}
        if path in self.files:
            return {'st_ifmt': 'S_IFREG', 'st_size': len(self.files[path])}
        if path in self.dirs:
            return {'st_ifmt': 'S_IFDIR', 'st_size': 0}
        raise AfcFileNotFoundError('mock missing', AfcError.READ_ERROR, path)

    async def makedirs(self, path):
        self.dirs.add(path)

    async def get_file_contents(self, path):
        return self.files[path]

    async def fopen(self, path, mode):
        assert mode == 'w'
        self.next_handle += 1
        self.handles[self.next_handle] = path
        self.files[path] = b''
        return self.next_handle

    async def fwrite(self, handle, data, *, chunk_size):
        path = self.handles[handle]
        self.files[path] += b'damaged' if path == self.corrupt_path else data

    async def fclose(self, handle):
        del self.handles[handle]

    async def rename(self, source, target):
        if target == self.fail_path:
            raise ConnectionError('mock cable removal')
        self.files[target] = self.files.pop(source)
        self.commits.append(target)

    async def rm_single(self, path, force=False):
        self.files.pop(path, None)


def new_pairing():
    key = Ed25519PrivateKey.generate()
    return plistlib.dumps({'identifier': 'test-only', 'private_key': key.private_bytes_raw(),
                           'public_key': key.public_key().public_bytes_raw()})


class ArchiveTests(unittest.TestCase):
    def test_real_payload(self):
        files = setup.read_payload(setup.KIT / 'Phone/Location-Map-source.zip')
        self.assertIn('phone_location.py', files)
        self.assertTrue(all('private' not in name.split('/') for name in files))
        self.assertFalse(any(name.startswith('ddi/') for name in files))

    def test_altered_payload_rejected(self):
        with TemporaryDirectory() as temp:
            archive = Path(temp) / 'payload.zip'
            archive.write_bytes(b'corrupt')
            with self.assertRaisesRegex(setup.SetupError, 'archive failed'):
                setup.read_payload(archive)

    def test_traversal_and_private_entries_rejected_even_with_matching_archive_hash(self):
        for name in ('../outside', 'private/pairing.plist', 'vendor\\escaped',
                     'vendor//unexpected', 'ddi/Image.dmg'):
            with self.subTest(name=name), TemporaryDirectory() as temp:
                archive = Path(temp) / 'payload.zip'
                with ZipFile(archive, 'w') as payload:
                    payload.writestr(setup.PROJECT + '/' + name.replace('\\', '/'), b'test')
                if '\\' in name:
                    archive.write_bytes(archive.read_bytes().replace(b'/vendor/escaped', b'/vendor\\escaped'))
                with patch.object(setup, 'PAYLOAD_HASH', hashlib.sha256(archive.read_bytes()).hexdigest()):
                    with self.assertRaisesRegex(setup.SetupError, 'unexpected file path'):
                        setup.read_payload(archive)

    def test_launcher_hash_and_all_release_checks_are_required(self):
        # The format header is tested here; the synthetic bytes are not an Apple-signed file.
        signed_format = b'AEA1' + bytes(128)
        with TemporaryDirectory() as temp:
            kit = Path(temp)
            (kit / 'Phone').mkdir()
            (kit / 'Installer').mkdir()
            (kit / 'Runtime/Python').mkdir(parents=True)
            (kit / 'Runtime/Python/python.exe').write_bytes(b'runtime fixture')
            for name in ('START-HERE.cmd', 'Start-Location-Map.ps1', 'Installer/setup_phone.py'):
                (kit / name).write_text('fixture', encoding='ascii')
            launcher = kit / 'Phone' / setup.SHORTCUT
            launcher.write_bytes(signed_format)
            manifest = kit / 'release.json'
            record = {
                'payload_sha256': setup.PAYLOAD_HASH,
                'launcher_sha256': setup.digest(signed_format),
                'windows_setup_sha256': setup.windows_setup_digest(kit),
                'checks': dict.fromkeys(setup.RELEASE_CHECKS, True),
                'tested_versions': {'ios_build': 'fixture', 'a_shell': '1', 'local_dev_vpn': '1'},
            }
            manifest.write_text(json.dumps(record), encoding='utf-8')
            self.assertEqual(setup.approved_launcher(kit)[0], signed_format)
            launcher.write_bytes(signed_format + b'changed')
            with self.assertRaises(setup.SetupError):
                setup.approved_launcher(kit)
            launcher.write_bytes(signed_format)
            record['checks']['unplugged_cellular_simulate_and_stop'] = False
            manifest.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(setup.SetupError):
                setup.approved_launcher(kit)
            record['checks']['unplugged_cellular_simulate_and_stop'] = True
            manifest.write_text(json.dumps(record), encoding='utf-8')
            (kit / 'Installer/setup_phone.py').write_text('changed', encoding='ascii')
            with self.assertRaises(setup.SetupError):
                setup.approved_launcher(kit)


class CryptexDownloadTests(unittest.TestCase):
    def test_download_retries_transport_failure_then_verifies_bytes(self):
        payload = b'fixture developer image'
        opener = Mock()
        opener.open.side_effect = [TimeoutError(), BytesIO(payload)]
        self.assertEqual(_download(opener, 'Image.dmg', len(payload), setup.digest(payload)), payload)
        self.assertEqual(opener.open.call_count, 2)

    def test_download_rejects_changed_or_oversized_data(self):
        payload = b'pinned'
        for received in (b'edited', b'pinned more bytes', b'short'):
            with self.subTest(received=received):
                opener = Mock()
                opener.open.return_value = BytesIO(received)
                with self.assertRaisesRegex(CryptexSetupError, 'failed verification'):
                    _download(opener, 'Image.dmg', len(payload), setup.digest(payload))
                opener.open.assert_called_once()

    def test_install_never_connects_when_image_is_unavailable(self):
        install = AsyncMock()
        with (patch.object(setup, 'download_assets', side_effect=CryptexSetupError('test offline')),
              patch.object(setup, 'install', install)):
            self.assertEqual(setup.main(['--stage-test-device']), 2)
        install.assert_not_called()


class PreviewLaunchTests(unittest.TestCase):
    def test_check_preview_verifies_local_files_without_phone_or_download(self):
        with (patch.object(setup, 'download_assets') as download,
              patch.object(setup, 'install', AsyncMock()) as install):
            self.assertEqual(setup.main(['--check-preview']), 0)
        download.assert_not_called()
        install.assert_not_called()

    def test_preview_refuses_changed_launcher_before_download_or_phone(self):
        with (patch.object(setup, 'read_launcher', return_value=b'AEA1 changed'),
              patch.object(setup, 'download_assets') as download,
              patch.object(setup, 'install', AsyncMock()) as install):
            self.assertEqual(setup.main(['--install-preview']), 2)
        download.assert_not_called()
        install.assert_not_called()


class EditorLauncherTests(unittest.IsolatedAsyncioTestCase):
    def test_paste_items_preserve_all_action_plists(self):
        path = setup.KIT / 'Sources/Location Map.unsigned.plist'
        actions = plistlib.loads(path.read_bytes())['WFWorkflowActions']
        items = launcher_items(path)
        self.assertEqual(len(items), len(actions))
        for item, expected in zip(items, actions):
            self.assertEqual(item['types'], [ACTION_TYPE])
            self.assertEqual(plistlib.loads(item['data'][ACTION_TYPE]['data']), expected)

    async def test_editor_uses_a_write_only_clipboard_request(self):
        rsd = object()
        clipboard = AsyncMock()
        clipboard.__aenter__.return_value.set.return_value = {'command': 'SET_REPLY'}
        actions = [{'types': [ACTION_TYPE], 'data': {ACTION_TYPE: {'data': b'fixture'}}}]
        with patch('editor_launcher.PasteboardService', return_value=clipboard):
            await _write_actions(rsd, actions)
        clipboard.__aenter__.return_value.set.assert_awaited_once_with(actions)
        clipboard.__aenter__.return_value.get.assert_not_called()

    async def test_wrong_identity_closes_before_any_editor_access(self):
        rsd = SimpleNamespace(udid='other', connect=AsyncMock(), close=AsyncMock())
        tunnel = SimpleNamespace(address='fd00::1', port=58783, auxiliary_metadata={})
        with patch('editor_launcher.RemoteServiceDiscoveryService', return_value=rsd):
            with self.assertRaisesRegex(EditorSetupError, 'selected USB iPhone'):
                async with _discover(SimpleNamespace(udid='chosen'), tunnel, AsyncMock()):
                    self.fail('wrong device reached editor setup')
        rsd.close.assert_awaited_once()


class StageTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        real_files = setup.read_payload(setup.KIT / 'Phone/Location-Map-source.zip')
        cls.files = {name: real_files[name] for name in ('startup_profile.py', 'phone_location.py')}
        cls.files['vendor/one/entry.txt'] = b'fixture'

    async def test_fresh_stage_and_idempotent_retry(self):
        afc = FakeAfc()
        pairing = new_pairing()
        with patch.object(setup, 'generate_pairing', AsyncMock(return_value=pairing)) as generate:
            await setup.stage_phone(object(), afc, self.files, b'launcher')
            self.assertEqual(afc.commits[-1], str(setup.PROFILE))
            self.assertEqual(afc.files[str(setup.PAIRING)], pairing)
            self.assertEqual(afc.files[str(setup.APP / setup.SHORTCUT)], b'launcher')
            profile = afc.files[str(setup.PROFILE)]
            self.assertTrue(profile.startswith(b'echo keep my startup\n'))
            self.assertIn(b'Path.home()', profile)
            self.assertNotIn(b'/Containers/Data/Application/', profile)
            afc.commits.clear()
            await setup.stage_phone(object(), afc, self.files, b'launcher')
            self.assertEqual(afc.commits, [])
            generate.assert_awaited_once()

    async def test_interrupted_profile_commit_resumes_without_pairing_again(self):
        afc = FakeAfc()
        profile = afc.files[str(setup.PROFILE)]
        afc.fail_path = str(setup.PROFILE)
        with patch.object(setup, 'generate_pairing', AsyncMock(return_value=new_pairing())) as generate:
            with self.assertRaises(ConnectionError):
                await setup.stage_phone(object(), afc, self.files, None)
            self.assertEqual(afc.files[str(setup.PROFILE)], profile)
            self.assertIn(str(setup.PAIRING), afc.files)
            afc.fail_path = None
            afc.commits.clear()
            await setup.stage_phone(object(), afc, self.files, None)
            self.assertEqual(afc.commits, [str(setup.PROFILE)])
            generate.assert_awaited_once()

    async def test_interrupted_file_transfer_never_publishes_startup(self):
        afc = FakeAfc()
        profile = afc.files[str(setup.PROFILE)]
        afc.fail_path = str(setup.APP / 'phone_location.py')
        with patch.object(setup, 'generate_pairing', AsyncMock(return_value=new_pairing())) as generate:
            with self.assertRaises(ConnectionError):
                await setup.stage_phone(object(), afc, self.files, None)
            self.assertEqual(afc.files[str(setup.PROFILE)], profile)
            generate.assert_not_awaited()
            afc.fail_path = None
            await setup.stage_phone(object(), afc, self.files, None)
            generate.assert_awaited_once()

    async def test_full_payload_and_failed_readback(self):
        files = setup.read_payload(setup.KIT / 'Phone/Location-Map-source.zip')
        afc = FakeAfc()
        target = setup.APP / 'phone_location.py'
        afc.corrupt_path = str(target.with_name('.phone_location.py.location-map-pending'))
        with patch.object(setup, 'generate_pairing', AsyncMock(return_value=new_pairing())) as generate:
            with self.assertRaisesRegex(setup.SetupError, 'file verification failed'):
                await setup.stage_phone(object(), afc, files, None)
            self.assertNotIn(str(target), afc.files)
            self.assertNotIn(afc.corrupt_path, afc.files)
            generate.assert_not_awaited()
            afc.corrupt_path = None
            await setup.stage_phone(object(), afc, files, None)
            generate.assert_awaited_once()
        for name, data in files.items():
            if not name.startswith('tests_'):
                self.assertEqual(afc.files[str(setup.APP / name)], data)

    async def test_staging_preserves_live_session_or_different_install(self):
        for path, data in ((setup.SESSION, b'{"requires_stop":true}'),
                           (setup.SESSION, b'{invalid'),
                           (setup.APP / 'phone_location.py', b'other'),
                           (setup.PAIRING, b'broken')):
            with self.subTest(path=path, data=data):
                afc = FakeAfc()
                afc.files[str(path)] = data
                before = afc.files.copy()
                with patch.object(setup, 'generate_pairing', AsyncMock()) as generate:
                    with self.assertRaises(setup.SetupError):
                        await setup.stage_phone(object(), afc, self.files, None)
                    self.assertEqual(afc.files, before)
                    generate.assert_not_awaited()

    async def test_staging_refuses_symlink(self):
        afc = FakeAfc()
        afc.links.add(str(setup.APP / 'vendor'))
        with self.assertRaisesRegex(setup.SetupError, 'unexpected type'):
            await setup.stage_phone(object(), afc, self.files, None)
        self.assertEqual(afc.commits, [])

    async def test_cancelled_write_leaves_pending_and_skips_usb_cleanup(self):
        afc = FakeAfc()
        entered_write = asyncio.Event()
        release_write = asyncio.Event()

        async def stalled_write(handle, data, *, chunk_size):
            entered_write.set()
            await release_write.wait()

        afc.fwrite = stalled_write
        afc.fclose = AsyncMock(side_effect=AssertionError('Cancelled transfer attempted a file close'))
        afc.rm_single = AsyncMock(side_effect=AssertionError('Cancelled transfer attempted removal'))
        target = setup.APP / 'app.py'
        task = asyncio.create_task(setup.write_verified(afc, target, b'fixture'))
        try:
            await asyncio.wait_for(entered_write.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            release_write.set()
        afc.fclose.assert_not_awaited()
        afc.rm_single.assert_not_awaited()
        self.assertIn(str(target.with_name('.app.py.location-map-pending')), afc.files)
        self.assertNotIn(str(target), afc.files)

    async def test_transfer_error_survives_stalled_file_cleanup(self):
        afc = FakeAfc()

        async def stalled_cleanup(*args, **kwargs):
            await asyncio.Event().wait()

        afc.fwrite = AsyncMock(side_effect=ConnectionError('original transfer failure'))
        afc.fclose = AsyncMock(side_effect=stalled_cleanup)
        afc.rm_single = AsyncMock(side_effect=stalled_cleanup)
        with patch.object(setup, 'AFC_CLEANUP_TIMEOUT', 0.01):
            with self.assertRaisesRegex(ConnectionError, 'original transfer failure'):
                await asyncio.wait_for(setup.write_verified(afc, setup.APP / 'app.py', b'fixture'), 1)
        afc.fclose.assert_awaited_once()
        afc.rm_single.assert_awaited_once()


class FakeLockdown:
    udid = 'test-phone'
    all_values = {'DeviceClass': 'iPhone', 'ProductVersion': '27.2', 'BuildVersion': 'fixture'}

    def __init__(self, developer_mode):
        self.get_developer_mode_status = AsyncMock(return_value=developer_mode)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_usb_only_same_device_after_restart(self):
        selected = SimpleNamespace(serial='chosen', connection_type='USB')
        other = SimpleNamespace(serial='other', connection_type='USB')
        network = SimpleNamespace(serial='nearby', connection_type='Network')
        steps = [[network], [selected, other], [selected, network], [other, network], [selected]]
        before, after = FakeLockdown(False), FakeLockdown(True)
        connector = AsyncMock(side_effect=[before, after])
        proxy = AsyncMock()
        proxy.__aenter__.return_value.get_apps.return_value = {setup.ASHELL: {}, setup.VPN: {}}
        amfi = AsyncMock()
        with (patch.object(setup.usbmux, 'list_devices', AsyncMock(side_effect=steps)),
              patch.object(setup, 'create_using_usbmux', connector),
              patch.object(setup, 'InstallationProxyService', return_value=proxy),
              patch.object(setup, 'AmfiService', return_value=amfi),
              patch.object(setup.asyncio, 'sleep', AsyncMock())):
            async with setup.ready_phone(Path('test-cache'), None) as phone:
                self.assertIs(phone, after)
        self.assertEqual(connector.await_count, 2)
        for call in connector.await_args_list:
            self.assertEqual(call.kwargs['serial'], 'chosen')
            self.assertEqual(call.kwargs['connection_type'], 'USB')
        amfi.reveal_developer_mode_option_in_ui.assert_awaited_once()

    async def test_declined_trust_stops_setup(self):
        selected = SimpleNamespace(serial='chosen', connection_type='USB')
        connector = AsyncMock(side_effect=UserDeniedPairingError('test-only'))
        with (patch.object(setup.usbmux, 'list_devices', AsyncMock(return_value=[selected])),
              patch.object(setup, 'create_using_usbmux', connector)):
            with self.assertRaisesRegex(setup.SetupError, 'Trust was declined'):
                async with setup.ready_phone(Path('test-cache'), None):
                    self.fail('should not stage')
        connector.assert_awaited_once()


class FirstLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_current_setup_receipt(self):
        afc = FakeAfc()
        nonce = 'b' * 32
        afc.files[str(setup.RECEIPT)] = json.dumps({'version': 1, 'nonce': 'a' * 32, 'status': 'ready'}).encode()

        async def app_launched(_):
            afc.files[str(setup.RECEIPT)] = json.dumps({'version': 1, 'nonce': nonce, 'status': 'ready'}).encode()

        sleep = AsyncMock(side_effect=app_launched)
        with patch.object(setup.asyncio, 'sleep', sleep):
            await setup.wait_for_first_launch(afc, nonce)
        sleep.assert_awaited_once_with(2)

    async def test_missing_receipt_never_reports_success(self):
        with self.assertRaisesRegex(setup.SetupError, 'First launch was not confirmed'):
            await setup.wait_for_first_launch(FakeAfc(), 'b' * 32, timeout=0)

    async def test_tunnel_with_unavailable_developer_service_fails_first_launch(self):
        afc = FakeAfc()
        nonce = 'b' * 32
        afc.files[str(setup.RECEIPT)] = json.dumps(
            {'version': 1, 'nonce': nonce, 'status': 'developer-unavailable'}).encode()
        with self.assertRaisesRegex(setup.SetupError, 'developer location service is unavailable'):
            await setup.wait_for_first_launch(afc, nonce)

    async def test_stalled_usb_receipt_read_has_a_deadline(self):
        afc = FakeAfc()

        async def stalled_stat(path):
            await asyncio.Event().wait()

        afc.stat = AsyncMock(side_effect=stalled_stat)
        with self.assertRaisesRegex(setup.SetupError, 'stopped responding over USB'):
            await asyncio.wait_for(setup.wait_for_first_launch(afc, 'b' * 32, timeout=0.01), 1)
        afc.stat.assert_awaited_once()

    async def test_stalled_documents_negotiation_closes_without_staging(self):
        lockdown = FakeLockdown(True)

        async def stalled_send_command(*args):
            await asyncio.Event().wait()

        afc = SimpleNamespace(send_command=AsyncMock(side_effect=stalled_send_command), aclose=AsyncMock())
        with (patch.object(setup, 'ready_phone', Mock(return_value=lockdown)),
              patch.object(setup, 'HouseArrestService', Mock(return_value=afc)),
              patch.object(setup, 'AFC_RESPONSE_TIMEOUT', 0.01),
              patch.object(setup, 'stage_phone', AsyncMock()) as stage):
            with self.assertRaisesRegex(setup.SetupError, 'stopped responding during setup'):
                await asyncio.wait_for(setup.install({}, None, None), 1)
        afc.send_command.assert_awaited_once_with(setup.ASHELL, setup.VEND_DOCUMENTS)
        afc.aclose.assert_awaited_once()
        stage.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

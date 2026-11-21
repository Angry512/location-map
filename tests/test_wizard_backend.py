"""Offline behavior checks for the new guided USB setup; never use a phone."""
import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

INSTALLER = Path(__file__).resolve().parents[1] / 'Support/Installer'
sys.path.insert(0, str(INSTALLER))
import wizard_backend as wizard

# Reuse the established AFC wire-shaped fixture, while exercising the real
# stage_phone implementation through this wizard's complete orchestration.
_spec = importlib.util.spec_from_file_location('usb_stage_fixture', Path(__file__).with_name('test_usb_setup.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def device(serial, kind='USB'):
    return SimpleNamespace(serial=serial, connection_type=kind)


def icon(identifier, name=None, **extra):
    return dict(displayIdentifier=identifier, displayName=name or identifier, **extra)


def map_icon():
    return icon('webclip-map', 'Location Map', webClipURL='/run-shortcut')


def records(wire):
    return [json.loads(line) for line in wire.getvalue().splitlines()]


class GuidedPhone:
    """A fake recipient and fake human approvals; no mobile service is opened."""
    def __init__(self, *, onboarding=False):
        self.afc = _fixture.FakeAfc()
        self.afc.send_command = AsyncMock()
        self.afc.aclose = AsyncMock()
        self.connected = []
        self.developer_checks = 0
        self.app_checks = 0
        self.onboarding = onboarding
        self.usb_checks = 0
        self.fresh_nonce = None
        self.receipt_reads = 0
        self.files_opened = 0
        self.layout_written = False
        self.tamper_layout = False
        self.delete_helper = False
        self.icon_reads = 0
        self.state = [[], [dict(iconType='widget', widgetIdentifier='weather', gridSize={'width': 4, 'height': 2}),
                          icon('old-first'), icon(wizard.setup.VPN), icon(wizard.setup.ASHELL)],
                      [icon('old-last'), map_icon()]]
        self.lockdown = SimpleNamespace(
            udid='selected',
            all_values={'DeviceClass': 'iPhone', 'ProductVersion': '27.2'},
            get_developer_mode_status=self.developer_status)
        self.generator = AsyncMock(return_value=_fixture.new_pairing())
        self.original_read = self.afc.get_file_contents
        self.afc.get_file_contents = self.read

    async def read(self, path):
        if path == str(wizard.setup.RECEIPT):
            self.receipt_reads += 1
        return await self.original_read(path)

    async def usb(self):
        self.usb_checks += 1
        if self.onboarding:
            # The other phone is never chosen while the selected phone reboots.
            return {1: [device('selected')], 2: [], 3: [device('other')]}.get(self.usb_checks, [device('selected')])
        return [device('selected')]

    async def developer_status(self):
        self.developer_checks += 1
        return not self.onboarding or self.developer_checks > 1

    async def connect(self, **kwargs):
        self.connected.append(kwargs['serial'])
        @asynccontextmanager
        async def connection():
            yield self.lockdown
        return connection()

    @asynccontextmanager
    async def proxy(self, *args):
        async def get_apps(**kwargs):
            self.app_checks += 1
            if (self.onboarding and self.app_checks == 1) or (self.delete_helper and self.files_opened):
                return {wizard.setup.VPN: {}}
            return {bundle: {} for bundle in wizard.HELPERS}
        yield SimpleNamespace(get_apps=get_apps)

    @asynccontextmanager
    async def board(self, *args):
        async def get():
            self.icon_reads += 1
            return deepcopy(self.state)
        async def set_(state):
            self.state = deepcopy(state)
            self.layout_written = True
            if self.tamper_layout:
                self.state[1] = [v for v in self.state[1] if v.get('widgetIdentifier') != 'weather']
        yield SimpleNamespace(get_icon_state=get, set_icon_state=set_)

    async def open_files(self, lockdown, assets):
        self.files_opened += 1
        assert str(wizard.setup.PAIRING) in self.afc.files
        assert b'Path.home()' in self.afc.files[str(wizard.setup.PROFILE)]
        self.fresh_nonce = self.afc.files[str(wizard.setup.CHALLENGE)].decode()
        # Even a valid older receipt must not bypass the fresh launch wait.
        self.afc.files[str(wizard.setup.RECEIPT)] = json.dumps(dict(version=1, nonce='older-install', status='ready')).encode()

    async def sleep(self, seconds):
        if seconds == 2 and self.fresh_nonce:
            self.afc.files[str(wizard.setup.RECEIPT)] = json.dumps(dict(version=1, nonce=self.fresh_nonce, status='ready')).encode()
        if seconds == 3 and self.layout_written:
            # Model the recipient choosing Remove from Home Screen (not Delete).
            for page in self.state:
                page[:] = [v for v in page if v.get('displayIdentifier') not in wizard.HELPERS]

    @contextmanager
    def services(self):
        with ExitStack() as stack:
            patches = [
                patch.object(wizard.usbmux, 'list_devices', self.usb),
                patch.object(wizard, 'create_using_usbmux', self.connect),
                patch.object(wizard, 'InstallationProxyService', self.proxy),
                patch.object(wizard, 'SpringBoardServicesService', self.board),
                patch.object(wizard, 'AmfiService', Mock(return_value=SimpleNamespace(reveal_developer_mode_option_in_ui=AsyncMock()))),
                patch.object(wizard, 'keep_awake', AsyncMock()),
                patch.object(wizard, 'download_assets', Mock(return_value={})),
                patch.object(wizard, 'HouseArrestService', Mock(return_value=self.afc)),
                patch.object(wizard.setup, 'generate_pairing', self.generator),
                patch.object(wizard, 'open_files', self.open_files),
                patch.object(wizard.asyncio, 'sleep', self.sleep),
            ]
            for replacement in patches:
                stack.enter_context(replacement)
            yield


class PureTests(unittest.TestCase):
    def test_complete_guided_flow_reboots_waits_for_download_and_preserves_identity(self):
        phone = GuidedPhone(onboarding=True)
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire):
            result = wizard.main([])
        self.assertEqual(result, 0)
        stages = [event['stage'] for event in records(wire)]
        self.assertLess(stages.index('developer'), stages.index('apps'))
        self.assertLess(stages.index('apps'), stages.index('transfer'))
        self.assertLess(stages.index('launcher'), stages.index('home'))
        self.assertEqual(stages[-1], 'done')
        self.assertEqual(set(phone.connected), {'selected'})
        self.assertGreaterEqual(phone.receipt_reads, 2)
        self.assertEqual(wizard.icon_summary(phone.state), ([(1, (2,))], set()))
        phone.generator.assert_awaited_once()
        files = wizard.setup.read_payload(wizard.setup.KIT / 'Phone/Location-Map-source.zip')
        for name, data in files.items():
            if '/' in name or not name.startswith('tests_'):
                self.assertEqual(phone.afc.files[str(wizard.setup.APP / name)], data)

    def test_interrupted_real_profile_commit_retries_without_losing_files_or_pairing(self):
        phone = GuidedPhone()
        original = phone.afc.files[str(wizard.setup.PROFILE)]
        phone.afc.fail_path = str(wizard.setup.PROFILE)
        first, second = io.StringIO(), io.StringIO()
        with phone.services():
            with redirect_stdout(first):
                failed = wizard.main([])
            self.assertEqual(failed, 2)
            self.assertEqual(phone.afc.files[str(wizard.setup.PROFILE)], original)
            self.assertIn(str(wizard.setup.PAIRING), phone.afc.files)
            self.assertEqual(phone.files_opened, 0)
            self.assertNotIn('done', [e['stage'] for e in records(first)])
            phone.afc.fail_path = None
            with redirect_stdout(second):
                completed = wizard.main([])
        self.assertEqual(completed, 0)
        phone.generator.assert_awaited_once()
        self.assertEqual(records(second)[-1]['stage'], 'done')

    def test_existing_root_shortcut_is_preserved_without_blocking_complete_install(self):
        phone = GuidedPhone()
        existing = b'recipient file; must not be replaced'
        root = str(wizard.setup.DOCUMENTS / wizard.setup.SHORTCUT)
        phone.afc.files[root] = existing
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire):
            result = wizard.main([])
        self.assertEqual(result, 0)
        self.assertEqual(phone.afc.files[root], existing)
        launcher_event = next(e for e in records(wire) if e['stage'] == 'launcher')
        self.assertIn('a-Shell > PhoneLocationOneApp > Location Map.shortcut', launcher_event['message'])

    def test_deleted_helper_blocks_success_before_any_icon_rearrangement(self):
        phone = GuidedPhone()
        phone.delete_helper = True
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire):
            result = wizard.main([])
        self.assertEqual(result, 2)
        self.assertEqual(phone.icon_reads, 0)
        self.assertIn('deleted', records(wire)[-1]['message'])
        self.assertNotIn('done', [e['stage'] for e in records(wire)])

    def test_unexpected_widget_loss_blocks_success(self):
        phone = GuidedPhone()
        phone.tamper_layout = True
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire):
            result = wizard.main([])
        self.assertEqual(result, 2)
        self.assertIn('Home Screen changed', records(wire)[-1]['message'])
        self.assertNotIn('done', [e['stage'] for e in records(wire)])

    def test_disk_full_is_actionable_even_when_afc_cleanup_also_fails(self):
        phone = GuidedPhone()
        phone.afc.fwrite = AsyncMock(side_effect=wizard.AfcException('private path must stay private', wizard.AfcError.NO_SPACE_LEFT))
        phone.afc.aclose = AsyncMock(side_effect=wizard.ConnectionTerminatedError())
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire):
            result = wizard.main([])
        self.assertEqual(result, 2)
        event = records(wire)[-1]
        self.assertIn('free space', event['title'])
        self.assertIn('iPhone Storage', event['message'])
        self.assertNotIn('private path', wire.getvalue())

    def test_cancellation_never_reports_done_and_closes_the_afc_session(self):
        phone = GuidedPhone()
        wire = io.StringIO()
        with phone.services(), redirect_stdout(wire), \
                patch.object(wizard.setup, 'stage_phone', AsyncMock(side_effect=asyncio.CancelledError())):
            result = wizard.main([])
        self.assertEqual(result, 130)
        self.assertNotIn('done', [e['stage'] for e in records(wire)])
        self.assertEqual(records(wire)[-1]['title'], 'Setup stopped')
        phone.afc.aclose.assert_awaited_once()

    def test_missing_package_files_never_claim_a_phone_connection_problem(self):
        for missing in ('Location-Map-source.zip', 'Location Map.shortcut'):
            with self.subTest(missing=missing), TemporaryDirectory() as temporary:
                kit = Path(temporary)
                (kit / 'Phone').mkdir()
                for name in ('Location-Map-source.zip', 'Location Map.shortcut'):
                    if name != missing:
                        (kit / 'Phone' / name).write_bytes((wizard.setup.KIT / 'Phone' / name).read_bytes())
                wire = io.StringIO()
                with redirect_stdout(wire), patch.object(wizard.setup, 'KIT', kit), \
                        patch.object(wizard.usbmux, 'list_devices') as usb, \
                        patch.object(wizard, 'download_assets') as download:
                    result = wizard.main(['--check'])
                self.assertEqual(result, 2)
                self.assertIn('USB setup files are missing', records(wire)[-1]['message'])
                self.assertNotIn('Reconnect', wire.getvalue())
                self.assertNotIn('private-local-path', wire.getvalue())
                usb.assert_not_called()
                download.assert_not_called()

    def test_target_is_not_switched_after_disconnect(self):
        self.assertEqual(wizard.choose_usb([device('other')], 'selected'), (None, 'reconnect'))
        self.assertEqual(wizard.choose_usb([device('selected')], 'selected'), ('selected', 'ready'))

    def test_multiple_devices_pause_even_if_selected_present(self):
        self.assertEqual(wizard.choose_usb([device('a'), device('b')], 'a'), (None, 'multiple'))

    def test_network_peers_are_not_install_targets(self):
        self.assertEqual(wizard.choose_usb([device('network', 'Network')], None), (None, 'missing'))

    def test_events_are_json_and_deduplicated(self):
        wire = io.StringIO()
        events = wizard.Events(wire)
        events.emit('trust', 'Connect', 'Unlock the iPhone.', 10)
        events.emit('trust', 'Connect', 'Unlock the iPhone.', 10)
        self.assertEqual(len(records(wire)), 1)
        self.assertEqual(records(wire)[0]['actions'], [])

    def test_arbitrary_library_output_is_never_forwarded(self):
        wire = io.StringIO()
        captured = wizard.TransferOutput(wizard.Events(wire))
        captured.write('secret-device-id and private path\nChecked secret of private new files.\nChecked 100 of 561 new files.\n')
        self.assertNotIn('secret', wire.getvalue())
        self.assertEqual(records(wire)[0]['stage'], 'transfer')

    def test_home_summary_recurses_into_folders(self):
        state = [[], [map_icon(), dict(iconType='folder', iconLists=[[icon(wizard.setup.VPN)]])]]
        maps, visible = wizard.icon_summary(state)
        self.assertEqual(maps, [(1, (0,))])
        self.assertEqual(visible, {wizard.setup.VPN})

    def test_first_page_plan_preserves_widget_and_every_app(self):
        widget = dict(iconType='widget', gridSize={'width': 4, 'height': 2}, widgetIdentifier='weather')
        before = [[icon('dock')], [widget, icon('first'), icon('last')], [icon('other'), map_icon()]]
        original = deepcopy(before)
        after = wizard.first_page_plan(before)
        self.assertEqual(before, original)
        self.assertEqual(after[1][0], widget)
        self.assertEqual(after[1][2], map_icon())
        self.assertEqual(after[2][1], icon('last'))
        self.assertEqual(sorted(repr(v) for page in before for v in page),
                         sorted(repr(v) for page in after for v in page))

    def test_no_layout_guess_for_duplicate_map_icons(self):
        self.assertIsNone(wizard.first_page_plan([[], [icon('a')], [map_icon(), map_icon()]]))

    def test_no_layout_guess_when_first_page_is_all_widgets(self):
        self.assertIsNone(wizard.first_page_plan([[], [dict(iconType='widget')], [map_icon()]]))

    def test_check_mode_never_downloads_or_contacts_devices(self):
        wire = io.StringIO()
        with redirect_stdout(wire), patch.object(wizard, 'download_assets') as download, \
                patch.object(wizard.usbmux, 'list_devices') as usb:
            result = wizard.main(['--check'])
        self.assertEqual(result, 0)
        download.assert_not_called()
        usb.assert_not_called()
        self.assertEqual(records(wire)[-1]['stage'], 'done')

    def test_unexpected_error_does_not_disclose_exception_details(self):
        wire = io.StringIO()
        with redirect_stdout(wire), patch.object(wizard, 'install', AsyncMock(side_effect=RuntimeError('secret-path-and-id'))):
            result = wizard.main([])
        self.assertEqual(result, 1)
        self.assertNotIn('secret-path', wire.getvalue())
        self.assertEqual(records(wire)[-1]['stage'], 'error')

    def test_full_flow_never_emits_done_when_launch_or_home_confirmation_fails(self):
        @asynccontextmanager
        async def ready(*args):
            yield object()
        afc = SimpleNamespace(send_command=AsyncMock(), aclose=AsyncMock())
        for blocked in ('receipt', 'home'):
            with self.subTest(blocked=blocked):
                wire = io.StringIO()
                receipt = AsyncMock(side_effect=wizard.WizardError('Launch not confirmed') if blocked == 'receipt' else None)
                home = AsyncMock(side_effect=wizard.WizardError('Home Screen not confirmed') if blocked == 'home' else None)
                with redirect_stdout(wire), \
                        patch.object(wizard, 'ready_phone', ready), \
                        patch.object(wizard, 'keep_awake', AsyncMock()), \
                        patch.object(wizard, 'download_assets', Mock(return_value={})), \
                        patch.object(wizard, 'HouseArrestService', Mock(return_value=afc)), \
                        patch.object(wizard.setup, 'stage_phone', AsyncMock()), \
                        patch.object(wizard.setup, 'read_file', AsyncMock(return_value=None)), \
                        patch.object(wizard.setup, 'write_verified', AsyncMock()), \
                        patch.object(wizard, 'shortcut_location', AsyncMock(return_value='a-Shell > Location Map.shortcut')), \
                        patch.object(wizard, 'open_files', AsyncMock()), \
                        patch.object(wizard, 'wait_receipt', receipt), \
                        patch.object(wizard, 'wait_home', home):
                    result = wizard.main([])
                self.assertEqual(result, 2)
                stages = [event['stage'] for event in records(wire)]
                self.assertEqual(stages[-1], 'error')
                self.assertNotIn('done', stages)
                if blocked == 'receipt':
                    home.assert_not_awaited()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_windows_usb_service_gets_driver_instructions(self):
        phone = GuidedPhone()
        wire = io.StringIO()
        with phone.services(), patch.object(wizard.usbmux, 'list_devices', AsyncMock(side_effect=[
                wizard.ConnectionFailedToUsbmuxdError('private device diagnostic'), [device('selected')]])):
            async with wizard.ready_phone(Path('temporary'), wizard.Events(wire), timeout=2):
                pass
        event = records(wire)[0]
        self.assertEqual(event['title'], 'Install Apple Devices on this PC')
        self.assertEqual(event['actions'][0]['url'], wizard.APPLE_DEVICES_URL)
        self.assertNotIn('private device', wire.getvalue())

    async def test_unresponsive_cleanup_is_bounded(self):
        async def stalled():
            await asyncio.Event().wait()
        afc = SimpleNamespace(aclose=AsyncMock(side_effect=stalled))
        with patch.object(wizard, 'CLEANUP_TIMEOUT', .01):
            await asyncio.wait_for(wizard.close_afc(afc), .25)
        afc.aclose.assert_awaited_once()

    async def test_concurrent_home_screen_edit_is_not_overwritten(self):
        original = [[], [icon('a'), icon('b')], [map_icon()]]
        edited = [[], [icon('b'), icon('a')], [map_icon()]]
        finished = [[], [map_icon(), icon('a')], [icon('b')]]
        board = SimpleNamespace(get_icon_state=AsyncMock(side_effect=[original, edited, finished]),
                                set_icon_state=AsyncMock())
        @asynccontextmanager
        async def springboard(*args):
            yield board
        with patch.object(wizard, 'require_helpers', AsyncMock()), \
                patch.object(wizard, 'SpringBoardServicesService', springboard), \
                patch.object(wizard.asyncio, 'sleep', AsyncMock()):
            await wizard.wait_home(object(), wizard.Events(io.StringIO()), timeout=2)
        board.set_icon_state.assert_not_awaited()

    async def test_developer_mode_is_handled_before_helper_lookup(self):
        lockdown = SimpleNamespace(get_developer_mode_status=AsyncMock(side_effect=[False, True]))
        @asynccontextmanager
        async def connected(**kwargs):
            yield lockdown
        async def connect(**kwargs):
            return connected(**kwargs)
        proxy = SimpleNamespace(get_apps=AsyncMock(return_value={key: {} for key in wizard.HELPERS}))
        @asynccontextmanager
        async def app_proxy(*args):
            yield proxy
        wire = io.StringIO()
        service = SimpleNamespace(reveal_developer_mode_option_in_ui=AsyncMock())
        with patch.object(wizard.usbmux, 'list_devices', AsyncMock(return_value=[device('phone')])), \
                patch.object(wizard, 'create_using_usbmux', connect), \
                patch.object(wizard, 'check_device'), \
                patch.object(wizard, 'AmfiService', Mock(return_value=service)), \
                patch.object(wizard, 'InstallationProxyService', app_proxy), \
                patch.object(wizard.asyncio, 'sleep', AsyncMock()):
            async with wizard.ready_phone(Path('temporary'), wizard.Events(wire), timeout=2) as selected:
                self.assertIs(selected, lockdown)
        self.assertEqual(records(wire)[0]['stage'], 'developer')
        proxy.get_apps.assert_awaited_once()
        service.reveal_developer_mode_option_in_ui.assert_awaited_once()

    async def test_old_receipt_does_not_satisfy_a_new_install(self):
        stale = json.dumps(dict(version=1, nonce='old', status='ready')).encode()
        fresh = json.dumps(dict(version=1, nonce='new', status='ready')).encode()
        with patch.object(wizard.setup, 'read_file', AsyncMock(side_effect=[stale, fresh])) as read, \
                patch.object(wizard.asyncio, 'sleep', AsyncMock()):
            await wizard.wait_receipt(object(), 'new', timeout=2)
        self.assertEqual(read.await_count, 2)

    async def test_developer_unavailable_is_not_success(self):
        failed = json.dumps(dict(version=1, nonce='new', status='developer-unavailable')).encode()
        with patch.object(wizard.setup, 'read_file', AsyncMock(return_value=failed)):
            with self.assertRaisesRegex(wizard.WizardError, 'could not connect'):
                await wizard.wait_receipt(object(), 'new', timeout=2)

    async def test_home_completion_requires_helpers_still_installed(self):
        board = SimpleNamespace(get_icon_state=AsyncMock(return_value=[[], [map_icon()]]))
        @asynccontextmanager
        async def springboard(*args):
            yield board
        proxy = SimpleNamespace(get_apps=AsyncMock(return_value={wizard.setup.VPN: {}}))
        @asynccontextmanager
        async def app_proxy(*args):
            yield proxy
        with patch.object(wizard, 'SpringBoardServicesService', springboard), \
                patch.object(wizard, 'InstallationProxyService', app_proxy):
            with self.assertRaisesRegex(wizard.WizardError, 'deleted'):
                await wizard.wait_home(object(), wizard.Events(io.StringIO()), timeout=2)

    async def test_home_completion_accepts_one_first_page_icon_and_hidden_helpers(self):
        board = SimpleNamespace(get_icon_state=AsyncMock(return_value=[[], [map_icon()]]))
        @asynccontextmanager
        async def springboard(*args):
            yield board
        proxy = SimpleNamespace(get_apps=AsyncMock(return_value={key: {} for key in wizard.HELPERS}))
        @asynccontextmanager
        async def app_proxy(*args):
            yield proxy
        with patch.object(wizard, 'SpringBoardServicesService', springboard), \
                patch.object(wizard, 'InstallationProxyService', app_proxy):
            await wizard.wait_home(object(), wizard.Events(io.StringIO()), timeout=2)

    async def test_challenge_is_published_before_opening_files_and_waiting_for_launch(self):
        order = []
        lockdown = object()
        @asynccontextmanager
        async def ready(*args):
            yield lockdown
        afc = SimpleNamespace(send_command=AsyncMock(), aclose=AsyncMock())
        async def awake(*args):
            await asyncio.Event().wait()
        async def write(afc, path, data):
            order.append('challenge' if path == wizard.setup.CHALLENGE else 'shortcut')
        async def open_files(*args): order.append('files')
        async def receipt(*args): order.append('receipt')
        with patch.object(wizard, 'ready_phone', ready), \
                patch.object(wizard, 'keep_awake', awake), \
                patch.object(wizard, 'download_assets', Mock(return_value={})), \
                patch.object(wizard, 'HouseArrestService', Mock(return_value=afc)), \
                patch.object(wizard.setup, 'stage_phone', AsyncMock()), \
                patch.object(wizard.setup, 'read_file', AsyncMock(return_value=None)), \
                patch.object(wizard.setup, 'write_verified', write), \
                patch.object(wizard, 'shortcut_location', AsyncMock(return_value='a-Shell > Location Map.shortcut')), \
                patch.object(wizard, 'open_files', open_files), \
                patch.object(wizard, 'wait_receipt', receipt), \
                patch.object(wizard, 'wait_home', AsyncMock()):
            await wizard.install({}, b'launcher', wizard.Events(io.StringIO()))
        self.assertLess(order.index('challenge'), order.index('files'))
        self.assertLess(order.index('files'), order.index('receipt'))
        afc.aclose.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

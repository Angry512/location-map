"""JSON-lines backend for the portable, guided Location Map setup window.

No device is contacted at import or in --check mode. Phone approvals remain
on the phone. A success event requires the fresh launch receipt and the final
Home Screen arrangement, rather than just a successful USB file transfer.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager, redirect_stderr, redirect_stdout, suppress
from copy import deepcopy
from importlib.metadata import version
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory

# -I deliberately ignores PYTHONPATH and the invoking working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_phone as setup
from cryptex_assets import CryptexSetupError, download_assets
from editor_launcher import EditorSetupError, _discover, _prepare_image
from usb_pairing import PINNED_PMD_VERSION, PairingSetupError, check_device
from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    AfcException, AppNotInstalledError, ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError, NotEnoughDiskSpaceError, PyMobileDevice3Exception,
    UserDeniedPairingError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote import tunnel_service
from pymobiledevice3.remote.core_device.app_service import AppServiceService
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.afc import AfcError
from pymobiledevice3.services.house_arrest import HouseArrestService, VEND_DOCUMENTS
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.power_assertion import PowerAssertionService
from pymobiledevice3.services.springboard import SpringBoardServicesService


ASHELL_URL = 'https://apps.apple.com/us/app/a-shell/id1473805438'
VPN_URL = 'https://apps.apple.com/us/app/localdevvpn/id6755608044'
APPLE_DEVICES_URL = 'ms-windows-store://pdp/?productid=9NP83LWLPZ9K'
HELPERS = {setup.ASHELL: 'a-Shell', setup.VPN: 'LocalDevVPN'}
PHONE_STEP_TIMEOUT = 30 * 60
USB_TIMEOUT = 15
CLEANUP_TIMEOUT = 3


class WizardError(RuntimeError):
    """Only fixed, recipient-readable messages belong in this exception."""


class Events:
    def __init__(self, wire=None):
        self.wire = wire if wire is not None else sys.stdout
        self.last = None

    def emit(self, stage, title, message, progress, actions=()):
        record = dict(stage=stage, title=title, message=message,
                      progress=progress, actions=list(actions))
        if record != self.last:
            print(json.dumps(record, ensure_ascii=True), file=self.wire, flush=True)
            self.last = record


class TransferOutput(io.TextIOBase):
    """Discard library text, translating only our known progress counters."""
    def __init__(self, events):
        self.events = events
        self.pending = ''

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            progress = re.fullmatch(r'Checked (\d{1,6}) of (\d{1,6}) new files\.', line.strip())
            if progress:
                self.events.emit('transfer', 'Copying Location Map',
                                 'Keep the phone connected and unlocked. Checked '
                                 + progress[1] + ' of ' + progress[2] + ' new files.', 55)
            elif line.startswith('Preparing device pairing.'):
                self.events.emit('transfer', 'Connecting Location Map to this iPhone',
                                 'Keep the phone unlocked. Approve a pairing prompt if one appears.', 60)
        return len(text)

    def flush(self):
        pass


class QuietOutput(io.TextIOBase):
    """Third-party diagnostics must not escape the fixed GUI error messages."""
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def choose_usb(devices, selected):
    """An unplug/restart must never silently switch setup to another phone."""
    serials = {d.serial for d in devices if d.connection_type == 'USB'}
    if len(serials) > 1:
        return None, 'multiple'
    if selected is not None:
        return (selected, 'ready') if selected in serials else (None, 'reconnect')
    return (next(iter(serials)), 'ready') if serials else (None, 'missing')


@asynccontextmanager
async def ready_phone(cache, events, timeout=PHONE_STEP_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    selected = None
    revealed = False
    while asyncio.get_running_loop().time() < deadline:
        yielded = False
        try:
            devices = await asyncio.wait_for(usbmux.list_devices(), USB_TIMEOUT)
            serial, state = choose_usb(devices, selected)
            if state != 'ready':
                messages = {
                    'multiple': 'Unplug other Apple devices. Leave only the iPhone you want to set up connected.',
                    'reconnect': 'Reconnect the same iPhone. After a restart, unlock it and tap Trust if asked.',
                    'missing': 'Plug in one iPhone and unlock it. Tap Trust on the phone, then enter its passcode. If it is not detected, install Apple Devices on this PC.',
                }
                events.emit('trust', 'Connect the iPhone', messages[state], 10,
                            [{'label': 'Get Apple Devices', 'url': APPLE_DEVICES_URL}])
            else:
                selected = serial
                args = dict(serial=selected, connection_type='USB',
                            pairing_records_cache_folder=cache, pair_timeout=8)
                async with await asyncio.wait_for(create_using_usbmux(**args), USB_TIMEOUT) as lockdown:
                    check_device(lockdown)
                    if not await asyncio.wait_for(lockdown.get_developer_mode_status(), USB_TIMEOUT):
                        if not revealed:
                            with suppress(PyMobileDevice3Exception, OSError, TimeoutError):
                                await asyncio.wait_for(AmfiService(lockdown).reveal_developer_mode_option_in_ui(), USB_TIMEOUT)
                                revealed = True
                        events.emit('developer', 'Turn on Developer Mode',
                                    'On the iPhone: Settings > Privacy & Security > Developer Mode. Turn it on and tap Restart. After it restarts, unlock it, tap Turn On, and enter the passcode. Leave the USB cable connected; setup will continue by itself.', 20)
                    else:
                        async with InstallationProxyService(lockdown) as proxy:
                            apps = await asyncio.wait_for(proxy.get_apps(bundle_identifiers=list(HELPERS)), USB_TIMEOUT)
                        missing = [bundle for bundle in HELPERS if bundle not in apps]
                        if missing:
                            actions = [{'label': 'Get ' + HELPERS[bundle],
                                        'url': ASHELL_URL if bundle == setup.ASHELL else VPN_URL}
                                       for bundle in missing]
                            names = ' and '.join(HELPERS[b] for b in missing)
                            events.emit('apps', 'Get the free helper apps',
                                        'On the iPhone, download ' + names + ' from the App Store. Choose the full a-Shell, not a-Shell mini. Approve the free downloads with the side button or Face ID if asked. A download is finished when the App Store button says Open. Setup will notice automatically.', 30, actions)
                        else:
                            yielded = True
                            yield lockdown
                            return
        except UserDeniedPairingError:
            raise WizardError('Trust was declined. Close setup, reconnect the iPhone, then open START HERE again and tap Trust on the phone.') from None
        except ConnectionFailedToUsbmuxdError:
            if yielded:
                raise
            events.emit('trust', 'Install Apple Devices on this PC',
                        'Windows cannot open its Apple USB service. Use Get Apple Devices below to install Apple Devices, then restart this PC and open START HERE again. The iPhone apps alone do not install the Windows USB driver.', 10,
                        [{'label': 'Get Apple Devices', 'url': APPLE_DEVICES_URL}])
        except (OSError, EOFError, TimeoutError, PyMobileDevice3Exception):
            if yielded:
                raise
            events.emit('trust', 'Waiting for the iPhone',
                        'Keep the same iPhone connected and unlocked. Tap Trust if asked. If it still does not appear, open Apple Devices on this PC.', 10,
                        [{'label': 'Get Apple Devices', 'url': APPLE_DEVICES_URL}])
        await asyncio.sleep(3)
    raise WizardError('Setup has waited 30 minutes. Close it, reconnect and unlock the same iPhone, then open START HERE to continue.')


async def keep_awake(lockdown):
    # This assertion expires on its own and never changes the Auto-Lock setting.
    while True:
        try:
            async with PowerAssertionService(lockdown) as power:
                async with power.create_power_assertion('PreventUserIdleDisplaySleep',
                                                         'Location Map setup', 60):
                    await asyncio.sleep(35)
        except (PyMobileDevice3Exception, OSError, EOFError):
            await asyncio.sleep(10)


async def open_files(lockdown, assets):
    """Best effort only: phone instructions work even if app launch is unavailable."""
    async with AsyncExitStack() as stack:
        proxy = await tunnel_service.CoreDeviceTunnelProxy.create(lockdown)
        stack.push_async_callback(proxy.close)
        original = tunnel_service.USE_USERSPACE_TUNNEL
        stack.callback(setattr, tunnel_service, 'USE_USERSPACE_TUNNEL', original)
        tunnel_service.USE_USERSPACE_TUNNEL = True
        tunnel = await stack.enter_async_context(proxy.start_tcp_tunnel())
        tunnel.client.tun.set_peer(tunnel.address)
        dial = await stack.enter_async_context(UserspaceDialPlane(tunnel.client.tun, tunnel.address))
        async with _discover(lockdown, tunnel, dial.dial) as rsd:
            await _prepare_image(rsd, assets)
        async with _discover(lockdown, tunnel, dial.dial) as rsd:
            async with AppServiceService(rsd) as apps:
                await apps.launch_application('com.apple.DocumentsApp', kill_existing=False)


async def wait_receipt(afc, nonce, timeout=PHONE_STEP_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        data = await asyncio.wait_for(setup.read_file(afc, setup.RECEIPT, 4096), USB_TIMEOUT)
        if data is not None:
            with suppress(ValueError, UnicodeError):
                receipt = json.loads(data)
                if isinstance(receipt, dict) and receipt.get('version') == 1 and receipt.get('nonce') == nonce:
                    if receipt.get('status') == 'ready':
                        return
                    if receipt.get('status') == 'developer-unavailable':
                        raise WizardError('The map opened but could not connect. Open LocalDevVPN and tap Connect, keep the phone online and unlocked, then close and reopen START HERE.')
        await asyncio.sleep(2)
    raise WizardError('The first map launch has not been confirmed. Check that LocalDevVPN is connected, run the Location Map shortcut and allow its prompts, then open START HERE again.')


async def shortcut_location(afc, launcher):
    """A convenience copy must never block setup or overwrite a recipient file."""
    root = setup.DOCUMENTS / setup.SHORTCUT
    info = await asyncio.wait_for(setup.stat_if_present(afc, root), USB_TIMEOUT)
    if info is None:
        await asyncio.wait_for(setup.write_verified(afc, root, launcher), USB_TIMEOUT)
        return 'a-Shell > Location Map.shortcut'
    if info.get('st_ifmt') == 'S_IFREG' and info.get('st_size') == len(launcher):
        current = await asyncio.wait_for(setup.read_file(afc, root, len(launcher)), USB_TIMEOUT)
        if current == launcher:
            return 'a-Shell > Location Map.shortcut'
    # stage_phone has already verified the copy inside our own project folder.
    return 'a-Shell > PhoneLocationOneApp > Location Map.shortcut'


async def close_afc(afc):
    # A disconnected phone must not hang Exit or mask the actual setup error.
    with suppress(Exception):
        await asyncio.wait_for(afc.aclose(), CLEANUP_TIMEOUT)


def icon_summary(state):
    """Read only layout metadata. Do not treat missing icons as uninstalled apps."""
    maps = []
    visible_helpers = set()
    def visit(value, page, path):
        if isinstance(value, dict):
            bundle = value.get('displayIdentifier') or value.get('bundleIdentifier')
            if bundle in HELPERS:
                visible_helpers.add(bundle)
            if value.get('displayName') == 'Location Map' and isinstance(value.get('webClipURL'), str):
                maps.append((page, path))
            if isinstance(value.get('iconLists'), list):
                visit(value['iconLists'], page, path + ('iconLists',))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, page, path + (index,))
    if not isinstance(state, list):
        raise WizardError('The iPhone Home Screen could not be checked. Keep it unlocked, then reopen START HERE.')
    for page, values in enumerate(state):
        visit(values, page, ())
    return maps, visible_helpers


def first_page_plan(state):
    """Swap the new root-level icon with an app; preserve widgets and all icons."""
    maps, _ = icon_summary(state)
    if len(maps) != 1 or maps[0][0] == 1 or len(maps[0][1]) != 1 or len(state) < 2:
        return None
    page, (slot,) = maps[0]
    candidates = [i for i, item in enumerate(state[1]) if isinstance(item, dict)
                  and item.get('displayIdentifier') and not item.get('iconType')
                  and not item.get('webClipURL')]
    if not candidates:
        return None
    result = deepcopy(state)
    target = candidates[-1]
    result[1][target], result[page][slot] = result[page][slot], result[1][target]
    return result


def layout_inventory(state):
    """Identity counts for all icons and widget definitions, independent of pages."""
    inventory = Counter()
    def freeze(value):
        if isinstance(value, dict):
            return tuple((key, freeze(child)) for key, child in sorted(value.items())
                         if key not in ('iconModDate', 'userSelectedElementIdentifier'))
        if isinstance(value, list):
            return tuple(freeze(child) for child in value)
        return repr(value)
    def visit(value):
        if isinstance(value, dict):
            identifier = value.get('displayIdentifier') or value.get('bundleIdentifier')
            if identifier:
                inventory[('icon', identifier)] += 1
            # Stack contents belong to a widget's identity, not separate app icons.
            if value.get('iconType') and value.get('iconType') not in ('folder',):
                if any(key in value for key in ('widgetIdentifier', 'elements', 'gridSize')):
                    inventory[('widget', freeze(value))] += 1
            if isinstance(value.get('iconLists'), list):
                visit(value['iconLists'])
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(state)
    return inventory


async def require_helpers(lockdown):
    async with InstallationProxyService(lockdown) as proxy:
        apps = await asyncio.wait_for(proxy.get_apps(bundle_identifiers=list(HELPERS)), USB_TIMEOUT)
    if not all(bundle in apps for bundle in HELPERS):
        raise WizardError('A helper app was deleted or has not finished downloading. Reinstall a-Shell and LocalDevVPN from the App Store, then open START HERE again. When hiding their icons, choose Remove from Home Screen, not Delete App.')


async def wait_home(lockdown, events, timeout=PHONE_STEP_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    arranged = False
    while asyncio.get_running_loop().time() < deadline:
        await require_helpers(lockdown)
        async with SpringBoardServicesService(lockdown) as board:
            state = await asyncio.wait_for(board.get_icon_state(), USB_TIMEOUT)
            maps, visible = icon_summary(state)
            if len(maps) > 1:
                raise WizardError('More than one Location Map Home Screen icon was found. Keep one icon, then reopen START HERE.')
            if len(maps) == 1 and maps[0][0] != 1 and not arranged:
                planned = first_page_plan(state)
                arranged = True
                if planned is not None:
                    # Do not overwrite a concurrent Home Screen edit made while
                    # the recipient was following the instructions.
                    current = await asyncio.wait_for(board.get_icon_state(), USB_TIMEOUT)
                    if current == state:
                        await asyncio.wait_for(board.set_icon_state(planned), USB_TIMEOUT)
                        await asyncio.sleep(1)
                        current = await asyncio.wait_for(board.get_icon_state(), USB_TIMEOUT)
                        if layout_inventory(current) != layout_inventory(state):
                            raise WizardError('The Home Screen changed while setup was arranging the icon. Setup has not marked it complete. Check the Home Screen, then reopen START HERE.')
                    state = current
                    maps, visible = icon_summary(state)
        if not maps:
            message = ('In Shortcuts, open Location Map, tap the small arrow beside its name, then Add to Home Screen > Add. Setup will put the icon on the first page when possible.\n\nThen keep a-Shell and LocalDevVPN installed, but remove their icons: hold each helper icon > Remove App > Remove from Home Screen. Do not choose Delete App.')
        elif maps[0][0] != 1:
            message = ('Move the Location Map icon to the first Home Screen page: hold it and drag it to the left edge until you reach the first page.\n\nKeep a-Shell and LocalDevVPN installed. Hold each helper icon > Remove App > Remove from Home Screen. Do not choose Delete App.')
        elif visible:
            names = ' and '.join(HELPERS[b] for b in HELPERS if b in visible)
            message = ('Location Map is on the first Home Screen page. Now hide the helper icons: hold ' + names + ' > Remove App > Remove from Home Screen. Do not choose Delete App. The helpers must stay installed in the App Library.')
        else:
            return
        events.emit('home', 'Finish the Home Screen', message, 90)
        await asyncio.sleep(3)
    raise WizardError('The Home Screen steps have not been confirmed. Keep both helper apps installed, add Location Map to the first page, then open START HERE again.')


async def install(files, launcher, events):
    os.environ.pop('USBMUXD_SOCKET_ADDRESS', None)
    with TemporaryDirectory(prefix='LocationMapUsbTrust-') as temporary:
        async with ready_phone(Path(temporary), events) as lockdown:
            awake = asyncio.create_task(keep_awake(lockdown))
            try:
                events.emit('transfer', 'Preparing the automatic setup',
                            'Leave the iPhone connected and unlocked. Keep this PC online while the checked developer files download.', 40)
                assets = await asyncio.to_thread(download_assets)
                afc = HouseArrestService(lockdown, documents_only=True)
                try:
                    await asyncio.wait_for(afc.send_command(setup.ASHELL, VEND_DOCUMENTS), USB_TIMEOUT)
                    events.emit('transfer', 'Copying Location Map',
                                'Setup is copying and checking the files. Leave the cable connected.', 50)
                    with redirect_stdout(TransferOutput(events)):
                        await asyncio.wait_for(setup.stage_phone(lockdown, afc, files | assets, launcher), 15 * 60)
                    location = await shortcut_location(afc, launcher)
                    nonce = secrets.token_hex(16)
                    await asyncio.wait_for(setup.write_verified(afc, setup.CHALLENGE, nonce.encode('ascii')), USB_TIMEOUT)
                    events.emit('launcher', 'Open Location Map on the iPhone',
                                '1. Open LocalDevVPN > Connect. Approve its VPN prompt.\n\n2. In Files, open Browse > On My iPhone > ' + location + '. Tap Add Shortcut. If you see a preview, tap Share > Shortcuts. Already added it? Skip this step.\n\n3. In Shortcuts, run Location Map again. Allow its VPN and location prompts. For sharing location with a-Shell, choose Always Allow. Setup will check the connection.', 70)
                    with suppress(Exception):
                        await asyncio.wait_for(open_files(lockdown, assets), 90)
                    await wait_receipt(afc, nonce)
                    await wait_home(lockdown, events)
                finally:
                    await close_afc(afc)
            finally:
                awake.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await awake


def main(argv=None):
    parser = argparse.ArgumentParser(description='Guided Location Map setup')
    parser.add_argument('--check', action='store_true', help='verify the package without contacting an iPhone')
    args = parser.parse_args(argv)
    events = Events()
    logging.disable(logging.CRITICAL)
    phone_started = False
    try:
        if version('pymobiledevice3') != PINNED_PMD_VERSION:
            raise WizardError('The setup runtime does not match this USB kit. Use the complete kit and open START HERE.')
        try:
            archive = setup.KIT / 'Phone/Location-Map-source.zip'
            signed = setup.KIT / 'Phone/Location Map.shortcut'
            if not archive.is_file() or not signed.is_file():
                raise FileNotFoundError
            files = setup.read_payload(archive)
            launcher = setup.preview_launcher(setup.KIT)
        except OSError:
            raise WizardError('Some USB setup files are missing or unreadable. Copy the complete kit to the USB drive, then open START HERE again.') from None
        if args.check:
            events.emit('done', 'USB package check passed',
                        'The app archive, signed launcher and bundled USB library match the checked package. No phone was contacted.', 100)
            return 0
        # Some upstream services print progress text. The GUI protocol must
        # contain only our bounded, non-sensitive JSON events.
        phone_started = True
        with redirect_stdout(TransferOutput(events)), redirect_stderr(QuietOutput()):
            asyncio.run(install(files, launcher, events))
        events.emit('done', 'Location Map is ready to try',
                    'Installation checks passed. Open Location Map on the first Home Screen page, choose a point, and tap Simulate. Tap Stop to restore the real location. Keep a-Shell and LocalDevVPN installed in the App Library. You can disconnect the USB cable.', 100)
        return 0
    except (WizardError, setup.SetupError, PairingSetupError, CryptexSetupError, EditorSetupError) as error:
        events.emit('error', 'Setup needs attention', str(error), 0)
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        events.emit('error', 'Setup stopped', 'Files already copied are kept. Open START HERE again with the same iPhone to continue.', 0)
        return 130
    except AfcException as error:
        if error.status == AfcError.NO_SPACE_LEFT:
            events.emit('error', 'The iPhone needs more free space',
                        'On the iPhone, open Settings > General > iPhone Storage and free some space. Then open START HERE again. Files already copied can be reused.', 0)
        elif error.status == AfcError.PERM_DENIED:
            events.emit('error', 'Unlock the iPhone',
                        'Unlock the same iPhone with its passcode, leave it connected, then open START HERE again.', 0)
        else:
            events.emit('error', 'The iPhone file transfer stopped',
                        'Reconnect and unlock the same iPhone, then open START HERE again. Files already copied can be reused.', 0)
        return 2
    except AppNotInstalledError:
        events.emit('error', 'Finish the a-Shell download',
                    'On the iPhone, finish installing the full a-Shell app. Wait until its App Store button says Open, then open START HERE again.', 0,
                    [{'label': 'Get a-Shell', 'url': ASHELL_URL}])
        return 2
    except NotEnoughDiskSpaceError:
        events.emit('error', 'More free storage is needed',
                    'Free storage on the iPhone under Settings > General > iPhone Storage, and check that this PC has free disk space. Then open START HERE again.', 0)
        return 2
    except OSError as error:
        if not phone_started:
            events.emit('error', 'The USB files could not be read',
                        'Keep the complete USB kit together, reconnect the USB drive, then open START HERE again.', 0)
        elif error.errno == 28:
            events.emit('error', 'This PC needs more free space',
                        'Free some space on this PC, then open START HERE again. No phone files need to be deleted.', 0)
        else:
            events.emit('error', 'Reconnect the iPhone',
                        'Reconnect and unlock the same iPhone, then open START HERE again. Files already copied can be reused.', 0)
        return 2
    except (ConnectionTerminatedError, EOFError, TimeoutError, PyMobileDevice3Exception):
        events.emit('error', 'Reconnect the iPhone',
                    'The connection was interrupted. Reconnect and unlock the same iPhone, then open START HERE again. Files already copied can be reused.', 0)
        return 2
    except Exception:
        events.emit('error', 'Setup could not finish',
                    'Nothing was marked complete. Keep the same iPhone connected and unlocked, read READ ME.txt, then open START HERE again.', 0)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

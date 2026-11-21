"""Pilot transfer of Location Map actions into the iPhone's Shortcut editor."""
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
import hashlib
import plistlib
from tempfile import TemporaryDirectory

from pymobiledevice3.exceptions import AlreadyMountedError, InvalidServiceError
from pymobiledevice3.remote import tunnel_service
from pymobiledevice3.remote.core_device.pasteboard_service import PasteboardService, data_item
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
from pymobiledevice3.services.cryptexd import CryptexdService

from cryptex_assets import COMPONENTS


WORKFLOW_SHA256 = 'f39c5d39ef093b948f775d58e618efb34265f8f7dc134d9d944f2592cea20656'
ACTION_TYPE = 'com.apple.shortcuts.action'


class EditorSetupError(RuntimeError):
    pass


def launcher_items(workflow_path):
    with workflow_path.open('rb') as source:
        data = source.read(64 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != WORKFLOW_SHA256:
        raise EditorSetupError('The editor test workflow failed its integrity check.')
    workflow = plistlib.loads(data)
    # A paste installs action rows only. It cannot install import questions.
    if workflow['WFWorkflowImportQuestions'] or not workflow['WFWorkflowActions']:
        raise EditorSetupError('The workflow is missing actions or requires import questions.')
    return [data_item(ACTION_TYPE, plistlib.dumps(action, fmt=plistlib.FMT_BINARY))
            for action in workflow['WFWorkflowActions']]


@asynccontextmanager
async def _discover(lockdown, tunnel, dial):
    rsd = RemoteServiceDiscoveryService((tunnel.address, tunnel.port), open_connection=dial,
                                        auxiliary_metadata=tunnel.auxiliary_metadata)
    try:
        await rsd.connect()
        if rsd.udid != lockdown.udid:
            raise EditorSetupError('The developer tunnel did not identify the selected USB iPhone.')
        yield rsd
    finally:
        await rsd.close()


async def _prepare_image(rsd, assets):
    # Assets were already fetched and verified before USB access. The upstream
    # installer wants a Restore directory. Keep it in Windows temporary storage.
    with TemporaryDirectory(prefix='LocationMapCryptex-') as temporary:
        directory = Path(temporary)
        for name in COMPONENTS:
            (directory / name).write_bytes(assets[f'ddi/{name}'])
        with suppress(AlreadyMountedError):
            await CryptexdService(rsd).auto_install_ddi(restore_dir=directory)


async def _write_actions(rsd, items):
    async with PasteboardService(rsd) as clipboard:
        response = await clipboard.set(items)
    if response.get('command') != 'SET_REPLY':
        raise EditorSetupError('The iPhone did not confirm the clipboard write.')


async def copy_to_editor(lockdown, assets, items):
    """Copy only kit actions. The caller supplies the already trusted USB session."""
    async with AsyncExitStack() as stack:
        proxy = await tunnel_service.CoreDeviceTunnelProxy.create(lockdown)
        stack.push_async_callback(proxy.close)
        stack.callback(setattr, tunnel_service, 'USE_USERSPACE_TUNNEL', tunnel_service.USE_USERSPACE_TUNNEL)
        tunnel_service.USE_USERSPACE_TUNNEL = True
        tunnel = await stack.enter_async_context(proxy.start_tcp_tunnel())
        tun = tunnel.client.tun
        tun.set_peer(tunnel.address)
        dial_plane = await stack.enter_async_context(UserspaceDialPlane(tun, tunnel.address))

        async with _discover(lockdown, tunnel, dial_plane.dial) as rsd:
            try:
                rsd.get_service_port(PasteboardService.SERVICE_NAME)
            except InvalidServiceError:
                await _prepare_image(rsd, assets)
            else:
                await _write_actions(rsd, items)
                return

        # Service discovery from before the mount still has the old service list.
        async with _discover(lockdown, tunnel, dial_plane.dial) as rsd:
            try:
                rsd.get_service_port(PasteboardService.SERVICE_NAME)
            except InvalidServiceError:
                raise EditorSetupError('The iPhone did not expose its developer clipboard service.') from None
            await _write_actions(rsd, items)

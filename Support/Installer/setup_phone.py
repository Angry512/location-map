"""Stage a verified Location Map payload over USB without exporting phone data."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import secrets
import stat
import sys
from tempfile import TemporaryDirectory
from xml.parsers.expat import ExpatError
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    AfcFileNotFoundError, ConnectionTerminatedError, PyMobileDevice3Exception, UserDeniedPairingError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import MAXIMUM_READ_SIZE
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.house_arrest import HouseArrestService, VEND_DOCUMENTS
from pymobiledevice3.services.installation_proxy import InstallationProxyService

from cryptex_assets import CryptexSetupError, download_assets
from editor_launcher import EditorSetupError, copy_to_editor, launcher_items
from usb_pairing import PINNED_PMD_VERSION, PairingSetupError, check_device, generate_pairing


KIT = Path(__file__).resolve().parents[1]
PROJECT = 'PhoneLocationOneApp'
DOCUMENTS = PurePosixPath('/Documents')
APP = DOCUMENTS / PROJECT
PROFILE = DOCUMENTS / '.profile'
PAIRING = APP / 'private/pairing.plist'
SESSION = APP / 'private/session.json'
CHALLENGE = APP / 'private/setup-challenge'
RECEIPT = APP / 'private/setup-receipt.json'
ASHELL = 'AsheKube.app.a-Shell'
VPN = 'com.jkcoxson.LocalDevVPN'
SHORTCUT = 'Location Map.shortcut'
AFC_RESPONSE_TIMEOUT = 15
AFC_CLEANUP_TIMEOUT = 3
PAYLOAD_HASH = 'b90bf6141a44e8b4d02ee9f2ac65fd4048f9427ed9639e3146392ed9c1ab9fe5'
PREVIEW_LAUNCHER_HASH = '6baf4f0b183b7a1f9a491255cffbab1d96e67fcd02792e7f213f127768b21676'
RELEASE_CHECKS = {
    'clean_windows_pc', 'fresh_usb_trust_and_pairing', 'interrupted_install_retry',
    'signed_shortcut_import_and_vpn_start', 'unplugged_wifi_simulate_and_stop',
    'unplugged_cellular_simulate_and_stop', 'cold_relaunch_and_reboot',
    'cellular_interruption_recovery', 'developer_image_recovery',
    'distribution_asset_review',
}
PRIVATE_NAMES = {'private', 'inbox', '__pycache__', 'pairing.plist', 'session.json',
                 'area.json', 'location-map-seed.txt', 'location-seed.txt',
                 'setup-challenge', 'setup-receipt.json'}


class SetupError(RuntimeError):
    """An actionable setup message without device IDs, paths or credentials."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def windows_setup_digest(kit):
    """Bind release evidence to the Windows scripts and their bundled runtime."""
    files = [kit / 'START-HERE.cmd', kit / 'Start-Location-Map.ps1']
    for directory in (kit / 'Installer', kit / 'Runtime/Python'):
        if not directory.is_dir():
            raise SetupError('The Windows installer or its runtime is missing.')
        files.extend(path for path in directory.rglob('*') if path.is_file())
    result = hashlib.sha256()
    for path in sorted(files):
        result.update(path.relative_to(kit).as_posix().encode('utf-8') + b'\0')
        with path.open('rb') as source:
            result.update(hashlib.file_digest(source, 'sha256').digest())
    return result.hexdigest()


def read_payload(archive_path):
    # The hardcoded archive digest ties these bytes to the tested app snapshot.
    # Check the archive in memory so its contents cannot change during staging.
    from io import BytesIO

    with archive_path.open('rb') as source:
        archive = source.read(32 * 1024 * 1024 + 1)
    if digest(archive) != PAYLOAD_HASH:
        raise SetupError('The app archive failed its integrity check. Use a complete copy of the kit.')

    files = {}
    with ZipFile(BytesIO(archive)) as payload:
        for entry in payload.infolist():
            parts = entry.filename.split('/')
            if (entry.orig_filename != entry.filename or parts[0] != PROJECT or len(parts) < 2 or
                    parts[1].casefold() == 'ddi' or
                    any(not re.fullmatch(r'[A-Za-z0-9_.+ -]+', part) or
                        part in ('.', '..') or part.casefold() in PRIVATE_NAMES for part in parts[1:]) or
                    entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)):
                raise SetupError('The app archive contains an unexpected file path.')
            name = '/'.join(parts[1:])
            if name in files:
                raise SetupError('The app archive contains duplicate files.')
            files[name] = payload.read(entry)

    sums = files.pop('SHA256SUMS', b'')
    expected = {}
    for line in sums.decode('ascii').splitlines():
        checksum, name = line.split('  ', 1)
        if name in expected:
            raise SetupError('The app file list contains duplicate entries.')
        expected[name] = checksum
    if (expected != {name: digest(data) for name, data in files.items()} or
            not {'phone_location.py', 'startup_profile.py', 'launch_proof.py', 'LICENSE'} <= files.keys()):
        raise SetupError('The app file list failed its integrity check.')
    return files


def approved_launcher(kit):
    try:
        record = json.loads((kit / 'release.json').read_text(encoding='utf-8'))
        launcher = read_launcher(kit)
        versions = record['tested_versions']
        checks = record['checks']
        if (record['payload_sha256'] == PAYLOAD_HASH and record['launcher_sha256'] == digest(launcher)
                and all(checks.get(check) is True for check in RELEASE_CHECKS)
                and all(isinstance(versions.get(key), str) and versions[key]
                        for key in ('ios_build', 'a_shell', 'local_dev_vpn'))
                and record['windows_setup_sha256'] == windows_setup_digest(kit)):
            # AEA1 is a format check. The release evidence must include actual iOS import.
            return launcher, versions
    except (OSError, KeyError, ValueError, TypeError, AttributeError, SetupError):
        pass
    if (kit / 'Phone' / SHORTCUT).is_file():
        raise SetupError('Preview only. The launcher is staged. A verified fresh-phone release record is still required.')
    raise SetupError('Preview only. A verified signed launcher and a fresh-phone release record are still required.')


def read_launcher(kit):
    limit = 4 * 1024 * 1024
    with (kit / 'Phone' / SHORTCUT).open('rb') as source:
        launcher = source.read(limit + 1)
    if not launcher.startswith(b'AEA1') or len(launcher) > limit:
        raise SetupError('The candidate launcher is not in the signed Shortcuts file format.')
    return launcher


def preview_launcher(kit):
    try:
        launcher = read_launcher(kit)
    except FileNotFoundError:
        raise SetupError('The signed launcher is missing. Keep the entire Location Map folder together.') from None
    if digest(launcher) != PREVIEW_LAUNCHER_HASH:
        raise SetupError('The signed launcher failed its integrity check. Use a complete copy of the preview kit.')
    return launcher


async def stat_if_present(afc, path):
    try:
        return await afc.stat(str(path))
    except AfcFileNotFoundError:
        return None


async def read_file(afc, path, max_size=1024 * 1024):
    info = await stat_if_present(afc, path)
    if info is None:
        return None
    if info['st_ifmt'] != 'S_IFREG' or not 0 <= info['st_size'] <= max_size:
        raise SetupError('An existing app file has an unexpected type or size. Setup paused to preserve it.')
    return await afc.get_file_contents(str(path))


async def ensure_directory(afc, path):
    info = await stat_if_present(afc, path)
    if info is None:
        await afc.makedirs(str(path))
        info = await stat_if_present(afc, path)
    if info is None or info['st_ifmt'] != 'S_IFDIR':
        raise SetupError('An existing app folder has an unexpected type. Setup paused to preserve it.')


async def write_verified(afc, path, data):
    pending = path.with_name('.' + path.name + '.location-map-pending')
    info = await stat_if_present(afc, pending)
    if info is not None and info['st_ifmt'] != 'S_IFREG':
        raise SetupError('An existing transfer file has an unexpected type. Setup paused to preserve it.')
    # Cancellation leaves the unpublished pending file for a retry to overwrite.
    # AFC session closure releases the handle without waiting for a phone reply.
    try:
        handle = await afc.fopen(str(pending), 'w')
        try:
            await afc.fwrite(handle, data, chunk_size=MAXIMUM_READ_SIZE)
        except ConnectionTerminatedError:
            raise
        except Exception:
            with suppress(Exception):
                await asyncio.wait_for(afc.fclose(handle), AFC_CLEANUP_TIMEOUT)
            raise
        await afc.fclose(handle)
        if await afc.get_file_contents(str(pending)) != data:
            raise SetupError('USB file verification failed. Reconnect the same phone and rerun setup.')
        await afc.rename(str(pending), str(path))
    except ConnectionTerminatedError:
        raise  # No cleanup RPCs on a dead connection. The pending file is safe to retry.
    except Exception:
        with suppress(Exception):
            await asyncio.wait_for(afc.rm_single(str(pending), force=True), AFC_CLEANUP_TIMEOUT)
        raise


async def require_idle(afc):
    journal = await read_file(afc, SESSION, 64 * 1024)
    if journal is not None:
        try:
            stopped = json.loads(journal)['requires_stop'] is False
        except (KeyError, ValueError, TypeError):
            stopped = False
        if not stopped:
            raise SetupError('Open the existing Location Map and tap Stop before running setup again.')


def validate_pairing(data):
    try:
        record = plistlib.loads(data)
        public = Ed25519PrivateKey.from_private_bytes(record['private_key']).public_key()
        if isinstance(record['identifier'], str) and record['identifier'] and public.public_bytes_raw() == record['public_key']:
            return
    except (KeyError, TypeError, ValueError, ExpatError, plistlib.InvalidFileException):
        pass
    raise SetupError('This phone has an invalid existing pairing file. Setup paused to preserve it.')


async def stage_phone(lockdown, afc, files, launcher):
    # Restrict this installer to clean installs and identical-payload retries.
    # Validate the whole destination before writing so a different install is preserved.
    await ensure_directory(afc, DOCUMENTS)
    await ensure_directory(afc, APP)
    await ensure_directory(afc, APP / 'private')
    await require_idle(afc)
    old_profile = await read_file(afc, PROFILE)
    startup = {}
    exec(compile(files['startup_profile.py'], 'startup_profile.py', 'exec'), startup)
    try:
        profile = startup['merge_profile']((old_profile or b'').decode('utf-8')).encode('utf-8')
    except (UnicodeError, ValueError):
        raise SetupError('The current a-Shell startup file could not be safely merged. It was preserved.') from None

    pairing = await read_file(afc, PAIRING, 64 * 1024)
    if pairing is not None:
        validate_pairing(pairing)

    planned = {APP / name: data for name, data in files.items()
               if '/' in name or not name.startswith('tests_')}
    if launcher is not None:
        planned[APP / SHORTCUT] = launcher
    directories = {parent for path in planned for parent in path.parents if APP in parent.parents}
    for directory in sorted(directories):
        await ensure_directory(afc, directory)

    missing = []
    for path, data in planned.items():
        existing = await read_file(afc, path, len(data))
        if existing is None:
            missing.append(path)
        elif existing != data:
            raise SetupError('A different Location Map payload is installed. This kit preserves it and stops.')

    print('Transferring and checking the app files...', flush=True)
    for index, path in enumerate(missing, 1):
        await write_verified(afc, path, planned[path])
        if index % 100 == 0:
            print(f'Checked {index} of {len(missing)} new files.', flush=True)

    if pairing is None:
        print('Preparing device pairing. Keep the phone unlocked and approve any pairing prompt there.', flush=True)
        pairing = await generate_pairing(lockdown)
        validate_pairing(pairing)
        await write_verified(afc, PAIRING, pairing)

    await require_idle(afc)
    if await read_file(afc, PROFILE) != old_profile:
        raise SetupError('a-Shell startup changed during setup. Rerun setup to merge the updated file.')
    if profile != old_profile:
        await write_verified(afc, PROFILE, profile)


async def wait_for_first_launch(afc, nonce, *, editor=False, timeout=8 * 60):
    if editor:
        print('On iPhone, open Shortcuts and create a blank shortcut. Paste the copied actions and name it Location Map.', flush=True)
        print('Run Location Map and allow the requested access.', flush=True)
    else:
        print('On iPhone, open Files > On My iPhone > a-Shell > PhoneLocationOneApp > Location Map.shortcut.', flush=True)
        print('If Files shows a preview, use Share > Shortcuts to import it.', flush=True)
        print('Add Shortcut, then run Location Map and allow the requested access.', flush=True)
    print('Waiting for the map, a fresh location handoff, and the iPhone developer location service...', flush=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        remaining = deadline - loop.time()
        try:
            receipt = await asyncio.wait_for(read_file(afc, RECEIPT, 4096), min(AFC_RESPONSE_TIMEOUT, remaining))
        except TimeoutError:
            raise SetupError('The iPhone stopped responding over USB. Unlock and reconnect it, then rerun Setup.') from None
        if receipt is not None:
            with suppress(ValueError, UnicodeError):
                result = json.loads(receipt)
                if isinstance(result, dict) and result.get('version') == 1 and result.get('nonce') == nonce:
                    if result.get('status') == 'ready':
                        return
                    if result.get('status') == 'developer-unavailable':
                        raise SetupError('The map opened, but the iPhone developer location service is unavailable. Keep the phone unlocked and online, then rerun Setup.')
        await asyncio.sleep(min(2, max(0, deadline - loop.time())))
    raise SetupError('First launch was not confirmed. Check VPN and Shortcuts permissions on the phone, then rerun setup.')


@asynccontextmanager
async def ready_phone(cache, tested_versions, timeout=20 * 60):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    selected = last_message = None
    revealed = False

    def status(message):
        nonlocal last_message
        if message != last_message:
            print(message, flush=True)
            last_message = message

    status('Connect and unlock one iPhone. Tap Trust and enter the passcode on the phone if prompted.')
    while loop.time() < deadline:
        ready = False
        try:
            devices = await usbmux.list_devices()
            usb = {device.serial for device in devices if device.connection_type == 'USB'}
            if len(usb) > 1:
                status('Disconnect other Apple devices. Setup waits for one USB iPhone.')
            elif len(usb) == 1 and (selected is None or selected in usb):
                selected = next(iter(usb))
                args = dict(serial=selected, connection_type='USB', pairing_records_cache_folder=cache)
                async with await create_using_usbmux(pair_timeout=12, **args) as lockdown:
                    check_device(lockdown)
                    if tested_versions and lockdown.all_values.get('BuildVersion') != tested_versions['ios_build']:
                        raise SetupError('This iOS build is outside this release’s verified configuration.')
                    async with InstallationProxyService(lockdown) as proxy:
                        apps = await proxy.get_apps(bundle_identifiers=[ASHELL, VPN])
                    if not {ASHELL, VPN} <= apps.keys():
                        status('Install full a-Shell and LocalDevVPN from the App Store. Setup will continue when both are installed.')
                    elif tested_versions and any(apps[bundle].get('CFBundleShortVersionString') != tested_versions[key]
                                                   for bundle, key in ((ASHELL, 'a_shell'), (VPN, 'local_dev_vpn'))):
                        raise SetupError('The installed helper versions are outside this release’s verified configuration.')
                    elif not await lockdown.get_developer_mode_status():
                        if not revealed:
                            with suppress(PyMobileDevice3Exception):
                                await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
                                revealed = True
                        status('On iPhone, go to Settings > Privacy & Security > Developer Mode. Enable it, restart, then confirm Turn On and unlock.')
                    else:
                        ready = True
                        yield lockdown
                        return
            elif selected is not None:
                status('Waiting for the same iPhone to reconnect by USB. Unlock it after any restart.')
            else:
                status('Plug in and unlock one iPhone. If it stays missing, open Apple Devices on Windows.')
        except UserDeniedPairingError:
            if ready:
                raise
            raise SetupError('Trust was declined on the phone. Start setup again when ready to approve it.') from None
        except (OSError, EOFError, PyMobileDevice3Exception):
            if ready:
                raise
            status('Waiting for USB and Trust. Open Apple Devices on Windows, and unlock the same iPhone.')
        await asyncio.sleep(3)
    raise SetupError('Setup timed out waiting for the iPhone. Reopen it to try again.')


async def install(files, launcher, tested_versions, editor_items=None):
    # Ignore any usbmux proxy override inherited from the invoking shell.
    os.environ.pop('USBMUXD_SOCKET_ADDRESS', None)
    # pymobiledevice3 can cache ordinary USB Trust credentials. Keep its copy in
    # a temporary PC folder. Apple's own USB service manages its separate record.
    with TemporaryDirectory(prefix='LocationMapUsbTrust-') as cache:
        async with ready_phone(Path(cache), tested_versions) as lockdown:
            afc = HouseArrestService(lockdown, documents_only=True)
            try:
                await asyncio.wait_for(afc.send_command(ASHELL, VEND_DOCUMENTS), AFC_RESPONSE_TIMEOUT)
                try:
                    await asyncio.wait_for(stage_phone(lockdown, afc, files, launcher), timeout=15 * 60)
                except TimeoutError:
                    raise SetupError('The app transfer timed out. Reconnect the same iPhone and rerun Setup to resume.') from None
                if launcher is not None or editor_items is not None:
                    nonce = secrets.token_hex(16)
                    await asyncio.wait_for(write_verified(afc, CHALLENGE, nonce.encode('ascii')), AFC_RESPONSE_TIMEOUT)
                    if editor_items is not None:
                        print('Copying the test actions to the selected iPhone clipboard...', flush=True)
                        await asyncio.wait_for(copy_to_editor(lockdown, files, editor_items), timeout=180)
                    await wait_for_first_launch(afc, nonce, editor=editor_items is not None)
            except TimeoutError:
                raise SetupError('The iPhone stopped responding during setup. Unlock and reconnect it, then rerun Setup.') from None
            finally:
                await afc.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Location Map USB Setup')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--install-preview', action='store_true', help='install the Windows-verified preview')
    mode.add_argument('--check-preview', action='store_true', help='verify the preview payload and launcher locally')
    mode.add_argument('--check-kit', action='store_true', help='check local files without connecting to a phone')
    mode.add_argument('--stage-test-device', action='store_true', help='stage on a dedicated test iPhone before launcher sign-off')
    mode.add_argument('--stage-editor-test-device', action='store_true',
                      help='stage a test iPhone and replace its clipboard with the launcher actions')
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)  # Upstream debug output can include device metadata.

    try:
        if version('pymobiledevice3') != PINNED_PMD_VERSION:
            raise SetupError('Use the kit’s bundled Windows runtime.')
        files = read_payload(KIT / 'Phone/Location-Map-source.zip')
        print('App archive integrity and bundled USB library version checks passed.', flush=True)
        editor_items = None
        if args.install_preview or args.check_preview:
            launcher = preview_launcher(KIT)
            tested_versions = None
            if args.check_preview:
                print('Preview payload and signed launcher match the checked Windows package.', flush=True)
                return 0
            print('Installing the Location Map preview. Read README.md for the iPhone verification limits.', flush=True)
        elif args.stage_editor_test_device:
            editor_items = launcher_items(KIT / 'Sources/Location Map.unsigned.plist')
            launcher = tested_versions = None
        elif args.stage_test_device:
            launcher = tested_versions = None
            with suppress(FileNotFoundError):
                launcher = read_launcher(KIT)
        else:
            launcher, tested_versions = approved_launcher(KIT)
        if args.check_kit:
            print('Kit files match the release record. No phone connection was opened.')
            return 0
        print('Downloading and verifying the developer image for the phone...', flush=True)
        asyncio.run(install(files | download_assets(), launcher, tested_versions, editor_items))
        if args.stage_test_device:
            print('Test files are staged. Follow Docs/Release-checklist.md for phone verification.')
        else:
            print('First launch confirmed. Add Location Map to the Home Screen using the Shortcut menu.')
        return 0
    except (SetupError, PairingSetupError, CryptexSetupError, EditorSetupError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Setup stopped. Keep the same iPhone and rerun to resume checked files.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Setup could not finish ({type(error).__name__}). Check the kit and unlock the same phone before retrying.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

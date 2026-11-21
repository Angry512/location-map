"""Fresh, verified RemotePairing credentials over an already trusted USB session.

Pinned API: pymobiledevice3 11.49.1, remote/tunnel_service.py
(`RemotePairingLockdownService`), also used by the upstream
`lockdown remotepairing --pair` command. No phone operation occurs at import.

No RemotePairing keys are read from or written to the PC/USB. The caller must
transfer returned bytes directly to the selected phone's private app directory,
then discard them. Ordinary Windows/Apple USB trust records are separate and
remain managed by Apple's USB service.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from importlib.metadata import version
import plistlib
import re
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pymobiledevice3.exceptions import RemotePairingCompletedError
from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService


PINNED_PMD_VERSION = "11.49.1"


class PairingSetupError(RuntimeError):
    """A fixed, non-secret installer message, never a raw device response."""


def check_device(lockdown):
    """Conservative app-protocol gate, independent of pairing's broader support."""
    values = lockdown.all_values
    if values.get("DeviceClass") != "iPhone":
        raise PairingSetupError("This kit currently supports iPhone only.")
    value = values.get("ProductVersion", "")
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){1,2}", value):
        raise PairingSetupError("The phone's iOS version could not be checked.")
    parts = tuple(map(int, value.split(".")))
    parts += (0,) * (3 - len(parts))
    if not ((26, 4, 0) <= parts < (28, 0, 0)):
        raise PairingSetupError("This kit currently requires iOS 26.4 through iOS 27.x.")
    if not lockdown.udid:
        raise PairingSetupError("Connect and trust the selected iPhone before pairing.")


class _MemoryPairing(RemotePairingLockdownService):
    """Use the upstream protocol with per-installation keys and no disk cache."""

    def __init__(self, service, remote_identifier):
        super().__init__(service, remote_identifier)
        self.identifier = str(uuid.uuid4()).upper()
        self._host_record = None

    @property
    def pair_record(self):
        return self._host_record

    @property
    def pair_record_path(self):
        # A future accidental call must fail rather than persist a secret.
        raise PairingSetupError("RemotePairing disk storage is disabled in this installer.")

    def save_pair_record(self):
        self._host_record = {
            "host_identifier": self.identifier,
            "public_key": self.ed25519_private_key.public_key().public_bytes_raw(),
            "private_key": self.ed25519_private_key.private_bytes_raw(),
            "remote_unlock_host_key": self.remote_unlock_host_key,
        }


async def _close(service):
    if service is not None:
        with suppress(Exception):
            await asyncio.wait_for(service.close(), timeout=10)


async def _generate(lockdown):
    service = verifier = None
    try:
        service = await _MemoryPairing.create(lockdown)
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            # The endpoint intentionally closes after successful pair-setup.
            pass
        record = service.pair_record
        if record is None:
            raise PairingSetupError("The phone did not complete fresh pairing. Keep it unlocked and retry setup.")
        await _close(service)
        service = None

        # Prove that the phone recognizes exactly these keys on a new control
        # connection. autopair=False prevents a failed verification from making
        # a replacement record or a second consent request.
        verifier = await _MemoryPairing.create(lockdown)
        verifier.identifier = record["host_identifier"]
        verifier._host_record = record
        await verifier.connect(autopair=False)
        # In pinned 11.49.1 connect() has no boolean return value and returns even
        # on failed verification when autopair=False. These ciphers are created
        # only on successful verification, by _init_client_server_main_encryption_keys.
        if not hasattr(verifier, "client_cip") or not hasattr(verifier, "server_cip"):
            raise PairingSetupError("Fresh pairing could not be verified. No credentials were installed.")

        private = record["private_key"]
        public = record["public_key"]
        key = Ed25519PrivateKey.from_private_bytes(private)
        if len(private) != 32 or len(public) != 32 or key.public_key().public_bytes_raw() != public:
            raise PairingSetupError("Generated pairing credentials failed their integrity check.")
        # This is the precise host-only RpPairingFile format used by the app.
        return plistlib.dumps({"identifier": record["host_identifier"],
                               "private_key": private, "public_key": public},
                              fmt=plistlib.FMT_XML, sort_keys=True)
    finally:
        await _close(verifier)
        await _close(service)


async def generate_pairing(lockdown, *, timeout=120):
    """Return a fresh verified host-only plist, retaining no PC/USB secret file.

    ``lockdown`` must be the installer's already connected, uniquely selected,
    trusted USB LockdownClient. Do not use an automatically chosen network peer.
    Developer Mode and the owner's passcode/Trust approvals must be completed.
    Raises PairingSetupError with a safe message; never retries in a loop (pairing
    creates a trusted host identity on the selected phone).
    """
    if version("pymobiledevice3") != PINNED_PMD_VERSION:
        raise PairingSetupError("The installer requires its bundled pymobiledevice3 11.49.1 runtime.")
    check_device(lockdown)
    try:
        if not await lockdown.get_developer_mode_status():
            raise PairingSetupError("Enable Developer Mode, restart, confirm Turn On, then unlock and retry setup.")
        return await asyncio.wait_for(_generate(lockdown), timeout=timeout)
    except PairingSetupError:
        raise
    except asyncio.TimeoutError:
        raise PairingSetupError("USB pairing timed out. Unlock the phone, confirm Trust, and retry setup.") from None
    except asyncio.CancelledError:
        raise
    except Exception:
        raise PairingSetupError("USB pairing failed. Check Developer Mode, the unlocked phone, and Apple USB drivers.") from None

#!/usr/bin/env python3
"""One native map + one on-phone controller. No browser or external-app URLs."""
from __future__ import annotations

import argparse
from contextlib import suppress
import ctypes as C
import json
import os
from pathlib import Path
import ssl
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))


def runtime_check(native=False):
    result = {'python': '.'.join(map(str, sys.version_info[:3])),
              'platform': sys.platform, 'tls_psk': bool(getattr(ssl, 'HAS_PSK', False)),
              'tls_psk_callback': hasattr(ssl.SSLContext, 'set_psk_client_callback')}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    key = Ed25519PrivateKey.generate()
    key.public_key().verify(key.sign(b'capability'), b'capability')
    a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    secret = HKDF(algorithm=hashes.SHA512(), length=32, salt=None, info=b'capability').derive(a.exchange(b.public_key()))
    cipher = ChaCha20Poly1305(secret)
    encrypted = cipher.encrypt(bytes(12), b'capability', None)
    assert cipher.decrypt(bytes(12), encrypted, None) == b'capability'
    result['crypto_primitives'] = True
    # Test the actual libffi closure mechanism, not merely an import of ctypes.
    callback = C.CFUNCTYPE(C.c_int)(lambda: 7)
    result['native_callback'] = callback() == 7
    import tcp_stack, services  # Source-only dependency/import verification.
    result['source_only_network_modules'] = True
    if native:
        from audio_keepalive import ObjC
        bridge = ObjC()
        result['host'] = bridge.bundle_capabilities()
        C.CDLL('/System/Library/Frameworks/UIKit.framework/UIKit')
        C.CDLL('/System/Library/Frameworks/MapKit.framework/MapKit')
        result['mapkit'] = bool(bridge.cls('MKMapView'))
        result['on_ui_main_thread'] = bool(bridge.send(bridge.cls('NSThread'), 'isMainThread', result=C.c_bool))
    result['ready_for_device_probe'] = all(result.get(key) for key in (
        'tls_psk', 'tls_psk_callback', 'crypto_primitives', 'native_callback', 'source_only_network_modules'))
    if native:
        result['background_audio_declared'] = 'audio' in result['host']['background_modes']
        result['ready_for_device_probe'] &= bool(result.get('mapkit') and result['background_audio_declared'])
    return result


def initial_area(path, supplied, pending_stop):
    from core import coordinate, read_journal
    saved = read_journal(path)
    result = saved.get('area')
    if isinstance(result, dict):
        try:
            result = dict(zip(('lat', 'lon'), coordinate(result['lat'], result['lon'])))
        except (ValueError, TypeError, KeyError):
            result = None
    else:
        result = None
    # A location supplied while a prior simulation is unconfirmed can be fake.
    # Keep the prior general-area seed instead of treating it as fresh real GPS.
    if supplied is not None and not pending_stop:
        result = dict(zip(('lat', 'lon'), coordinate(*supplied)))
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.new')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'area': result}, out)
        os.replace(temp, path)
    return result


def keep_interpreter_alive(on_main_thread, on_tick):
    if not on_main_thread:
        while True:
            on_tick()
            time.sleep(.5)
    # a-Shell normally runs Python off the UIKit main thread. If a host changes
    # that, service its run loop rather than freezing UIKit with time.sleep().
    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    mode = C.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
    run = cf.CFRunLoopRunInMode
    run.argtypes = [C.c_void_p, C.c_double, C.c_bool]
    run.restype = C.c_int32
    while True:
        on_tick()
        run(mode, .5, False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Native phone-only location map')
    parser.add_argument('--check', action='store_true', help='local runtime checks; no phone-service connection')
    parser.add_argument('--check-native', action='store_true', help='also check installed iOS host APIs')
    parser.add_argument('--pairing', type=Path, default=ROOT / 'private/pairing.plist')
    parser.add_argument('--latitude', type=float)
    parser.add_argument('--longitude', type=float)
    args = parser.parse_args(argv)
    if (args.latitude is None) != (args.longitude is None):
        parser.error('Pass both latitude and longitude for the initial area')
    if args.check or args.check_native:
        try:
            result = runtime_check(native=args.check_native)
            print(json.dumps(result, indent=2))
            return 0 if result['ready_for_device_probe'] else 1
        except Exception as error:
            # No pairing record is opened by this diagnostic.
            print(json.dumps({'ready_for_device_probe': False, 'error_type': type(error).__name__}))
            return 1
    if sys.platform not in ('darwin', 'ios'):
        print('Run this native map inside a-Shell on the iPhone. --check is available on this computer.')
        return 1
    os.umask(0o077)
    private = ROOT / 'private'
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One interpreter owns the native view and session. A second launch must not
    # duplicate developer tunnels or automatically repeat a prior Set.
    import fcntl
    lock = open(private / 'runtime.lock', 'a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Location Map is already running in this app.')
        lock.close()
        return 0
    screen = controller = watcher = None
    try:
        capabilities = runtime_check(native=True)
        if not capabilities['ready_for_device_probe'] or not capabilities.get('mapkit'):
            raise RuntimeError('Required native runtime capability unavailable')
        from core import Controller
        from transport import DeviceSession
        from ui_native import show
        controller = Controller(args.pairing, private / 'session.json',
                                link_factory=DeviceSession.open_link).start()
        supplied = None if args.latitude is None else (args.latitude, args.longitude)
        from area_seed import read_seed, AreaSeedWatcher, newest_seed, shortcut_seed_paths
        seed_paths = shortcut_seed_paths(ROOT)
        latest_seed = newest_seed(seed_paths)
        seed = read_seed(latest_seed[0]) if latest_seed else None
        if supplied is None and seed is not None:
            supplied = (seed['lat'], seed['lon'])
        area = initial_area(private / 'area.json', supplied, controller.snapshot()['requires_stop'])
        screen = show(controller, initial_area=area)
        watcher = AreaSeedWatcher(seed_paths, private / 'area.json', controller, screen.update_area).start()
        from launch_proof import LaunchProof
        proof = LaunchProof(private, seed_paths, controller)
        keep_interpreter_alive(capabilities.get('on_ui_main_thread', False), proof.tick)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        with suppress(Exception):
            import traceback
            (private / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        print('Location Map could not start (' + type(error).__name__ + '). Run --check-native for a capability report.')
        return 1
    finally:
        if watcher is not None:
            watcher.close()
        if controller is not None:
            controller.close()
        if screen is not None:
            with suppress(Exception):
                screen.close()
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())

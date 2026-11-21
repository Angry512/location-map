"""Build a USB-specific candidate from the pinned, tested owner snapshot."""
import argparse
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


OWNER_SHA256 = 'e01a619e0d9671c9a80d3e6a8d5247b3c011daba37c66e7b3d2541e48f440c76'
PROJECT = 'PhoneLocationOneApp/'
OVERLAY = ('core.py', 'launch_proof.py', 'phone_location.py', 'tests_end_to_end.py', 'transport.py', 'LAUNCHER.md')


def build(source, destination):
    if source.resolve() == destination.resolve():
        raise ValueError('Use a separate destination for the USB candidate')
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != OWNER_SHA256:
        raise ValueError('The owner archive does not match the pinned snapshot')
    with ZipFile(BytesIO(original)) as base:
        files = {name: base.read(name) for name in base.namelist()
                 if name != PROJECT + 'SHA256SUMS' and not name.startswith(PROJECT + 'ddi/')}
    for name in OVERLAY:
        files[PROJECT + name] = Path(__file__).with_name(name).read_bytes()
    readme = PROJECT + 'README.md'
    files[readme] = files[readme].replace(b'with bundled Cryptex assets',
                                          b'with Cryptex assets delivered during USB setup')
    files[PROJECT + 'SHA256SUMS'] = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name.removeprefix(PROJECT)}\n'
        for name, data in sorted(files.items())).encode('ascii')

    pending = destination.with_suffix('.pending.zip')
    with ZipFile(pending, 'w') as payload:
        for name, data in sorted(files.items()):
            info = ZipInfo(name, date_time=(2026, 11, 5, 0, 0, 0))
            payload.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=6)
    pending.replace(destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='pinned owner source ZIP')
    parser.add_argument('destination', type=Path, help='USB candidate ZIP path')
    args = parser.parse_args()
    print(build(args.source, args.destination))

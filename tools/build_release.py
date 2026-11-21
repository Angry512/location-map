"""Build the complete Windows download without including local phone data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'Support/Runtime.zip')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    expected = (ROOT / 'Support/runtime.sha256').read_text().strip()
    if sha(runtime) != expected:
        raise SystemExit('Runtime does not match Support/runtime.sha256.')
    # These are the only recipient files; Git, tests, caches and website files
    # never enter the install archive. Runtime is supplied independently.
    files = {'START HERE.cmd': ROOT / 'START HERE.cmd', 'READ ME.txt': ROOT / 'READ ME.txt'}
    for path in sorted((ROOT / 'Support').rglob('*')):
        if path.is_symlink():
            raise SystemExit('Symlinks are not allowed in the release.')
        if path.is_file() and path.name not in {'Manifest.json', 'Runtime.zip'}:
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            files[path.relative_to(ROOT).as_posix()] = path
    files['Support/Runtime.zip'] = runtime
    for name, expected_hash in (
        ('Support/Phone/Location-Map-source.zip', 'b90bf6141a44e8b4d02ee9f2ac65fd4048f9427ed9639e3146392ed9c1ab9fe5'),
        ('Support/Phone/Location Map.shortcut', '6baf4f0b183b7a1f9a491255cffbab1d96e67fcd02792e7f213f127768b21676'),
    ):
        if sha(files[name]) != expected_hash:
            raise SystemExit(f'Pinned app or launcher changed: {name}')
    manifest = {'format': 1, 'files': {
        name: {'sha256': sha(path), 'size': path.stat().st_size}
        for name, path in sorted(files.items())}}
    manifest_path = ROOT / 'Support/Manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    files['Support/Manifest.json'] = manifest_path
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'Location-Map-Windows.zip'
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED, compresslevel=6) as bundle:
        for name, path in sorted(files.items()):
            bundle.write(path, 'Location Map/' + name,
                         compress_type=ZIP_STORED if path.suffix.lower() == '.zip' else ZIP_DEFLATED)
    # Read the completed archive and compare every entry to its source bytes.
    with ZipFile(archive) as bundle:
        if len(bundle.infolist()) != len(files):
            raise SystemExit('Unexpected archive entries.')
        for name, path in files.items():
            actual = hashlib.sha256(bundle.read('Location Map/' + name)).hexdigest()
            if actual != sha(path):
                raise SystemExit(f'Archive verification failed: {name}')
    runtime_asset = output / 'Runtime.zip'
    if runtime != runtime_asset:
        shutil.copyfile(runtime, runtime_asset)
    checksums = ''.join(f'{sha(path)}  {path.name}\n' for path in (archive, runtime_asset))
    (output / 'SHA256SUMS.txt').write_text(checksums, encoding='ascii')
    print(f'Verified {len(files)} recipient files; download {archive.stat().st_size:,} bytes.')
    print(checksums, end='')


if __name__ == '__main__':
    main()

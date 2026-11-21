"""Remove unused pip command wrappers that embed a build-machine path.

The guided installer invokes python.exe directly. It never uses these
site-packages/bin/*.exe wrappers; library code, native modules and notices
are preserved byte for byte. Input and output must be different archives.
"""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise SystemExit('Choose a separate output archive.')
    manifest, skipped = {}, []
    with ZipFile(args.source) as source, ZipFile(args.output, 'w', ZIP_DEFLATED, compresslevel=6) as target:
        for item in source.infolist():
            if item.filename.startswith('Lib/site-packages/bin/') and item.filename.endswith('.exe'):
                skipped.append(item.filename)
                continue
            data = source.read(item)
            target.writestr(item, data)
            if not item.is_dir():
                manifest[item.filename] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    with ZipFile(args.output) as target:
        for name, record in manifest.items():
            if hashlib.sha256(target.read(name)).hexdigest() != record['sha256']:
                raise SystemExit('Runtime read-back check failed.')
    support = Path(__file__).resolve().parents[1] / 'Support'
    (support / 'Runtime.manifest.json').write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n')
    with args.output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (support / 'runtime.sha256').write_text(digest + '\n', encoding='ascii')
    print(f'Removed {len(skipped)} unused wrappers; verified {len(manifest)} unchanged runtime files.')
    print('SHA-256:', digest)


if __name__ == '__main__':
    main()

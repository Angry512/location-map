"""Verify the Apple-signed Location Map launcher and compare its embedded workflow."""
import argparse
from datetime import UTC, datetime
import hashlib
import hmac
from pathlib import Path
import plistlib
import struct
import sys

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import lzfse


# Apple Root CA - G3, published at https://support.apple.com/en-us/126047.
# The intermediate is the Apple System Integration CA 4 used for these exports.
ROOT = '63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179'
INTERMEDIATE = '7da79ba48b1c160852433c15a684dfe4747b81d1720787c339609cab7c68326c'
WORKFLOW_SIGNING = x509.ObjectIdentifier('1.2.840.113635.100.18.1')
MAX_SIGNED = 1024 * 1024
MAX_WORKFLOW = 64 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def signing_key(auth, now):
    metadata = plistlib.loads(auth)
    chain = metadata.get('SigningCertificateChain') if isinstance(metadata, dict) else None
    require(isinstance(chain, list) and len(chain) == 3 and all(isinstance(c, bytes) for c in chain),
            'Unexpected signing certificate chain.')
    leaf, intermediate, root = map(x509.load_der_x509_certificate, chain)
    require(root.fingerprint(hashes.SHA256()).hex() == ROOT and
            intermediate.fingerprint(hashes.SHA256()).hex() == INTERMEDIATE,
            'Unrecognized Apple signing authority.')
    for cert in (leaf, intermediate, root):
        require(cert.not_valid_before_utc <= now <= cert.not_valid_after_utc,
                'A signing certificate is outside its validity period.')
    leaf.verify_directly_issued_by(intermediate)
    intermediate.verify_directly_issued_by(root)
    require(not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca and
            leaf.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature and
            leaf.extensions.get_extension_for_oid(WORKFLOW_SIGNING) is not None,
            'The Apple leaf certificate is not a workflow signing certificate.')
    public = leaf.public_key()
    require(isinstance(public, ec.EllipticCurvePublicKey) and isinstance(public.curve, ec.SECP256R1),
            'Unexpected workflow signing key.')
    return public, leaf.not_valid_after_utc


def derive(key, info, salt=None):
    return HKDF(hashes.SHA256(), 32, salt, info).derive(key)


def authenticate(key, data, salt, expected):
    actual = hmac.digest(key, salt + data + struct.pack('<Q', len(salt)), 'sha256')
    require(hmac.compare_digest(actual, expected), 'Apple archive authentication failed.')


def extract_archive(data, now):
    # Supported shape is the small single-segment AEA profile-0 Shortcut export.
    # AEA field layout and HKDF labels: https://github.com/kinnay/AEA/blob/d22f4fdf620436d12f1a893bbb11b90ac52fa646/FORMAT.md
    require(12 <= len(data) <= MAX_SIGNED, 'Unexpected signed file size.')
    magic, profile, auth_size = struct.unpack_from('<4sII', data)
    require(magic == b'AEA1' and profile == 0 and 0 < auth_size <= 16 * 1024,
            'Expected a signed AEA1 workflow.')
    cursor = 12 + auth_size
    require(cursor + 304 <= len(data), 'Truncated archive prologue.')
    auth = data[12:cursor]
    public, expires = signing_key(auth, now)
    signature = data[cursor:cursor + 128]
    require(signature[0] == 0x30 and 0 < signature[1] < 126, 'Malformed archive signature.')
    signature_size = 2 + signature[1]
    require(not any(signature[signature_size:]), 'Malformed archive signature padding.')
    prologue = data[:cursor] + bytes(128) + data[cursor + 128:cursor + 304]
    public.verify(signature[:signature_size], prologue, ec.ECDSA(hashes.SHA256()))
    cursor += 128

    def read(length):
        nonlocal cursor
        end = cursor + length
        require(end <= len(data), 'Truncated archive payload.')
        result = data[cursor:end]
        cursor = end
        return result

    seed, salt, root_mac, root_header, cluster_mac = (read(n) for n in (32, 32, 32, 48, 32))
    public_point = public.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    main_key = derive(seed, b'AEA_AMK' + data[4:8] + public_point, salt)
    authenticate(derive(main_key, b'AEA_RHEK'), root_header, cluster_mac + auth, root_mac)
    raw_size, archive_size, segment_size, count, compression, checksum = struct.unpack_from('<QQIIcB', root_header)
    require(0 < raw_size <= MAX_WORKFLOW and raw_size <= segment_size and archive_size == len(data) and
            0 < count <= 512 and compression == b'e' and checksum == 2,
            'Unexpected archive layout for a small Shortcut.')
    cluster_key = derive(main_key, b'AEA_CK' + bytes(4))
    headers, next_mac, segment_macs = read(40 * count), read(32), read(32 * count)
    authenticate(derive(cluster_key, b'AEA_CHEK'), headers, next_mac + segment_macs, cluster_mac)
    original, compressed, expected = struct.unpack_from('<II32s', headers)
    require(original == raw_size and 0 < compressed <= original and not any(headers[40:]),
            'Unexpected archive segment layout.')
    segment = read(compressed)
    authenticate(derive(cluster_key, b'AEA_SK' + bytes(4)), segment, b'', segment_macs[:32])
    archive = lzfse.decompress(segment) if compressed < original else segment
    require(len(archive) == original and hashlib.sha256(archive).digest() == expected,
            'Archive segment checksum mismatch.')
    return archive, expires


def workflow_plist(archive):
    def header(start):
        require(archive[start:start + 4] == b'AA01' and start + 6 <= len(archive), 'Missing Apple Archive header.')
        size = struct.unpack_from('<H', archive, start + 4)[0]
        require(6 <= size <= len(archive) - start, 'Invalid Apple Archive header length.')
        return archive[start + 6:start + size], start + size

    directory, offset = header(0)
    require(directory.startswith(b'TYP1DPATP\0\0'), 'Unexpected Apple Archive directory.')
    entry, offset = header(offset)
    require(entry.startswith(b'TYP1FPATP\x0e\0Shortcut.wflow') and entry[-6:-2] == b'DATA',
            'The Apple Archive does not contain a single small Shortcut.wflow.')
    size = struct.unpack_from('<H', entry, len(entry) - 2)[0]
    require(size == len(archive) - offset and archive[offset:].startswith(b'bplist00'),
            'The Apple Archive workflow has an unexpected size or format.')
    return plistlib.loads(archive[offset:])


def same_plist(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_plist(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_plist(a, b) for a, b in zip(left, right))
    return left == right


def verify(signed, unsigned, *, now=None, allow_shortcuty_metadata=False):
    archive, expires = extract_archive(signed, datetime.now(UTC) if now is None else now)
    actual = workflow_plist(archive)
    expected = plistlib.loads(unsigned)
    for workflow in (actual, expected):
        require(isinstance(workflow, dict) and isinstance(workflow.get('WFWorkflowActions'), list),
                'Expected a Shortcuts workflow property list.')
    if allow_shortcuty_metadata:
        if expected.get('WFWorkflowName') == 'Location Map' and 'WFWorkflowName' not in actual:
            # The import candidate is staged as Location Map.shortcut.
            del expected['WFWorkflowName']
        if expected.get('WFWorkflowClientVersion') == '5037.109' and actual.get('WFWorkflowClientVersion') == '5037.0.17':
            expected['WFWorkflowClientVersion'] = '5037.0.17'
    # The comparison retains plist types while ignoring serialization and
    # object sharing differences between binary and XML property lists.
    differing = sorted(key for key in actual.keys() | expected.keys()
                       if key not in actual or key not in expected or not same_plist(actual[key], expected[key]))
    if differing:
        raise ValueError('Signed workflow differs from the input in ' + ', '.join(differing))
    return len(actual['WFWorkflowActions']), expires


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('signed', type=Path)
    parser.add_argument('unsigned', type=Path)
    parser.add_argument('--allow-shortcuty-metadata', action='store_true',
                        help='allow the observed Shortcuty name omission and client version normalization only')
    args = parser.parse_args()
    try:
        with args.signed.open('rb') as file:
            signed = file.read(MAX_SIGNED + 1)
        with args.unsigned.open('rb') as file:
            unsigned = file.read(MAX_WORKFLOW + 1)
        require(len(unsigned) <= MAX_WORKFLOW, 'Unexpected unsigned workflow size.')
        count, expires = verify(signed, unsigned, allow_shortcuty_metadata=args.allow_shortcuty_metadata)
    except InvalidSignature:
        print('Verification failed. The archive or certificate signature is invalid.', file=sys.stderr)
        return 1
    except (OSError, ValueError, x509.ExtensionNotFound, lzfse.error) as error:
        print(f'Verification failed. {error}', file=sys.stderr)
        return 1
    print(f'Verified Apple signature and an exact match for all {count} workflow actions.')
    if args.allow_shortcuty_metadata:
        print('Metadata comparison allows only the observed client version rewrite and workflow name omission.')
    else:
        print('All workflow metadata matches exactly.')
    print(f'Signed file SHA-256 {hashlib.sha256(signed).hexdigest()}')
    print(f'Leaf certificate valid through {expires.isoformat()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

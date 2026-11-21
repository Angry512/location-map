"""Fetch the exact Cryptex assets used by the Location Map candidate."""
import hashlib
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE = ('https://raw.githubusercontent.com/doronz88/DeveloperDiskImage/'
          'f0a9fb8ef81df9217fb2d2bcb4e3351af903408b/'
          'PersonalizedImages/Xcode_iOS_DDI_Cryptex/')
COMPONENTS = {
    'BuildManifest.plist': (804946, '27385d7582b03b36bb3104e22b520aee0c47d72fecb4e8ecfe12ef5d966c7012'),
    'Image.dmg': (15895040, '873097f695a8b9734e2abc54f795a8874d40ff6fd11208ecb01ef29534c7c176'),
    'Image.dmg.cryptex_info': (430, 'edf49aef55aacc063d4d7be05b713bb545ce2993b3f62bcc15eccd75e610ee6c'),
    'Image.dmg.root_hash': (229, '3543fad2805b88119695c417e12679380b3b5a2742994bbcc839c8e2de5d7302'),
    'Image.dmg.trustcache': (1895, 'f7f21986074eee03a215aca16ecfc78d6bf183600d8a0d2fb691f9896782e6f0'),
}


class CryptexSetupError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


def _download(opener, name, size, checksum):
    request = Request(SOURCE + name, headers={'User-Agent': 'Location-Map-Setup/1.0'})
    for attempt in range(2):
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(size + 1)
            break
        except (HTTPException, OSError):
            if attempt == 1:
                raise CryptexSetupError('The developer image could not be downloaded. Check internet access on Windows and reopen Setup.') from None
    if len(data) != size or hashlib.sha256(data).hexdigest() != checksum:
        raise CryptexSetupError('The developer image failed verification. Setup stopped before connecting to an iPhone.')
    return data


def download_assets():
    """Return checked files in memory without using credentials or a disk cache."""
    opener = build_opener(_NoRedirect())
    return {f'ddi/{name}': _download(opener, name, size, checksum)
            for name, (size, checksum) in COMPONENTS.items()}

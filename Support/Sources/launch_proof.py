"""A private first-launch acknowledgement for the one-time USB setup."""
import json
import os
import re
import time
from xml.parsers.expat import ExpatError

from area_seed import newest_seed, read_seed


CHALLENGE = 'setup-challenge'
RECEIPT = 'setup-receipt.json'
RADIO_SETTLE_SECONDS = 10.0


class LaunchProof:
    def __init__(self, private, seed_paths, controller):
        self.challenge = private / CHALLENGE
        self.receipt = private / RECEIPT
        self.seed_paths = tuple(path for path in seed_paths if path.name == 'location-map-seed.txt')
        self.controller = controller
        self.completed = None
        self.current_nonce = None
        self.probe = None
        self.probe_after = None

    def tick(self):
        # Called only after the native map is presented. This reads and writes the
        # phone's own sandbox and sends neither coordinates nor device credentials.
        try:
            with self.challenge.open('rb') as request:
                nonce = request.read(33)
                created = os.fstat(request.fileno()).st_mtime_ns
            if nonce == self.completed or re.fullmatch(rb'[0-9a-f]{32}', nonce) is None:
                return
            if nonce != self.current_nonce:
                self.current_nonce = nonce
                self.probe = None
                self.probe_after = None
            if self.probe is None:
                state = self.controller.snapshot()
                if not state['link_ready'] or state['state'] != 'idle' or state['requires_stop']:
                    return
                seed = newest_seed(self.seed_paths)
                if seed is None or seed[1] <= created or read_seed(seed[0]) is None:
                    return
                if self.probe_after is None:
                    self.probe_after = time.monotonic() + RADIO_SETTLE_SECONDS
                if time.monotonic() < self.probe_after:
                    return  # Give the launcher's eight-second cellular pause time to finish.
                self.probe = self.controller.probe_location_service()
            if self.probe is None or not self.probe.done():
                return
            status = 'ready' if self.probe.result() else 'developer-unavailable'
            pending = self.receipt.with_suffix('.pending')
            receipt = {'version': 1, 'nonce': nonce.decode('ascii'), 'status': status}
            pending.write_text(json.dumps(receipt), encoding='ascii')
            os.replace(pending, self.receipt)
            self.completed = nonce
        except (OSError, ValueError, ExpatError):
            # If files are being replaced or Setup has ended, normal map use continues.
            return

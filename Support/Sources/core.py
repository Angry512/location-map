"""Serialized on-phone location session with expiring acknowledgments.

No automatic replay after restart/sleep. Snapshot reads never perform network,
disk, or native UI work. A saved point is a selection, never proof of activity.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import threading
import time

LEASE = 12.0
RESEND = 5.0
IO_TIMEOUT = 25.0
MAX_PENDING_AGE = 180.0
LINK_WARM_WINDOW = 20.0
IDLE_BACKGROUND_GRACE = 35.0


def coordinate(lat, lon):
    if type(lat) not in (int, float) or type(lon) not in (int, float):
        raise ValueError('Use numeric coordinates')
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Latitude must be -90..90 and longitude -180..180')
    return float(lat), float(lon)


def elapsed(mono, wall):
    age = time.time() - wall
    return float('inf') if age < 0 else max(time.monotonic() - mono, age)


def read_journal(path):
    try:
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Controller:
    def __init__(self, pairing_path, journal_path, *, session_factory=None, audio_factory=None,
                 link_factory=None):
        self.pairing_path = Path(pairing_path)
        self.journal_path = Path(journal_path)
        self._session_factory = session_factory
        self._audio_factory = audio_factory
        self._link_factory = link_factory
        self._lock = threading.RLock()
        self._loop = None
        self._event = None
        self._thread = None
        self._runner = None
        self._ready = threading.Event()
        self._closing = False
        self._generation = 0
        self._pending = None
        self._active = None
        self._ack_mono = self._ack_wall = None
        self._state = 'idle'
        self._message = 'Choose a location.'
        self._selection = None
        self._needs_stop = False
        self._background_audio = False
        self._warm_until = 0.0
        self._idle_release_at = 0.0
        self._link_ready = False
        self._setup_probe = None
        saved = read_journal(self.journal_path)
        if isinstance(saved.get('point'), list) and len(saved['point']) == 2:
            with suppress(ValueError, TypeError):
                self._selection = coordinate(*saved['point'])
        if saved.get('requires_stop') is True:
            self._needs_stop = True
            self._state = 'error'
            self._message = 'Previous session is unconfirmed. Stop can reconnect and restore your location.'

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._thread_main, name='location-session', daemon=True)
        self._thread.start()
        if not self._ready.wait(3):
            raise RuntimeError('Location session could not start')
        return self

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception:
            self._publish(self._generation, 'error', 'Location engine stopped unexpectedly. Reopen the app.')
        finally:
            self._loop = None
            self._runner = None

    def _wake(self):
        if self._loop is not None and self._event is not None:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._event.set)

    def set(self, lat, lon):
        point = coordinate(lat, lon)
        with self._lock:
            if self._closing:
                return
            self._generation += 1
            self._selection = point
            self._pending = (self._generation, 'set', point, time.monotonic(), time.time())
            self._state, self._message = 'connecting', 'Setting your location…'
            self._ack_mono = self._ack_wall = None
            self._needs_stop = True
            try:
                self._save()
            except OSError:
                self._pending = None
                self._state, self._message = 'error', 'Could not save session state. No new location was applied.'
                return
        self._wake()

    def set_foreground(self, foreground):
        """Prepare a link during launch, including a brief cellular data pause."""
        if self._link_factory is None:
            return
        with self._lock:
            now = time.monotonic()
            if foreground:
                self._idle_release_at = float('inf')
                self._warm_until = now + LINK_WARM_WINDOW
            else:
                self._idle_release_at = now + IDLE_BACKGROUND_GRACE
        self._wake()

    def probe_location_service(self):
        """Ask the serialized worker to open the service without simulating."""
        with self._lock:
            if self._closing or self._link_factory is None:
                return None
            if self._setup_probe is None or self._setup_probe.done():
                self._setup_probe = Future()
            probe = self._setup_probe
        self._wake()
        return probe

    def stop(self):
        with self._lock:
            if self._closing:
                return
            self._generation += 1
            self._pending = (self._generation, 'clear', None, time.monotonic(), time.time())
            self._state, self._message = 'connecting', 'Stopping simulation…'
            self._ack_mono = self._ack_wall = None
        self._wake()

    def snapshot(self):
        with self._lock:
            state, message = self._state, self._message
            age = None if self._ack_mono is None else elapsed(self._ack_mono, self._ack_wall)
            fresh = state == 'active' and age is not None and age < LEASE
            if state == 'active' and not fresh:
                state, message = 'error', 'Live confirmation expired. Simulation is unconfirmed.'
            data = {'state': state, 'message': message, 'fresh': fresh,
                    'confirmed_age_ms': int(age * 1000) if age is not None and math.isfinite(age) else None,
                    'requires_stop': self._needs_stop, 'generation': self._generation,
                    'background_audio': self._background_audio,
                    'link_ready': self._link_ready,
                    'confirmation_source': 'developer_service_ack'}
            if self._selection:
                data.update(lat=self._selection[0], lon=self._selection[1])
            return data

    def _save(self):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        value = {'version': 1, 'requires_stop': self._needs_stop,
                 'point': list(self._selection) if self._selection else None}
        temporary = self.journal_path.with_suffix('.new')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600) if hasattr(os, 'fchmod') else None
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(json.dumps(value, separators=(',', ':')))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.journal_path)
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            raise

    def _publish(self, generation, state, message, *, ack=False, ack_at=None):
        with self._lock:
            if generation != self._generation:
                return False
            self._state, self._message = state, message
            if ack:
                self._ack_mono, self._ack_wall = ack_at or (time.monotonic(), time.time())
            else:
                self._ack_mono = self._ack_wall = None
            return True

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._runner = asyncio.current_task()
        self._event = asyncio.Event()
        self._ready.set()
        session = None
        audio = None
        active_generation = None
        next_send = 0.0

        def stop_audio():
            nonlocal audio
            if audio is not None:
                with suppress(Exception):
                    audio.stop()
                audio = None
            with self._lock:
                self._background_audio = False

        async def disconnect():
            nonlocal session
            stop_audio()
            with self._lock:
                self._link_ready = False
            if session is not None:
                with suppress(Exception):
                    await asyncio.wait_for(session.aclose(), 3)
                session = None

        async def clear_existing():
            if session is None:
                return False
            clear_if_open = getattr(session, 'clear_if_open', None)
            if clear_if_open is not None:
                return await clear_if_open()
            await session.clear()
            return True

        async def clear_after_failure(generation, message):
            self._active = None
            self._publish(generation, 'error', message)
            if session is not None:
                with suppress(Exception):
                    await asyncio.wait_for(clear_existing(), IO_TIMEOUT)
            await disconnect()

        try:
            while not self._closing:
                self._event.clear()
                with self._lock:
                    work, self._pending = self._pending, None
                    warm_until, release_at = self._warm_until, self._idle_release_at
                    setup_probe = self._setup_probe
                if session is not None and self._active is None and not getattr(session, 'link_alive', True):
                    await disconnect()
                if work:
                    generation, operation, point, queued_mono, queued_wall = work
                    try:
                        if operation == 'set' and elapsed(queued_mono, queued_wall) >= MAX_PENDING_AGE:
                            await clear_after_failure(generation, 'This request expired. Choose the location again.')
                            continue
                        if session is None:
                            if self._session_factory is None:
                                from transport import DeviceSession
                                factory = DeviceSession.connect
                            else:
                                factory = self._session_factory
                            session = await asyncio.wait_for(factory(self.pairing_path), MAX_PENDING_AGE)
                        # A queued Stop must replace a Set that was waiting on connection.
                        with self._lock:
                            current = generation == self._generation and not self._closing
                        if not current:
                            continue
                        # A warmed tunnel may still need a personalized image after
                        # the launcher has restored internet access.
                        prepare = getattr(session, 'prepare_location', None)
                        if prepare is not None:
                            await asyncio.wait_for(prepare(), MAX_PENDING_AGE)
                            with self._lock:
                                current = generation == self._generation and not self._closing
                            if not current:
                                continue
                        if operation == 'set' and elapsed(queued_mono, queued_wall) >= MAX_PENDING_AGE:
                            await clear_after_failure(generation, 'This request expired. Choose the location again.')
                            continue
                        started_mono, started_wall = time.monotonic(), time.time()
                        if operation == 'set':
                            await asyncio.wait_for(session.set(*point), IO_TIMEOUT)
                            ack_at = (time.monotonic(), time.time())
                            with self._lock:
                                current = generation == self._generation and not self._closing
                            if not current or elapsed(started_mono, started_wall) >= LEASE:
                                await clear_after_failure(generation, 'Request was interrupted. Set the location again.')
                                continue
                            if audio is None:
                                if self._audio_factory is None:
                                    from audio_keepalive import AudioKeepalive
                                    audio = AudioKeepalive()
                                else:
                                    audio = self._audio_factory()
                                audio.start()
                            if not audio.status().get('playing'):
                                raise RuntimeError('Background session unavailable')
                            with self._lock:
                                current = generation == self._generation and not self._closing
                                fresh = elapsed(*ack_at) < LEASE
                                if current and fresh:
                                    self._active, active_generation = point, generation
                                    self._background_audio = True
                                    self._publish(generation, 'active', 'Location simulation is running.',
                                                  ack=True, ack_at=ack_at)
                            if not current or not fresh:
                                await clear_after_failure(generation, 'Request was interrupted. Set the location again.')
                                continue
                            next_send = time.monotonic() + RESEND
                        else:
                            await asyncio.wait_for(session.clear(), IO_TIMEOUT)
                            self._active = None
                            with self._lock:
                                if generation == self._generation:
                                    self._needs_stop = False
                                    self._save()
                            self._publish(generation, 'idle', 'Simulation stopped.')
                            if self._link_factory is not None and time.monotonic() < release_at:
                                stop_audio()
                            else:
                                await disconnect()
                    except Exception as error:
                        await clear_after_failure(generation,
                            getattr(error, 'location_map_hint', None) or
                            'Could not confirm the device request. Stop can reconnect.')
                elif self._active is not None:
                    with self._lock:
                        age = elapsed(self._ack_mono, self._ack_wall) if self._ack_mono is not None else float('inf')
                    try:
                        playing = audio is not None and audio.status().get('playing')
                    except Exception:
                        playing = False
                    if age >= LEASE or not playing:
                        await clear_after_failure(active_generation, 'Simulation was interrupted. Set the location again, or Stop.')
                    elif time.monotonic() >= next_send:
                        started_mono, started_wall = time.monotonic(), time.time()
                        try:
                            await asyncio.wait_for(session.set(*self._active), IO_TIMEOUT)
                            ack_at = (time.monotonic(), time.time())
                            if elapsed(started_mono, started_wall) >= LEASE:
                                raise RuntimeError('Confirmation expired')
                            self._publish(active_generation, 'active', 'Location simulation is running.',
                                          ack=True, ack_at=ack_at)
                            next_send = time.monotonic() + RESEND
                        except Exception:
                            await clear_after_failure(active_generation, 'The simulation connection stopped. Reconnect with Stop or select a new point.')
                elif self._link_factory is not None:
                    now = time.monotonic()
                    if session is not None and now >= release_at:
                        await disconnect()
                    elif session is not None and setup_probe is not None and not setup_probe.done():
                        if not setup_probe.set_running_or_notify_cancel():
                            continue
                        try:
                            await asyncio.wait_for(session.prepare_location(), MAX_PENDING_AGE)
                        except asyncio.CancelledError:
                            setup_probe.set_result(False)
                            raise
                        except Exception:
                            setup_probe.set_result(False)
                        else:
                            setup_probe.set_result(getattr(session, 'link_alive', True))
                    elif session is None and now < min(warm_until, release_at):
                        try:
                            session = await asyncio.wait_for(self._link_factory(self.pairing_path), 2.5)
                        except Exception:
                            pass  # The VPN route may arrive during this bounded launch window.
                        else:
                            with self._lock:
                                self._link_ready = True
                try:
                    monitor_link = (self._link_factory is not None and
                                    (session is not None or time.monotonic() < min(warm_until, release_at)))
                    await asyncio.wait_for(self._event.wait(), 0.5 if self._active or monitor_link else 60)
                except asyncio.TimeoutError:
                    pass
        finally:
            if session is not None and self._needs_stop:
                try:
                    cleared = await asyncio.wait_for(clear_existing(), 3)
                except Exception:
                    cleared = False
                if cleared:
                    # A successful shutdown clear is a real service ACK. Keep
                    # restart recovery conservative only when it was unconfirmed.
                    self._active = None
                    with self._lock:
                        self._needs_stop = False
                        with suppress(OSError):
                            self._save()
            await disconnect()

    def close(self):
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._generation += 1
            self._pending = None
            self._state, self._message = 'error', 'Location engine closed.'
            self._ack_mono = self._ack_wall = None
        self._wake()
        if self._loop is not None and self._runner is not None:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._runner.cancel)
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(7)

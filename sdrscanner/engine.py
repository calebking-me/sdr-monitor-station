"""Receiver engine: reader thread -> DSP thread -> audio, recordings, GUI snapshots.

The GUI never touches the DSP state directly. Live settings (squelch, volume,
pan, mute, solo, record) are read from the shared ChannelConfig objects every
frame. Structural changes (add/remove/retune a channel) are signalled with
`channels_changed()` and applied by the DSP thread at the next chunk.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from .activity import ActivityEvent, IqRecorder, WavRecorder
from .audio import AudioManager, pan_gains
from .config import SOURCE_FILE, AppConfig
from .dsp import Channelizer, ChannelProcessor, spectrum_db
from .planner import Plan, plan_center
from .sources import CHUNK_SAMPLES, EndOfStream, IQSource, SourceError, make_source

SPECTRUM_FFT = 2048
RECONNECT_DELAY = 2.0


@dataclass
class Chunk:
    samples: np.ndarray
    fs: float
    center: float


@dataclass
class ChannelStatus:
    rssi_db: float = -120.0
    noise_db: float = -120.0
    open: bool = False
    carrier: bool = False
    tone: Optional[float] = None
    state: str = "idle"       # idle | active | out_of_band | disabled | stopped


@dataclass
class Snapshot:
    spectrum_rows: list[np.ndarray] = field(default_factory=list)
    spectrum_lo: float = 0.0
    spectrum_hi: float = 0.0
    center: float = 0.0
    sample_rate: float = 0.0
    channels: dict[str, ChannelStatus] = field(default_factory=dict)
    events: list[ActivityEvent] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    running: bool = False
    source_ok: bool = False
    dsp_load: float = 0.0
    dropped: int = 0
    plan: Optional[Plan] = None
    iq_recording: Optional[str] = None


@dataclass
class _OpenEvent:
    start: float
    peak: float
    tone: Optional[float]
    recorder: Optional[WavRecorder]


class Engine:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._q: queue.Queue[Chunk] = queue.Queue(maxsize=24)

        self._spectrum: deque[np.ndarray] = deque(maxlen=64)
        self._status: dict[str, ChannelStatus] = {}
        self._events: deque[ActivityEvent] = deque()
        self._messages: deque[str] = deque(maxlen=200)
        # (channel_id, start, end or None) for the activity timeline
        self.history: deque[tuple[str, float, Optional[float]]] = deque(maxlen=20000)
        self._history_lock = threading.Lock()

        self._center = 0.0
        self._fs = 0.0
        self._source_ok = False
        self._dsp_load = 0.0
        self._dropped = 0
        self._plan: Optional[Plan] = None

        self._dirty = threading.Event()
        self._radio_dirty = threading.Event()
        self._want_center: Optional[float] = None
        self._iq_request: Optional[str] = None      # path to start, "" to stop
        self._iq_rec: Optional[IqRecorder] = None
        self.audio: Optional[AudioManager] = None

    # ----------------------------------------------------------------- control
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._q = queue.Queue(maxsize=24)
        self._dropped = 0
        self.audio = AudioManager(self._message)
        self._dirty.set()
        self._threads = [
            threading.Thread(target=self._reader_loop, name="sdr-reader", daemon=True),
            threading.Thread(target=self._dsp_loop, name="sdr-dsp", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []
        if self.audio:
            self.audio.close_all()
        self._source_ok = False

    def channels_changed(self) -> None:
        self._dirty.set()

    def radio_changed(self) -> None:
        """Gain / PPM / centre-mode changed (source type changes need a restart)."""
        self._radio_dirty.set()
        self._dirty.set()

    def start_iq_recording(self, path: str) -> None:
        self._iq_request = path

    def stop_iq_recording(self) -> None:
        self._iq_request = ""

    def _message(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._messages.append(f"[{stamp}] {text}")

    def poll(self) -> Snapshot:
        """Collect everything new since the last poll (called from the GUI thread)."""
        snap = Snapshot()
        with self._lock:
            snap.spectrum_rows = list(self._spectrum)
            self._spectrum.clear()
            snap.channels = {k: ChannelStatus(**vars(v)) for k, v in self._status.items()}
            snap.events = list(self._events)
            self._events.clear()
        while self._messages:
            snap.messages.append(self._messages.popleft())
        snap.center, snap.sample_rate = self._center, self._fs
        snap.spectrum_lo = self._center - self._fs / 2
        snap.spectrum_hi = self._center + self._fs / 2
        snap.running = self._running
        snap.source_ok = self._source_ok
        snap.dsp_load = self._dsp_load
        snap.dropped = self._dropped
        snap.plan = self._plan
        snap.iq_recording = str(self._iq_rec.path) if self._iq_rec else None
        return snap

    def history_snapshot(self) -> list[tuple[str, float, Optional[float]]]:
        with self._history_lock:
            return list(self.history)

    # ----------------------------------------------------------------- planning
    def _initial_center(self) -> float:
        r = self.config.radio
        plan = plan_center(self.config.channels, r.sample_rate, r.auto_center, r.manual_center)
        return plan.center

    def _make_plan(self, fs: float, current_center: float, tunable: bool) -> Plan:
        r = self.config.radio
        if not tunable:
            return plan_center(self.config.channels, fs, fixed_center=current_center)
        return plan_center(self.config.channels, fs, r.auto_center, r.manual_center)

    # ----------------------------------------------------------------- threads
    def _reader_loop(self) -> None:
        src: Optional[IQSource] = None
        cfg = self.config.radio
        try:
            while self._running:
                if src is None:
                    try:
                        src = make_source(cfg)
                        src.open(self._initial_center())
                        self._source_tunable = src.tunable
                        self._center, self._fs = src.center, src.sample_rate
                        self._source_ok = True
                        self._message(f"{src.describe()} started: {src.center / 1e6:.4f} MHz, "
                                      f"{src.sample_rate / 1e6:.3f} MS/s")
                    except SourceError as e:
                        self._source_ok = False
                        self._message(f"{e} - retrying in {RECONNECT_DELAY:.0f} s")
                        src = None
                        self._sleep(RECONNECT_DELAY)
                        continue
                    except Exception as e:
                        self._source_ok = False
                        self._message(f"Source error: {e}")
                        self._running = False
                        break

                if self._radio_dirty.is_set():
                    self._radio_dirty.clear()
                    try:
                        src.set_gain(cfg.agc, cfg.gain_db)
                        src.set_ppm(cfg.ppm)
                    except Exception as e:
                        self._message(f"Could not apply radio settings: {e}")
                want = self._want_center
                if want is not None and src.tunable and abs(want - src.center) > 1:
                    try:
                        src.set_center(want)
                        self._center = src.center
                        self._message(f"Retuned to {src.center / 1e6:.4f} MHz")
                    except Exception as e:
                        self._message(f"Retune failed: {e}")
                    self._want_center = None

                try:
                    x = src.read(CHUNK_SAMPLES)
                except EndOfStream:
                    self._message("IQ file playback finished")
                    self._running = False
                    break
                except SourceError as e:
                    self._source_ok = False
                    self._message(f"{e} - reconnecting")
                    src.close()
                    src = None
                    self._sleep(RECONNECT_DELAY)
                    continue
                chunk = Chunk(x, src.sample_rate, src.center)
                try:
                    self._q.put_nowait(chunk)
                except queue.Full:
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                    self._dropped += 1
                    self._q.put_nowait(chunk)
        finally:
            if src is not None:
                src.close()
            self._source_ok = False

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.05)

    def _dsp_loop(self) -> None:
        try:
            self._dsp_loop_inner()
        except Exception:
            self._message("DSP thread crashed:\n" + traceback.format_exc())
            self._running = False
        finally:
            self._close_all_events()
            if self._iq_rec:
                self._iq_rec.close()
                self._iq_rec = None

    def _dsp_loop_inner(self) -> None:
        chz: Optional[Channelizer] = None
        procs: dict[str, ChannelProcessor] = {}
        open_events: dict[str, _OpenEvent] = {}
        self._open_events = open_events
        while self._running:
            try:
                chunk = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            t0 = time.perf_counter()

            self._handle_iq_request(chunk)
            if self._iq_rec is not None:
                if (self._iq_rec.center, self._iq_rec.sample_rate) != (chunk.center, chunk.fs):
                    self._message("IQ recording stopped (radio retuned)")
                    self._iq_rec.close()
                    self._iq_rec = None
                else:
                    self._iq_rec.write(chunk.samples)

            if chz is None or chz.fs != chunk.fs or chz.center != chunk.center:
                chz = Channelizer(chunk.fs, chunk.center)
                procs.clear()
                self._dirty.set()
            if self._dirty.is_set():
                self._dirty.clear()
                self._rebuild(chz, procs, open_events, chunk)

            row = spectrum_db(chunk.samples, SPECTRUM_FFT)
            with self._lock:
                self._spectrum.append(row)

            self._process_frames(chz, procs, open_events, chunk.samples)

            dt = time.perf_counter() - t0
            load = dt / (len(chunk.samples) / chunk.fs)
            self._dsp_load = 0.9 * self._dsp_load + 0.1 * load

    def _handle_iq_request(self, chunk: Chunk) -> None:
        req = self._iq_request
        if req is None:
            return
        self._iq_request = None
        if self._iq_rec is not None:
            self._iq_rec.close()
            self._message(f"IQ recording saved: {self._iq_rec.path}")
            self._iq_rec = None
        if req:
            try:
                self._iq_rec = IqRecorder(Path(req), chunk.fs, chunk.center)
                self._message(f"Recording IQ to {self._iq_rec.path}")
            except OSError as e:
                self._message(f"Cannot start IQ recording: {e}")

    def _rebuild(self, chz: Channelizer, procs: dict, open_events: dict, chunk: Chunk) -> None:
        tunable = getattr(self, "_source_tunable", True)
        plan = self._make_plan(chunk.fs, chunk.center, tunable)
        self._plan = plan
        if tunable and abs(plan.center - chunk.center) > 1:
            self._want_center = plan.center
        wanted = {}
        for ch in self.config.channels:
            if ch.enabled:
                wanted[ch.id] = ch
        status = {}
        for ch_id in list(procs):
            ch = wanted.get(ch_id)
            if ch is None or procs[ch_id]._key != ch.dsp_key() or procs[ch_id].cfg is not ch:
                del procs[ch_id]
                self._close_event(ch_id, open_events, time.time())
        for ch in self.config.channels:
            if not ch.enabled:
                status[ch.id] = ChannelStatus(state="disabled")
                continue
            if ch.id not in procs:
                p = ChannelProcessor(chz, ch)
                if not p.valid:
                    status[ch.id] = ChannelStatus(state="out_of_band")
                    continue
                p._key = ch.dsp_key()
                procs[ch.id] = p
            status[ch.id] = ChannelStatus()
        with self._lock:
            self._status = status
        if self.audio:
            keys = {ch.audio_device for ch in wanted.values()}
            self.audio.close_unused(keys)

    def _process_frames(self, chz: Channelizer, procs: dict, open_events: dict, x: np.ndarray) -> None:
        cfgs = {cid: p.cfg for cid, p in procs.items()}
        any_solo = any(c.solo for c in cfgs.values())
        rec_dir = self.config.effective_recordings_dir()
        frame_dur = chz.frame_seconds
        latest: dict[str, object] = {}
        for frame_index, X in chz.frames(x):
            now = time.time()
            mixes: dict[str, np.ndarray] = {}
            for cid, p in procs.items():
                fr = p.process(X, frame_index, chz)
                cfg = p.cfg
                latest[cid] = fr
                ev = open_events.get(cid)
                if fr.open and ev is None:
                    rec = None
                    if cfg.record:
                        try:
                            rec = WavRecorder(rec_dir, cfg.name, cfg.frequency, now)
                        except OSError as e:
                            self._message(f"Recording failed for {cfg.name}: {e}")
                    ev = _OpenEvent(now - frame_dur, fr.rssi_db, fr.tone, rec)
                    open_events[cid] = ev
                    with self._history_lock:
                        self.history.append((cid, ev.start, None))
                elif not fr.open and ev is not None:
                    self._close_event(cid, open_events, now)
                    ev = None
                if ev is not None:
                    ev.peak = max(ev.peak, fr.rssi_db)
                    if fr.tone:
                        ev.tone = fr.tone
                    if ev.recorder is not None:
                        ev.recorder.write(fr.audio)

                audible = (fr.open and not cfg.mute and (cfg.solo or not any_solo)
                           and cfg.volume > 0)
                if not audible:
                    continue
                left, right = pan_gains(cfg.pan)
                mono = fr.audio * cfg.volume
                mix = mixes.get(cfg.audio_device)
                if mix is None:
                    mix = np.zeros((len(mono), 2), dtype=np.float32)
                    mixes[cfg.audio_device] = mix
                mix[:, 0] += mono * left
                mix[:, 1] += mono * right
            if self.audio and self.audio.enabled:
                devices = {c.audio_device for c in cfgs.values()}
                n = chz.hop_c
                for key in devices:
                    mix = mixes.get(key)
                    if mix is None:
                        mix = np.zeros((n, 2), dtype=np.float32)
                    np.clip(mix, -1.0, 1.0, out=mix)
                    self.audio.write(key, mix)

        if latest:
            with self._lock:
                for cid, fr in latest.items():
                    st = self._status.get(cid)
                    if st is None:
                        continue
                    st.rssi_db, st.noise_db = fr.rssi_db, fr.noise_db
                    st.open, st.carrier, st.tone = fr.open, fr.carrier, fr.tone
                    st.state = "active" if fr.open else "idle"

    def _close_event(self, cid: str, open_events: dict, now: float) -> None:
        ev = open_events.pop(cid, None)
        if ev is None:
            return
        cfg = self.config.channel(cid)
        path = ""
        if ev.recorder is not None:
            ev.recorder.close()
            path = str(ev.recorder.path)
        event = ActivityEvent(cid, cfg.name if cfg else cid, cfg.frequency if cfg else 0.0,
                              ev.start, now, ev.peak, ev.tone, path)
        with self._lock:
            self._events.append(event)
        with self._history_lock:
            for i in range(len(self.history) - 1, -1, -1):
                h = self.history[i]
                if h[0] == cid and h[2] is None:
                    self.history[i] = (cid, h[1], now)
                    break

    def _close_all_events(self) -> None:
        events = getattr(self, "_open_events", {})
        now = time.time()
        for cid in list(events):
            self._close_event(cid, events, now)
        with self._lock:
            for st in self._status.values():
                st.open = False
                st.state = "stopped" if st.state in ("idle", "active") else st.state

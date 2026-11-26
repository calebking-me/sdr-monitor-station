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
from .config import AppConfig
from .dsp import ChannelBank, Channelizer, spectrum_db
from .planner import Plan, plan_center
from .sources import CHUNK_SAMPLES, EndOfStream, IQSource, SourceError, make_source

SPECTRUM_FFT_SIZES = (1024, 2048, 4096, 8192)
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
    key: tuple               # channel's dsp_key when the transmission started
    name: str
    frequency: float
    mode: str
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
        self._open_events: dict[str, _OpenEvent] = {}

        self._center = 0.0
        self._fs = 0.0
        self._source_ok = False
        self._source_tunable = True
        self._dsp_load = 0.0
        self._dropped = 0
        self._plan: Optional[Plan] = None
        self.spectrum_fft = 2048          # display FFT size; may be changed while running

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

    @property
    def center(self) -> float:
        """Current tuned centre frequency (0 if not receiving). Does not consume anything."""
        return self._center

    @property
    def sample_rate(self) -> float:
        return self._fs

    def start(self) -> None:
        if self._running:
            return
        self.stop()                       # join threads left over from a run that ended by itself
        self._running = True
        self._q = queue.Queue(maxsize=24)
        self._dropped = 0
        self._dsp_load = 0.0
        self.audio = AudioManager(self._message)
        self._dirty.set()
        self._threads = [
            threading.Thread(target=self._reader_loop, name="sdr-reader", daemon=True),
            threading.Thread(target=self._dsp_loop, name="sdr-dsp", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Stop receiving and release the source and audio devices (idempotent)."""
        self._running = False
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=6)
        self._threads = []
        if self.audio:
            self.audio.close_all()
            self.audio = None
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
        """Collect everything new since the last poll (GUI thread). Consumes events/rows/messages."""
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
        rec = self._iq_rec
        snap.iq_recording = str(rec.path) if rec else None
        return snap

    def history_snapshot(self) -> list[tuple[str, float, Optional[float]]]:
        with self._history_lock:
            return list(self.history)

    def clear_history(self) -> None:
        with self._history_lock:
            self.history = deque((h for h in self.history if h[2] is None), maxlen=20000)

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
                        src = None
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
                    try:
                        self._q.put_nowait(chunk)
                    except queue.Full:
                        pass
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
            rec, self._iq_rec = self._iq_rec, None
            if rec:
                rec.close()

    def _dsp_loop_inner(self) -> None:
        chz: Optional[Channelizer] = None
        bank: Optional[ChannelBank] = None
        open_events = self._open_events
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
                    rec, self._iq_rec = self._iq_rec, None
                    rec.close()
                else:
                    self._iq_rec.write(chunk.samples)

            if chz is None or chz.fs != chunk.fs or chz.center != chunk.center:
                # New tuning: every channel's DSP state starts over.
                now = time.time()
                for cid in list(open_events):
                    self._close_event(cid, now)
                chz = Channelizer(chunk.fs, chunk.center)
                bank = None
                self._dirty.set()
            if self._dirty.is_set() or bank is None:
                self._dirty.clear()
                bank = self._rebuild(chz, bank, chunk)

            nfft = self.spectrum_fft if self.spectrum_fft in SPECTRUM_FFT_SIZES else 2048
            row = spectrum_db(chunk.samples, nfft)
            with self._lock:
                self._spectrum.append(row)

            self._process_frames(chz, bank, chunk.samples)

            dt = time.perf_counter() - t0
            load = dt / (len(chunk.samples) / chunk.fs)
            self._dsp_load = 0.9 * self._dsp_load + 0.1 * load

    def _handle_iq_request(self, chunk: Chunk) -> None:
        req = self._iq_request
        if req is None:
            return
        self._iq_request = None
        if self._iq_rec is not None:
            rec, self._iq_rec = self._iq_rec, None
            rec.close()
            self._message(f"IQ recording saved: {rec.path}")
        if req:
            try:
                self._iq_rec = IqRecorder(Path(req), chunk.fs, chunk.center)
                self._message(f"Recording IQ to {self._iq_rec.path}")
            except OSError as e:
                self._message(f"Cannot start IQ recording: {e}")

    def _rebuild(self, chz: Channelizer, old: Optional[ChannelBank], chunk: Chunk) -> ChannelBank:
        tunable = self._source_tunable
        plan = self._make_plan(chunk.fs, chunk.center, tunable)
        self._plan = plan
        if tunable and abs(plan.center - chunk.center) > 1:
            self._want_center = plan.center
        channels = list(self.config.channels)
        enabled = [c for c in channels if c.enabled]
        bank = ChannelBank(chz, enabled, old)
        live = {r.cfg.id: r.key for r in bank.rows}
        now = time.time()
        for cid, ev in list(self._open_events.items()):
            if live.get(cid) != ev.key:      # removed, disabled, or retuned/re-moded
                self._close_event(cid, now)
        rejected = {c.id for c in bank.rejected}
        status = {}
        for ch in channels:
            if not ch.enabled:
                status[ch.id] = ChannelStatus(state="disabled")
            elif ch.id in rejected:
                status[ch.id] = ChannelStatus(state="out_of_band")
            else:
                status[ch.id] = ChannelStatus()
        with self._lock:
            self._status = status
        if self.audio:
            self.audio.close_unused({ch.audio_device for ch in enabled})
        return bank

    def _process_frames(self, chz: Channelizer, bank: ChannelBank, x: np.ndarray) -> None:
        rows = bank.rows
        cfgs = [r.cfg for r in rows]
        C = len(cfgs)
        rec_dir = self.config.effective_recordings_dir()
        frame_dur = chz.frame_seconds
        open_events = self._open_events
        last = None
        devices = sorted({c.audio_device for c in cfgs})
        dev_rows = {k: np.array([i for i, c in enumerate(cfgs) if c.audio_device == k], dtype=np.int64)
                    for k in devices}
        for frame_index, X in chz.frames(x):
            if C == 0:
                continue
            f = bank.process(X, frame_index)
            last = f
            now = time.time()
            for i, cfg in enumerate(cfgs):
                cid = cfg.id
                is_open = f.open[i]
                ev = open_events.get(cid)
                if is_open and ev is None:
                    rec = None
                    if cfg.record:
                        try:
                            rec = WavRecorder(rec_dir, cfg.name, cfg.frequency, now)
                        except OSError as e:
                            self._message(f"Recording failed for {cfg.name}: {e}")
                    ev = _OpenEvent(rows[i].key, cfg.name, cfg.frequency, cfg.mode, now - frame_dur,
                                    float(f.rssi[i]), f.tones[i], rec)
                    open_events[cid] = ev
                    with self._history_lock:
                        self.history.append((cid, ev.start, None))
                elif not is_open and ev is not None:
                    self._close_event(cid, now)
                    ev = None
                if ev is not None:
                    ev.peak = max(ev.peak, float(f.rssi[i]))
                    if f.tones[i]:
                        ev.tone = f.tones[i]
                    if ev.recorder is not None:
                        try:
                            ev.recorder.write(f.audio[i])
                        except OSError as e:
                            self._message(f"Recording stopped for {cfg.name}: {e}")
                            ev.recorder.close()
                            ev.recorder = None

            if self.audio and self.audio.enabled:
                any_solo = any(c.solo for c in cfgs)
                gl = np.zeros(C, dtype=np.float32)
                gr = np.zeros(C, dtype=np.float32)
                for i, c in enumerate(cfgs):
                    if f.open[i] and not c.mute and (c.solo or not any_solo) and c.volume > 0:
                        left, right = pan_gains(c.pan)
                        gl[i], gr[i] = c.volume * left, c.volume * right
                for key in devices:
                    idx = dev_rows[key]
                    a = f.audio[idx]
                    mix = np.empty((a.shape[1], 2), dtype=np.float32)
                    mix[:, 0] = gl[idx] @ a
                    mix[:, 1] = gr[idx] @ a
                    np.clip(mix, -1.0, 1.0, out=mix)
                    self.audio.write(key, mix)

        if last is not None:
            with self._lock:
                for i, cfg in enumerate(cfgs):
                    st = self._status.get(cfg.id)
                    if st is None:
                        continue
                    st.rssi_db, st.noise_db = float(last.rssi[i]), float(last.noise[i])
                    st.open, st.carrier, st.tone = last.open[i], last.carrier[i], last.tones[i]
                    st.state = "active" if last.open[i] else "idle"

    def _close_event(self, cid: str, now: float) -> None:
        ev = self._open_events.pop(cid, None)
        if ev is None:
            return
        path = ""
        if ev.recorder is not None:
            ev.recorder.close()
            path = str(ev.recorder.path)
        event = ActivityEvent(cid, ev.name, ev.frequency, ev.start, now, ev.peak, ev.tone, path, ev.mode)
        with self._lock:
            self._events.append(event)
        with self._history_lock:
            for i in range(len(self.history) - 1, -1, -1):
                h = self.history[i]
                if h[0] == cid and h[2] is None:
                    self.history[i] = (cid, h[1], now)
                    break

    def _close_all_events(self) -> None:
        now = time.time()
        for cid in list(self._open_events):
            self._close_event(cid, now)
        with self._lock:
            for st in self._status.values():
                st.open = False
                st.state = "stopped" if st.state in ("idle", "active") else st.state

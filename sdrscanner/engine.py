"""Receiver engine: reader thread -> DSP thread -> audio, recordings, GUI snapshots.

The GUI never touches the DSP state directly. Live settings (squelch, volume,
pan, mute, solo, record) are read from the shared ChannelConfig objects every
frame. Structural changes (add/remove/retune a channel) are signalled with
`channels_changed()` and applied by the DSP thread at the next chunk.

Scanning: when the monitored channels do not fit in one tuning and scanning is
on, the DSP thread receives one scan set at a time and asks the reader thread
to retune between sets (planner.plan_scan, scan.ScanController). Every retune
request has a sequence number; the reader tags each chunk with the request it
was read after, and the DSP thread drops chunks from before the latest one.
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
from typing import Callable, Optional

import numpy as np

from .activity import ActivityEvent, IqRecorder, WavRecorder
from .audio import AudioManager, pan_gains
from .config import MODE_AM, AppConfig
from .dsp import ChannelBank, Channelizer, spectrum_db
from .planner import Plan, ScanPlan, ScanSet, classify, plan_center, plan_scan
from .scan import TONE_WAIT_CTCSS, TONE_WAIT_DCS, ScanController
from .sources import CHUNK_SAMPLES, EndOfStream, IQSource, SourceError, make_source

SPECTRUM_FFT_SIZES = (1024, 2048, 4096, 8192)
RECONNECT_DELAY = 2.0
CENTER_TOLERANCE_HZ = 500.0     # a chunk tuned this close to a scan set's centre belongs to that set


@dataclass
class Chunk:
    samples: np.ndarray
    fs: float
    center: float
    seq: int = 0              # the retune request this chunk was read after


@dataclass
class ChannelStatus:
    rssi_db: float = -120.0
    noise_db: float = -120.0
    open: bool = False
    carrier: bool = False
    tone: Optional[float] = None
    dcs: Optional[str] = None
    state: str = "idle"       # idle | active | scanning | out_of_band | disabled | not_in_list | stopped
    scan_set: int = 0         # number of the channel's scan set while scanning (0 = not scanning)


@dataclass
class ScanStatus:
    sets: list[ScanSet]       # in frequency order (do not modify)
    index: int                # set being received
    state: str                # scanning | active | dwell | hold, or "single" when one tuning covers all
    cycle_seconds: float      # time to check every set once while all are quiet
    skipped: list[int] = field(default_factory=list)   # sets the receiver could not tune to


@dataclass
class Snapshot:
    spectrum_rows: list[np.ndarray] = field(default_factory=list)
    spectrum_centers: list[float] = field(default_factory=list)    # tuning of each row
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
    scan: Optional[ScanStatus] = None
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
    dcs: Optional[str]
    recorder: Optional[WavRecorder]
    last_open: float = 0.0   # time of the last frame with the squelch open
    holding: bool = False    # squelch closed, waiting up to hold_time for the signal to return
    gap: int = 0             # samples of silence to write if it does return


class Engine:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._q: queue.Queue[Chunk] = queue.Queue(maxsize=24)

        self._spectrum: deque[tuple[float, np.ndarray]] = deque(maxlen=64)
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
        # Retune requests from the DSP thread to the reader thread: (sequence number, centre).
        self._tune_seq = 0
        self._tune_req: Optional[tuple[int, float]] = None
        self._tune_failed: Optional[tuple[int, float]] = None
        # Scanning (DSP thread). The GUI sets scan_hold and queues scan_step / scan_goto commands.
        self._scan_plan: Optional[ScanPlan] = None
        self._scan: Optional[ScanController] = None
        self._scan_on = False
        self._scan_skip: set[float] = set()          # centres the receiver failed to tune to
        self._scan_cmds: deque[tuple[str, int]] = deque(maxlen=8)
        self._scan_rebuild = False                    # rebuild the bank for the set just moved to
        self.scan_hold = False
        self._iq_request: Optional[str] = None      # path to start, "" to stop
        self._iq_rec: Optional[IqRecorder] = None
        self.audio: Optional[AudioManager] = None
        # Makes the IQ source from the radio settings (the self-test plugs in a simulator).
        self.source_factory: Optional[Callable[[object], IQSource]] = None
        # channel id -> unix time until which the channel is muted (temporary mutes)
        self.mute_until: dict[str, float] = {}

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
        self._tune_seq, self._tune_req, self._tune_failed = 0, None, None
        self._scan_plan, self._scan, self._scan_on = None, None, False
        self._scan_skip = set()
        self._scan_cmds.clear()
        self._scan_rebuild = False
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
        self._scan_on = False

    def channels_changed(self) -> None:
        self._dirty.set()

    def radio_changed(self) -> None:
        """Gain / PPM / centre-mode changed (source type changes need a restart)."""
        self._radio_dirty.set()
        self._dirty.set()

    def scan_step(self, step: int = 1) -> None:
        """Move the scanner to the next (+1) or previous (-1) set now."""
        self._scan_cmds.append(("step", int(step)))

    def scan_goto(self, index: int) -> None:
        """Move the scanner to set `index` (0-based) now."""
        self._scan_cmds.append(("goto", int(index)))

    @property
    def scanning(self) -> bool:
        """True while the receiver steps between several scan sets."""
        return self._scan_on

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
            rows = list(self._spectrum)
            self._spectrum.clear()
            snap.spectrum_rows = [r for _, r in rows]
            snap.spectrum_centers = [c for c, _ in rows]
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
        snap.scan = self._scan_status()
        rec = self._iq_rec
        snap.iq_recording = str(rec.path) if rec else None
        return snap

    def history_snapshot(self) -> list[tuple[str, float, Optional[float]]]:
        with self._history_lock:
            return list(self.history)

    def clear_history(self) -> None:
        with self._history_lock:
            self.history = deque((h for h in self.history if h[2] is None), maxlen=20000)

    def _scan_status(self) -> Optional[ScanStatus]:
        sp, ctl = self._scan_plan, self._scan
        if sp is None or not sp.sets:
            return None
        listen = self.config.scan.listen_time
        if not self._scan_on or ctl is None:
            return ScanStatus(sp.sets, 0, "single", 0.0)
        skipped = [i for i, s in enumerate(sp.sets) if s.center in self._scan_skip]
        return ScanStatus(sp.sets, ctl.index, ctl.describe(), sp.cycle_seconds(listen), skipped)

    # ----------------------------------------------------------------- planning
    def _initial_tuning(self) -> tuple[int, float]:
        """(retune sequence number, centre) to open the source at."""
        req = self._tune_req
        if req is not None:            # reopening after an error: stay on the requested set
            return req
        r = self.config.radio
        monitored = self.config.monitored_channels()
        if self.config.scan.enabled:
            sp = plan_scan(monitored, r.sample_rate)
            if len(sp.sets) > 1:
                return 0, sp.sets[0].center
        return 0, plan_center(monitored, r.sample_rate, r.auto_center or self.config.scan.enabled,
                              r.manual_center).center

    def _initial_center(self) -> float:
        return self._initial_tuning()[1]

    def _make_plan(self, fs: float, current_center: float, tunable: bool) -> Plan:
        r = self.config.radio
        monitored = self.config.monitored_channels()       # enabled and in the active list
        if not tunable:
            return plan_center(monitored, fs, fixed_center=current_center)
        # Scanning always places its tunings automatically, also when one tuning covers everything.
        return plan_center(monitored, fs, r.auto_center or self.config.scan.enabled, r.manual_center)

    def _request_tune(self, center: float) -> None:
        self._tune_seq += 1
        self._tune_req = (self._tune_seq, float(center))

    def _settle_samples(self, src: IQSource) -> int:
        """Samples to throw away after a retune (still from the old frequency, or settling)."""
        ms = self.config.scan.settle_ms
        seconds = ms / 1000.0 if ms > 0 else getattr(src, "retune_settle", 0.05)
        n = int(seconds * src.sample_rate)
        return -(-n // 256) * 256          # whole USB packets (librtlsdr reads multiples of 512 bytes)

    # ----------------------------------------------------------------- threads
    def _reader_loop(self) -> None:
        src: Optional[IQSource] = None
        cfg = self.config.radio
        applied = 0          # sequence number of the last retune request carried out
        discard = 0          # samples still to throw away after a retune
        try:
            while self._running:
                if src is None:
                    try:
                        src = (self.source_factory or make_source)(cfg)
                        applied, center = self._initial_tuning()
                        discard = 0
                        src.open(center)
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
                req = self._tune_req
                if req is not None and req[0] != applied:
                    seq, want = req
                    if src.tunable and abs(want - src.center) > 1:
                        try:
                            src.set_center(want)
                            self._center = src.center
                            discard = self._settle_samples(src)
                            if not self._scan_on:
                                self._message(f"Retuned to {src.center / 1e6:.4f} MHz")
                        except Exception as e:
                            self._tune_failed = (seq, want)
                            self._message(f"Retune to {want / 1e6:.4f} MHz failed: {e}")
                    applied = seq

                try:
                    if discard > 0:
                        # Samples in the USB / network buffers were taken before the retune.
                        discard -= max(1, len(src.read(min(discard, CHUNK_SAMPLES))))
                        continue
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
                chunk = Chunk(x, src.sample_rate, src.center, applied)
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
            if self._scan_on and chunk.seq != self._tune_seq:
                continue                       # read before the scanner's latest retune
            t0 = time.perf_counter()

            self._handle_iq_request(chunk)
            if self._iq_rec is not None:
                if (self._iq_rec.center, self._iq_rec.sample_rate) != (chunk.center, chunk.fs):
                    self._message("IQ recording stopped (radio retuned)")
                    rec, self._iq_rec = self._iq_rec, None
                    rec.close()
                else:
                    self._iq_rec.write(chunk.samples)

            replan = False
            rebuild = bank is None
            if chz is None or chz.fs != chunk.fs or chz.center != chunk.center:
                # New tuning: every channel's DSP state starts over.
                now = time.time()
                for cid, ev in list(open_events.items()):
                    self._close_event(cid, ev.last_open if ev.holding else now)
                chz = Channelizer(chunk.fs, chunk.center)
                bank = None
                rebuild = True
                replan = not self._scan_on       # a scanner retune keeps its scan sets
            if self._dirty.is_set():
                self._dirty.clear()
                replan = rebuild = True
            if self._scan_rebuild:               # moved to another set (even one at the same tuning)
                self._scan_rebuild = False
                rebuild = True
            if rebuild:
                bank = self._rebuild(chz, bank, chunk, replan)
                if self._scan_on and chunk.seq != self._tune_seq:
                    continue                   # the scanner is moving to another set

            nfft = self.spectrum_fft if self.spectrum_fft in SPECTRUM_FFT_SIZES else 2048
            row = spectrum_db(chunk.samples, nfft)
            with self._lock:
                self._spectrum.append((chunk.center, row))

            any_open, tone_wait = self._process_frames(chz, bank, chunk.samples)
            if self._scan_on:
                self._scan_after_chunk(chz, chunk, any_open, tone_wait)

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

    def _rebuild(self, chz: Channelizer, old: Optional[ChannelBank], chunk: Chunk,
                 replan: bool = True) -> ChannelBank:
        tunable = self._source_tunable
        enabled = self.config.monitored_channels()
        if not replan and self._scan_on and self._scan_plan is not None and self._scan is not None:
            return self._rebuild_scan(chz, old, chunk, self._scan_plan, enabled, expect=self._scan.index)
        sp = plan_scan(enabled, chunk.fs) if tunable and self.config.scan.enabled else None
        if sp is not None and len(sp.sets) > 1:
            return self._rebuild_scan(chz, old, chunk, sp, enabled)
        self._scan_plan, self._scan, self._scan_on = sp, None, False
        plan = self._make_plan(chunk.fs, chunk.center, tunable)
        self._plan = plan
        if tunable and abs(plan.center - chunk.center) > 1:
            self._request_tune(plan.center)
        bank = ChannelBank(chz, enabled, old)
        self._apply_bank(bank, enabled)
        return bank

    def _rebuild_scan(self, chz: Channelizer, old: Optional[ChannelBank], chunk: Chunk,
                      sp: ScanPlan, enabled: list, expect: Optional[int] = None) -> ChannelBank:
        """Scanning: receive the scan set this chunk was tuned to, or move to one.

        `expect` is the set the scanner just moved to; otherwise the set is found by its centre.
        """
        idx = None
        if expect is not None and expect < len(sp.sets) \
                and abs(sp.sets[expect].center - chunk.center) <= CENTER_TOLERANCE_HZ:
            idx = expect
        if idx is None:
            idx = sp.index_of_center(chunk.center, CENTER_TOLERANCE_HZ)
        skip = {i for i, s in enumerate(sp.sets) if s.center in self._scan_skip}
        here = idx is not None and idx not in skip
        chans = []
        if here:
            ids = set(sp.sets[idx].channel_ids)
            chans = [c for c in enabled if c.id in ids]
        bank = ChannelBank(chz, chans, old)
        self._apply_bank(bank, enabled, sp)        # statuses first, so a GUI poll never sees sets without them
        ctl = self._scan
        if ctl is None:
            ctl = ScanController(self.config.scan, len(sp.sets), idx if here else 0)
        ctl.n, ctl.skip = len(sp.sets), skip
        self._scan_plan, self._scan, self._scan_on = sp, ctl, True
        if not here:
            # Scanning just started or the sets changed: go to the set nearest the current tuning.
            order = sorted(range(len(sp.sets)), key=lambda i: abs(sp.sets[i].center - chunk.center))
            self._scan_hop(chz, next((i for i in order if i not in skip), order[0]), force=True)
            return bank
        if idx != ctl.index:
            ctl.enter(idx)
        self._plan = classify(Plan(chunk.center, chunk.fs), chans)
        return bank

    def _apply_bank(self, bank: ChannelBank, enabled: list, sp: Optional[ScanPlan] = None) -> None:
        """Close transmissions of channels the new bank no longer receives; publish statuses."""
        channels = list(self.config.channels)
        monitored_ids = {c.id for c in enabled}
        set_of = {cid: s.number for s in sp.sets for cid in s.channel_ids} if sp and len(sp.sets) > 1 else {}
        live = {r.cfg.id: r.key for r in bank.rows}
        now = time.time()
        for cid, ev in list(self._open_events.items()):
            if live.get(cid) != ev.key:      # removed, disabled, or retuned/re-moded
                self._close_event(cid, ev.last_open if ev.holding else now)
        rejected = {c.id for c in bank.rejected}
        status = {}
        for ch in channels:
            if not ch.enabled:
                status[ch.id] = ChannelStatus(state="disabled")
            elif ch.id not in monitored_ids:
                status[ch.id] = ChannelStatus(state="not_in_list")
            elif ch.id in rejected:
                status[ch.id] = ChannelStatus(state="out_of_band")
            elif ch.id in live:
                status[ch.id] = ChannelStatus(scan_set=set_of.get(ch.id, 0))
            else:                                   # in another scan set
                status[ch.id] = ChannelStatus(state="scanning", scan_set=set_of.get(ch.id, 0))
        with self._lock:
            self._status = status
        if self.audio:
            # Devices of every monitored channel stay open, so scanning does not reopen them.
            self.audio.close_unused({ch.audio_device for ch in enabled})

    # ----------------------------------------------------------------- scanning
    def _scan_after_chunk(self, chz: Channelizer, chunk: Chunk, any_open: bool, tone_wait: float) -> None:
        ctl, sp = self._scan, self._scan_plan
        if ctl is None or sp is None:
            return
        ctl.hold = self.scan_hold
        failed = self._tune_failed
        if failed is not None and failed[0] == chunk.seq:
            # The retune to this set failed (e.g. outside the tuner's range): skip the set from now on.
            self._tune_failed = None
            self._scan_skip.add(failed[1])
            ctl.skip = {i for i, s in enumerate(sp.sets) if s.center in self._scan_skip}
            bad = sp.index_of_center(failed[1])
            where = f"Scan set {sp.sets[bad].number} ({sp.sets[bad].label()})" if bad is not None else \
                f"The scan set at {failed[1] / 1e6:.4f} MHz"
            self._message(f"{where} skipped: the receiver cannot tune there")
            self._scan_hop(chz, ctl.next_index(), force=True)
            return
        target = None
        while self._scan_cmds:
            kind, value = self._scan_cmds.popleft()
            target = ctl.next_index(value) if kind == "step" else value % ctl.n
        if target is not None:
            self._scan_hop(chz, target, force=True)
        elif ctl.update(len(chunk.samples) / chunk.fs, any_open, tone_wait):
            self._scan_hop(chz, ctl.next_index())

    def _scan_hop(self, chz: Channelizer, index: int, force: bool = False) -> None:
        """Leave the current set: end its transmissions and ask the reader to retune."""
        ctl, sp = self._scan, self._scan_plan
        index %= len(sp.sets)
        if index == ctl.index and not force:
            ctl.enter(index)
            return
        now = time.time()
        for cid, ev in list(self._open_events.items()):
            self._close_event(cid, ev.last_open + chz.frame_seconds if ev.holding else now)
        ctl.enter(index)
        self._request_tune(sp.sets[index].center)
        self._scan_rebuild = True
        with self._lock:
            for st in self._status.values():
                if st.state in ("idle", "active"):
                    st.state, st.open, st.carrier, st.tone, st.dcs = "scanning", False, False, None, None

    def _process_frames(self, chz: Channelizer, bank: ChannelBank, x: np.ndarray) -> tuple[bool, float]:
        rows = bank.rows
        cfgs = [r.cfg for r in rows]
        C = len(cfgs)
        rec_dir = self.config.effective_recordings_dir()
        frame_dur = chz.frame_seconds
        hop = chz.hop_c
        open_events = self._open_events
        last = None
        devices = sorted({c.audio_device for c in cfgs})
        dev_rows = {k: np.array([i for i, c in enumerate(cfgs) if c.audio_device == k], dtype=np.int64)
                    for k in devices}
        now0 = time.time()
        muted = {cid for cid, until in list(self.mute_until.items()) if until > now0}
        duck = float(10 ** (self.config.priority_duck_db / 20.0))
        any_open = False
        for frame_index, X in chz.frames(x):
            if C == 0:
                continue
            f = bank.process(X, frame_index)
            last = f
            any_open = any_open or any(f.open)
            now = time.time()
            for i, cfg in enumerate(cfgs):
                cid = cfg.id
                is_open = f.open[i]
                ev = open_events.get(cid)
                if is_open:
                    if ev is None:
                        rec = None
                        if cfg.record:
                            try:
                                rec = WavRecorder(rec_dir, cfg.name, cfg.frequency, now)
                            except OSError as e:
                                self._message(f"Recording failed for {cfg.name}: {e}")
                        ev = _OpenEvent(rows[i].key, cfg.name, cfg.frequency, cfg.mode, now - frame_dur,
                                        float(f.rssi[i]), f.tones[i], f.dcs[i], rec)
                        open_events[cid] = ev
                        with self._history_lock:
                            self.history.append((cid, ev.start, None))
                    elif ev.holding:
                        # The signal came back within the hold time: same transmission.
                        ev.holding = False
                        if ev.gap:
                            self._record(ev, np.zeros(ev.gap, dtype=np.float32), cfg)
                        ev.gap = 0
                    ev.last_open = now
                    ev.peak = max(ev.peak, float(f.rssi[i]))
                    if f.tones[i]:
                        ev.tone = f.tones[i]
                    if f.dcs[i]:
                        ev.dcs = f.dcs[i]
                    self._record(ev, f.audio[i], cfg)
                elif ev is not None:
                    if not ev.holding:
                        ev.holding = True
                        ev.gap = 0
                        self._record(ev, f.audio[i], cfg)       # the squelch fade-out
                    else:
                        ev.gap += hop
                    if now - ev.last_open >= cfg.hold_time:
                        self._close_event(cid, ev.last_open + frame_dur)

            if self.audio and self.audio.enabled:
                any_solo = any(c.solo for c in cfgs)
                audible = [f.open[i] and not c.mute and c.id not in muted and (c.solo or not any_solo)
                           and c.volume > 0 for i, c in enumerate(cfgs)]
                priority_on = any(audible[i] and cfgs[i].priority for i in range(C))
                gl = np.zeros(C, dtype=np.float32)
                gr = np.zeros(C, dtype=np.float32)
                for i, c in enumerate(cfgs):
                    if audible[i]:
                        g = c.volume * (duck if priority_on and not c.priority else 1.0)
                        left, right = pan_gains(c.pan)
                        gl[i], gr[i] = g * left, g * right
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
                    st.dcs = last.dcs[i]
                    st.state = "active" if last.open[i] else "idle"
        # A carrier on a tone-squelched channel whose tone is not decoded yet: how long to wait for it.
        tone_wait = 0.0
        if last is not None:
            for i, cfg in enumerate(cfgs):
                if last.carrier[i] and not last.open[i] and cfg.mode != MODE_AM:
                    if cfg.dcs_code:
                        tone_wait = max(tone_wait, TONE_WAIT_DCS)
                    elif cfg.ctcss_hz:
                        tone_wait = max(tone_wait, TONE_WAIT_CTCSS)
        return any_open, tone_wait

    def _record(self, ev: _OpenEvent, audio: np.ndarray, cfg) -> None:
        if ev.recorder is None:
            return
        try:
            ev.recorder.write(audio)
        except OSError as e:
            self._message(f"Recording stopped for {cfg.name}: {e}")
            ev.recorder.close()
            ev.recorder = None

    def _close_event(self, cid: str, end: float) -> None:
        ev = self._open_events.pop(cid, None)
        if ev is None:
            return
        end = max(ev.start, min(end, time.time()))
        path = ""
        if ev.recorder is not None:
            ev.recorder.close()
            path = str(ev.recorder.path)
        event = ActivityEvent(cid, ev.name, ev.frequency, ev.start, end, ev.peak, ev.tone, path, ev.mode, ev.dcs)
        with self._lock:
            self._events.append(event)
        with self._history_lock:
            for i in range(len(self.history) - 1, -1, -1):
                h = self.history[i]
                if h[0] == cid and h[2] is None:
                    self.history[i] = (cid, h[1], end)
                    break

    def _close_all_events(self) -> None:
        now = time.time()
        for cid, ev in list(self._open_events.items()):
            self._close_event(cid, ev.last_open if ev.holding else now)
        with self._lock:
            for st in self._status.values():
                st.open = False
                st.state = "stopped" if st.state in ("idle", "active", "scanning") else st.state

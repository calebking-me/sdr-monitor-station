"""Signal processing: channelizer, FM/AM demodulation, squelch, CTCSS detection.

Channelization uses a fast-convolution (overlap-save) filter bank. One large
FFT of the wideband IQ is shared by every channel; each channel then selects
the FFT bins around its frequency, applies its low-pass filter in the
frequency domain and runs a small inverse FFT. Selecting only `nc` of `n` bins
decimates the channel straight down to the audio rate (48 kHz).

All channels are processed together as rows of matrices (`ChannelBank`): one
batched IFFT, one batched discriminator, one batched voice filter, and so on.
The per-frame cost is therefore almost independent of the number of channels,
which matters for presets such as the 40 CB channels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from fractions import Fraction
from typing import Any, Optional, Sequence

import numpy as np
import scipy.fft as sfft
from scipy import signal

from . import dcs
from .config import CTCSS_TONES, MODE_AM, MODE_FM  # noqa: F401  (CTCSS_TONES re-exported)

AUDIO_RATE = 48_000
HEADROOM = 0.5         # full deviation / 100% AM -> -6 dBFS audio, leaves room for mixing


def _largest_prime_factor(n: int) -> int:
    p, largest = 2, 1
    while p * p <= n:
        while n % p == 0:
            largest, n = p, n // p
        p += 1
    return max(largest, n) if n > 1 else largest


def choose_fft_sizes(fs: float, out_rate: int = AUDIO_RATE) -> tuple[int, int]:
    """Pick (n, nc): input FFT size and per-channel IFFT size.

    The channel output rate is fs * nc / n, which should equal `out_rate`.
    nc must be a multiple of 4*q (q = denominator of fs/out_rate) so that the
    75% hop is an integer number of samples at both rates.
    """
    if not (math.isfinite(fs) and 60_000 <= fs <= 100e6):
        raise ValueError(f"unsupported sample rate {fs!r}")
    ratio = Fraction(int(round(fs)), out_rate).limit_denominator(64)
    p, q = ratio.numerator, ratio.denominator
    step = 4 * q
    best = None
    nc = step * max(1, math.ceil(768 / step))
    while nc <= 2048 or best is None:
        n = nc * p // q
        score = (_largest_prime_factor(n) > 7, _largest_prime_factor(n), abs(nc - 1024))
        if best is None or score < best[0]:
            best = (score, n, nc)
        nc += step
        if nc > 8192:
            break
    return best[1], best[2]


class Channelizer:
    """Overlap-save fast-convolution front end (75% hop)."""

    def __init__(self, fs: float, center: float):
        self.fs = float(fs)
        self.center = float(center)
        self.n, self.nc = choose_fft_sizes(fs)
        self.hop_c = 3 * self.nc // 4
        self.overlap_c = self.nc - self.hop_c
        self.hop = self.hop_c * self.n // self.nc
        self.rate = self.fs * self.nc / self.n
        self.frame_seconds = self.hop / self.fs
        self._buf = np.zeros(self.n - self.hop, dtype=np.complex64)
        self._frame_index = 0

    def max_offset(self) -> float:
        """Largest |offset| at which a channel's bins do not wrap around."""
        return self.fs / 2 - self.rate / 2

    def accepts(self, freq_hz: float) -> bool:
        return abs(freq_hz - self.center) <= self.max_offset()

    def frames(self, x: np.ndarray):
        """Yield (frame_index, X) for every complete frame available."""
        buf = np.concatenate((self._buf, x.astype(np.complex64, copy=False)))
        pos = 0
        while pos + self.n <= len(buf):
            X = sfft.fft(buf[pos:pos + self.n])
            yield self._frame_index, X
            self._frame_index += 1
            pos += self.hop
        self._buf = buf[pos:].copy()


def channel_cutoff(bandwidth: float, mode: str = MODE_FM) -> float:
    """One-sided channel filter cutoff for a channel of the given width."""
    if mode == MODE_AM:
        return max(1_500.0, bandwidth / 2)
    if bandwidth <= 12_500:
        return 0.44 * bandwidth                       # 12.5 kHz -> 5.5 kHz
    return 5_500.0 + (bandwidth - 12_500.0) * 0.24    # 25 kHz -> 8.5 kHz


def nominal_deviation(bandwidth: float) -> float:
    return 2_500.0 if bandwidth < 18_000 else 5_000.0


class Squelch:
    """Carrier-level squelch with hysteresis, optional tone gate and hang time."""

    def __init__(self):
        self.carrier = False
        self.open = False
        self._hang_left = 0.0

    def update(self, rssi_db: float, level_db: float, hysteresis_db: float,
               hang_time: float, tone_ok: bool, dt: float) -> bool:
        threshold = level_db - hysteresis_db if self.carrier else level_db
        self.carrier = rssi_db >= threshold
        if self.carrier and tone_ok:
            self.open = True
            self._hang_left = hang_time
        elif self.open:
            self._hang_left -= dt
            if self._hang_left <= 0:
                self.open = False
        return self.open


@dataclass
class _Row:
    """Per-channel scalar state; survives bank rebuilds while the channel's key is unchanged."""
    cfg: Any
    key: tuple
    squelch: Squelch = field(default_factory=Squelch)
    noise_db: float = -100.0
    noise_init: bool = False
    gain: float = 0.0
    tone: Optional[float] = None
    candidate: Optional[float] = None
    streak: int = 0
    filled: int = 0                      # samples in the sub-audio ring buffer
    dcs_class: Optional[int] = None      # confirmed DCS class (see dcs.py)
    dcs_candidate: Optional[int] = None
    dcs_streak: int = 0
    dcs_miss: int = 0


@dataclass
class BankFrame:
    audio: np.ndarray        # (C, hop) float32, squelch-gated, before volume/pan
    rssi: np.ndarray         # (C,) dBFS
    noise: np.ndarray        # (C,) dBFS noise-floor estimate
    open: list[bool]
    carrier: list[bool]
    tones: list[Optional[float]]
    dcs: list[Optional[str]] = field(default_factory=list)    # detected DCS code, e.g. "023N"


class ChannelBank:
    """Receives every channel of one Channelizer as rows of shared matrices."""

    CT_WINDOW_S = 0.5
    CT_OPEN_RATIO = 0.35
    CT_CLOSE_RATIO = 0.2
    CT_CONFIRM = 3           # consecutive agreeing estimates (~0.25 s) before reporting a tone
    CT_EVERY = 4             # evaluate tones every 4th frame (~16 Hz)
    DCS_WINDOW_S = 0.75      # ~100 bits = 4 code words at 134.4 bit/s
    DCS_PHASES = 6           # bit-clock phases tried per evaluation
    DCS_EVERY = 8            # evaluate DCS every 8th frame (~8 Hz)
    DCS_MIN_HITS = 23        # a clean stream matches on every 23-bit window
    DCS_DOMINANCE = 0.8

    def __init__(self, chz: Channelizer, cfgs: Sequence[Any], previous: Optional["ChannelBank"] = None):
        self.chz = chz
        n, nc, hop = chz.n, chz.nc, chz.hop_c
        rate = chz.rate
        self.rejected: list[Any] = [c for c in cfgs if not chz.accepts(c.frequency)]
        cfgs = [c for c in cfgs if chz.accepts(c.frequency)]
        C = len(cfgs)
        self.rows: list[_Row] = []
        prev_map = {}
        if previous is not None and previous.chz is chz:
            prev_map = {r.key: i for i, r in enumerate(previous.rows)}

        self.modes = [c.mode for c in cfgs]
        self.am_idx = np.array([i for i, c in enumerate(cfgs) if c.mode == MODE_AM], dtype=np.int64)
        self.fm_mask = np.array([c.mode != MODE_AM for c in cfgs], dtype=bool)

        off = np.array([c.frequency - chz.center for c in cfgs], dtype=np.float64)
        k0 = np.round(off * n / chz.fs).astype(np.int64)
        residual = off - k0 * chz.fs / n
        bins = np.fft.fftfreq(nc, 1.0 / nc).astype(np.int64)
        self.idx = (k0[:, None] + bins[None, :]) % n
        self.k0_hop = (k0 * chz.hop) % n
        self.nco_step = -residual / rate
        ramp = np.arange(hop, dtype=np.float64)
        self.nco_rot = np.exp(2j * np.pi * self.nco_step[:, None] * ramp[None, :]).astype(np.complex64)
        self.ramp01 = np.linspace(0.0, 1.0, hop, dtype=np.float32)

        ntaps = min(nc - hop - 1, 255) | 1
        cache: dict[float, np.ndarray] = {}
        H = np.empty((C, nc), dtype=np.complex64)
        for i, c in enumerate(cfgs):
            cut = channel_cutoff(c.bandwidth, c.mode)
            if cut not in cache:
                taps = signal.firwin(ntaps, cut, fs=rate, window=("kaiser", 7.0))
                cache[cut] = (sfft.fft(taps, nc) * (nc / n)).astype(np.complex64)
            H[i] = cache[cut]
        self.H = H
        self.scale = np.array([rate / (2 * np.pi * nominal_deviation(c.bandwidth)) for c in cfgs],
                              dtype=np.float32)

        # De-emphasis: one-pole low-pass per distinct time constant (FM rows only).
        groups: dict[float, list[int]] = {}
        for i, c in enumerate(cfgs):
            if c.mode != MODE_AM and c.deemphasis_us > 0:
                groups.setdefault(float(c.deemphasis_us), []).append(i)
        self.deemph = []
        for tau, rows in groups.items():
            alpha = 1.0 - math.exp(-1.0 / (rate * tau * 1e-6))
            b, a = np.array([alpha]), np.array([1.0, alpha - 1.0])
            _, h = signal.freqz(b, a, worN=[1000.0], fs=rate)   # 0 dB at 1 kHz
            self.deemph.append((b / abs(h[0]), a, np.array(rows, dtype=np.int64)))
        self.de_zi = np.zeros((C, 1))

        self.voice_sos = signal.butter(4, [300.0, 3000.0], btype="bandpass", fs=rate, output="sos")
        self.voice_zi = np.zeros((self.voice_sos.shape[0], C, 2))

        self.ct_dec = max(1, int(round(rate / 3000.0)))
        ct_rate = rate / self.ct_dec
        self.ct_sos = signal.butter(4, 270.0, fs=rate, output="sos")
        self.ct_zi = np.zeros((self.ct_sos.shape[0], C, 2))
        self.ct_n = int(self.CT_WINDOW_S * ct_rate)
        self.dcs_n = int(self.DCS_WINDOW_S * ct_rate)
        # One ring buffer of the low-passed, decimated discriminator output feeds both
        # the CTCSS (last 0.5 s) and DCS (last 0.75 s) detectors.
        self.ring_n = max(self.ct_n, self.dcs_n)
        self.ring = np.zeros((C, self.ring_n))
        self.ring_pos = previous.ring_pos if prev_map else 0
        sp = ct_rate / dcs.BIT_RATE                       # ~22.3 samples per bit
        nbits = int((self.dcs_n - sp) // sp)
        self.dcs_bounds = np.array([[int(round(ph * sp / self.DCS_PHASES + k * sp)) for k in range(nbits + 1)]
                                    for ph in range(self.DCS_PHASES)], dtype=np.int64)
        self.dcs_weights = (1 << np.arange(dcs.WORD_BITS)).astype(np.int64)
        self.dcs_patterns, self.dcs_pattern_class = dcs.lookup_arrays()
        self.ct_win = np.hanning(self.ct_n)
        t = np.arange(self.ct_n) / ct_rate
        self.ct_basis = (np.exp(-2j * np.pi * np.outer(CTCSS_TONES, t)) * self.ct_win).T  # (n, tones)
        self.ct_norm_amp = (self.ct_win.sum() / 2) ** 2
        self.ct_norm_pow = (self.ct_win ** 2).sum() / 2
        self.ct_phase = previous.ct_phase if prev_map else 0
        self.ct_counter = previous.ct_counter if prev_map else 0

        self.nco_phase = np.zeros(C)
        self.prev = np.ones(C, dtype=np.complex64)
        self.am_carrier = np.zeros(C)

        for i, c in enumerate(cfgs):
            key = c.dsp_key()
            j = prev_map.get(key)
            if j is None:
                self.rows.append(_Row(cfg=c, key=key))
                continue
            old = previous.rows[j]
            old.cfg = c
            self.rows.append(old)
            self.nco_phase[i] = previous.nco_phase[j]
            self.prev[i] = previous.prev[j]
            self.am_carrier[i] = previous.am_carrier[j]
            self.de_zi[i] = previous.de_zi[j]
            self.voice_zi[:, i] = previous.voice_zi[:, j]
            self.ct_zi[:, i] = previous.ct_zi[:, j]
            self.ring[i] = previous.ring[j]

    def __len__(self) -> int:
        return len(self.rows)

    def ids(self) -> list[str]:
        return [r.cfg.id for r in self.rows]

    # ------------------------------------------------------------------ processing
    def process(self, X: np.ndarray, frame_index: int) -> BankFrame:
        chz = self.chz
        C, hop, n = len(self.rows), chz.hop_c, chz.n
        if C == 0:
            e = np.zeros((0, hop), dtype=np.float32)
            return BankFrame(e, np.zeros(0), np.zeros(0), [], [], [], [])

        # Extract every channel: gather bins, filter, batched inverse FFT.
        y = sfft.ifft(X[self.idx] * self.H, axis=1)[:, chz.overlap_c:]
        ph = ((self.k0_hop * (frame_index % n)) % n) / n
        rot = np.exp(2j * np.pi * (self.nco_phase - ph)).astype(np.complex64)
        y = (y * self.nco_rot) * rot[:, None]
        self.nco_phase = (self.nco_phase + self.nco_step * hop) % 1.0

        power = np.mean(y.real ** 2 + y.imag ** 2, axis=1)
        rssi = 10.0 * np.log10(power + 1e-20)

        # FM discriminator for all rows (AM rows are overwritten below).
        prev = np.concatenate((self.prev[:, None], y[:, :-1]), axis=1)
        self.prev = y[:, -1].copy()
        d = (np.angle(y * np.conj(prev)) * self.scale[:, None]).astype(np.float64)

        s = d.copy()
        for b, a, rows in self.deemph:
            s[rows], self.de_zi[rows] = signal.lfilter(b, a, d[rows], axis=1, zi=self.de_zi[rows])
        if len(self.am_idx):
            s[self.am_idx] = self._am_detect(y[self.am_idx])

        audio, self.voice_zi = signal.sosfilt(self.voice_sos, s, axis=1, zi=self.voice_zi)
        audio = (audio * HEADROOM).astype(np.float32)

        self._subaudio(d)

        # Noise floor: fast down, slow up.
        rssi_l = rssi.tolist()
        opened, carrier, tones, codes, targets = [], [], [], [], np.empty(C, dtype=np.float32)
        gains = np.empty(C, dtype=np.float32)
        noise = np.empty(C)
        dt = chz.frame_seconds
        for i, r in enumerate(self.rows):
            v = rssi_l[i]
            if not r.noise_init:
                r.noise_db, r.noise_init = v, True
            else:
                r.noise_db += (0.25 if v < r.noise_db else 0.002) * (v - r.noise_db)
            noise[i] = r.noise_db
            cfg = r.cfg
            is_fm = self.modes[i] != MODE_AM
            tone = r.tone if is_fm else None
            code = r.dcs_class if is_fm else None
            tone_ok = True
            if is_fm and cfg.dcs_code:
                tone_ok = code is not None and code in dcs.accept_classes(cfg.dcs_code, cfg.dcs_polarity)
            elif is_fm and cfg.ctcss_hz:
                tone_ok = tone is not None and abs(tone - cfg.ctcss_hz) < 0.6
            is_open = r.squelch.update(v, cfg.squelch_db, cfg.hysteresis_db, cfg.hang_time, tone_ok, dt)
            gains[i] = r.gain
            targets[i] = 1.0 if is_open else 0.0
            r.gain = float(targets[i])
            opened.append(is_open)
            carrier.append(r.squelch.carrier)
            tones.append(tone if code is None else None)
            codes.append(dcs.class_name(code) if code is not None else None)

        g = gains[:, None] + (targets - gains)[:, None] * self.ramp01[None, :]
        audio *= g
        return BankFrame(audio, rssi, noise, opened, carrier, tones, codes)

    def _am_detect(self, y: np.ndarray) -> np.ndarray:
        """Envelope detector with carrier-tracking AGC: output = modulation index."""
        env = np.abs(y)                       # float32
        m = env.mean(axis=1, dtype=np.float64)
        old = self.am_carrier[self.am_idx]
        reset = (old <= 0) | (m > 2.0 * old) | (m < 0.5 * old)
        new = np.where(reset, m, old + 0.25 * (m - old))
        start = np.where(reset, m, old)
        car = (start[:, None] + (new - start)[:, None] * self.ramp01[None, :]).astype(np.float32)
        self.am_carrier[self.am_idx] = new
        return env / np.maximum(car, np.float32(1e-9)) - np.float32(1.0)

    def _subaudio(self, d: np.ndarray) -> None:
        """Feed the sub-audio ring buffer and run the CTCSS / DCS detectors when due."""
        if not self.fm_mask.any():
            return
        lp, self.ct_zi = signal.sosfilt(self.ct_sos, d, axis=1, zi=self.ct_zi)
        dec = lp[:, self.ct_phase::self.ct_dec]
        self.ct_phase = (self.ct_phase - d.shape[1]) % self.ct_dec
        k = dec.shape[1]
        if k:
            if k > self.ring_n:
                dec, k = dec[:, -self.ring_n:], self.ring_n
            idx = (self.ring_pos + np.arange(k)) % self.ring_n
            self.ring[:, idx] = dec
            self.ring_pos = (self.ring_pos + k) % self.ring_n
        for r in self.rows:
            r.filled = min(self.ring_n, r.filled + k)
        self.ct_counter += 1
        if self.ct_counter % self.CT_EVERY == 0:
            self._ctcss_eval()
        if self.ct_counter % self.DCS_EVERY == 2:
            self._dcs_eval()

    def _recent(self, rows: list[int], n: int) -> np.ndarray:
        """The last n ring-buffer samples of `rows`, oldest first."""
        idx = (self.ring_pos - n + np.arange(n)) % self.ring_n
        return self.ring[np.ix_(rows, idx)]

    def _ctcss_eval(self) -> None:
        ready = [i for i, r in enumerate(self.rows) if r.filled >= self.ct_n and self.fm_mask[i]]
        if not ready:
            return
        x = self._recent(ready, self.ct_n)
        x = x - x.mean(axis=1, keepdims=True)
        powers = np.abs(x @ self.ct_basis) ** 2 / self.ct_norm_amp          # (R, tones)
        total = np.square(x * self.ct_win).sum(axis=1) / self.ct_norm_pow   # (R,)
        best = np.argmax(powers, axis=1)
        for j, i in enumerate(ready):
            ratio = float(powers[j, best[j]] / total[j]) if total[j] > 1e-12 else 0.0
            self._tone_state(self.rows[i], CTCSS_TONES[int(best[j])], ratio)

    def _dcs_eval(self) -> None:
        """Slice the sub-audio stream into bits and look up every 23-bit window."""
        ready, idle = [], []
        for i, r in enumerate(self.rows):
            if not self.fm_mask[i]:
                continue
            if r.filled >= self.dcs_n and r.squelch.carrier:
                ready.append(i)
            elif r.dcs_class is not None or r.dcs_candidate is not None:
                idle.append(i)
        for i in idle:
            self._dcs_state(self.rows[i], None)
        if not ready:
            return
        x = self._recent(ready, self.dcs_n)
        lo, hi = np.percentile(x, [10, 90], axis=1)
        x = x - ((lo + hi) / 2)[:, None]                 # slice at the middle of the two FSK levels
        cs = np.concatenate((np.zeros((len(ready), 1)), np.cumsum(x, axis=1)), axis=1)
        sums = cs[:, self.dcs_bounds[:, 1:]] - cs[:, self.dcs_bounds[:, :-1]]   # (R, phases, bits)
        bits = (sums > 0).astype(np.int64)
        windows = np.lib.stride_tricks.sliding_window_view(bits, dcs.WORD_BITS, axis=2)
        ints = windows @ self.dcs_weights                                         # (R, phases, windows)
        pos = np.minimum(np.searchsorted(self.dcs_patterns, ints), len(self.dcs_patterns) - 1)
        hit = self.dcs_patterns[pos] == ints
        cls = np.where(hit, self.dcs_pattern_class[pos], -1)
        best_phase = np.argmax(hit.sum(axis=2), axis=1)
        for j, i in enumerate(ready):
            c = cls[j, best_phase[j]]
            c = c[c >= 0]
            found = None
            if len(c) >= self.DCS_MIN_HITS:
                counts = np.bincount(c)
                top = int(np.argmax(counts))
                if counts[top] >= self.DCS_MIN_HITS and counts[top] >= self.DCS_DOMINANCE * len(c):
                    found = top
            self._dcs_state(self.rows[i], found)

    @staticmethod
    def _dcs_state(r: _Row, found: Optional[int]) -> None:
        if found is None:
            r.dcs_candidate, r.dcs_streak = None, 0
            r.dcs_miss += 1
            if r.dcs_miss >= 2:
                r.dcs_class = None
            return
        r.dcs_miss = 0
        if found == r.dcs_candidate:
            r.dcs_streak += 1
        else:
            r.dcs_candidate, r.dcs_streak = found, 1
        if found == r.dcs_class or r.dcs_streak >= 2:
            r.dcs_class = found

    def _tone_state(self, r: _Row, best: float, ratio: float) -> None:
        if ratio >= self.CT_OPEN_RATIO and best == r.candidate:
            r.streak += 1
        else:
            r.candidate = best if ratio >= self.CT_OPEN_RATIO else None
            r.streak = 1 if r.candidate else 0
        if r.tone is not None and r.tone == best:
            if ratio < self.CT_CLOSE_RATIO:
                r.tone = None
        elif r.streak >= self.CT_CONFIRM:
            r.tone = best
        elif r.tone is not None and (ratio < self.CT_CLOSE_RATIO or best != r.tone):
            r.tone = None


@dataclass
class ChannelFrame:
    audio: np.ndarray        # mono float32, squelch-gated, before volume/pan
    rssi_db: float
    noise_db: float
    open: bool
    carrier: bool
    tone: Optional[float]


class ChannelProcessor:
    """Single-channel convenience wrapper around ChannelBank (tests, self-test)."""

    def __init__(self, chz: Channelizer, cfg):
        self.cfg = cfg
        self.bank = ChannelBank(chz, [cfg])

    @property
    def valid(self) -> bool:
        return len(self.bank) == 1

    def process(self, X: np.ndarray, frame_index: int, chz: Optional[Channelizer] = None) -> ChannelFrame:
        f = self.bank.process(X, frame_index)
        return ChannelFrame(f.audio[0], float(f.rssi[0]), float(f.noise[0]), f.open[0], f.carrier[0], f.tones[0])


@lru_cache(maxsize=8)
def _spectrum_window(nfft: int) -> tuple[np.ndarray, float]:
    win = np.blackman(nfft).astype(np.float32)
    win.setflags(write=False)
    return win, float(win.sum() ** 2)


def spectrum_db(x: np.ndarray, nfft: int = 2048, max_avg: int = 16) -> np.ndarray:
    """Averaged, windowed power spectrum in dBFS, fft-shifted."""
    count = min(max_avg, len(x) // nfft)
    if count == 0:
        x = np.pad(x, (0, nfft - len(x)))
        count = 1
    step = len(x) // count
    segs = np.stack([x[i * step:i * step + nfft] for i in range(count)])
    win, norm = _spectrum_window(nfft)
    X = sfft.fft(segs * win, axis=1)
    p = np.mean(X.real ** 2 + X.imag ** 2, axis=0) / norm
    return sfft.fftshift(10 * np.log10(p + 1e-20)).astype(np.float32)


def u8_to_complex(buf: np.ndarray) -> np.ndarray:
    """Convert interleaved unsigned 8-bit IQ (RTL-SDR native) to complex64."""
    if len(buf) % 2:
        buf = buf[:-1]
    f = buf.astype(np.float32)
    f -= 127.4
    f *= 1.0 / 128.0
    return f.view(np.complex64)


def complex_to_u8(x: np.ndarray) -> np.ndarray:
    f = np.empty(len(x) * 2, dtype=np.float32)
    f[0::2] = x.real
    f[1::2] = x.imag
    return np.clip(np.round(f * 128.0 + 127.4), 0, 255).astype(np.uint8)

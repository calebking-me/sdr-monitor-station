"""Signal processing: channelizer, FM demodulator, squelch, CTCSS detection.

Channelization uses a fast-convolution (overlap-save) filter bank. One large
FFT of the wideband IQ is shared by every channel; each channel then selects
the FFT bins around its frequency, applies its low-pass filter in the
frequency domain and runs a small inverse FFT. Selecting only `nc` of `n` bins
decimates the channel straight down to the audio rate (48 kHz), so adding a
channel costs one small IFFT instead of a full-rate mixer and FIR filter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

import numpy as np
import scipy.fft as sfft
from scipy import signal

AUDIO_RATE = 48_000

CTCSS_TONES = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4,
    100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5,
    141.3, 146.2, 150.0, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
    173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)


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


class ChannelTap:
    """Extracts one channel from the shared FFT frames."""

    def __init__(self, chz: "Channelizer", offset_hz: float, cutoff_hz: float):
        self.offset_hz = offset_hz
        n, nc = chz.n, chz.nc
        self.k0 = int(round(offset_hz * n / chz.fs))
        residual = offset_hz - self.k0 * chz.fs / n
        bins = np.fft.fftfreq(nc, 1.0 / nc).astype(np.int64)
        self.idx = (self.k0 + bins) % n
        ntaps = min(chz.nc - chz.hop_c - 1, 255) | 1
        taps = signal.firwin(ntaps, cutoff_hz, fs=chz.rate, window=("kaiser", 7.0))
        self.H = (sfft.fft(taps, nc) * (nc / n)).astype(np.complex64)
        # Per-frame phase correction makes the bin shift equivalent to a continuous mixer.
        self._k0_hop = (self.k0 * chz.hop) % n
        # Fine NCO removes the residual (< half a bin) frequency error.
        self._nco_step = -residual / chz.rate
        self._nco_phase = 0.0
        self._ramp = np.arange(chz.hop_c, dtype=np.float64)

    def extract(self, X: np.ndarray, frame_index: int, chz: "Channelizer") -> np.ndarray:
        y = sfft.ifft(X[self.idx] * self.H)[chz.overlap_c:]
        ph = (self._k0_hop * (frame_index % chz.n)) % chz.n / chz.n
        cycles = self._nco_phase - ph + self._nco_step * self._ramp
        y = y * np.exp(2j * np.pi * cycles).astype(np.complex64)
        self._nco_phase = (self._nco_phase + self._nco_step * chz.hop_c) % 1.0
        return y.astype(np.complex64, copy=False)


class Channelizer:
    """Overlap-save fast-convolution channelizer (75% hop)."""

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

    def max_offset(self, bandwidth: float) -> float:
        """Largest |offset| at which a channel's bins do not wrap around."""
        return self.fs / 2 - self.rate / 2

    def make_tap(self, freq_hz: float, cutoff_hz: float) -> Optional[ChannelTap]:
        off = freq_hz - self.center
        if abs(off) > self.max_offset(cutoff_hz):
            return None
        return ChannelTap(self, off, cutoff_hz)

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


def channel_cutoff(bandwidth: float) -> float:
    """Channel filter cutoff (one-sided) for a 12.5 or 25 kHz channel."""
    return 5_500.0 if bandwidth <= 12_500 else 8_500.0


def nominal_deviation(bandwidth: float) -> float:
    return 2_500.0 if bandwidth <= 12_500 else 5_000.0


class FMDemod:
    """Quadrature FM discriminator with de-emphasis and voice band-pass."""

    HEADROOM = 0.5     # full deviation -> -6 dBFS audio, leaves room for mixing channels

    def __init__(self, rate: float, bandwidth: float, deemphasis_us: float = 750.0):
        self.rate = rate
        self.scale = rate / (2 * np.pi * nominal_deviation(bandwidth))
        self._prev = np.complex64(1.0)
        self.deemph = deemphasis_us > 0
        if self.deemph:
            alpha = 1.0 - math.exp(-1.0 / (rate * deemphasis_us * 1e-6))
            self._de_b = np.array([alpha])
            self._de_a = np.array([1.0, alpha - 1.0])
            # Normalise gain to 0 dB at 1 kHz.
            _, h = signal.freqz(self._de_b, self._de_a, worN=[1000.0], fs=rate)
            self._de_b = self._de_b / abs(h[0])
            self._de_zi = np.zeros(1)
        self._voice_sos = signal.butter(4, [300.0, 3000.0], btype="bandpass", fs=rate, output="sos")
        self._voice_zi = np.zeros((self._voice_sos.shape[0], 2))

    def discriminate(self, y: np.ndarray) -> np.ndarray:
        """Raw instantaneous frequency, scaled so nominal deviation = 1.0."""
        prev = np.concatenate(([self._prev], y[:-1]))
        self._prev = y[-1]
        return (np.angle(y * np.conj(prev)) * self.scale).astype(np.float64)

    def audio(self, d: np.ndarray) -> np.ndarray:
        if self.deemph:
            d, self._de_zi = signal.lfilter(self._de_b, self._de_a, d, zi=self._de_zi)
        a, self._voice_zi = signal.sosfilt(self._voice_sos, d, zi=self._voice_zi)
        return (a * self.HEADROOM).astype(np.float32)


class CtcssDetector:
    """Detects sub-audible CTCSS tones in the discriminator output."""

    WINDOW_S = 0.5
    OPEN_RATIO = 0.35
    CLOSE_RATIO = 0.2
    CONFIRM = 3        # consecutive agreeing estimates (~0.25 s) before reporting a tone

    def __init__(self, rate: float):
        self.dec = max(1, int(round(rate / 3000.0)))
        self.rate = rate / self.dec
        self._sos = signal.butter(4, 270.0, fs=rate, output="sos")
        self._zi = np.zeros((self._sos.shape[0], 2))
        self._phase = 0
        self._n = int(self.WINDOW_S * self.rate)
        self._hist = np.zeros(self._n)
        self._filled = 0
        self._win = np.hanning(self._n)
        t = np.arange(self._n) / self.rate
        tones = np.asarray(CTCSS_TONES)
        self._basis = (np.exp(-2j * np.pi * np.outer(tones, t)) * self._win).astype(np.complex128)
        self._norm_amp = (self._win.sum() / 2) ** 2
        self._norm_pow = (self._win ** 2).sum() / 2
        self.tone: Optional[float] = None    # currently detected tone
        self.ratio = 0.0
        self._counter = 0
        self._candidate: Optional[float] = None
        self._streak = 0

    def update(self, d: np.ndarray) -> Optional[float]:
        lp, self._zi = signal.sosfilt(self._sos, d, zi=self._zi)
        dec = lp[self._phase::self.dec]
        self._phase = (self._phase - len(d)) % self.dec
        k = len(dec)
        if k >= self._n:
            self._hist[:] = dec[-self._n:]
        else:
            self._hist[:-k] = self._hist[k:]
            self._hist[-k:] = dec
        self._filled = min(self._n, self._filled + k)
        self._counter += 1
        if self._filled < self._n or self._counter % 4:
            return self.tone
        x = self._hist - self._hist.mean()
        powers = np.abs(self._basis @ x) ** 2 / self._norm_amp   # amplitude^2 per tone
        total = (np.square(x * self._win)).sum() / self._norm_pow  # amplitude^2 of everything
        i = int(np.argmax(powers))
        self.ratio = float(powers[i] / total) if total > 1e-12 else 0.0
        best = CTCSS_TONES[i]
        if self.ratio >= self.OPEN_RATIO and best == self._candidate:
            self._streak += 1
        else:
            self._candidate = best if self.ratio >= self.OPEN_RATIO else None
            self._streak = 1 if self._candidate else 0
        if self.tone is not None and self.tone == best:
            if self.ratio < self.CLOSE_RATIO:
                self.tone = None
        elif self._streak >= self.CONFIRM:
            self.tone = best
        elif self.tone is not None and (self.ratio < self.CLOSE_RATIO or best != self.tone):
            self.tone = None
        return self.tone

    def reset(self):
        self.tone = None
        self._filled = 0
        self._candidate, self._streak = None, 0


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
class ChannelFrame:
    audio: np.ndarray        # mono float32, squelch-gated, before volume/pan
    rssi_db: float
    noise_db: float
    open: bool
    carrier: bool
    tone: Optional[float]


class ChannelProcessor:
    """Full per-channel receive chain operating on channelizer frames."""

    def __init__(self, chz: Channelizer, cfg):
        self.cfg = cfg   # ChannelConfig, read live for squelch/tone parameters
        self.tap = chz.make_tap(cfg.frequency, channel_cutoff(cfg.bandwidth))
        self.rate = chz.rate
        self.dt = chz.frame_seconds
        self.demod = FMDemod(chz.rate, cfg.bandwidth, cfg.deemphasis_us)
        self.ctcss = CtcssDetector(chz.rate)
        self.squelch = Squelch()
        self.noise_db = -100.0
        self._noise_init = False
        self._gain = 0.0
        self._ramp = np.linspace(0.0, 1.0, chz.hop_c, dtype=np.float32)

    @property
    def valid(self) -> bool:
        return self.tap is not None

    def process(self, X: np.ndarray, frame_index: int, chz: Channelizer) -> ChannelFrame:
        y = self.tap.extract(X, frame_index, chz)
        p = float(np.mean(y.real ** 2 + y.imag ** 2))
        rssi = 10.0 * math.log10(p + 1e-20)
        if not self._noise_init:
            self.noise_db, self._noise_init = rssi, True
        else:
            k = 0.25 if rssi < self.noise_db else 0.002
            self.noise_db += k * (rssi - self.noise_db)

        d = self.demod.discriminate(y)
        cfg = self.cfg
        tone = self.ctcss.update(d)
        tone_ok = True
        if cfg.ctcss_hz:
            tone_ok = tone is not None and abs(tone - cfg.ctcss_hz) < 0.6
        was_open = self.squelch.open
        is_open = self.squelch.update(rssi, cfg.squelch_db, cfg.hysteresis_db,
                                      cfg.hang_time, tone_ok, self.dt)
        if not is_open and not was_open and self._gain == 0.0:
            audio = np.zeros(len(y), dtype=np.float32)
            # Keep filter state warm so the next opening is click-free.
            self.demod.audio(d)
        else:
            audio = self.demod.audio(d)
            target = 1.0 if is_open else 0.0
            if target != self._gain:
                audio *= self._gain + (target - self._gain) * self._ramp
                self._gain = target
            elif target == 0.0:
                audio[:] = 0.0
        return ChannelFrame(audio, rssi, self.noise_db, is_open, self.squelch.carrier, tone)


def spectrum_db(x: np.ndarray, nfft: int = 2048, max_avg: int = 16) -> np.ndarray:
    """Averaged, windowed power spectrum in dBFS, fft-shifted."""
    count = min(max_avg, len(x) // nfft)
    if count == 0:
        x = np.pad(x, (0, nfft - len(x)))
        count = 1
    step = len(x) // count
    segs = np.stack([x[i * step:i * step + nfft] for i in range(count)])
    win = np.blackman(nfft).astype(np.float32)
    X = sfft.fft(segs * win, axis=1)
    p = np.mean(np.abs(X) ** 2, axis=0) / (win.sum() ** 2)
    return sfft.fftshift(10 * np.log10(p + 1e-20)).astype(np.float32)


def u8_to_complex(buf: np.ndarray) -> np.ndarray:
    """Convert interleaved unsigned 8-bit IQ (RTL-SDR native) to complex64."""
    f = buf.astype(np.float32)
    f -= 127.4
    f *= 1.0 / 128.0
    return f.view(np.complex64)


def complex_to_u8(x: np.ndarray) -> np.ndarray:
    f = np.empty(len(x) * 2, dtype=np.float32)
    f[0::2] = x.real
    f[1::2] = x.imag
    return np.clip(np.round(f * 128.0 + 127.4), 0, 255).astype(np.uint8)

"""A tunable synthetic radio for tests and the self-test (no hardware needed).

Transmitters sit at absolute frequencies and key up in bursts. Whatever is
inside the current tuning (centre +- half the sample rate) is synthesised into
the IQ stream, so retuning behaves like a real receiver: signals outside the
tuning disappear, signals inside appear at their offset. Time advances with the
samples read, so it keeps running across retunes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..config import MODE_AM, MODE_FM
from ..sources import IQSource
from .gen_iq import dcs_waveform


@dataclass
class SimTransmitter:
    frequency: float                      # Hz
    level_db: float = -40.0               # carrier level, dBFS
    bursts: list[tuple[float, float]] = field(default_factory=list)   # (start, end) s; empty = always on
    audio_hz: float = 1000.0              # test tone
    deviation: float = 2_500.0
    mode: str = MODE_FM
    ctcss: Optional[float] = None
    dcs: Optional[str] = None
    dcs_inverted: bool = False

    def on(self, t: np.ndarray) -> np.ndarray:
        if not self.bursts:
            return np.ones(len(t), dtype=bool)
        m = np.zeros(len(t), dtype=bool)
        for s, e in self.bursts:
            m |= (t >= s) & (t < e)
        return m


class SimulatedSource(IQSource):
    tunable = True
    name = "Simulator"
    retune_settle = 0.0

    def __init__(self, transmitters: list[SimTransmitter], sample_rate: float = 2_400_000.0,
                 noise_db: float = -60.0, realtime: bool = True, seed: int = 1,
                 tune_range: tuple[float, float] = (24e6, 1_766e6)):
        super().__init__()
        self.transmitters = transmitters
        self.fs = float(sample_rate)
        self.noise_amp = 10 ** (noise_db / 20) / np.sqrt(2)
        self.realtime = realtime
        self.tune_range = tune_range
        self.rng = np.random.default_rng(seed)
        self.n = 0                                       # samples produced so far
        self.retunes: list[float] = []
        self._phase = [0.0] * len(transmitters)
        self._t0 = 0.0

    @property
    def stream_time(self) -> float:
        return self.n / self.fs

    def open(self, center: float) -> None:
        self.sample_rate = self.fs
        self._check(center)
        self.center = float(center)
        self._t0 = time.monotonic()

    def _check(self, center: float) -> None:
        lo, hi = self.tune_range
        if not lo <= center <= hi:
            raise ValueError(f"{center / 1e6:.3f} MHz is outside the tuning range")

    def set_center(self, center: float) -> None:
        self._check(center)
        self.retunes.append(float(center))
        self.center = float(center)

    def set_gain(self, agc: bool, gain_db: float) -> None:
        pass

    def set_ppm(self, ppm: int) -> None:
        pass

    def read(self, n: int) -> np.ndarray:
        fs = self.fs
        t = (self.n + np.arange(n)) / fs
        x = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)) * self.noise_amp
        for k, tx in enumerate(self.transmitters):
            off = tx.frequency - self.center
            if abs(off) > fs / 2:
                continue
            on = tx.on(t)
            if not on.any():
                continue
            audio = 0.8 * np.sin(2 * np.pi * tx.audio_hz * t)
            if tx.mode == MODE_FM and tx.ctcss:
                audio = 0.85 * audio + 0.15 * np.sin(2 * np.pi * tx.ctcss * t)
            elif tx.mode == MODE_FM and tx.dcs:
                audio = 0.8 * audio + dcs_waveform(tx.dcs, tx.dcs_inverted, t)
            amp = 10 ** (tx.level_db / 20)
            if tx.mode == MODE_AM:
                ph = self._phase[k] + 2 * np.pi * off / fs * np.arange(1, n + 1)
                env = amp * (1.0 + 0.6 * audio)
            else:
                ph = self._phase[k] + np.cumsum(2 * np.pi * (off + tx.deviation * audio) / fs)
                env = amp
            self._phase[k] = float(ph[-1] % (2 * np.pi))
            x += on * env * np.exp(1j * ph)
        self.n += n
        if self.realtime:
            delay = self._t0 + self.n / fs - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return x.astype(np.complex64)

    def close(self) -> None:
        pass

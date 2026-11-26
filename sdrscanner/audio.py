"""Audio output: one stereo stream per sound device, fed by a jitter buffer.

Channels are mixed per device in the DSP thread; each device's PortAudio
callback pulls from its buffer. Small clock differences between the SDR and
the sound card are absorbed by dropping or padding samples at the buffer edges.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Optional

import numpy as np

from .dsp import AUDIO_RATE

try:
    import sounddevice as sd
    _SD_ERROR: Optional[str] = None
except Exception as e:  # PortAudio missing (e.g. headless Linux without libportaudio2)
    sd = None
    _SD_ERROR = str(e)

DEFAULT_KEY = ""


def audio_available() -> tuple[bool, str]:
    return (sd is not None, _SD_ERROR or "")


def device_key(dev: dict, hostapis) -> str:
    return f"{hostapis[dev['hostapi']]['name']} :: {dev['name']}"


def list_output_devices() -> list[tuple[str, str]]:
    """Return (key, label) for every output device. The key is stable across restarts."""
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return []
    out = []
    for dev in devices:
        if dev["max_output_channels"] > 0:
            key = device_key(dev, hostapis)
            out.append((key, key))
    return out


def resolve_device(key: str) -> Optional[int]:
    """Map a device key to a PortAudio index (None = system default)."""
    if sd is None or not key:
        return None
    try:
        hostapis = sd.query_hostapis()
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and device_key(dev, hostapis) == key:
                return i
    except Exception:
        pass
    return None


class LinearResampler:
    """Streaming linear-interpolation resampler for devices that refuse 48 kHz."""

    def __init__(self, ratio: float):
        self.ratio = ratio          # output rate / input rate
        self._t = 0.0
        self._last = np.zeros(2, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.vstack((self._last[None, :], x))
        step = 1.0 / self.ratio
        pos = np.arange(self._t, len(x) - 1, step)
        i = pos.astype(np.int64)
        frac = (pos - i)[:, None].astype(np.float32)
        y = x[i] * (1 - frac) + x[i + 1] * frac
        self._t = (pos[-1] + step) - (len(x) - 1) if len(pos) else self._t - (len(x) - 1)
        self._last = x[-1].copy()
        return y.astype(np.float32)


class AudioOutput:
    TARGET_S = 0.12
    MAX_S = 0.5

    def __init__(self, key: str, on_error: Callable[[str], None]):
        self.key = key
        self._on_error = on_error
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._buffered = 0
        self._prefill = True
        self._resampler: Optional[LinearResampler] = None
        self.underruns = 0
        index = resolve_device(key)
        if key and index is None:
            on_error(f"Audio device '{key}' not found; using the default output")
        self.rate = AUDIO_RATE
        try:
            self._stream = sd.OutputStream(device=index, channels=2, samplerate=AUDIO_RATE,
                                           dtype="float32", callback=self._callback, latency="low")
        except Exception:
            info = sd.query_devices(index, "output") if index is not None else sd.query_devices(kind="output")
            self.rate = int(info["default_samplerate"])
            self._resampler = LinearResampler(self.rate / AUDIO_RATE)
            self._stream = sd.OutputStream(device=index, channels=2, samplerate=self.rate,
                                           dtype="float32", callback=self._callback, latency="low")
        self._stream.start()

    def write(self, stereo: np.ndarray) -> None:
        if self._resampler is not None:
            stereo = self._resampler.process(stereo)
        with self._lock:
            self._chunks.append(stereo)
            self._buffered += len(stereo)
            if self._buffered > self.MAX_S * self.rate:
                target = int(self.TARGET_S * self.rate)
                while self._buffered > target and self._chunks:
                    c = self._chunks.popleft()
                    self._buffered -= len(c)

    def _callback(self, outdata, frames, time_info, status) -> None:
        with self._lock:
            if self._prefill:
                if self._buffered < self.TARGET_S * self.rate:
                    outdata.fill(0)
                    return
                self._prefill = False
            filled = 0
            while filled < frames and self._chunks:
                c = self._chunks[0]
                take = min(frames - filled, len(c))
                outdata[filled:filled + take] = c[:take]
                filled += take
                self._buffered -= take
                if take == len(c):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = c[take:]
            if filled < frames:
                outdata[filled:] = 0
                self._prefill = True
                self.underruns += 1

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class AudioManager:
    def __init__(self, on_error: Callable[[str], None] = lambda m: None):
        self._outputs: dict[str, AudioOutput] = {}
        self._failed: set[str] = set()
        self._on_error = on_error
        self.enabled = sd is not None
        if not self.enabled:
            on_error(f"Audio disabled: {_SD_ERROR}")

    def write(self, key: str, stereo: np.ndarray) -> None:
        if not self.enabled or key in self._failed:
            return
        out = self._outputs.get(key)
        if out is None:
            try:
                out = AudioOutput(key, self._on_error)
            except Exception as e:
                self._failed.add(key)
                self._on_error(f"Cannot open audio device '{key or 'default'}': {e}")
                return
            self._outputs[key] = out
        out.write(stereo)

    def close_unused(self, keys: set[str]) -> None:
        for k in list(self._outputs):
            if k not in keys:
                self._outputs.pop(k).close()

    def close_all(self) -> None:
        for out in self._outputs.values():
            out.close()
        self._outputs.clear()
        self._failed.clear()


def pan_gains(pan: float) -> tuple[float, float]:
    """Balance law: centre = full level on both sides, fading one side out."""
    pan = max(-1.0, min(1.0, pan))
    return min(1.0, 1.0 - pan), min(1.0, 1.0 + pan)

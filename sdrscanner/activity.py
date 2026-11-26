"""Activity log, per-transmission WAV recording and IQ recording."""

from __future__ import annotations

import csv
import re
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .dsp import AUDIO_RATE, complex_to_u8

RECORD_DECIMATION = 3            # 48 kHz -> 16 kHz (audio is already band-limited to 3 kHz)
RECORD_RATE = AUDIO_RATE // RECORD_DECIMATION


@dataclass
class ActivityEvent:
    channel_id: str
    channel_name: str
    frequency: float
    start: float                 # unix time
    end: float
    peak_dbfs: float
    tone: Optional[float] = None
    recording: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


CSV_FIELDS = ["start", "end", "channel", "frequency_mhz", "duration_s", "peak_dbfs", "ctcss_hz", "recording"]


def event_row(ev: ActivityEvent) -> dict:
    return {
        "start": datetime.fromtimestamp(ev.start).isoformat(sep=" ", timespec="seconds"),
        "end": datetime.fromtimestamp(ev.end).isoformat(sep=" ", timespec="seconds"),
        "channel": ev.channel_name,
        "frequency_mhz": f"{ev.frequency / 1e6:.5f}",
        "duration_s": f"{ev.duration:.1f}",
        "peak_dbfs": f"{ev.peak_dbfs:.1f}",
        "ctcss_hz": f"{ev.tone:.1f}" if ev.tone else "",
        "recording": ev.recording,
    }


def export_csv(events: Iterable[ActivityEvent], path: str | Path) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for ev in events:
            w.writerow(event_row(ev))
            n += 1
    return n


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "channel"


class WavRecorder:
    """Writes one transmission to a 16 kHz mono 16-bit WAV file."""

    def __init__(self, directory: Path, channel_name: str, frequency: float, start: float):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(start).strftime("%Y%m%d_%H%M%S")
        stem = f"{stamp}_{_safe(channel_name)}_{frequency / 1e6:.4f}MHz"
        self.path = directory / f"{stem}.wav"
        n = 2
        while self.path.exists():
            self.path = directory / f"{stem}_{n}.wav"
            n += 1
        self._w = wave.open(str(self.path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(RECORD_RATE)

    def write(self, audio: np.ndarray) -> None:
        a = np.clip(audio[::RECORD_DECIMATION], -1.0, 1.0)
        self._w.writeframes((a * 32767).astype("<i2").tobytes())

    def close(self) -> None:
        try:
            self._w.close()
        except Exception:
            pass


class IqRecorder:
    """Records the raw wideband IQ as SigMF cu8 for later playback."""

    def __init__(self, base: Path, sample_rate: float, center: float):
        from .sources import sigmf_base, write_sigmf_meta
        base.parent.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.center = center
        self.base = sigmf_base(base)
        self.path = Path(self.base + ".sigmf-data")
        self._f = open(self.path, "wb")
        self.samples = 0
        write_sigmf_meta(self.base, sample_rate, center, "sdrscanner IQ recording")

    def write(self, x: np.ndarray) -> None:
        self._f.write(complex_to_u8(x).tobytes())
        self.samples += len(x)

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass

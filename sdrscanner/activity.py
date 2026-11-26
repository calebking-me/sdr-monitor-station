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
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "|", "%")


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
    mode: str = "FM"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


CSV_FIELDS = ["start", "end", "channel", "frequency_mhz", "mode", "duration_s", "peak_dbfs",
              "ctcss_hz", "recording"]


def csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection (cells starting with = + - @ ...).

    Text that would be evaluated by Excel/LibreOffice is prefixed with a quote.
    Plain negative numbers such as "-42.5" are left alone.
    """
    s = "" if value is None else str(value)
    if s.startswith(_FORMULA_PREFIXES):
        try:
            float(s)
            return s
        except ValueError:
            return "'" + s
    return s


def event_row(ev: ActivityEvent) -> dict:
    return {
        "start": datetime.fromtimestamp(ev.start).isoformat(sep=" ", timespec="seconds"),
        "end": datetime.fromtimestamp(ev.end).isoformat(sep=" ", timespec="seconds"),
        "channel": ev.channel_name,
        "frequency_mhz": f"{ev.frequency / 1e6:.5f}",
        "mode": ev.mode,
        "duration_s": f"{ev.duration:.1f}",
        "peak_dbfs": f"{ev.peak_dbfs:.1f}",
        "ctcss_hz": f"{ev.tone:.1f}" if ev.tone else "",
        "recording": ev.recording,
    }


def export_csv(events: Iterable[ActivityEvent], path: str | Path) -> int:
    n = 0
    # utf-8-sig so Excel recognises the encoding of non-ASCII channel names.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for ev in events:
            w.writerow({k: csv_safe(v) for k, v in event_row(ev).items()})
            n += 1
    return n


def safe_filename(name: str, max_len: int = 40) -> str:
    """Reduce arbitrary text to a short, portable file-name fragment (no paths)."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return s[:max_len].rstrip("._") or "channel"


def _create_exclusive(directory: Path, stem: str, suffix: str):
    """Open a new file without ever overwriting an existing one."""
    for n in range(1, 1000):
        path = directory / (f"{stem}{suffix}" if n == 1 else f"{stem}_{n}{suffix}")
        try:
            return path, open(path, "xb")
        except FileExistsError:
            continue
    raise OSError(f"too many files named {stem}* in {directory}")


class WavRecorder:
    """Writes one transmission to a 16 kHz mono 16-bit WAV file."""

    def __init__(self, directory: Path, channel_name: str, frequency: float, start: float):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(start).strftime("%Y%m%d_%H%M%S")
        stem = f"{stamp}_{safe_filename(channel_name)}_{frequency / 1e6:.4f}MHz"
        self.path, self._file = _create_exclusive(directory, stem, ".wav")
        self._w = wave.open(self._file, "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(RECORD_RATE)

    def write(self, audio: np.ndarray) -> None:
        a = np.clip(audio[::RECORD_DECIMATION], -1.0, 1.0)
        self._w.writeframes((a * 32767).astype("<i2").tobytes())

    def close(self) -> None:
        for obj in (self._w, self._file):
            try:
                obj.close()
            except Exception:
                pass


class IqRecorder:
    """Records the raw wideband IQ as SigMF cu8 for later playback."""

    def __init__(self, base: Path, sample_rate: float, center: float):
        from .sources import sigmf_base, write_sigmf_meta
        base = Path(base)
        base.parent.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.center = center
        path, self._f = _create_exclusive(base.parent, Path(sigmf_base(base)).name, ".sigmf-data")
        self.path = path
        self.base = sigmf_base(path)
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

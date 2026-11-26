"""Configuration data model and JSON profile persistence."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

PROFILE_VERSION = 1

SOURCE_RTLSDR = "rtlsdr"
SOURCE_RTLTCP = "rtl_tcp"
SOURCE_FILE = "file"
SOURCE_TYPES = (SOURCE_RTLSDR, SOURCE_RTLTCP, SOURCE_FILE)

# Sample rates the RTL2832U handles reliably (Hz).
RTL_SAMPLE_RATES = (
    1_024_000, 1_400_000, 1_800_000, 1_920_000, 2_048_000,
    2_400_000, 2_560_000, 2_880_000, 3_200_000,
)

BANDWIDTHS = (12_500, 25_000)

CHANNEL_COLORS = (
    "#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176",
    "#4db6ac", "#f06292", "#a1887f", "#90a4ae", "#aed581", "#7986cb",
)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ChannelConfig:
    """A single monitored frequency and its per-channel settings."""

    name: str = "Channel"
    frequency: float = 146_520_000.0      # Hz
    bandwidth: float = 12_500.0           # Hz, 12.5 kHz (narrow) or 25 kHz (wide)
    enabled: bool = True
    squelch_db: float = -50.0             # dBFS, opens at this level
    hysteresis_db: float = 3.0            # closes at squelch_db - hysteresis_db
    hang_time: float = 0.5                # seconds squelch stays open after signal drops
    ctcss_hz: Optional[float] = None      # None = carrier squelch only
    deemphasis_us: float = 750.0          # 0 disables de-emphasis
    volume: float = 1.0                   # linear gain, 0..2
    pan: float = 0.0                      # -1 = left only, 0 = centre, +1 = right only
    mute: bool = False
    solo: bool = False
    audio_device: str = ""                # "" = system default output
    record: bool = False                  # write each transmission to WAV
    color: str = ""
    id: str = field(default_factory=_new_id)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChannelConfig":
        names = {f.name for f in fields(cls)}
        ch = cls(**{k: v for k, v in d.items() if k in names})
        ch.frequency = float(ch.frequency)
        ch.bandwidth = float(ch.bandwidth)
        if ch.ctcss_hz is not None:
            ch.ctcss_hz = float(ch.ctcss_hz)
        ch.pan = max(-1.0, min(1.0, float(ch.pan)))
        ch.volume = max(0.0, min(2.0, float(ch.volume)))
        if not ch.id:
            ch.id = _new_id()
        return ch

    def dsp_key(self) -> tuple:
        """Settings that require the channel's DSP chain to be rebuilt."""
        return (self.id, self.frequency, self.bandwidth, self.deemphasis_us)


@dataclass
class RadioConfig:
    source: str = SOURCE_RTLSDR
    device_index: int = 0
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 1234
    file_path: str = ""
    file_center: float = 0.0              # Hz, for files without metadata
    file_rate: float = 0.0                # Hz, for files without metadata
    file_loop: bool = True
    sample_rate: float = 2_400_000.0
    agc: bool = False
    gain_db: float = 30.0
    ppm: int = 0
    auto_center: bool = True
    manual_center: float = 146_000_000.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RadioConfig":
        names = {f.name for f in fields(cls)}
        rc = cls(**{k: v for k, v in d.items() if k in names})
        if rc.source not in SOURCE_TYPES:
            rc.source = SOURCE_RTLSDR
        return rc


@dataclass
class AppConfig:
    radio: RadioConfig = field(default_factory=RadioConfig)
    channels: list[ChannelConfig] = field(default_factory=list)
    recordings_dir: str = ""
    timeline_minutes: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "radio": asdict(self.radio),
            "channels": [asdict(c) for c in self.channels],
            "recordings_dir": self.recordings_dir,
            "timeline_minutes": self.timeline_minutes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppConfig":
        cfg = cls(
            radio=RadioConfig.from_dict(d.get("radio", {})),
            channels=[ChannelConfig.from_dict(c) for c in d.get("channels", [])],
            recordings_dir=str(d.get("recordings_dir", "")),
            timeline_minutes=int(d.get("timeline_minutes", 10)),
        )
        cfg.ensure_colors()
        return cfg

    def ensure_colors(self) -> None:
        used = {c.color for c in self.channels if c.color}
        palette = [c for c in CHANNEL_COLORS if c not in used] or list(CHANNEL_COLORS)
        i = 0
        for ch in self.channels:
            if not ch.color:
                ch.color = palette[i % len(palette)]
                i += 1

    def next_color(self) -> str:
        used = [c.color for c in self.channels]
        for c in CHANNEL_COLORS:
            if c not in used:
                return c
        return CHANNEL_COLORS[len(used) % len(CHANNEL_COLORS)]

    def channel(self, ch_id: str) -> Optional[ChannelConfig]:
        for c in self.channels:
            if c.id == ch_id:
                return c
        return None

    def effective_recordings_dir(self) -> Path:
        if self.recordings_dir:
            return Path(self.recordings_dir)
        return Path.home() / "SDRScanner" / "recordings"


def save_profile(cfg: AppConfig, path: str | os.PathLike) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_profile(path: str | os.PathLike) -> AppConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Profile must be a JSON object")
    return AppConfig.from_dict(data)


def user_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "SDRScanner"


def last_profile_path() -> Path:
    return user_config_dir() / "last_profile.json"


def format_mhz(hz: float) -> str:
    return f"{hz / 1e6:.4f}"

"""Configuration data model, validation and JSON profile persistence.

Profiles, channel CSVs and presets can come from other people, so every value
read from them is validated: numbers must be finite and are clamped to sane
ranges, strings are length-limited and stripped of control characters, and
colours must be plain #rrggbb. Anything that cannot be repaired is dropped and
reported in `AppConfig.load_warnings` instead of reaching the DSP or the GUI.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import dcs

PROFILE_VERSION = 3
MAX_PROFILE_BYTES = 5 * 1024 * 1024
MAX_CHANNELS = 256
MAX_GROUPS = 64
MAX_GROUP_NAME = 40
DEFAULT_HOLD_TIME = 2.0

SOURCE_RTLSDR = "rtlsdr"
SOURCE_RTLTCP = "rtl_tcp"
SOURCE_FILE = "file"
SOURCE_TYPES = (SOURCE_RTLSDR, SOURCE_RTLTCP, SOURCE_FILE)

# Sample rates the RTL2832U handles reliably (Hz).
RTL_SAMPLE_RATES = (
    1_024_000, 1_400_000, 1_800_000, 1_920_000, 2_048_000,
    2_400_000, 2_560_000, 2_880_000, 3_200_000,
)

MODE_FM = "FM"
MODE_AM = "AM"
MODES = (MODE_FM, MODE_AM)

# Suggested channel bandwidths per mode (any value in MIN..MAX_BANDWIDTH is accepted).
FM_BANDWIDTHS = (12_500.0, 25_000.0)
AM_BANDWIDTHS = (6_000.0, 8_000.0, 10_000.0)
DEFAULT_BANDWIDTH = {MODE_FM: 12_500.0, MODE_AM: 8_000.0}
MIN_BANDWIDTH = 3_000.0
MAX_BANDWIDTH = 30_000.0

MIN_FREQ = 100_000.0              # IQ files from other SDRs may be HF
MAX_FREQ = 6_000_000_000.0
MIN_FILE_RATE = 100_000.0
MAX_FILE_RATE = 40_000_000.0
MAX_NAME_LEN = 64

CTCSS_TONES = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4,
    100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5,
    141.3, 146.2, 150.0, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
    173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)

CHANNEL_COLORS = (
    "#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176",
    "#4db6ac", "#f06292", "#a1887f", "#90a4ae", "#aed581", "#7986cb",
)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- value cleaning

def clean_text(value: Any, max_len: int = MAX_NAME_LEN) -> str:
    """Printable single-line text: control/bidi characters removed, length-limited."""
    s = _CONTROL_RE.sub(" ", str(value if value is not None else ""))
    s = " ".join(s.split())
    return s[:max_len]


def clean_color(value: Any) -> str:
    s = str(value or "").strip()
    return s.lower() if _COLOR_RE.match(s) else ""


def parse_float(value: Any) -> float:
    """float() that rejects NaN/inf and booleans."""
    if isinstance(value, bool):
        raise ValueError("expected a number")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError("number must be finite")
    return v


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "x", "✓"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    raise ValueError(f"not a yes/no value: {value!r}")


def snap_ctcss(value: Any) -> Optional[float]:
    """Return the standard CTCSS tone closest to `value` (None/0/'off' = no tone)."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "off", "none", "no", "0", "csq")):
        return None
    v = parse_float(value)
    if v == 0:
        return None
    best = min(CTCSS_TONES, key=lambda t: abs(t - v))
    if abs(best - v) > 0.6:
        raise ValueError(f"{v:g} Hz is not a standard CTCSS tone")
    return best


class _Fixer:
    """Collects warnings while coercing values into range."""

    def __init__(self, warnings: Optional[list[str]], where: str):
        self.warnings = warnings if warnings is not None else []
        self.where = where

    def warn(self, msg: str) -> None:
        self.warnings.append(f"{self.where}: {msg}")

    def num(self, value: Any, default: float, lo: float, hi: float, label: str) -> float:
        try:
            v = parse_float(value)
        except (TypeError, ValueError):
            self.warn(f"invalid {label} {value!r}, using {default:g}")
            return default
        if v < lo or v > hi:
            c = min(hi, max(lo, v))
            self.warn(f"{label} {v:g} out of range, using {c:g}")
            return c
        return v

    def integer(self, value: Any, default: int, lo: int, hi: int, label: str) -> int:
        return int(round(self.num(value, default, lo, hi, label)))

    def boolean(self, value: Any, default: bool, label: str) -> bool:
        try:
            return parse_bool(value)
        except ValueError:
            self.warn(f"invalid {label} {value!r}, using {default}")
            return default


# --------------------------------------------------------------------------- data model

@dataclass
class ChannelConfig:
    """A single monitored frequency and its per-channel settings."""

    name: str = "Channel"
    frequency: float = 146_520_000.0      # Hz
    mode: str = MODE_FM                   # "FM" (narrow/wide voice FM) or "AM"
    bandwidth: float = 12_500.0           # Hz, channel filter width
    enabled: bool = True
    squelch_db: float = -50.0             # dBFS, opens at this level
    hysteresis_db: float = 3.0            # closes at squelch_db - hysteresis_db
    hang_time: float = 0.5                # seconds squelch stays open after signal drops
    ctcss_hz: Optional[float] = None      # None = carrier squelch only (FM only)
    dcs_code: Optional[str] = None        # DCS tone squelch, e.g. "023" (FM only; wins over CTCSS)
    dcs_polarity: str = "N"               # "N", "I" or "either"
    deemphasis_us: float = 750.0          # 0 disables de-emphasis (FM only)
    volume: float = 1.0                   # linear gain, 0..2
    pan: float = 0.0                      # -1 = left only, 0 = centre, +1 = right only
    mute: bool = False
    solo: bool = False
    audio_device: str = ""                # "" = system default output
    record: bool = False                  # write each transmission to WAV
    hold_time: float = DEFAULT_HOLD_TIME  # s: gaps shorter than this stay in one recording / log entry
    priority: bool = False                # other channels are ducked while this one is active
    alert: bool = False                   # pop-up / sound when this channel becomes active
    color: str = ""
    id: str = field(default_factory=_new_id)

    @classmethod
    def from_dict(cls, d: dict[str, Any], warnings: Optional[list[str]] = None) -> "ChannelConfig":
        """Build a validated channel. Raises ValueError if it has no usable frequency."""
        if not isinstance(d, dict):
            raise ValueError("channel entry is not an object")
        name = clean_text(d.get("name", "Channel")) or "Channel"
        fx = _Fixer(warnings, f"channel '{name}'")
        try:
            freq = parse_float(d.get("frequency"))
        except (TypeError, ValueError):
            raise ValueError(f"channel '{name}': missing or invalid frequency {d.get('frequency')!r}")
        if not MIN_FREQ <= freq <= MAX_FREQ:
            raise ValueError(f"channel '{name}': frequency {freq / 1e6:g} MHz is out of range")
        mode = str(d.get("mode", MODE_FM)).strip().upper()
        if mode not in MODES:
            fx.warn(f"unsupported mode {mode!r}, using FM")
            mode = MODE_FM
        ch = cls(name=name, frequency=float(round(freq)), mode=mode)
        ch.bandwidth = fx.num(d.get("bandwidth", DEFAULT_BANDWIDTH[mode]), DEFAULT_BANDWIDTH[mode],
                              MIN_BANDWIDTH, MAX_BANDWIDTH, "bandwidth")
        ch.enabled = fx.boolean(d.get("enabled", True), True, "enabled")
        ch.squelch_db = fx.num(d.get("squelch_db", -50.0), -50.0, -150.0, 0.0, "squelch")
        ch.hysteresis_db = fx.num(d.get("hysteresis_db", 3.0), 3.0, 0.0, 40.0, "hysteresis")
        ch.hang_time = fx.num(d.get("hang_time", 0.5), 0.5, 0.0, 60.0, "hang time")
        try:
            ch.ctcss_hz = snap_ctcss(d.get("ctcss_hz"))
        except (TypeError, ValueError) as e:
            fx.warn(f"{e}; tone squelch disabled")
            ch.ctcss_hz = None
        try:
            code, pol = dcs.parse_code(d.get("dcs_code") or "")
        except ValueError as e:
            fx.warn(f"{e}; DCS squelch disabled")
            code, pol = None, None
        ch.dcs_code = code
        polarity = str(d.get("dcs_polarity") or pol or "N")
        ch.dcs_polarity = polarity if polarity in dcs.POLARITIES else "N"
        if ch.mode == MODE_AM:
            ch.ctcss_hz = None
            ch.dcs_code = None
        if ch.dcs_code and ch.ctcss_hz:
            fx.warn("both CTCSS and DCS were set; using DCS")
            ch.ctcss_hz = None
        ch.deemphasis_us = fx.num(d.get("deemphasis_us", 750.0), 750.0, 0.0, 2000.0, "de-emphasis")
        ch.volume = fx.num(d.get("volume", 1.0), 1.0, 0.0, 2.0, "volume")
        ch.pan = fx.num(d.get("pan", 0.0), 0.0, -1.0, 1.0, "pan")
        ch.mute = fx.boolean(d.get("mute", False), False, "mute")
        ch.solo = fx.boolean(d.get("solo", False), False, "solo")
        ch.record = fx.boolean(d.get("record", False), False, "record")
        ch.hold_time = fx.num(d.get("hold_time", DEFAULT_HOLD_TIME), DEFAULT_HOLD_TIME, 0.0, 30.0, "hold time")
        ch.priority = fx.boolean(d.get("priority", False), False, "priority")
        ch.alert = fx.boolean(d.get("alert", False), False, "alert")
        ch.audio_device = clean_text(d.get("audio_device", ""), 256)
        ch.color = clean_color(d.get("color", ""))
        cid = str(d.get("id", "") or "")
        ch.id = cid if _ID_RE.match(cid) else _new_id()
        return ch

    def dsp_key(self) -> tuple:
        """Settings that require the channel's DSP chain to be rebuilt."""
        return (self.id, self.frequency, self.mode, self.bandwidth, self.deemphasis_us)

    def copy(self, **changes) -> "ChannelConfig":
        d = asdict(self)
        d.update(changes)
        return ChannelConfig(**d)

    def tone_text(self) -> str:
        """Human-readable tone squelch setting ('' for carrier squelch)."""
        if self.mode == MODE_AM:
            return ""
        if self.dcs_code:
            return dcs.describe(self.dcs_code, self.dcs_polarity)
        if self.ctcss_hz:
            return f"CTCSS {self.ctcss_hz:.1f}"
        return ""


@dataclass
class ChannelGroup:
    """A named channel list ("scan list") that can be monitored on its own."""

    name: str
    ids: list[str] = field(default_factory=list)


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
    def from_dict(cls, d: dict[str, Any], warnings: Optional[list[str]] = None) -> "RadioConfig":
        if not isinstance(d, dict):
            d = {}
        fx = _Fixer(warnings, "radio")
        rc = cls()
        src = str(d.get("source", SOURCE_RTLSDR))
        rc.source = src if src in SOURCE_TYPES else SOURCE_RTLSDR
        rc.device_index = fx.integer(d.get("device_index", 0), 0, 0, 31, "device index")
        host = clean_text(d.get("tcp_host", "127.0.0.1"), 253).replace(" ", "")
        rc.tcp_host = host or "127.0.0.1"
        rc.tcp_port = fx.integer(d.get("tcp_port", 1234), 1234, 1, 65535, "rtl_tcp port")
        rc.file_path = str(d.get("file_path", "") or "")[:4096]
        fc = fx.num(d.get("file_center", 0.0), 0.0, 0.0, MAX_FREQ, "file centre frequency")
        rc.file_center = fc if fc >= MIN_FREQ else 0.0
        fr = fx.num(d.get("file_rate", 0.0), 0.0, 0.0, MAX_FILE_RATE, "file sample rate")
        rc.file_rate = fr if fr >= MIN_FILE_RATE else 0.0
        rc.file_loop = fx.boolean(d.get("file_loop", True), True, "file loop")
        sr = fx.num(d.get("sample_rate", 2.4e6), 2.4e6, 0.0, 1e9, "sample rate")
        rc.sample_rate = float(min(RTL_SAMPLE_RATES, key=lambda r: abs(r - sr)))
        rc.agc = fx.boolean(d.get("agc", False), False, "AGC")
        rc.gain_db = fx.num(d.get("gain_db", 30.0), 30.0, 0.0, 60.0, "gain")
        rc.ppm = fx.integer(d.get("ppm", 0), 0, -500, 500, "PPM correction")
        rc.auto_center = fx.boolean(d.get("auto_center", True), True, "auto centre")
        rc.manual_center = fx.num(d.get("manual_center", 146e6), 146e6, MIN_FREQ, MAX_FREQ, "manual centre")
        return rc


@dataclass
class AppConfig:
    radio: RadioConfig = field(default_factory=RadioConfig)
    channels: list[ChannelConfig] = field(default_factory=list)
    recordings_dir: str = ""
    timeline_minutes: int = 10
    groups: list[ChannelGroup] = field(default_factory=list)
    active_group: str = ""                # "" = monitor all channels
    priority_duck_db: float = -15.0       # level of other channels while a priority channel is active
    alert_sound: bool = True
    alert_popup: bool = True
    # Problems found while loading (not saved).
    load_warnings: list[str] = field(default_factory=list, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "radio": asdict(self.radio),
            "channels": [asdict(c) for c in self.channels],
            "recordings_dir": self.recordings_dir,
            "timeline_minutes": self.timeline_minutes,
            "groups": [{"name": g.name, "ids": list(g.ids)} for g in self.groups],
            "active_group": self.active_group,
            "priority_duck_db": self.priority_duck_db,
            "alert_sound": self.alert_sound,
            "alert_popup": self.alert_popup,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppConfig":
        if not isinstance(d, dict):
            raise ValueError("Profile must be a JSON object")
        warnings: list[str] = []
        channels: list[ChannelConfig] = []
        raw = d.get("channels", [])
        if not isinstance(raw, list):
            warnings.append("'channels' is not a list; ignored")
            raw = []
        if len(raw) > MAX_CHANNELS:
            warnings.append(f"profile has {len(raw)} channels; only the first {MAX_CHANNELS} were loaded")
            raw = raw[:MAX_CHANNELS]
        seen: set[str] = set()
        for entry in raw:
            try:
                ch = ChannelConfig.from_dict(entry, warnings)
            except ValueError as e:
                warnings.append(f"{e} - channel skipped")
                continue
            if ch.id in seen:
                ch.id = _new_id()
            seen.add(ch.id)
            channels.append(ch)
        fx = _Fixer(warnings, "profile")
        cfg = cls(
            radio=RadioConfig.from_dict(d.get("radio", {}), warnings),
            channels=channels,
            recordings_dir=str(d.get("recordings_dir", "") or "")[:4096],
            timeline_minutes=fx.integer(d.get("timeline_minutes", 10), 10, 1, 240, "timeline minutes"),
            priority_duck_db=fx.num(d.get("priority_duck_db", -15.0), -15.0, -60.0, 0.0, "priority ducking"),
            alert_sound=fx.boolean(d.get("alert_sound", True), True, "alert sound"),
            alert_popup=fx.boolean(d.get("alert_popup", True), True, "alert pop-up"),
        )
        cfg.groups = _load_groups(d.get("groups", []), {c.id for c in channels}, warnings)
        active = clean_text(d.get("active_group", ""), MAX_GROUP_NAME)
        g = cfg.group(active) if active else None
        cfg.active_group = g.name if g is not None else ""
        cfg.load_warnings = warnings
        cfg.ensure_colors()
        return cfg

    # ------------------------------------------------------------- channel lists
    def group(self, name: str) -> Optional[ChannelGroup]:
        key = (name or "").casefold()
        for g in self.groups:
            if g.name.casefold() == key:
                return g
        return None

    def active_ids(self) -> Optional[set[str]]:
        """Ids of the active channel list, or None when all channels are monitored."""
        g = self.group(self.active_group) if self.active_group else None
        return set(g.ids) if g is not None else None

    def visible_channels(self) -> list[ChannelConfig]:
        """Channels shown in the channel panel (the active list, in channel order)."""
        ids = self.active_ids()
        return list(self.channels) if ids is None else [c for c in self.channels if c.id in ids]

    def monitored_channels(self) -> list[ChannelConfig]:
        """Channels that are received right now: enabled and in the active list."""
        ids = self.active_ids()
        return [c for c in self.channels if c.enabled and (ids is None or c.id in ids)]

    def groups_of(self, ch_id: str) -> list[str]:
        return [g.name for g in self.groups if ch_id in g.ids]

    def prune_groups(self) -> None:
        """Drop ids of channels that no longer exist."""
        ids = {c.id for c in self.channels}
        for g in self.groups:
            g.ids = [i for i in g.ids if i in ids]

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


def unique_group_name(cfg: AppConfig, wanted: str) -> str:
    base = clean_text(wanted, MAX_GROUP_NAME) or "List"
    name, n = base, 2
    while cfg.group(name) is not None:
        suffix = f" ({n})"
        name = base[: MAX_GROUP_NAME - len(suffix)] + suffix
        n += 1
    return name


def _load_groups(raw: Any, channel_ids: set[str], warnings: list[str]) -> list[ChannelGroup]:
    groups: list[ChannelGroup] = []
    if not isinstance(raw, list):
        if raw:
            warnings.append("'groups' is not a list; ignored")
        return groups
    seen: set[str] = set()
    for entry in raw[:MAX_GROUPS]:
        if not isinstance(entry, dict):
            warnings.append("invalid channel list entry skipped")
            continue
        name = clean_text(entry.get("name", ""), MAX_GROUP_NAME)
        if not name or name.casefold() in seen:
            warnings.append(f"channel list {name!r} skipped (empty or duplicate name)")
            continue
        ids_raw = entry.get("ids", [])
        ids = [i for i in ids_raw if isinstance(i, str) and i in channel_ids] if isinstance(ids_raw, list) else []
        seen.add(name.casefold())
        groups.append(ChannelGroup(name, list(dict.fromkeys(ids))))
    if isinstance(raw, list) and len(raw) > MAX_GROUPS:
        warnings.append(f"only the first {MAX_GROUPS} channel lists were loaded")
    return groups


def save_profile(cfg: AppConfig, path: str | os.PathLike) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_profile(path: str | os.PathLike) -> AppConfig:
    path = Path(path)
    if path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError(f"Profile is larger than {MAX_PROFILE_BYTES // (1024 * 1024)} MB")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise ValueError(f"Not a valid profile file: {e}") from e
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

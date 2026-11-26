"""Import / export channel lists as CSV (our own format, and CHIRP exports).

Native format - one channel per row, header required, columns in any order,
only `frequency` is mandatory (see README "Importing channels from CSV"):

    name,frequency_mhz,mode,bandwidth_khz,squelch_db,ctcss_hz,volume_pct,pan_pct,enabled
    Dispatch,460.1250,FM,12.5,-55,100.0,100,-50,yes

CHIRP exports (Location, Name, Frequency, Duplex, Tone, cToneFreq, Mode, ...) are
recognised automatically. Files are treated as untrusted input: size and row
counts are capped, every value is validated, and bad rows are skipped and
reported rather than aborting the import.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import dcs
from .activity import csv_safe
from .config import (
    DEFAULT_BANDWIDTH, MAX_FREQ, MIN_FREQ, MODE_AM, MODE_FM, ChannelConfig, parse_bool, parse_float,
)

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 2000

EXPORT_COLUMNS = [
    "name", "frequency_mhz", "mode", "bandwidth_khz", "squelch_db", "hysteresis_db", "hang_time_s",
    "hold_time_s", "ctcss_hz", "dcs", "dcs_polarity", "deemphasis_us", "volume_pct", "pan_pct", "enabled",
    "record", "priority", "alert", "audio_device", "color", "lists",
]
LIST_SEPARATOR = "|"

# normalised header -> (field, unit hint)
_ALIASES = {
    "name": ("name", None), "label": ("name", None), "alpha_tag": ("name", None),
    "alphatag": ("name", None), "channel_name": ("name", None), "channel": ("name", None),
    "frequency": ("frequency", None), "freq": ("frequency", None), "rx_frequency": ("frequency", None),
    "rx_freq": ("frequency", None), "receive_frequency": ("frequency", None),
    "frequency_mhz": ("frequency", "mhz"), "freq_mhz": ("frequency", "mhz"), "mhz": ("frequency", "mhz"),
    "rx_frequency_mhz": ("frequency", "mhz"), "frequency_khz": ("frequency", "khz"),
    "frequency_hz": ("frequency", "hz"), "freq_hz": ("frequency", "hz"),
    "mode": ("mode", None), "modulation": ("mode", None), "emission": ("mode", None),
    "bandwidth": ("bandwidth", None), "bw": ("bandwidth", None), "filter": ("bandwidth", None),
    "bandwidth_khz": ("bandwidth", "khz"), "bw_khz": ("bandwidth", "khz"),
    "bandwidth_hz": ("bandwidth", "hz"), "bw_hz": ("bandwidth", "hz"),
    "squelch": ("squelch_db", None), "squelch_db": ("squelch_db", None), "sql": ("squelch_db", None),
    "squelch_dbfs": ("squelch_db", None),
    "hysteresis": ("hysteresis_db", None), "hysteresis_db": ("hysteresis_db", None),
    "hang": ("hang_time", None), "hang_time": ("hang_time", None), "hang_time_s": ("hang_time", None),
    "hang_s": ("hang_time", None), "delay": ("hang_time", None),
    "ctcss": ("ctcss_hz", None), "ctcss_hz": ("ctcss_hz", None), "tone": ("ctcss_hz", None),
    "pl": ("ctcss_hz", None), "pl_tone": ("ctcss_hz", None), "rx_tone": ("ctcss_hz", None),
    "rx_ctcss": ("ctcss_hz", None), "ctcss_tone": ("ctcss_hz", None), "tone_squelch": ("ctcss_hz", None),
    "deemphasis": ("deemphasis_us", None), "de_emphasis": ("deemphasis_us", None),
    "deemphasis_us": ("deemphasis_us", None),
    "volume": ("volume", "pct"), "volume_pct": ("volume", "pct"), "vol": ("volume", "pct"),
    "pan": ("pan", "pct"), "pan_pct": ("pan", "pct"), "balance": ("pan", "pct"),
    "enabled": ("enabled", None), "enable": ("enabled", None), "active": ("enabled", None),
    "record": ("record", None), "rec": ("record", None),
    "audio_device": ("audio_device", None), "device": ("audio_device", None),
    "audio_output": ("audio_device", None), "output": ("audio_device", None),
    "color": ("color", None), "colour": ("color", None),
    "dcs": ("dcs", None), "dcs_code": ("dcs", None), "dtcs": ("dcs", None), "dpl": ("dcs", None),
    "cdcss": ("dcs", None), "dcs_polarity": ("dcs_polarity", None), "polarity": ("dcs_polarity", None),
    "hold": ("hold_time", None), "hold_time": ("hold_time", None), "hold_time_s": ("hold_time", None),
    "hold_s": ("hold_time", None), "record_hold": ("hold_time", None),
    "priority": ("priority", None), "prio": ("priority", None),
    "alert": ("alert", None), "notify": ("alert", None),
    "list": ("lists", None), "lists": ("lists", None), "group": ("lists", None), "groups": ("lists", None),
    "channel_list": ("lists", None), "channel_lists": ("lists", None),
}


@dataclass
class ImportResult:
    channels: list[ChannelConfig] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)     # rows that were skipped
    warnings: list[str] = field(default_factory=list)   # values that were repaired
    format: str = "native"
    rows_read: int = 0
    memberships: dict[str, list[str]] = field(default_factory=dict)   # channel id -> list names


def _norm_header(h: str) -> str:
    h = (h or "").replace("﻿", "").strip().lower()
    h = re.sub(r"[()\[\]]", " ", h)
    return re.sub(r"[\s\-/]+", "_", h).strip("_")


def _number(text: str) -> float:
    s = text.strip().replace(" ", "")
    if "," in s and "." not in s:          # decimal comma (European spreadsheets)
        s = s.replace(",", ".")
    try:
        return parse_float(s)
    except (TypeError, ValueError):
        raise ValueError(f"{text.strip()!r} is not a number") from None


def parse_frequency(value: str, hint: Optional[str] = None) -> float:
    """Parse '462.5625', '462.5625 MHz', '27185k', '146520000' ... into Hz."""
    s = str(value).strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty frequency")
    unit = hint
    for suffix, u in (("ghz", "ghz"), ("mhz", "mhz"), ("khz", "khz"), ("hz", "hz"), ("g", "ghz"),
                      ("m", "mhz"), ("k", "khz")):
        if s.endswith(suffix):
            s, unit = s[: -len(suffix)], u
            break
    v = _number(s)
    if unit is None:                      # guess from magnitude
        unit = "mhz" if v <= 6000 else ("khz" if v < 6_000_000 else "hz")
    hz = v * {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0}[unit]
    if not MIN_FREQ <= hz <= MAX_FREQ:
        raise ValueError(f"frequency {value!r} is out of range")
    return float(round(hz))


def parse_bandwidth(value: str, hint: Optional[str] = None) -> float:
    s = str(value).strip().lower().replace(" ", "")
    unit = hint
    for suffix, u in (("khz", "khz"), ("hz", "hz"), ("k", "khz")):
        if s.endswith(suffix):
            s, unit = s[: -len(suffix)], u
            break
    v = _number(s)
    if unit is None:
        unit = "khz" if v <= 100 else "hz"
    return v * (1e3 if unit == "khz" else 1.0)


def parse_mode(value: str) -> tuple[str, Optional[float]]:
    """Map a mode string to (mode, default bandwidth or None). Raises for unsupported modes."""
    m = str(value or "").strip().upper()
    if m in ("", "FM", "NFM", "FMN", "NARROW", "N"):
        return MODE_FM, (12_500.0 if m in ("NFM", "FMN", "NARROW", "N") else None)
    if m in ("WFMN",):
        return MODE_FM, 25_000.0
    if m in ("AM", "NAM", "AMN"):
        return MODE_AM, (6_000.0 if m in ("NAM", "AMN") else None)
    if m in ("WFM", "FMW", "BFM"):
        raise ValueError("broadcast FM (WFM) is not supported")
    raise ValueError(f"mode {value!r} is not supported (FM and AM only)")


def read_text_file(path: str | Path, max_bytes: int = MAX_IMPORT_BYTES) -> str:
    p = Path(path)
    if p.stat().st_size > max_bytes:
        raise ValueError(f"{p.name} is larger than {max_bytes // 1024} KB")
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _delimiter(header_line: str) -> str:
    """Pick the delimiter from the header row (data rows may contain decimal commas)."""
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: (counts[d], d == ","))
    return best if counts[best] else ","


def parse_channels_csv(text: str, default_squelch: float = -50.0) -> ImportResult:
    res = ImportResult()
    if len(text) > MAX_IMPORT_BYTES:
        raise ValueError("CSV text is too large")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise ValueError("The file contains no rows")
    body = "\n".join(lines)
    reader = csv.reader(io.StringIO(body), delimiter=_delimiter(lines[0]))
    try:
        header = next(reader)
    except (StopIteration, csv.Error) as e:
        raise ValueError(f"Cannot read CSV header: {e}") from e
    keys = [_norm_header(h) for h in header]
    chirp = "location" in keys and "frequency" in keys and ("duplex" in keys or "ctonefreq" in keys)
    if chirp:
        res.format = "chirp"
    elif not any(_ALIASES.get(k, (None,))[0] == "frequency" for k in keys):
        raise ValueError("No frequency column found. Expected a header such as "
                         "'name,frequency_mhz,mode,...' or a CHIRP export.")
    try:
        for line_no, row in enumerate(reader, start=2):
            if res.rows_read >= MAX_IMPORT_ROWS:
                res.errors.append(f"stopped after {MAX_IMPORT_ROWS} rows")
                break
            if not any(c.strip() for c in row):
                continue
            res.rows_read += 1
            cells = {k: (row[i].strip() if i < len(row) else "") for i, k in enumerate(keys)}
            try:
                d = _chirp_row(cells, res, line_no) if chirp else _native_row(cells, res, line_no)
            except (ValueError, TypeError) as e:
                res.errors.append(f"line {line_no}: {e}")
                continue
            lists = d.pop("_lists", [])
            d.setdefault("squelch_db", default_squelch)
            try:
                ch = ChannelConfig.from_dict(d, res.warnings)
            except ValueError as e:
                res.errors.append(f"line {line_no}: {e}")
                continue
            res.channels.append(ch)
            if lists:
                res.memberships[ch.id] = lists
    except csv.Error as e:
        res.errors.append(f"CSV error: {e}")
    return res


def _parse_dcs(text: str, polarity: str, res: ImportResult, line_no: int, out: dict) -> None:
    try:
        code, pol = dcs.parse_code(text)
    except ValueError as e:
        res.warnings.append(f"line {line_no}: {e}; DCS squelch not set")
        return
    if code:
        p = (polarity or "").strip().upper()
        p = {"N": "N", "NORMAL": "N", "I": "I", "R": "I", "INV": "I", "INVERTED": "I",
             "EITHER": "either", "BOTH": "either", "ANY": "either"}.get(p, pol or "N")
        out["dcs_code"], out["dcs_polarity"] = code, p


def _native_row(cells: dict[str, str], res: ImportResult, line_no: int) -> dict:
    d: dict = {}
    hints: dict[str, Optional[str]] = {}
    for k, v in cells.items():
        if k not in _ALIASES or v == "":
            continue
        fname, hint = _ALIASES[k]
        if fname in d:                     # first matching column wins
            continue
        d[fname], hints[fname] = v, hint
    if "frequency" not in d:
        raise ValueError("no frequency")
    out: dict = {"frequency": parse_frequency(d["frequency"], hints.get("frequency"))}
    mode, mode_bw = parse_mode(d.get("mode", "FM"))
    out["mode"] = mode
    if "bandwidth" in d:
        out["bandwidth"] = parse_bandwidth(d["bandwidth"], hints.get("bandwidth"))
    else:
        out["bandwidth"] = mode_bw or DEFAULT_BANDWIDTH[mode]
    out["name"] = d.get("name") or f"{out['frequency'] / 1e6:.4f} MHz"
    for fname in ("squelch_db", "hysteresis_db", "hang_time", "hold_time", "deemphasis_us"):
        if fname in d and d[fname].strip().lower() not in ("off", "none"):
            out[fname] = _number(d[fname])
    if "ctcss_hz" in d:
        out["ctcss_hz"] = d["ctcss_hz"]
    if "volume" in d:
        out["volume"] = _number(d["volume"].rstrip("%")) / 100.0
    if "pan" in d:
        out["pan"] = _number(d["pan"].rstrip("%")) / 100.0
    for fname in ("enabled", "record", "priority", "alert"):
        if fname in d:
            out[fname] = parse_bool(d[fname])
    if "dcs" in d:
        _parse_dcs(d["dcs"], d.get("dcs_polarity", ""), res, line_no, out)
        if out.get("dcs_code"):
            out.pop("ctcss_hz", None)
    if "lists" in d:
        out["_lists"] = [n.strip() for n in d["lists"].split(LIST_SEPARATOR) if n.strip()][:16]
    for fname in ("audio_device", "color"):
        if fname in d:
            out[fname] = d[fname]
    if "deemphasis_us" in d and d["deemphasis_us"].lower() in ("off", "none"):
        out["deemphasis_us"] = 0.0
    return out


def _chirp_row(cells: dict[str, str], res: ImportResult, line_no: int) -> dict:
    freq = cells.get("frequency", "")
    if not freq:
        raise ValueError("no frequency")
    out: dict = {"frequency": parse_frequency(freq, "mhz")}
    mode, bw = parse_mode(cells.get("mode", "FM") or "FM")
    out["mode"] = mode
    out["bandwidth"] = bw or (25_000.0 if mode == MODE_FM else DEFAULT_BANDWIDTH[MODE_AM])
    name = cells.get("name", "")
    out["name"] = name or f"Mem {cells.get('location', '?')}"
    tone_mode = cells.get("tone", "").strip().lower()
    # DtcsPolarity is two letters: transmit then receive; R = reversed (inverted).
    pol = (cells.get("dtcspolarity", "") or "NN").strip().upper()
    rx_pol = pol[1] if len(pol) >= 2 else "N"
    if mode == MODE_FM:
        if tone_mode == "tsql":
            out["ctcss_hz"] = cells.get("ctonefreq") or cells.get("rtonefreq")
        elif tone_mode == "dtcs":
            _parse_dcs(cells.get("dtcscode", ""), rx_pol, res, line_no, out)
        elif tone_mode == "cross":
            parts = cells.get("crossmode", "").lower().split("->")
            rx = parts[1].strip() if len(parts) == 2 else ""
            if rx == "tone":
                out["ctcss_hz"] = cells.get("ctonefreq")
            elif rx == "dtcs":
                _parse_dcs(cells.get("rxdtcscode") or cells.get("dtcscode", ""), rx_pol, res, line_no, out)
    if cells.get("skip", "").strip().upper() == "S":
        out["enabled"] = False
    return out


def import_channels_csv(path: str | Path, default_squelch: float = -50.0) -> ImportResult:
    return parse_channels_csv(read_text_file(path), default_squelch)


def channel_row(ch: ChannelConfig, lists: Iterable[str] = ()) -> list[str]:
    yn = {True: "yes", False: "no"}
    return [
        ch.name, f"{ch.frequency / 1e6:.6f}".rstrip("0").rstrip("."), ch.mode,
        f"{ch.bandwidth / 1e3:g}", f"{ch.squelch_db:g}", f"{ch.hysteresis_db:g}", f"{ch.hang_time:g}",
        f"{ch.hold_time:g}", f"{ch.ctcss_hz:.1f}" if ch.ctcss_hz and not ch.dcs_code else "",
        ch.dcs_code or "", ch.dcs_polarity if ch.dcs_code else "", f"{ch.deemphasis_us:g}",
        f"{ch.volume * 100:g}", f"{ch.pan * 100:g}", yn[ch.enabled], yn[ch.record], yn[ch.priority],
        yn[ch.alert], ch.audio_device, ch.color, LIST_SEPARATOR.join(lists),
    ]


def export_channels_csv(channels: Iterable[ChannelConfig], path: str | Path,
                        groups_of: Optional[Callable[[str], list[str]]] = None) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for ch in channels:
            lists = groups_of(ch.id) if groups_of else []
            w.writerow([csv_safe(v) for v in channel_row(ch, lists)])
            n += 1
    return n

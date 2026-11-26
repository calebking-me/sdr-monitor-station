"""IQ sample sources: RTL-SDR USB dongle, rtl_tcp server and IQ files."""

from __future__ import annotations

import json
import re
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .config import SOURCE_FILE, SOURCE_RTLSDR, SOURCE_RTLTCP, RadioConfig
from .dsp import u8_to_complex

CHUNK_SAMPLES = 131_072   # ~55 ms at 2.4 MS/s; multiple of 256 as librtlsdr requires


class SourceError(Exception):
    """Recoverable source error (device unplugged, connection lost)."""


class EndOfStream(Exception):
    """A file source reached its end and is not looping."""


class IQSource:
    tunable = True
    name = "source"

    def __init__(self):
        self.sample_rate = 0.0
        self.center = 0.0

    def open(self, center: float) -> None: ...
    def read(self, n: int) -> np.ndarray: ...
    def close(self) -> None: ...
    def set_center(self, center: float) -> None: ...
    def set_gain(self, agc: bool, gain_db: float) -> None: ...
    def set_ppm(self, ppm: int) -> None: ...

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------- RTL-SDR

def list_rtlsdr_devices() -> list[str]:
    """Return a label per attached dongle, or raise ImportError if librtlsdr is missing."""
    import importlib
    from ctypes import c_ubyte
    lib = importlib.import_module("rtlsdr.librtlsdr").librtlsdr   # loads the DLL
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        name = lib.rtlsdr_get_device_name(i)
        name = name.decode(errors="replace") if isinstance(name, bytes) else str(name)
        serial = (c_ubyte * 256)()
        r = lib.rtlsdr_get_device_usb_strings(i, None, None, serial)
        sn = "".join(chr(b) for b in serial if b) if r == 0 else "?"
        out.append(f"{i}: {name} (SN {sn})")
    return out


class RtlSdrSource(IQSource):
    name = "RTL-SDR"

    def __init__(self, cfg: RadioConfig):
        super().__init__()
        self.cfg = cfg
        self._sdr = None

    def open(self, center: float) -> None:
        try:
            from rtlsdr import RtlSdr
        except ImportError as e:  # librtlsdr DLL / .so not found
            raise RuntimeError(
                "librtlsdr could not be loaded. On Windows install 'pyrtlsdr[lib]' or put "
                "librtlsdr.dll (and libusb-1.0.dll) next to the program or on PATH. "
                f"Details: {e}") from e
        try:
            sdr = RtlSdr(device_index=self.cfg.device_index)
        except Exception as e:
            raise SourceError(f"Cannot open RTL-SDR #{self.cfg.device_index}: {e}") from e
        self._sdr = sdr
        sdr.sample_rate = self.cfg.sample_rate
        if self.cfg.ppm:
            sdr.freq_correction = int(self.cfg.ppm)
        sdr.center_freq = center
        self.set_gain(self.cfg.agc, self.cfg.gain_db)
        self.sample_rate = float(sdr.sample_rate)
        self.center = float(sdr.center_freq)
        try:
            sdr.read_bytes(2 * 16384)   # flush stale samples from the USB buffers
        except Exception:
            pass

    def read(self, n: int) -> np.ndarray:
        if self._sdr is None:
            raise SourceError("RTL-SDR not open")
        try:
            buf = self._sdr.read_bytes(2 * n)
        except Exception as e:
            self._sdr = None
            raise SourceError(f"RTL-SDR read failed (device unplugged?): {e}") from e
        return u8_to_complex(np.frombuffer(buf, dtype=np.uint8).copy())

    def close(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.close()
            except Exception:
                pass
            self._sdr = None

    def set_center(self, center: float) -> None:
        if self._sdr is not None:
            self._sdr.center_freq = center
            self.center = float(self._sdr.center_freq)

    def set_gain(self, agc: bool, gain_db: float) -> None:
        if self._sdr is None:
            return
        if agc:
            self._sdr.gain = "auto"
        else:
            self._sdr.gain = float(gain_db)

    def set_ppm(self, ppm: int) -> None:
        if self._sdr is not None and int(ppm) != 0:
            try:
                self._sdr.freq_correction = int(ppm)
            except Exception:
                pass

    def describe(self) -> str:
        return f"RTL-SDR #{self.cfg.device_index}"


# --------------------------------------------------------------------------- rtl_tcp

class RtlTcpSource(IQSource):
    """Minimal client for the rtl_tcp protocol (unsigned 8-bit IQ stream)."""

    name = "rtl_tcp"
    CMD_FREQ, CMD_RATE, CMD_GAIN_MODE, CMD_GAIN, CMD_PPM, CMD_AGC = 1, 2, 3, 4, 5, 8

    def __init__(self, cfg: RadioConfig):
        super().__init__()
        self.cfg = cfg
        self._sock: Optional[socket.socket] = None
        self.tuner = 0

    def open(self, center: float) -> None:
        try:
            s = socket.create_connection((self.cfg.tcp_host, int(self.cfg.tcp_port)), timeout=5)
            s.settimeout(5)
            hdr = self._recv_exact(s, 12)
        except OSError as e:
            raise SourceError(f"Cannot connect to rtl_tcp at {self.cfg.tcp_host}:{self.cfg.tcp_port}: {e}") from e
        if hdr[:4] != b"RTL0":
            s.close()
            raise SourceError("Server did not send an rtl_tcp header")
        self.tuner = struct.unpack(">I", hdr[4:8])[0]
        self._sock = s
        self._cmd(self.CMD_RATE, int(self.cfg.sample_rate))
        self._cmd(self.CMD_PPM, int(self.cfg.ppm))
        self.set_center(center)
        self.set_gain(self.cfg.agc, self.cfg.gain_db)
        self.sample_rate = float(int(self.cfg.sample_rate))

    @staticmethod
    def _recv_exact(s: socket.socket, nbytes: int) -> bytearray:
        buf = bytearray(nbytes)
        view = memoryview(buf)
        got = 0
        while got < nbytes:
            r = s.recv_into(view[got:])
            if r == 0:
                raise OSError("connection closed by server")
            got += r
        return buf

    def _cmd(self, cmd: int, value: int) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))
        except OSError as e:
            raise SourceError(f"rtl_tcp command failed: {e}") from e

    def read(self, n: int) -> np.ndarray:
        if self._sock is None:
            raise SourceError("rtl_tcp not connected")
        try:
            buf = self._recv_exact(self._sock, 2 * n)
        except OSError as e:
            self.close()
            raise SourceError(f"rtl_tcp connection lost: {e}") from e
        return u8_to_complex(np.frombuffer(buf, dtype=np.uint8))

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def set_center(self, center: float) -> None:
        self._cmd(self.CMD_FREQ, int(round(center)))
        self.center = float(int(round(center)))

    def set_gain(self, agc: bool, gain_db: float) -> None:
        if agc:
            self._cmd(self.CMD_GAIN_MODE, 0)
        else:
            self._cmd(self.CMD_GAIN_MODE, 1)
            self._cmd(self.CMD_GAIN, int(round(gain_db * 10)))

    def set_ppm(self, ppm: int) -> None:
        self._cmd(self.CMD_PPM, int(ppm))

    def describe(self) -> str:
        return f"rtl_tcp {self.cfg.tcp_host}:{self.cfg.tcp_port}"


# --------------------------------------------------------------------------- files

RAW_FORMATS = {
    # extension -> (numpy dtype of one component, scale to +-1, offset)
    ".cu8": (np.uint8, 1 / 128.0, 127.4),
    ".u8": (np.uint8, 1 / 128.0, 127.4),
    ".bin": (np.uint8, 1 / 128.0, 127.4),     # rtl_sdr default output
    ".cs8": (np.int8, 1 / 128.0, 0.0),
    ".cs16": (np.int16, 1 / 32768.0, 0.0),
    ".cf32": (np.float32, 1.0, 0.0),
    ".cfile": (np.float32, 1.0, 0.0),
    ".fc32": (np.float32, 1.0, 0.0),
    ".raw": (np.float32, 1.0, 0.0),
}

SIGMF_TYPES = {
    "cu8": (np.uint8, 1 / 128.0, 127.4),
    "ci8": (np.int8, 1 / 128.0, 0.0),
    "ci16_le": (np.int16, 1 / 32768.0, 0.0),
    "cf32_le": (np.float32, 1.0, 0.0),
}

_HZ_RE = re.compile(r"(\d{5,11})\s*Hz", re.IGNORECASE)


@dataclass
class FileInfo:
    path: Path
    kind: str                     # "sigmf", "wav" or "raw"
    center: Optional[float]
    sample_rate: Optional[float]
    dtype: object = np.uint8
    scale: float = 1 / 128.0
    offset: float = 127.4
    data_path: Optional[Path] = None


def inspect_iq_file(path: str | Path) -> FileInfo:
    """Work out format, centre frequency and sample rate of an IQ recording."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".sigmf-meta", ".sigmf-data"):
        meta_path = Path(sigmf_base(p) + ".sigmf-meta")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        g = meta.get("global", {})
        dt = g.get("core:datatype", "cu8")
        if dt not in SIGMF_TYPES:
            raise ValueError(f"Unsupported SigMF datatype {dt!r}")
        caps = meta.get("captures") or [{}]
        dtype, scale, offset = SIGMF_TYPES[dt]
        return FileInfo(p, "sigmf", caps[0].get("core:frequency"), g.get("core:sample_rate"),
                        dtype, scale, offset, Path(sigmf_base(p) + ".sigmf-data"))

    center = None
    m = _HZ_RE.search(p.name)
    if m:
        center = float(m.group(1))
    if suffix == ".wav":
        from scipy.io import wavfile
        rate, data = wavfile.read(str(p), mmap=True)
        if data.ndim != 2 or data.shape[1] != 2:
            raise ValueError("WAV IQ files must have two channels (I and Q)")
        return FileInfo(p, "wav", center, float(rate), data.dtype, 1.0, 0.0, p)
    if suffix not in RAW_FORMATS:
        raise ValueError(f"Unknown IQ file type {suffix!r}")
    dtype, scale, offset = RAW_FORMATS[suffix]
    rate = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(M|k)sps", p.name, re.IGNORECASE)
    if m:
        rate = float(m.group(1)) * (1e6 if m.group(2).lower() == "m" else 1e3)
    return FileInfo(p, "raw", center, rate, dtype, scale, offset, p)


class FileSource(IQSource):
    """Plays an IQ recording back in real time (optionally looping)."""

    tunable = False
    name = "IQ file"

    def __init__(self, cfg: RadioConfig, realtime: bool = True):
        super().__init__()
        self.cfg = cfg
        self.realtime = realtime
        self._data = None
        self._pos = 0
        self._t0 = 0.0
        self._sent = 0
        self.info: Optional[FileInfo] = None

    def open(self, center: float) -> None:
        if not self.cfg.file_path:
            raise RuntimeError("No IQ file selected (Settings > Source)")
        p = Path(self.cfg.file_path)
        if not p.exists():
            raise RuntimeError(f"IQ file not found: {p}")
        info = inspect_iq_file(p)
        self.info = info
        self.center = float(info.center or self.cfg.file_center or 0.0)
        self.sample_rate = float(info.sample_rate or self.cfg.file_rate or 0.0)
        if self.center <= 0 or self.sample_rate <= 0:
            raise RuntimeError(
                "The IQ file's centre frequency / sample rate are unknown. "
                "Enter them in Settings > Source.")
        if info.kind == "wav":
            from scipy.io import wavfile
            _, data = wavfile.read(str(p), mmap=True)
            self._data = data.reshape(-1)
            if data.dtype == np.uint8:
                self._scale, self._offset = 1 / 128.0, 128.0
            elif data.dtype == np.int16:
                self._scale, self._offset = 1 / 32768.0, 0.0
            elif data.dtype == np.int32:
                self._scale, self._offset = 1 / 2147483648.0, 0.0
            else:
                self._scale, self._offset = 1.0, 0.0
        else:
            self._data = np.memmap(info.data_path, dtype=info.dtype, mode="r")
            self._scale, self._offset = info.scale, info.offset
        usable = len(self._data) - (len(self._data) % 2)
        self._data = self._data[:usable]
        if usable < 2:
            raise RuntimeError("IQ file is empty")
        self._pos = 0
        self._t0 = time.monotonic()
        self._sent = 0

    def read(self, n: int) -> np.ndarray:
        if self._data is None:
            raise EndOfStream()
        need = 2 * n
        parts = []
        while need > 0:
            remaining = len(self._data) - self._pos
            if remaining <= 0:
                if not self.cfg.file_loop:
                    if parts:
                        break
                    raise EndOfStream()
                self._pos = 0
                remaining = len(self._data)
            take = min(need, remaining)
            parts.append(np.asarray(self._data[self._pos:self._pos + take]))
            self._pos += take
            need -= take
        raw = np.concatenate(parts).astype(np.float32)
        if self._offset:
            raw -= self._offset
        raw *= self._scale
        x = raw.view(np.complex64)
        if self.realtime:
            self._sent += len(x)
            due = self._t0 + self._sent / self.sample_rate
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:      # fell far behind: resync rather than burst
                self._t0 = time.monotonic() - self._sent / self.sample_rate
        return x

    def close(self) -> None:
        self._data = None

    def describe(self) -> str:
        return f"File {Path(self.cfg.file_path).name}"


def make_source(cfg: RadioConfig) -> IQSource:
    if cfg.source == SOURCE_RTLSDR:
        return RtlSdrSource(cfg)
    if cfg.source == SOURCE_RTLTCP:
        return RtlTcpSource(cfg)
    if cfg.source == SOURCE_FILE:
        return FileSource(cfg)
    raise ValueError(f"Unknown source type {cfg.source!r}")


def sigmf_base(base: str | Path) -> str:
    s = str(base)
    for suf in (".sigmf-data", ".sigmf-meta"):
        if s.lower().endswith(suf):
            return s[: -len(suf)]
    return s


def write_sigmf(base: str | Path, iq_u8: np.ndarray, sample_rate: float, center: float,
                description: str = "") -> Path:
    """Write a cu8 SigMF recording (data + meta). Returns the .sigmf-data path."""
    data_path = Path(sigmf_base(base) + ".sigmf-data")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    iq_u8.astype(np.uint8).tofile(data_path)
    write_sigmf_meta(base, sample_rate, center, description)
    return data_path


def write_sigmf_meta(base: str | Path, sample_rate: float, center: float, description: str = "") -> Path:
    meta = {
        "global": {
            "core:datatype": "cu8",
            "core:sample_rate": float(sample_rate),
            "core:version": "1.0.0",
            "core:description": description,
            "core:recorder": "sdrscanner",
        },
        "captures": [{"core:sample_start": 0, "core:frequency": float(center)}],
        "annotations": [],
    }
    p = Path(sigmf_base(base) + ".sigmf-meta")
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return p

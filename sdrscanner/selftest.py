"""`sdrscanner --self-test`: verify an installation (or a frozen .exe) works.

Checks the DSP chain, audio library, librtlsdr loading, the Qt GUI and an
end-to-end IQ playback run. No dongle or sound card is required; a missing
dongle is reported, not treated as a failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

from . import __version__


def _dsp() -> str:
    import numpy as np
    from .config import ChannelConfig
    from .dsp import AUDIO_RATE, Channelizer, ChannelProcessor
    fs, off = 2_400_000, 300_000.0
    t = np.arange(fs) / fs
    x = (0.05 * np.exp(1j * 2 * np.pi * np.cumsum(off + 2500 * np.sin(2 * np.pi * 1000 * t)) / fs)).astype(np.complex64)
    chz = Channelizer(fs, 0)
    p = ChannelProcessor(chz, ChannelConfig(frequency=off, squelch_db=-60, deemphasis_us=0))
    out = [p.process(X, i, chz).audio for i, X in chz.frames(x)]
    y = np.concatenate(out)[AUDIO_RATE // 4:]
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1 / AUDIO_RATE)
    near = np.abs(f - 1000) < 20
    sinad = 10 * np.log10(Y[near].sum() / Y[~near].sum())
    if sinad < 40:
        raise RuntimeError(f"FM demodulator SINAD too low: {sinad:.1f} dB")
    am_x = (0.05 * (1 + 0.6 * np.sin(2 * np.pi * 1000 * t)) * np.exp(2j * np.pi * -off * t)).astype(np.complex64)
    chz = Channelizer(fs, 0)
    p = ChannelProcessor(chz, ChannelConfig(frequency=-off, mode="AM", bandwidth=8000, squelch_db=-60))
    y = np.concatenate([p.process(X, i, chz).audio for i, X in chz.frames(am_x)])[AUDIO_RATE // 4:]
    level = float(np.std(y) * np.sqrt(2))
    if not 0.25 < level < 0.35:
        raise RuntimeError(f"AM demodulator level wrong: {level:.3f}")
    from .dsp import ChannelBank
    from .tools.gen_iq import dcs_waveform
    t = np.arange(int(fs * 1.6)) / fs
    dx = (0.05 * np.exp(1j * 2 * np.pi * np.cumsum(off + 2500 * dcs_waveform("023", False, t)) / fs)
          ).astype(np.complex64)
    chz = Channelizer(fs, 0)
    bank = ChannelBank(chz, [ChannelConfig(frequency=off, squelch_db=-60)])
    code = None
    for i, X in chz.frames(dx):
        code = bank.process(X, i).dcs[0] or code
    if code != "023N":
        raise RuntimeError(f"DCS decoder returned {code!r} for DCS 023")
    return (f"FM 1 kHz test tone SINAD {sinad:.1f} dB; AM level {level:.2f} (expected 0.30); "
            f"DCS 023 decoded")


def _audio() -> str:
    from .audio import audio_available, list_output_devices
    ok, err = audio_available()
    if not ok:
        raise RuntimeError(f"PortAudio not available: {err}")
    import sounddevice as sd
    return f"{sd.get_portaudio_version()[1]}; {len(list_output_devices())} output device(s)"


def _librtlsdr() -> str:
    from .native import librtlsdr_description
    from .sources import list_rtlsdr_devices
    desc = librtlsdr_description()
    devs = list_rtlsdr_devices()
    return f"{desc}; {len(devs)} dongle(s) attached" + (f": {', '.join(devs)}" if devs else "")


def _scanning() -> str:
    """Scanning between two sets 316 MHz apart with a simulated, tunable radio."""
    from .config import AppConfig, ChannelConfig
    from .engine import Engine
    from .tools.sim_source import SimTransmitter, SimulatedSource
    src = SimulatedSource([SimTransmitter(462.5625e6, -40, bursts=[(0.3, 10.0)])])
    cfg = AppConfig(channels=[ChannelConfig(name="A", frequency=146.52e6, squelch_db=-60),
                              ChannelConfig(name="B", frequency=462.5625e6, squelch_db=-60)])
    cfg.scan.enabled = True
    cfg.scan.listen_time = 0.2
    eng = Engine(cfg)
    eng.source_factory = lambda _cfg: src
    eng.start()
    visited, opened = set(), False
    try:
        deadline = time.time() + 8
        while time.time() < deadline and not opened:
            snap = eng.poll()
            if snap.scan is not None and snap.scan.state != "single":
                visited.add(snap.scan.index)
            st = snap.channels.get(cfg.channels[1].id)
            opened = bool(st and st.open)
            time.sleep(0.02)
    finally:
        eng.stop()
    if not opened or len(visited) < 2:
        raise RuntimeError(f"scanner did not stop on the active set (visited {sorted(visited)}, "
                           f"{len(src.retunes)} retunes)")
    return f"2 scan sets, {len(src.retunes)} retune(s), stopped on the active channel"


def _gui_and_playback() -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    from PySide6.QtWidgets import QApplication
    from .gui.main_window import MainWindow
    from .gui.view import ViewSettings
    from .tools.gen_iq import SynthChannel, generate, make_profile

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        fs, center = 2_400_000.0, 460e6
        chans = [SynthChannel("A", -200_000, -40, bursts=[(0.2, 1.6)]),
                 SynthChannel("B", 250_000, -45, ctcss=100.0, bursts=[(0.3, 1.8)])]
        data, chans = generate(str(Path(tmp) / "selftest"), 2.2, center, fs, channels=chans)
        cfg = make_profile(data, chans, center, fs)
        cfg.radio.file_loop = False
        win = MainWindow(cfg, view=ViewSettings(), persist_view=False)   # never touch user settings
        win._autosave = False
        win.show()
        win.toggle_running()
        deadline = time.time() + 20
        while time.time() < deadline and (win.engine.running or len(win.events) < 2):
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
        win._tick()
        names = sorted({e.channel_name for e in win.events})
        tones = sorted({e.tone for e in win.events if e.tone})
        win.engine.stop()
        win.close()
    if names != ["A", "B"] or tones != [100.0]:
        raise RuntimeError(f"unexpected playback result: channels={names} tones={tones}")
    return f"GUI started ({app.platformName()}), IQ playback detected channels {names}, CTCSS {tones}"


def run(report_path: str | None = None) -> int:
    lines = [f"SDR Scanner {__version__} self-test",
             f"Python {sys.version.split()[0]} on {sys.platform}, frozen={bool(getattr(sys, 'frozen', False))}"]
    failed = 0
    checks: list[tuple[str, Callable[[], str], bool]] = [
        ("DSP", _dsp, True),
        ("Audio (PortAudio)", _audio, True),
        ("librtlsdr", _librtlsdr, True),
        ("Scanning", _scanning, True),
        ("GUI + IQ playback", _gui_and_playback, True),
    ]
    for name, fn, required in checks:
        try:
            lines.append(f"PASS  {name}: {fn()}")
        except Exception as e:
            failed += required
            lines.append(f"FAIL  {name}: {e}")
            lines.extend("      " + ln for ln in traceback.format_exc().rstrip().splitlines()[-6:])
    lines.append("RESULT: " + ("OK" if not failed else f"{failed} check(s) failed"))
    text = "\n".join(lines) + "\n"
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
    return 1 if failed else 0

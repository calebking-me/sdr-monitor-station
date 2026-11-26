"""Generate a synthetic multi-channel FM/AM IQ recording (SigMF cu8) for testing.

    python -m sdrscanner.tools.gen_iq demo --seconds 60

Creates demo.sigmf-data / demo.sigmf-meta with several FM channels and one AM
channel that key up in bursts with voice-like audio; some FM channels carry CTCSS
tones. A matching profile
(demo_profile.json) is written so the GUI can be tried without hardware.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

from ..config import MODE_AM, MODE_FM, AppConfig, ChannelConfig, RadioConfig, SOURCE_FILE, save_profile
from ..dsp import complex_to_u8
from ..sources import write_sigmf_meta, sigmf_base

AUDIO_FS = 16_000
_VOICE_HP = signal.butter(6, [300, 3000], btype="bandpass", fs=AUDIO_FS, output="sos")


@dataclass
class SynthChannel:
    name: str
    offset: float                  # Hz from centre
    level_db: float                # carrier level, dBFS
    deviation: float = 2_500.0
    ctcss: float | None = None
    bursts: list[tuple[float, float]] = field(default_factory=list)   # (start, end) seconds
    pitch: float = 140.0
    mode: str = MODE_FM
    am_depth: float = 0.8          # AM modulation index at full voice level


def default_channels(seconds: float, rng: np.random.Generator) -> list[SynthChannel]:
    chans = [
        SynthChannel("Dispatch", -612_500, -35, ctcss=100.0, pitch=120),
        SynthChannel("Fire", -387_500, -45, ctcss=156.7, pitch=170),
        SynthChannel("Public works", -150_000, -50, pitch=110),
        SynthChannel("Simplex", 212_500, -40, pitch=200),
        SynthChannel("Repeater out", 437_500, -55, ctcss=88.5, pitch=135),
        SynthChannel("Wideband 25k", 662_500, -42, deviation=5_000, pitch=150),
        SynthChannel("AM test", 300_000, -45, pitch=125, mode=MODE_AM),
    ]
    for i, c in enumerate(chans):
        t = 1.0 + i * 1.3
        while t < seconds - 1:
            dur = rng.uniform(1.5, 5.0)
            c.bursts.append((t, min(seconds - 0.2, t + dur)))
            t += dur + rng.uniform(3.0, 14.0)
    return chans


def voice_like(t: np.ndarray, pitch: float, rng: np.random.Generator) -> np.ndarray:
    """Crude vowel-ish signal: harmonic series with moving formants and syllable envelope."""
    f0 = pitch * (1 + 0.08 * np.sin(2 * np.pi * 0.7 * t))
    phase = 2 * np.pi * np.cumsum(f0) / AUDIO_FS
    formant = 700 + 500 * np.sin(2 * np.pi * 1.3 * t + rng.uniform(0, 6))
    sig = np.zeros_like(t)
    for h in range(1, 20):
        fh = h * f0
        weight = np.exp(-((fh - formant) / 500.0) ** 2) + 0.3 * np.exp(-((fh - 2200) / 400.0) ** 2)
        sig += weight * np.sin(h * phase)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t + rng.uniform(0, 6)) ** 2
    sig *= env
    # Radios high-pass voice at 300 Hz so it does not interfere with CTCSS.
    sig = signal.sosfiltfilt(_VOICE_HP, sig)
    return sig / (np.max(np.abs(sig)) + 1e-9)


def generate(path: str, seconds: float = 60.0, center: float = 460_000_000.0,
             fs: float = 2_400_000.0, seed: int = 1, noise_dbfs: float = -45.0,
             channels: list[SynthChannel] | None = None) -> tuple[Path, list[SynthChannel]]:
    rng = np.random.default_rng(seed)
    chans = channels if channels is not None else default_channels(seconds, rng)
    base = sigmf_base(path)
    out = Path(base + ".sigmf-data")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_sigmf_meta(base, fs, center, "sdrscanner synthetic test signal")
    phases = {c.name: 0.0 for c in chans}
    carrier_ph = {c.name: rng.uniform(0, 2 * np.pi) for c in chans}
    block_s = 0.5
    n_blocks = int(np.ceil(seconds / block_s))
    noise_amp = 10 ** (noise_dbfs / 20) / np.sqrt(2)
    with open(out, "wb") as f:
        for b in range(n_blocks):
            t0 = b * block_s
            n = int(round(min(block_s, seconds - t0) * fs))
            if n <= 0:
                break
            t = t0 + np.arange(n) / fs
            x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * noise_amp
            x += 0.004 + 0.003j          # small DC offset like a real dongle
            ta = t0 + np.arange(int(n * AUDIO_FS / fs) + 2) / AUDIO_FS
            for c in chans:
                on_a = np.zeros(len(ta), dtype=bool)
                for s, e in c.bursts:
                    on_a |= (ta >= s) & (ta < e)
                if not on_a.any():
                    continue
                audio = 0.8 * voice_like(ta, c.pitch, rng)
                if c.ctcss and c.mode == MODE_FM:
                    audio = 0.85 * audio + 0.15 * np.sin(2 * np.pi * c.ctcss * ta)
                audio *= on_a
                a = np.interp(t, ta, audio)
                on = np.interp(t, ta, on_a.astype(float)) > 0.5
                amp = 10 ** (c.level_db / 20)
                if c.mode == MODE_AM:
                    ph = phases[c.name] + 2 * np.pi * c.offset / fs * np.arange(1, n + 1)
                    env = amp * (1.0 + c.am_depth * a)
                else:
                    inst = 2 * np.pi * (c.offset + c.deviation * a) / fs
                    ph = phases[c.name] + np.cumsum(inst)
                    env = amp
                phases[c.name] = float(ph[-1] % (2 * np.pi))
                x += on * env * np.exp(1j * (ph + carrier_ph[c.name]))
            f.write(complex_to_u8(x.astype(np.complex64)).tobytes())
    return out, chans


def make_profile(data_path: Path, chans: list[SynthChannel], center: float, fs: float) -> AppConfig:
    cfg = AppConfig(radio=RadioConfig(source=SOURCE_FILE, file_path=str(data_path.resolve()),
                                      file_center=center, file_rate=fs, file_loop=True))
    pans = [-0.8, 0.8, 0.0, -0.4, 0.4, 0.0, 0.6]
    for i, c in enumerate(chans):
        if c.mode == MODE_AM:
            bw = 8_000.0
        else:
            bw = 12_500.0 if c.deviation <= 2_500 else 25_000.0
        cfg.channels.append(ChannelConfig(
            name=c.name, frequency=center + c.offset, mode=c.mode, bandwidth=bw,
            squelch_db=-60.0, ctcss_hz=c.ctcss if c.mode == MODE_FM else None,
            pan=pans[i % len(pans)]))
    cfg.ensure_colors()
    return cfg


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", help="output base name (writes .sigmf-data/.sigmf-meta)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--center", type=float, default=460.0, help="centre frequency, MHz")
    ap.add_argument("--rate", type=float, default=2.4, help="sample rate, MS/s")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    center, fs = args.center * 1e6, args.rate * 1e6
    data, chans = generate(args.output, args.seconds, center, fs, args.seed)
    prof = Path(sigmf_base(args.output) + "_profile.json")
    save_profile(make_profile(data, chans, center, fs), prof)
    print(f"Wrote {data} ({data.stat().st_size / 1e6:.1f} MB) and profile {prof}")
    print(f"Run:  python -m sdrscanner \"{prof}\" --start")


if __name__ == "__main__":
    main()

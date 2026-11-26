import numpy as np
import pytest

from sdrscanner.config import ChannelConfig
from sdrscanner.dsp import (
    AUDIO_RATE, Channelizer, ChannelProcessor, CtcssDetector, Squelch, choose_fft_sizes,
    complex_to_u8, spectrum_db, u8_to_complex,
)
from sdrscanner.config import RTL_SAMPLE_RATES


@pytest.mark.parametrize("fs", RTL_SAMPLE_RATES)
def test_fft_sizes_give_exact_audio_rate(fs):
    n, nc = choose_fft_sizes(fs)
    chz = Channelizer(fs, 0)
    assert chz.rate == pytest.approx(AUDIO_RATE)
    assert chz.hop * nc == chz.hop_c * n
    assert chz.hop_c % 3 == 0          # WAV recorder decimates by 3


def fm_signal(fs, seconds, offset, tone_hz=1000.0, dev=2500.0, amp=0.05, ctcss=None):
    t = np.arange(int(fs * seconds)) / fs
    a = np.sin(2 * np.pi * tone_hz * t)
    if ctcss:
        a = 0.85 * a + 0.15 * np.sin(2 * np.pi * ctcss * t)
    ph = 2 * np.pi * np.cumsum(offset + dev * a) / fs
    return (amp * np.exp(1j * ph)).astype(np.complex64)


def run(chz, proc, x, chunk=131_072):
    frames = []
    for i in range(0, len(x), chunk):
        for fi, X in chz.frames(x[i:i + chunk]):
            frames.append(proc.process(X, fi, chz))
    return frames


@pytest.mark.parametrize("fs,offset", [(2_400_000, 300_300.0), (2_048_000, -612_510.0), (1_024_000, 0.0 + 150_000)])
def test_demodulates_tone_cleanly(fs, offset):
    chz = Channelizer(fs, 100e6)
    cfg = ChannelConfig(frequency=100e6 + offset, squelch_db=-60, deemphasis_us=0)
    proc = ChannelProcessor(chz, cfg)
    frames = run(chz, proc, fm_signal(fs, 1.5, offset))
    assert all(f.open for f in frames[10:])
    assert np.mean([f.rssi_db for f in frames[10:]]) == pytest.approx(20 * np.log10(0.05), abs=0.5)
    y = np.concatenate([f.audio for f in frames])[AUDIO_RATE // 2:]
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1 / AUDIO_RATE)
    assert f[np.argmax(Y)] == pytest.approx(1000, abs=2)
    near = np.abs(f - 1000) < 20
    sinad = 10 * np.log10(Y[near].sum() / Y[~near].sum())
    assert sinad > 40, sinad   # block-boundary discontinuities would destroy this


def test_adjacent_channel_rejection():
    fs = 2_400_000
    chz = Channelizer(fs, 0)
    # Strong signal 25 kHz away from a quiet channel must not open its squelch.
    x = fm_signal(fs, 1.0, 125_000, amp=0.2)
    proc = ChannelProcessor(chz, ChannelConfig(frequency=100_000, squelch_db=-60))
    frames = run(chz, proc, x)
    assert max(f.rssi_db for f in frames[5:]) < -60


def test_squelch_hysteresis_and_hang():
    sq = Squelch()
    dt = 0.016
    assert sq.update(-55, -50, 3, 0.1, True, dt) is False
    assert sq.update(-49, -50, 3, 0.1, True, dt) is True
    assert sq.update(-52, -50, 3, 0.1, True, dt) is True     # within hysteresis
    assert sq.carrier
    sq.update(-60, -50, 3, 0.1, True, dt)                     # signal gone, hang starts
    assert sq.open and not sq.carrier
    for _ in range(10):
        sq.update(-60, -50, 3, 0.1, True, dt)
    assert sq.open is False


def test_ctcss_detection_and_tone_squelch():
    fs = 2_400_000
    chz = Channelizer(fs, 0)
    x = fm_signal(fs, 2.0, 200_000, ctcss=127.3)
    right = ChannelProcessor(chz, ChannelConfig(frequency=200_000, squelch_db=-60, ctcss_hz=127.3))
    chz2 = Channelizer(fs, 0)
    wrong = ChannelProcessor(chz2, ChannelConfig(frequency=200_000, squelch_db=-60, ctcss_hz=131.8))
    fr = run(chz, right, x)
    fw = run(chz2, wrong, x)
    assert fr[-1].tone == 127.3
    assert any(f.open for f in fr) and fr[-1].open
    assert not any(f.open for f in fw)


def test_ctcss_no_false_tone_on_voice_band_audio():
    det = CtcssDetector(AUDIO_RATE)
    rng = np.random.default_rng(0)
    for _ in range(80):
        det.update(rng.standard_normal(768) * 0.3)
    assert det.tone is None


def test_u8_roundtrip_and_spectrum():
    x = (0.5 * np.exp(1j * np.linspace(0, 50, 4096))).astype(np.complex64)
    y = u8_to_complex(complex_to_u8(x))
    assert np.max(np.abs(x - y)) < 0.01
    s = spectrum_db(x, 1024)
    assert s.shape == (1024,)

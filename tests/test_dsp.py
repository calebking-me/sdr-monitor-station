import numpy as np
import pytest

from sdrscanner.config import MODE_AM, MODE_FM, RTL_SAMPLE_RATES, ChannelConfig
from sdrscanner.dsp import (
    AUDIO_RATE, ChannelBank, Channelizer, ChannelProcessor, Squelch, channel_cutoff, choose_fft_sizes,
    complex_to_u8, spectrum_db, u8_to_complex,
)


@pytest.mark.parametrize("fs", RTL_SAMPLE_RATES)
def test_fft_sizes_give_exact_audio_rate(fs):
    n, nc = choose_fft_sizes(fs)
    chz = Channelizer(fs, 0)
    assert chz.rate == pytest.approx(AUDIO_RATE)
    assert chz.hop * nc == chz.hop_c * n
    assert chz.hop_c % 3 == 0          # WAV recorder decimates by 3


@pytest.mark.parametrize("fs", [0, -1, 1e12, float("nan"), float("inf"), 10_000])
def test_absurd_sample_rates_are_rejected(fs):
    with pytest.raises(ValueError):
        choose_fft_sizes(fs)


def fm_signal(fs, seconds, offset, tone_hz=1000.0, dev=2500.0, amp=0.05, ctcss=None):
    t = np.arange(int(fs * seconds)) / fs
    a = np.sin(2 * np.pi * tone_hz * t)
    if ctcss:
        a = 0.85 * a + 0.15 * np.sin(2 * np.pi * ctcss * t)
    ph = 2 * np.pi * np.cumsum(offset + dev * a) / fs
    return (amp * np.exp(1j * ph)).astype(np.complex64)


def am_signal(fs, seconds, offset, tone_hz=1000.0, depth=0.6, amp=0.05):
    t = np.arange(int(fs * seconds)) / fs
    env = amp * (1 + depth * np.sin(2 * np.pi * tone_hz * t))
    return (env * np.exp(2j * np.pi * offset * t)).astype(np.complex64)


def run(chz, proc, x, chunk=131_072):
    frames = []
    for i in range(0, len(x), chunk):
        for fi, X in chz.frames(x[i:i + chunk]):
            frames.append(proc.process(X, fi, chz))
    return frames


def run_bank(chz, bank, x, chunk=131_072, on_chunk=None):
    out = []
    for n, i in enumerate(range(0, len(x), chunk)):
        if on_chunk:
            bank = on_chunk(n, bank) or bank
        for fi, X in chz.frames(x[i:i + chunk]):
            out.append((bank, bank.process(X, fi)))
    return out


def tone_quality(y, tone_hz):
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1 / AUDIO_RATE)
    near = np.abs(f - tone_hz) < 20
    return f[np.argmax(Y)], 10 * np.log10(Y[near].sum() / Y[~near].sum())


@pytest.mark.parametrize("fs,offset", [(2_400_000, 300_300.0), (2_048_000, -612_510.0), (1_024_000, 150_000.0)])
def test_demodulates_fm_tone_cleanly(fs, offset):
    chz = Channelizer(fs, 100e6)
    cfg = ChannelConfig(frequency=100e6 + offset, squelch_db=-60, deemphasis_us=0)
    proc = ChannelProcessor(chz, cfg)
    frames = run(chz, proc, fm_signal(fs, 1.5, offset))
    assert all(f.open for f in frames[10:])
    assert np.mean([f.rssi_db for f in frames[10:]]) == pytest.approx(20 * np.log10(0.05), abs=0.5)
    peak, sinad = tone_quality(np.concatenate([f.audio for f in frames])[AUDIO_RATE // 2:], 1000)
    assert peak == pytest.approx(1000, abs=2)
    assert sinad > 40, sinad   # block-boundary discontinuities would destroy this


@pytest.mark.parametrize("amp", [0.005, 0.05, 0.4])
def test_am_demodulation_level_is_independent_of_signal_strength(amp):
    fs, off = 2_400_000, -250_000.0
    chz = Channelizer(fs, 0)
    proc = ChannelProcessor(chz, ChannelConfig(frequency=off, mode=MODE_AM, bandwidth=8_000, squelch_db=-90))
    frames = run(chz, proc, am_signal(fs, 1.0, off, depth=0.6, amp=amp))
    y = np.concatenate([f.audio for f in frames])[AUDIO_RATE // 4:]
    peak, sinad = tone_quality(y, 1000)
    assert peak == pytest.approx(1000, abs=2)
    assert sinad > 35
    # 60 % modulation -> 0.6 * HEADROOM(0.5) = 0.3 peak, whatever the carrier level.
    assert np.std(y) * np.sqrt(2) == pytest.approx(0.3, rel=0.08)
    assert frames[-1].tone is None          # no CTCSS on AM


def test_bank_separates_many_mixed_channels():
    fs = 2_400_000
    specs = [(-600_000, MODE_FM, 700.0), (-300_000, MODE_AM, 900.0), (0 + 120_000, MODE_FM, 1100.0),
             (350_000, MODE_AM, 1300.0), (620_000, MODE_FM, 1700.0)]
    x = np.zeros(int(fs * 1.2), dtype=np.complex64)
    for off, mode, tone in specs:
        x += (fm_signal(fs, 1.2, off, tone) if mode == MODE_FM else am_signal(fs, 1.2, off, tone))
    chz = Channelizer(fs, 0)
    cfgs = [ChannelConfig(frequency=off, mode=mode, bandwidth=12_500 if mode == MODE_FM else 8_000,
                          squelch_db=-60, deemphasis_us=0) for off, mode, _ in specs]
    bank = ChannelBank(chz, cfgs)
    assert len(bank) == len(specs)
    out = run_bank(chz, bank, x)
    for i, (_, _, tone) in enumerate(specs):
        y = np.concatenate([f.audio[i] for _, f in out])[AUDIO_RATE // 4:]
        peak, sinad = tone_quality(y, tone)
        assert peak == pytest.approx(tone, abs=3), (i, peak)
        assert sinad > 30, (i, sinad)


def test_bank_rebuild_keeps_existing_channel_seamless():
    """Adding a channel mid-stream must not glitch a channel that is already playing."""
    fs, off = 2_400_000, 200_000.0
    x = fm_signal(fs, 2.0, off) + fm_signal(fs, 2.0, -400_000.0, 600.0)
    chz = Channelizer(fs, 0)
    a = ChannelConfig(frequency=off, squelch_db=-60, deemphasis_us=0)
    b = ChannelConfig(frequency=-400_000.0, squelch_db=-60, deemphasis_us=0)

    def on_chunk(n, bank):
        if n == 8:                                   # ~0.4 s in
            return ChannelBank(chz, [b, a], bank)    # different order, new channel first
        return None

    out = run_bank(chz, ChannelBank(chz, [a]), x, on_chunk=on_chunk)
    ya = np.concatenate([f.audio[bk.ids().index(a.id)] for bk, f in out])[AUDIO_RATE // 4:]
    peak, sinad = tone_quality(ya, 1000)
    assert sinad > 40, sinad


def test_out_of_range_channel_is_rejected():
    chz = Channelizer(2_400_000, 100e6)
    inside = ChannelConfig(frequency=100.5e6)
    outside = ChannelConfig(frequency=103e6)
    bank = ChannelBank(chz, [inside, outside])
    assert bank.ids() == [inside.id]
    assert bank.rejected == [outside]


def test_empty_bank_processes_frames():
    chz = Channelizer(2_400_000, 0)
    bank = ChannelBank(chz, [])
    for fi, X in chz.frames(np.zeros(200_000, dtype=np.complex64)):
        f = bank.process(X, fi)
        assert f.audio.shape[0] == 0


def test_adjacent_channel_rejection():
    fs = 2_400_000
    chz = Channelizer(fs, 0)
    # Strong signal 25 kHz away from a quiet channel must not open its squelch.
    x = fm_signal(fs, 1.0, 125_000, amp=0.2)
    proc = ChannelProcessor(chz, ChannelConfig(frequency=100_000, squelch_db=-60))
    frames = run(chz, proc, x)
    assert max(f.rssi_db for f in frames[5:]) < -60


def test_cutoffs():
    assert channel_cutoff(12_500) == pytest.approx(5_500)
    assert channel_cutoff(25_000) == pytest.approx(8_500)
    assert channel_cutoff(8_000, MODE_AM) == pytest.approx(4_000)
    assert channel_cutoff(30_000) < AUDIO_RATE / 2


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


def test_zero_hang_closes_immediately():
    sq = Squelch()
    sq.update(-40, -50, 3, 0.0, True, 0.016)
    assert sq.update(-70, -50, 3, 0.0, True, 0.016) is False


def test_ctcss_detection_and_tone_squelch():
    fs = 2_400_000
    x = fm_signal(fs, 2.0, 200_000, ctcss=127.3)
    chz = Channelizer(fs, 0)
    right = ChannelProcessor(chz, ChannelConfig(frequency=200_000, squelch_db=-60, ctcss_hz=127.3))
    chz2 = Channelizer(fs, 0)
    wrong = ChannelProcessor(chz2, ChannelConfig(frequency=200_000, squelch_db=-60, ctcss_hz=131.8))
    fr = run(chz, right, x)
    fw = run(chz2, wrong, x)
    assert fr[-1].tone == 127.3
    assert any(f.open for f in fr) and fr[-1].open
    assert not any(f.open for f in fw)


def test_ctcss_no_false_tone_on_noise():
    fs = 2_400_000
    rng = np.random.default_rng(0)
    x = ((rng.standard_normal(fs * 2) + 1j * rng.standard_normal(fs * 2)) * 0.02).astype(np.complex64)
    chz = Channelizer(fs, 0)
    proc = ChannelProcessor(chz, ChannelConfig(frequency=100_000, squelch_db=-150))
    frames = run(chz, proc, x)
    assert all(f.tone is None for f in frames)


def test_u8_roundtrip_and_spectrum():
    x = (0.5 * np.exp(1j * np.linspace(0, 50, 4096))).astype(np.complex64)
    y = u8_to_complex(complex_to_u8(x))
    assert np.max(np.abs(x - y)) < 0.01
    s = spectrum_db(x, 1024)
    assert s.shape == (1024,)
    assert u8_to_complex(np.zeros(5, dtype=np.uint8)).shape == (2,)   # odd byte count tolerated
    for n in (1024, 2048, 4096, 8192):
        assert spectrum_db(x[:1000], n).shape == (n,)                   # shorter than the FFT

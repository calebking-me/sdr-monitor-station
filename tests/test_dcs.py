"""DCS code tables, parsing and decoding."""

import numpy as np
import pytest
from scipy import signal

from sdrscanner import dcs
from sdrscanner.config import ChannelConfig
from sdrscanner.dsp import ChannelBank, Channelizer
from sdrscanner.tools.gen_iq import dcs_waveform

FS = 2_400_000


def test_known_codeword():
    # DCS 023 is published as 0x763813 ([check(11), data(12)], data = 0x800 | 0o23).
    assert dcs.golay_word(0o23) == 0x763813


def test_standard_code_table_structure():
    assert len(dcs.STANDARD_CODES) == 104 == len(set(dcs.STANDARD_CODES))
    patterns, classes = dcs.lookup_arrays()
    # Every rotation of every standard code is a distinct 23-bit pattern ...
    assert len(patterns) == 104 * 23 == len(set(patterns.tolist()))
    assert np.all(np.diff(patterns) > 0)            # sorted, for searchsorted
    # ... and the set is closed under inversion (inverted codes are other standard codes).
    for code in dcs.STANDARD_CODES:
        assert dcs.class_of(code, True) is not None


@pytest.mark.parametrize("a,b", [(0o23, 0o47), (0o25, 0o244), (0o26, 0o464), (0o31, 0o627), (0o32, 0o51)])
def test_documented_inversion_pairs(a, b):
    assert dcs.class_of(a, True) == dcs.class_of(b, False)


def test_rotation_aliases():
    assert dcs.class_of(0o23, False) == dcs.class_of(0o340, False) == dcs.class_of(0o766, False)
    assert dcs.class_name(dcs.class_of(0o340, False)) == "023N"


@pytest.mark.parametrize("text,expected", [
    ("023", ("023", None)), ("D023N", ("023", "N")), ("d754i", ("754", "I")), ("DCS 411", ("411", None)),
    ("23", ("023", None)), ("023R", ("023", "I")), ("off", (None, None)), ("", (None, None)),
])
def test_parse(text, expected):
    assert dcs.parse_code(text) == expected


@pytest.mark.parametrize("text", ["088", "1234", "abc", "D9", "-23", "0o23"])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        dcs.parse_code(text)


def test_accept_classes():
    n = dcs.class_of(0o23, False)
    i = dcs.class_of(0o23, True)
    assert dcs.accept_classes("023", "N") == {n}
    assert dcs.accept_classes("023", "I") == {i}
    assert dcs.accept_classes("023", "either") == {n, i}
    assert dcs.accept_classes("xyz", "N") == frozenset()


def _fm_with_dcs(code, inverted, seconds, off=150_000.0, noise=0.003, voice=True, seed=1):
    rng = np.random.default_rng(seed)
    n = int(FS * seconds)
    t = np.arange(n) / FS
    a = dcs_waveform(code, inverted, t) if code else np.zeros(n)
    if voice:
        a = a + 0.6 * np.sin(2 * np.pi * 900 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 2.1 * t))
    ph = 2 * np.pi * np.cumsum(off + 2500 * a) / FS
    x = 0.05 * np.exp(1j * ph) + noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return x.astype(np.complex64)


def _decode(x, cfgs):
    chz = Channelizer(FS, 0)
    bank = ChannelBank(chz, cfgs)
    seen = []
    for i in range(0, len(x), 131072):
        for fi, X in chz.frames(x[i:i + 131072]):
            f = bank.process(X, fi)
            seen.append(f)
    return seen


@pytest.mark.parametrize("code,inverted,expected", [
    ("023", False, "023N"), ("754", False, "754N"), ("023", True, "047N"), ("411", False, "411N"),
    ("162", False, "162N"), ("631", True, None),
])
def test_decodes_codes(code, inverted, expected):
    if expected is None:
        expected = dcs.class_name(dcs.class_of(int(code, 8), True))
    frames = _decode(_fm_with_dcs(code, inverted, 2.5), [ChannelConfig(frequency=150_000, squelch_db=-80)])
    assert frames[-1].dcs[0] == expected
    assert frames[-1].tones[0] is None


def test_decodes_in_noise_and_no_false_codes():
    frames = _decode(_fm_with_dcs("244", False, 2.5, noise=0.02), [ChannelConfig(frequency=150_000, squelch_db=-80)])
    assert frames[-1].dcs[0] == "244N"
    for x in (_fm_with_dcs(None, False, 4.0, seed=4),
              (0.003 * (np.random.default_rng(5).standard_normal(FS * 4) * (1 + 1j))).astype(np.complex64)):
        frames = _decode(x, [ChannelConfig(frequency=150_000, squelch_db=-150)])
        assert not any(f.dcs[0] for f in frames)


def test_dcs_squelch_right_wrong_and_either():
    rng = np.random.default_rng(3)
    quiet = (0.003 * (rng.standard_normal(FS * 2) + 1j * rng.standard_normal(FS * 2))).astype(np.complex64)
    x = np.concatenate([quiet, _fm_with_dcs("023", False, 3.0), quiet])
    cfgs = [ChannelConfig(frequency=150_000, squelch_db=-60, dcs_code="023", dcs_polarity="N"),
            ChannelConfig(frequency=150_000, squelch_db=-60, dcs_code="025", dcs_polarity="N"),
            ChannelConfig(frequency=150_000, squelch_db=-60, dcs_code="023", dcs_polarity="I"),
            ChannelConfig(frequency=150_000, squelch_db=-60, dcs_code="023", dcs_polarity="either"),
            ChannelConfig(frequency=150_000, squelch_db=-60, dcs_code="047", dcs_polarity="I")]
    frames = _decode(x, cfgs)
    dt = Channelizer(FS, 0).frame_seconds
    opened = [[k * dt for k, f in enumerate(frames) if f.open[j]] for j in range(len(cfgs))]
    assert opened[0] and opened[0][0] - 2.0 < 0.8           # opens within 0.8 s of key-up
    assert not opened[1] and not opened[2]
    assert opened[3] and opened[4]


def test_waveform_is_sub_audible():
    t = np.arange(48000) / 48000
    w = dcs_waveform("023", False, t)
    f, p = signal.welch(w, fs=48000, nperseg=8192)
    assert p[f > 300].sum() < 0.05 * p.sum()

import random

import pytest

from sdrscanner.channel_io import (
    MAX_IMPORT_BYTES, MAX_IMPORT_ROWS, export_channels_csv, import_channels_csv, parse_bandwidth,
    parse_channels_csv, parse_frequency,
)
from sdrscanner.config import MODE_AM, MODE_FM, ChannelConfig
from sdrscanner.presets import PRESETS


@pytest.mark.parametrize("text,hz", [
    ("462.5625", 462_562_500), ("462.5625 MHz", 462_562_500), ("462,5625", 462_562_500),
    ("27185k", 27_185_000), ("27185 kHz", 27_185_000), ("146520000", 146_520_000),
    ("146520000 Hz", 146_520_000), ("1.2965 GHz", 1_296_500_000), ("162550", 162_550_000),
])
def test_frequency_parsing(text, hz):
    assert parse_frequency(text) == hz


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "-5", "0", "9000000 MHz", "1e30"])
def test_bad_frequencies_rejected(text):
    with pytest.raises(ValueError):
        parse_frequency(text)


def test_bandwidth_parsing():
    assert parse_bandwidth("12.5") == 12_500
    assert parse_bandwidth("25k") == 25_000
    assert parse_bandwidth("8000") == 8_000
    assert parse_bandwidth("11,25") == 11_250


def test_native_csv_with_semicolons_decimal_commas_and_bad_rows():
    text = ("# exported from a spreadsheet\n"
            "Name;Frequency (MHz);Mode;BW;Tone;Volume;Pan;Enabled\n"
            "Dispatch;460,1250;NFM;12,5;100;80;-50;yes\n"
            "Air;121.5 MHz;AM;;;;;\n"
            "Bad;abc;FM\n"
            "Broadcast;101.1;WFM\n"
            "Rpt;146.94;FM;25k;103.5;;;no\n")
    r = parse_channels_csv(text)
    assert r.format == "native" and r.rows_read == 5
    by = {c.name: c for c in r.channels}
    assert set(by) == {"Dispatch", "Air", "Rpt"}
    d = by["Dispatch"]
    assert (d.frequency, d.mode, d.bandwidth, d.ctcss_hz, d.volume, d.pan, d.enabled) == \
        (460_125_000, MODE_FM, 12_500, 100.0, 0.8, -0.5, True)
    assert by["Air"].mode == MODE_AM and by["Air"].bandwidth == 8_000 and by["Air"].ctcss_hz is None
    assert by["Rpt"].enabled is False and by["Rpt"].bandwidth == 25_000
    assert len(r.errors) == 2 and any("WFM" in e for e in r.errors) and any("line 5" in e for e in r.errors)


def test_minimal_csv_needs_only_frequency():
    r = parse_channels_csv("frequency\n146.52\n446.00625\n")
    assert [c.frequency for c in r.channels] == [146_520_000, 446_006_250]
    assert r.channels[0].name == "146.5200 MHz"


def test_tab_delimited_and_bom():
    r = parse_channels_csv("﻿name\tfreq_mhz\tsquelch\nA\t151.82\t-62\n")
    assert r.channels[0].squelch_db == -62 and r.channels[0].frequency == 151_820_000


def test_missing_frequency_column_is_an_error():
    with pytest.raises(ValueError, match="frequency"):
        parse_channels_csv("name,mode\nA,FM\n")
    with pytest.raises(ValueError):
        parse_channels_csv("# only a comment\n\n")


def test_invalid_tone_is_repaired_not_fatal():
    r = parse_channels_csv("frequency,ctcss\n146.52,125.0\n")   # 2 Hz from any standard tone
    assert r.channels[0].ctcss_hz is None
    assert any("CTCSS" in w for w in r.warnings)


CHIRP = """Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,DtcsCode,DtcsPolarity,Mode,TStep,Skip,Comment,URCALL,RPT1CALL,RPT2CALL,DVCODE
0,W1AW,146.940000,-,0.600000,Tone,100.0,88.5,023,NN,FM,5.00,,,,,,
1,TSQL rpt,147.120000,+,0.600000,TSQL,88.5,123.0,023,NN,FM,5.00,S,,,,,
2,NOAA,162.550000,,0.000000,,88.5,88.5,023,NN,FM,5.00,,,,,,
3,DCS,145.230000,-,0.600000,DTCS,88.5,88.5,023,NN,NFM,5.00,,,,,,
4,DSTAR,145.670000,,0.000000,,88.5,88.5,023,NN,DV,5.00,,,,,,
5,,121.500000,,0.000000,,88.5,88.5,023,NN,AM,5.00,,,,,,
"""


def test_chirp_export_is_recognised():
    r = parse_channels_csv(CHIRP)
    assert r.format == "chirp"
    by = {c.name: c for c in r.channels}
    assert by["W1AW"].ctcss_hz is None and by["W1AW"].bandwidth == 25_000       # 'Tone' = TX only
    assert by["TSQL rpt"].ctcss_hz == 123.0 and by["TSQL rpt"].enabled is False   # Skip=S
    assert by["DCS"].bandwidth == 12_500 and by["DCS"].ctcss_hz is None
    assert by["Mem 5"].mode == MODE_AM
    assert "DSTAR" not in by and any("DV" in e for e in r.errors)
    assert any("DCS" in w for w in r.warnings)


def test_export_import_roundtrip(tmp_path):
    chans = [ChannelConfig(name="Dispatch", frequency=460_125_000, ctcss_hz=100.0, pan=-0.5, volume=1.2,
                           squelch_db=-57, hang_time=1.25, record=True, color="#123abc"),
             ChannelConfig(name="CB 19", frequency=27_185_000, mode=MODE_AM, bandwidth=8_000,
                           deemphasis_us=0, enabled=False)]
    p = tmp_path / "ch.csv"
    assert export_channels_csv(chans, p) == 2
    back = import_channels_csv(p)
    assert not back.errors and not back.warnings
    for a, b in zip(chans, back.channels):
        for f in ("name", "frequency", "mode", "bandwidth", "squelch_db", "hang_time", "ctcss_hz",
                  "deemphasis_us", "volume", "pan", "enabled", "record", "color"):
            assert getattr(a, f) == pytest.approx(getattr(b, f)) if isinstance(getattr(a, f), float) \
                else getattr(a, f) == getattr(b, f), f


def test_export_neutralises_formula_injection(tmp_path):
    p = tmp_path / "x.csv"
    export_channels_csv([ChannelConfig(name='=HYPERLINK("http://evil","x")', frequency=146.52e6, pan=-0.5),
                         ChannelConfig(name="@SUM(A1)", frequency=146.55e6)], p)
    body = p.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in body and "'@SUM" in body
    assert ",-50," in body            # plain negative numbers are left alone


def test_presets_export_and_reimport(tmp_path):
    for preset in PRESETS:
        p = tmp_path / f"{preset.key}.csv"
        export_channels_csv(preset.make_channels(), p)
        back = import_channels_csv(p)
        assert not back.errors, (preset.key, back.errors)
        assert [c.frequency for c in back.channels] == [c.frequency for c in preset.make_channels()]


def test_size_and_row_limits(tmp_path):
    big = tmp_path / "big.csv"
    big.write_text("frequency\n" + "146.52\n" * (MAX_IMPORT_BYTES // 7 + 10))
    with pytest.raises(ValueError, match="larger"):
        import_channels_csv(big)
    many = "frequency\n" + "146.52\n" * (MAX_IMPORT_ROWS + 50)
    r = parse_channels_csv(many)
    assert len(r.channels) == MAX_IMPORT_ROWS and any("stopped" in e for e in r.errors)


def test_cp1252_file(tmp_path):
    p = tmp_path / "w.csv"
    p.write_bytes("name,frequency\nCaf\xe9 r\xe9p\xe9teur,146.52\n".encode("cp1252"))
    r = import_channels_csv(p)
    assert r.channels[0].name == "Café répéteur"


def test_fuzzed_csv_never_crashes():
    rng = random.Random(1234)
    alphabet = ['a', '1', '.', ',', ';', '\t', '"', '\n', '-', 'k', 'M', 'H', 'z', ' ', '#', '=', '\x00', 'é']
    headers = ["name", "frequency", "mode", "bandwidth", "ctcss", "volume", "pan", "enabled", "squelch"]
    for _ in range(400):
        hdr = rng.sample(headers, rng.randint(1, len(headers)))
        if "frequency" not in hdr:
            hdr.append("frequency")
        lines = [",".join(hdr)]
        for _ in range(rng.randint(0, 6)):
            lines.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))))
        try:
            r = parse_channels_csv("\n".join(lines))
        except ValueError:
            continue
        for c in r.channels:
            assert 100_000 <= c.frequency <= 6e9
            assert "\x00" not in c.name and len(c.name) <= 64

"""Regression tests for the security review (untrusted files, names, DLL loading)."""

import json
import sys

import pytest

from sdrscanner import native
from sdrscanner.activity import ActivityEvent, WavRecorder, csv_safe, export_csv, safe_filename
from sdrscanner.config import SOURCE_FILE, RadioConfig
from sdrscanner.sources import FileSource, inspect_iq_file


@pytest.mark.parametrize("value,expected", [
    ("=1+1", "'=1+1"), ("+cmd", "'+cmd"), ("-cmd|calc", "'-cmd|calc"), ("@SUM(A1)", "'@SUM(A1)"),
    ("\tx", "'\tx"), ("-42.5", "-42.5"), ("+3", "+3"), ("normal", "normal"), ("", ""), (None, ""),
])
def test_csv_safe(value, expected):
    assert csv_safe(value) == expected


def test_activity_log_export_is_formula_safe(tmp_path):
    ev = ActivityEvent("id", '=HYPERLINK("http://x","y")', 146.52e6, 0.0, 1.0, -42.0)
    p = tmp_path / "log.csv"
    export_csv([ev], p)
    text = p.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text and ",-42.0," in text


@pytest.mark.parametrize("name", ["../../evil", "..\\..\\evil", "/etc/passwd", "C:\\Windows\\x",
                                  "a" * 300, "", "....", "con", "nul\x00x"])
def test_recording_file_names_cannot_escape_the_folder(tmp_path, name):
    rec = WavRecorder(tmp_path, name, 146.52e6, 0.0)
    rec.close()
    assert rec.path.parent == tmp_path
    assert len(rec.path.name) < 100
    assert "/" not in safe_filename(name) and "\\" not in safe_filename(name)


def test_recorder_never_overwrites(tmp_path):
    a = WavRecorder(tmp_path, "ch", 146.52e6, 0.0)
    b = WavRecorder(tmp_path, "ch", 146.52e6, 0.0)
    a.close()
    b.close()
    assert a.path != b.path and a.path.exists() and b.path.exists()


def test_dll_search_excludes_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rtlsdr.dll").write_bytes(b"MZ planted")
    assert tmp_path.resolve() not in [d.resolve() for d in native.app_dirs()]


def test_frozen_dll_search_is_only_exe_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SDRScanner.exe"))
    assert native.app_dirs() == [tmp_path.resolve()]


def _sigmf(tmp_path, meta):
    (tmp_path / "x.sigmf-data").write_bytes(b"\x80\x80" * 1000)
    (tmp_path / "x.sigmf-meta").write_text(json.dumps(meta) if not isinstance(meta, str) else meta)
    return tmp_path / "x.sigmf-meta"


@pytest.mark.parametrize("rate", [1e12, -1, "fast", None, float("nan"), 10])
def test_absurd_sigmf_sample_rate_is_refused(tmp_path, rate):
    meta = {"global": {"core:datatype": "cu8", "core:sample_rate": rate},
            "captures": [{"core:frequency": 146e6}]}
    p = _sigmf(tmp_path, meta if rate == rate else json.dumps(meta).replace("NaN", '"NaN"'))
    assert inspect_iq_file(p).sample_rate is None
    src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p)), realtime=False)
    with pytest.raises(RuntimeError):
        src.open(0)          # no memory blow-up from a 20-billion-point FFT


@pytest.mark.parametrize("meta", ['{"global": 5}', "[]", "{bad json", '{"global": {"core:datatype": "x"}}'])
def test_malformed_sigmf_metadata(tmp_path, meta):
    p = _sigmf(tmp_path, meta)
    with pytest.raises(ValueError):
        inspect_iq_file(p)
    with pytest.raises(RuntimeError):
        FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p)), realtime=False).open(0)


def test_oversized_sigmf_metadata(tmp_path):
    p = _sigmf(tmp_path, " " * (1024 * 1024 + 10))
    with pytest.raises(ValueError, match="too large"):
        inspect_iq_file(p)


def test_no_shell_or_eval_in_sources():
    """Static check: the code base never shells out or evaluates strings."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "sdrscanner"
    # Bare builtins only: Qt's dialog.exec() / app.exec() are fine.
    bad = re.compile(r"\b(subprocess|os\.system|os\.popen|pickle|shell=True|yaml\.load\()|(?<![.\w])(eval|exec)\(")
    hits = [f"{p.name}:{i}" for p in root.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bad.search(line)]
    assert not hits, hits


def test_corrupt_wav_files_fail_cleanly(tmp_path):
    import random

    import numpy as np
    from scipy.io import wavfile
    good = tmp_path / "g_146520000Hz.wav"
    wavfile.write(good, 250_000, (np.random.default_rng(0).standard_normal((4000, 2)) * 1000).astype(np.int16))
    base = good.read_bytes()
    rng = random.Random(5)
    for i in range(300):
        b = bytearray(base)
        for _ in range(rng.randint(1, 12)):
            b[rng.randrange(0, 60)] = rng.randrange(256)
        if rng.random() < 0.3:
            b = b[:rng.randrange(0, 80)]
        p = tmp_path / f"f{i}_146520000Hz.wav"
        p.write_bytes(bytes(b))
        src = FileSource(RadioConfig(source=SOURCE_FILE, file_path=str(p)), realtime=False)
        try:
            src.open(0)
            src.read(1000)
        except RuntimeError:
            pass                                   # the only acceptable failure
        finally:
            src.close()

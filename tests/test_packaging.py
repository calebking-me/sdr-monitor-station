import os
import sys

import pytest

from sdrscanner import native


@pytest.fixture
def fresh_native(monkeypatch):
    monkeypatch.setitem(native._state, "prepared", False)
    monkeypatch.setitem(native._state, "user_dll", None)
    monkeypatch.delitem(sys.modules, "pyrtlsdrlib", raising=False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))   # restored after the test


@pytest.mark.parametrize("dll_name", ["rtlsdr.dll", "librtlsdr.dll"])
def test_user_dll_overrides_bundled_library_on_windows(fresh_native, monkeypatch, tmp_path, dll_name):
    (tmp_path / dll_name).write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(native, "app_dirs", lambda: [tmp_path])
    native.prepare_native_libs()
    assert native._state["user_dll"] == tmp_path / dll_name
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    assert "pyrtlsdrlib" in sys.modules and sys.modules["pyrtlsdrlib"] is None


def test_no_user_dll_keeps_bundled_library(fresh_native, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(native, "app_dirs", lambda: [tmp_path])
    native.prepare_native_libs()
    assert native._state["user_dll"] is None
    assert "pyrtlsdrlib" not in sys.modules or sys.modules["pyrtlsdrlib"] is not None


def test_frozen_app_dir_is_exe_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SDRScanner.exe"))
    assert native.app_dirs()[0] == tmp_path.resolve()


def test_self_test_passes(tmp_path):
    pytest.importorskip("PySide6.QtWidgets", reason="Qt libraries not available")
    from sdrscanner import selftest
    report = tmp_path / "report.txt"
    code = selftest.run(str(report))
    text = report.read_text()
    # librtlsdr may legitimately be missing on a dev box; everything else must pass.
    failures = [ln for ln in text.splitlines() if ln.startswith("FAIL") and "librtlsdr" not in ln]
    assert not failures, text
    assert "PASS  GUI + IQ playback" in text
    assert "PASS  Scanning" in text
    assert code in (0, 1)

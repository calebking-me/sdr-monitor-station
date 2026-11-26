"""GUI smoke test (runs headless with QT_QPA_PLATFORM=offscreen)."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="Qt libraries not available")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sdrscanner.tools.gen_iq import SynthChannel, generate, make_profile  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_runs_file_playback(app, tmp_path):
    from sdrscanner.gui.main_window import MainWindow
    fs, center = 2_400_000.0, 460e6
    chans = [SynthChannel("One", -200_000, -40, bursts=[(0.2, 2.5)]),
             SynthChannel("Two", 250_000, -45, ctcss=100.0, bursts=[(0.5, 2.0)])]
    data, chans = generate(str(tmp_path / "g"), 3.0, center, fs, channels=chans)
    cfg = make_profile(data, chans, center, fs)
    cfg.radio.file_loop = False
    win = MainWindow(cfg)
    win.show()
    win.toggle_running()
    deadline = time.time() + 15
    while time.time() < deadline and (win.engine.running or not win.events):
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    win._tick()
    win._refresh_timeline()
    assert {e.channel_name for e in win.events} == {"One", "Two"}
    assert win.log_table.rowCount() == len(win.events)
    assert set(win.channel_list.strips) == {c.id for c in cfg.channels}
    win.close()

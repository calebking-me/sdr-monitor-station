"""GUI tests for scanning: toolbar controls, status, waiting strips, per-set waterfalls, settings."""

import time

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="Qt libraries not available")

from PySide6.QtWidgets import QApplication  # noqa: E402

import sdrscanner.engine as engine_mod  # noqa: E402
from sdrscanner.config import AppConfig, ChannelConfig  # noqa: E402
from sdrscanner.tools.sim_source import SimTransmitter, SimulatedSource  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    from sdrscanner.gui.main_window import MainWindow
    from sdrscanner.gui.view import ViewSettings
    made = []

    def make(cfg=None, **view):
        w = MainWindow(cfg or AppConfig(), view=ViewSettings(**view), persist_view=False)
        w._autosave = False
        made.append(w)
        return w
    yield make
    for w in made:
        w.engine.stop()
        w.close()


def pump(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def far_apart():
    return AppConfig(channels=[
        ChannelConfig(name="2m", frequency=146.52e6, squelch_db=-60, hang_time=0.2),
        ChannelConfig(name="NOAA", frequency=162.55e6, squelch_db=-60, bandwidth=25_000, hang_time=0.2),
        ChannelConfig(name="FRS1", frequency=462.5625e6, squelch_db=-60, hang_time=0.2)])


def test_scan_toolbar_and_plan_preview(window):
    win = window(far_apart())
    assert "turn on Scan (F6)" in win.status_plan.text()
    assert not win.act_scan.isChecked() and not win.act_hold.isEnabled()
    win.act_scan.trigger()
    assert win.config.scan.enabled
    assert win.status_plan.text().startswith("Scanning 3 sets")
    assert "Set 3: 462.5625 MHz (FRS1)" in win.status_plan.toolTip()
    assert win.scan_status.text() == "3 sets – press Start to scan"
    assert win.act_hold.isEnabled() and not win.act_scan_next.isEnabled()   # not running yet
    win.scan_speed.setValue(1.25)
    win.scan_dwell.setValue(4.5)
    assert (win.config.scan.listen_time, win.config.scan.dwell_time) == (1.25, 4.5)
    assert "cycle ≈ 3.8 s" in win.status_plan.text()
    win.act_hold.trigger()
    assert win.engine.scan_hold
    win.config.channels[1].enabled = win.config.channels[2].enabled = False   # the rest fits in one tuning
    win._channels_changed()
    assert win.scan_status.text() == "all channels fit in one tuning"
    win.act_scan.trigger()
    assert not win.config.scan.enabled and win.scan_status.text() == ""


def test_scanning_in_the_window(window, app, monkeypatch):
    src = SimulatedSource([SimTransmitter(462.5625e6, -40, bursts=[(1.2, 3.0)])])
    monkeypatch.setattr(engine_mod, "make_source", lambda cfg: src)
    cfg = far_apart()
    cfg.scan.enabled = True
    cfg.scan.listen_time = 0.25
    cfg.scan.dwell_time = 0.6
    win = window(cfg)
    win.show()
    win.toggle_running()
    frs = cfg.channels[2].id
    try:
        assert pump(app, lambda: win.scan_status.text().startswith("Set "))
        assert win.act_scan_next.isEnabled() and win.act_scan_prev.isEnabled()
        assert pump(app, lambda: "scanning · set" in win.channel_list.strips[frs].tone.text())
        # The transmission on the far set stops the scanner there.
        assert pump(app, lambda: "Set 3/3" in win.scan_status.text() and "active" in win.scan_status.text())
        assert win.channel_list.strips[frs].led._on
        assert len(win.spectrum._band_mem) >= 1               # the other sets' waterfalls are kept
        assert pump(app, lambda: any(e.channel_name == "FRS1" for e in win.events), 6)
        # Hold, then step by hand.
        win.act_hold.trigger()
        assert pump(app, lambda: win.scan_status.text().endswith("· hold"), 3)
        held = win.engine.poll().scan.index
        win.act_scan_next.trigger()
        assert pump(app, lambda: f"Set {(held + 1) % 3 + 1}/3" in win.scan_status.text(), 3)
        win.zoom_to_channels()                                # zooms within the set being received
    finally:
        win.toggle_running()
    assert not win.act_scan_next.isEnabled()
    assert win.status_src.text() == "Stopped"


def test_spectrum_keeps_a_waterfall_per_band(window):
    from sdrscanner.gui import spectrum as spectrum_mod
    from sdrscanner.gui.view import ViewSettings
    # Parented to a window, so Qt destroys it in order (an orphan can crash at interpreter exit).
    sp = spectrum_mod.SpectrumView(ViewSettings(), window())
    width = 2048
    sp.set_band(145e6, 147e6, 146e6)
    sp.add_rows([np.full(width, -50.0, dtype=np.float32)])
    sp.set_band(461e6, 463e6, 462e6)
    assert sp._wf[0, 0] == -120.0                              # a new band starts empty
    sp.add_rows([np.full(width, -80.0, dtype=np.float32)])
    sp.set_band(145e6, 147e6, 146e6)
    assert sp._wf[0, 0] == pytest.approx(-50.0, abs=0.1)       # the first band's waterfall is back
    assert sp._avg is not None and sp._avg[0] == pytest.approx(-50.0)
    sp.set_band(461e6, 463e6, 462e6)
    assert sp._wf[0, 0] == pytest.approx(-80.0, abs=0.1)
    # Memory stays bounded however many tunings are visited.
    for i in range(80):
        sp.set_band(200e6 + i * 3e6, 202e6 + i * 3e6, 201e6 + i * 3e6)
        sp.add_rows([np.full(width, -70.0, dtype=np.float32)])
    total = sum(m["wf"].nbytes for m in sp._band_mem.values())
    assert total <= spectrum_mod.BAND_MEMORY_BYTES and len(sp._band_mem) < 80
    sp.forget_bands()
    assert not sp._band_mem


def test_settings_dialog_scan_section(window):
    from sdrscanner.gui.settings import SettingsDialog
    cfg = AppConfig()
    dlg = SettingsDialog(cfg, window(cfg))
    assert dlg.scan_busy.text() == "never" and dlg.scan_settle.text() == "automatic"
    dlg.scan_enabled.setChecked(True)
    dlg.scan_listen.setValue(0.8)
    dlg.scan_dwell.setValue(3.0)
    dlg.scan_busy.setValue(30)
    dlg.scan_settle.setValue(120)
    assert dlg.apply() is False                                # no restart needed
    assert (cfg.scan.enabled, cfg.scan.listen_time, cfg.scan.dwell_time, cfg.scan.max_dwell,
            cfg.scan.settle_ms) == (True, 0.8, 3.0, 30.0, 120.0)


def test_picker_shows_scan_sets(window):
    from sdrscanner.gui.picker import ChannelPickerDialog
    cfg = AppConfig()
    cfg.scan.enabled = True
    win = window(cfg)
    dlg = ChannelPickerDialog(cfg, win, use_presets=True)
    dlg.preset_combo.setCurrentIndex(dlg.preset_combo.findData("frs"))
    dlg._select(lambda r: True)
    status = [dlg.table.item(r, 6).text() for r in range(dlg.table.rowCount())]
    assert status.count("scan set 1 of 2") == 15 and status.count("scan set 2 of 2") == 7
    assert "scanning 2 sets" in dlg.summary.text() and "won't fit" not in dlg.summary.text()
    cfg.scan.enabled = False                                 # without scanning: the old fit check
    dlg._refresh_status()
    assert "won't fit" in dlg.summary.text()
    assert dlg.table.item(0, 6).text() in ("fits", "outside capture band")

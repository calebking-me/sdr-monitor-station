"""GUI tests (run headless with QT_QPA_PLATFORM=offscreen)."""

import json
import time

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="Qt libraries not available")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sdrscanner.config import SOURCE_RTLSDR, AppConfig, ChannelConfig  # noqa: E402
from sdrscanner.tools.gen_iq import SynthChannel, generate, make_profile  # noqa: E402


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


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_main_window_runs_file_playback(app, window, tmp_path):
    fs, center = 2_400_000.0, 460e6
    chans = [SynthChannel("One", -200_000, -40, bursts=[(0.2, 2.5)]),
             SynthChannel("Two", 250_000, -45, ctcss=100.0, bursts=[(0.5, 2.0)]),
             SynthChannel("Three", 500_000, -45, mode="AM", bursts=[(0.3, 1.5)])]
    data, chans = generate(str(tmp_path / "g"), 3.0, center, fs, channels=chans)
    cfg = make_profile(data, chans, center, fs)
    cfg.radio.file_loop = False
    win = window(cfg)
    win.show()
    win.toggle_running()
    deadline = time.time() + 20
    while time.time() < deadline and (win.engine.running or len(win.events) < 3):
        app.processEvents()
        time.sleep(0.02)
    pump(app, 0.2)
    win.tabs.setCurrentIndex(1)
    win._refresh_timeline()
    assert {e.channel_name for e in win.events} == {"One", "Two", "Three"}
    assert win.log_table.rowCount() == len(win.events)
    assert set(win.channel_list.strips) == {c.id for c in cfg.channels}
    assert win.engine.audio is None          # released when playback ended by itself
    assert win.act_start.text().startswith("▶")


def test_user_text_is_never_rendered_as_html(window):
    evil = '<img src="file:///etc/passwd"><b>bold</b>'
    cfg = AppConfig(channels=[ChannelConfig(name=evil, frequency=146.52e6, color="#ff0000")])
    win = window(cfg)
    strip = next(iter(win.channel_list.strips.values()))
    assert "&lt;img" in strip.name.text() and "<img" not in strip.name.text()
    assert "&lt;img" in strip.name.toolTip()
    marker = win.spectrum._markers[cfg.channels[0].id]["label"]
    assert marker.textItem.toPlainText() == evil          # shown literally
    assert win.status_plan.textFormat() == Qt.PlainText


def test_preset_picker_selects_channels_that_fit(window):
    from sdrscanner.gui.picker import ChannelPickerDialog
    cfg = AppConfig()
    cfg.radio.source = SOURCE_RTLSDR
    win = window(cfg)
    dlg = ChannelPickerDialog(cfg, win, use_presets=True)
    dlg.preset_combo.setCurrentIndex(dlg.preset_combo.findData("frs"))
    assert dlg.table.rowCount() == 22
    dlg._select_fitting()
    picked = dlg.selected_channels()
    assert len(picked) == 15 and all(c.frequency < 463e6 for c in picked)
    assert "15 selected" in dlg.summary.text()
    dlg.preset_combo.setCurrentIndex(dlg.preset_combo.findData("cb"))
    assert len(dlg.selected_channels()) == 40
    assert "won't fit" not in dlg.summary.text()
    win.add_channels(dlg.selected_channels())
    assert len(cfg.channels) == 40 and len(win.channel_list.strips) == 40
    # Opening the same preset again marks everything as a duplicate and selects nothing.
    dlg2 = ChannelPickerDialog(cfg, win, use_presets=True)
    dlg2.preset_combo.setCurrentIndex(dlg2.preset_combo.findData("cb"))
    assert dlg2.selected_channels() == []
    assert dlg2.table.item(0, 6).text() == "already in your list"


def test_import_csv_through_the_window(window, tmp_path, monkeypatch):
    from sdrscanner.gui import main_window as mw
    monkeypatch.setattr(mw.ChannelPickerDialog, "exec", lambda self: 1)
    p = tmp_path / "list.csv"
    p.write_text("name,frequency_mhz,mode,ctcss\nA,146.52,FM,100\nB,146.55,AM,\nbad,xyz,FM,\n")
    win = window()
    win.import_channels(str(p))
    assert [c.name for c in win.config.channels] == ["A", "B"]
    assert win.config.channels[1].mode == "AM"
    win.import_channels(str(p))                    # second time: duplicates are not selected
    assert len(win.config.channels) == 2
    out = tmp_path / "out.csv"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.export_channels()
    assert "146.52" in out.read_text(encoding="utf-8-sig")


def test_window_modes(app, window):
    win = window()
    win.present()
    win.set_window_mode("borderless")
    assert win.windowFlags() & Qt.FramelessWindowHint
    assert win.contentsMargins().left() == 6 and win._win_controls.isVisible()
    edges = win._edges_at(win.rect().topLeft())
    assert edges == (Qt.Edge.LeftEdge | Qt.Edge.TopEdge)
    assert win._edges_at(win.rect().center()) == Qt.Edge(0)
    # Pressing on empty menu-bar space starts a window move (no-op offscreen, must not crash).
    mb = win.menuBar()
    pos = QPointF(mb.width() - win._win_controls.width() - 30, mb.height() / 2)
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, pos, mb.mapToGlobal(pos), Qt.LeftButton,
                     Qt.LeftButton, Qt.NoModifier)
    assert win.eventFilter(mb, ev) is True
    win.toggle_fullscreen()
    assert win.view.window_mode == "fullscreen"
    win._escape()
    assert win.view.window_mode == "borderless"
    win.set_always_on_top(True)
    assert win.windowFlags() & Qt.WindowStaysOnTopHint
    win.set_window_mode("normal")
    assert not (win.windowFlags() & Qt.FramelessWindowHint)
    assert win.contentsMargins().left() == 0 and not win._win_controls.isVisible()
    assert win._edges_at(win.rect().topLeft()) == Qt.Edge(0)


def test_view_options(app, window):
    from sdrscanner.gui import theme
    cfg = AppConfig(channels=[ChannelConfig(name=f"c{i}", frequency=146e6 + i * 25e3) for i in range(5)])
    win = window(cfg)
    win.set_theme("light")
    assert theme.name() == "light" and app.palette().window().color().name() == theme.THEMES["light"]["window"]
    win.set_theme("dark")
    win.set_layout("horizontal")
    assert win.main_split.orientation() == Qt.Horizontal
    win._set_view("compact_strips", True)
    assert all(s.compact for s in win.channel_list.strips.values())
    for f in ("show_spectrum", "show_waterfall", "show_panel"):
        win._set_view(f, False)
    assert win.view.show_panel is True               # never hides everything
    win._set_view("show_toolbar", False)
    assert not win.toolbar.isVisibleTo(win)
    win.sort_channels()
    win._set_all_enabled(False)
    assert not any(c.enabled for c in cfg.channels)


def test_display_dialog_and_spectrum_options(app, window):
    from sdrscanner.gui.display import DisplayDialog
    win = window()
    dlg = DisplayDialog(win.view, 2.4e6, win)
    dlg.fft.setCurrentIndex(dlg.fft.findData(8192))
    dlg.cmap.setCurrentIndex(dlg.cmap.findData("turbo"))
    dlg.auto.setChecked(False)
    dlg.peak.setChecked(True)
    dlg.apply()
    win._apply_view()
    assert win.engine.spectrum_fft == 8192 and win.view.colormap == "turbo"
    sp = win.spectrum
    sp.set_band(459e6, 461e6, 460e6)
    for width in (2048, 8192, 1024):                 # FFT size changes on the fly
        sp.add_rows([np.random.uniform(-90, -60, width).astype(np.float32) for _ in range(3)])
        assert sp._wf.shape == (300, width)
    sp.zoom_to(459.9e6, 460.1e6)
    lo, hi = sp.visible_range_hz()
    assert 459.8e6 < lo < 459.9e6 and 460.1e6 < hi < 460.2e6
    sp.zoom_full()


def test_view_settings_survive_garbage(tmp_path):
    from sdrscanner.gui.view import ViewSettings
    p = tmp_path / "view.json"
    p.write_text(json.dumps({"theme": "neon", "fft_size": 3, "averaging": "x", "level_min": 50,
                             "show_spectrum": "yes", "geometry": 5, "window_mode": "huge", "tab": 99,
                             "unknown": 1}))
    v = ViewSettings.load(p)
    assert v.theme == "dark" and v.fft_size == 2048 and v.window_mode == "normal" and v.tab == 0
    assert v.level_max > v.level_min and isinstance(v.show_spectrum, bool)
    p.write_text("{not json")
    assert ViewSettings.load(p) == ViewSettings()
    v.theme, v.compact_strips = "light", True
    v.save(p)
    assert ViewSettings.load(p).theme == "light" and ViewSettings.load(p).compact_strips

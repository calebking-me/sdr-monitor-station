"""GUI tests for selection, reordering, bulk edit, channel lists, streamlined view and alerts."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="Qt libraries not available")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sdrscanner.activity import ActivityEvent  # noqa: E402
from sdrscanner.config import AppConfig, ChannelConfig, ChannelGroup  # noqa: E402
from sdrscanner.engine import ChannelStatus  # noqa: E402


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


def five():
    return AppConfig(channels=[ChannelConfig(name=n, frequency=146e6 + i * 25e3) for i, n in enumerate("ABCDE")])


def names(win):
    return [c.name for c in win.config.channels]


def test_selection_with_modifiers(window):
    win = window(five())
    cl = win.channel_list
    ids = cl.order
    cl._on_pressed(ids[1], Qt.NoModifier)
    assert cl.selected == [ids[1]]
    cl._on_pressed(ids[3], Qt.ShiftModifier)
    assert cl.selected == ids[1:4]
    cl._on_pressed(ids[2], Qt.ControlModifier)
    assert cl.selected == [ids[1], ids[3]]
    assert cl.strips[ids[1]].selected and not cl.strips[ids[2]].selected
    win.act_select_all.trigger()
    assert cl.selected == ids


def test_drag_drop_and_move_actions(window):
    win = window(five())
    ids = {c.name: c.id for c in win.config.channels}
    win.reorder_channels([ids["D"], ids["E"]], 1)              # drop D,E before B
    assert names(win) == list("ADEBC")
    win.channel_list.set_selection([ids["C"]])
    win.move_selected("up")
    assert names(win) == list("ADECB")
    win.move_selected("top")
    assert names(win) == list("CADEB")
    win.move_selected("bottom")
    assert names(win) == list("ADEBC")
    win.channel_list.set_selection([ids["A"]])
    win.move_selected("up")                                    # already first: no change
    assert names(win) == list("ADEBC")
    assert win.channel_list.order == [win.config.channels[i].id for i in range(5)]


def test_reorder_within_a_list_keeps_hidden_channels_in_place(window):
    cfg = five()
    ids = {c.name: c.id for c in cfg.channels}
    cfg.groups = [ChannelGroup("BD", [ids["B"], ids["D"]])]
    cfg.active_group = "BD"
    win = window(cfg)
    assert [s.cfg.name for s in win.channel_list.strips.values()] == ["B", "D"]
    win.channel_list.set_selection([ids["D"]])
    win.move_selected("up")
    assert names(win) == list("ADCBE")                          # B and D swapped; A, C, E untouched


def test_bulk_edit(window):
    from sdrscanner.gui.bulk import BulkEditDialog
    cfg = five()
    cfg.channels[4].mode = "AM"
    cfg.groups = [ChannelGroup("Mine", [])]
    win = window(cfg)
    ids = [c.id for c in cfg.channels]
    dlg = BulkEditDialog(cfg, ids[2:], win)
    assert dlg.checked_ids() == ids[2:]
    dlg.set_field("squelch")
    dlg.sq_value.setValue(-63)
    dlg.set_field("tone")
    dlg.tone.set_value(None, "754", "I")
    dlg.set_field("hold_time")
    dlg.hold.setValue(4.5)
    dlg.set_field("list")
    assert dlg.apply() == 3
    c, d, e = cfg.channels[2:]
    assert c.squelch_db == d.squelch_db == e.squelch_db == -63
    assert (c.dcs_code, c.dcs_polarity) == ("754", "I") and e.dcs_code is None     # AM skipped
    assert c.hold_time == 4.5 and cfg.channels[0].hold_time == 2.0                    # unticked untouched
    assert cfg.group("Mine").ids == ids[2:]
    assert cfg.channels[0].squelch_db == -50                                         # unselected untouched


def test_bulk_edit_auto_squelch_uses_noise_floor(window):
    from sdrscanner.gui.bulk import BulkEditDialog
    cfg = five()
    win = window(cfg)
    dlg = BulkEditDialog(cfg, [cfg.channels[0].id], win, noise_of=lambda cid: -91.4)
    dlg.set_field("squelch")
    dlg.sq_kind.setCurrentIndex(1)
    dlg.sq_value.setValue(10)
    dlg.apply()
    assert cfg.channels[0].squelch_db == -81


def test_channel_lists_switching(window):
    cfg = five()
    ids = {c.name: c.id for c in cfg.channels}
    cfg.groups = [ChannelGroup("AC", [ids["A"], ids["C"]]), ChannelGroup("Empty", [])]
    win = window(cfg)
    assert win.list_combo.count() == 5                           # all, 2 lists, separator, manage
    win._switch_list_number(1)
    assert cfg.active_group == "AC"
    assert list(win.channel_list.strips) == [ids["A"], ids["C"]]
    assert {c.id for c in cfg.monitored_channels()} == {ids["A"], ids["C"]}
    assert set(win.spectrum._markers) == {ids["A"], ids["C"]}
    win.set_active_list("Empty")
    assert not win.channel_list.strips and "empty" in win.channel_list.empty.text()
    win.set_active_list("")
    assert len(win.channel_list.strips) == 5
    win._add_to_list([ids["B"]], "New one")
    assert cfg.group("New one").ids == [ids["B"]]


def test_manage_lists_dialog(window):
    from sdrscanner.gui.lists import ListsDialog
    cfg = five()
    win = window(cfg)
    dlg = ListsDialog(cfg, win)
    g = dlg.add_group("Repeaters")
    assert g is not None and dlg.add_group("repeaters") is None   # duplicate names refused
    dlg.members.item(0).setCheckState(Qt.Checked)
    dlg.members.item(3).setCheckState(Qt.Checked)
    assert g.ids == [cfg.channels[0].id, cfg.channels[3].id]
    assert cfg.groups == []                                      # works on a copy until accepted
    cfg.groups = dlg.result_groups()
    win.set_active_list("Repeaters")
    assert len(win.channel_list.strips) == 2


def test_preset_into_new_list_and_monitor(window):
    cfg = five()
    win = window(cfg)
    from sdrscanner.presets import PRESETS_BY_KEY
    added = win.add_channels(PRESETS_BY_KEY["noaa"].make_channels(), list_name="NOAA Weather Radio", monitor=True)
    assert added == 7 and cfg.active_group == "NOAA Weather Radio"
    assert len(win.channel_list.strips) == 7
    # A channel added while a list is active joins that list.
    win.add_channels([ChannelConfig(name="X", frequency=162.6e6)])
    assert len(cfg.group("NOAA Weather Radio").ids) == 8


def test_csv_lists_column_creates_lists(window, tmp_path, monkeypatch):
    from sdrscanner.gui import main_window as mw
    monkeypatch.setattr(mw.ChannelPickerDialog, "exec", lambda self: 1)
    p = tmp_path / "c.csv"
    p.write_text("name,frequency,lists,dcs\nA,146.52,Simplex|Local,\nB,146.94,Local,023N\n")
    win = window()
    win.import_channels(str(p))
    cfg = win.config
    assert {g.name for g in cfg.groups} == {"Simplex", "Local"}
    assert len(cfg.group("Local").ids) == 2 and len(cfg.group("Simplex").ids) == 1
    assert cfg.channels[1].dcs_code == "023"


def test_streamlined_view_and_alt_drag(app, window):
    win = window()
    win.present()
    win.act_streamlined.trigger()
    assert win.view.streamlined
    assert not win.menuBar().isVisibleTo(win) and not win.toolbar.isVisibleTo(win)
    assert not win.tabs.tabBar().isVisibleTo(win)
    assert win.act_streamlined in win.spectrum.extra_actions   # still reachable by right-click
    win.set_window_mode("borderless")
    assert win._app_filter
    target = win.spectrum.spec_plot
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(20, 20), target.mapToGlobal(QPointF(20, 20)),
                     Qt.LeftButton, Qt.LeftButton, Qt.AltModifier)
    assert win.eventFilter(target, ev) is True                  # Alt+drag starts a window move
    plain = QMouseEvent(QEvent.MouseButtonPress, QPointF(20, 20), target.mapToGlobal(QPointF(20, 20)),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    assert win.eventFilter(target, plain) is False
    win.act_streamlined.trigger()
    win.set_window_mode("normal")
    assert win.menuBar().isVisibleTo(win) and not win._app_filter


def test_alerts_learn_tone_temp_mute_and_heard(window, monkeypatch):
    cfg = five()
    a = cfg.channels[0]
    a.alert = True
    win = window(cfg)
    win._on_channel_opened(a.id)
    win._on_channel_opened(a.id)                                # rate limited
    assert win.messages.toPlainText().count("Alert:") == 1
    strip = win.channel_list.strips[a.id]
    strip.update_status(ChannelStatus(rssi_db=-40, noise_db=-90, open=True, dcs="754N", state="active"))
    win.learn_tone(a.id)
    assert (a.dcs_code, a.dcs_polarity, a.ctcss_hz) == ("754", "N", None)
    strip.update_status(ChannelStatus(rssi_db=-40, noise_db=-90, open=True, tone=100.0, state="active"))
    win.learn_tone(a.id)
    assert a.ctcss_hz == 100.0 and a.dcs_code is None
    win.mute_for([a.id], 300)
    assert win.engine.mute_until[a.id] > time.time() + 290 and strip.mute.text() == "M⏱"
    win.mute_for([a.id], 0)
    assert a.id not in win.engine.mute_until and strip.mute.text() == "M"
    t = time.time()
    win._add_event(ActivityEvent(a.id, a.name, a.frequency, t - 3, t, -40.0, None, "", "FM", "023N"))
    win._add_event(ActivityEvent(a.id, a.name, a.frequency, t - 1, t, -40.0, 100.0, ""))
    assert "2×" in strip.heard.text()
    assert win.log_table.item(1, 6).text() == "DCS 023N" and win.log_table.item(0, 6).text() == "CTCSS 100.0"


def test_log_double_click_opens_recording(window, tmp_path, monkeypatch):
    from sdrscanner.gui import main_window as mw
    opened = []
    monkeypatch.setattr(mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    rec = tmp_path / "x.wav"
    rec.write_bytes(b"RIFF")
    win = window()
    t = time.time()
    win._add_event(ActivityEvent("id", "A", 1e8, t - 1, t, -40.0, None, str(rec)))
    win._log_double_clicked(0, 3)
    assert opened == [str(rec)]
    win._add_event(ActivityEvent("id", "A", 1e8, t - 1, t, -40.0, None, ""))
    win._log_double_clicked(0, 3)
    assert len(opened) == 1


def test_channel_dialog_dcs_and_lists(window):
    from sdrscanner.gui.channels import ChannelDialog
    g1, g2 = ChannelGroup("One"), ChannelGroup("Two")
    ch = ChannelConfig(name="X", frequency=146.52e6, ctcss_hz=100.0)
    g2.ids.append(ch.id)
    dlg = ChannelDialog(ch, None, "Edit", [g1, g2])
    assert dlg.selected_groups() == {"Two"}
    dlg.tone_sq.set_value(None, "023", "either")
    dlg.lists.item(0).setCheckState(Qt.Checked)
    dlg.lists.item(1).setCheckState(Qt.Unchecked)
    dlg.priority.setChecked(True)
    dlg.hold.setValue(3.0)
    dlg.apply()
    assert (ch.dcs_code, ch.dcs_polarity, ch.ctcss_hz) == ("023", "either", None)
    assert ch.priority and ch.hold_time == 3.0
    assert g1.ids == [ch.id] and g2.ids == []
    assert "DCS 023" in ch.tone_text()


def test_delete_and_duplicate_selection_keep_lists_consistent(window, monkeypatch):
    cfg = five()
    ids = [c.id for c in cfg.channels]
    cfg.groups = [ChannelGroup("L", ids[:3])]
    win = window(cfg)
    monkeypatch.setattr(win, "_ask", lambda *a: True)
    win.channel_list.set_selection([ids[0]])
    win.duplicate_selected()
    assert names(win)[:2] == ["A", "A copy"] and len(cfg.group("L").ids) == 4
    win.channel_list.set_selection([ids[0], ids[1]])
    win.delete_selected()
    assert "A" not in names(win) and "B" not in names(win)
    assert all(cfg.channel(i) for i in cfg.group("L").ids)       # no dangling ids

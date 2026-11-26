"""Main application window."""

from __future__ import annotations

import html
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .. import __version__
from ..activity import ActivityEvent, event_row, export_csv
from ..channel_io import export_channels_csv, import_channels_csv
from ..config import (
    MAX_CHANNELS, MAX_GROUPS, MODE_FM, SOURCE_FILE, AppConfig, ChannelConfig, ChannelGroup, last_profile_path,
    load_profile, save_profile, unique_group_name,
)
from ..engine import Engine
from ..planner import plan_center
from . import theme
from .bulk import BulkEditDialog
from .channels import ChannelDialog, ChannelList, ChannelStrip
from .lists import ListsDialog
from .display import DisplayDialog
from .picker import ChannelPickerDialog, describe_import
from .settings import SettingsDialog
from .spectrum import SpectrumView
from .timeline import ActivityTimeline
from .view import COLORMAPS, LABEL_MODES, ViewSettings
from .widgets import plain_label

LOG_COLUMNS = ["Start", "Channel", "Frequency (MHz)", "Mode", "Duration (s)", "Peak (dBFS)", "Tone",
               "Recording"]
ALERT_REPEAT_S = 15.0           # at most one alert per channel in this time
MANAGE_LISTS = "__manage__"
MAX_LOG_ROWS = 5000
RESIZE_MARGIN = 6
APP_TITLE = "SDR Multi-Channel Scanner"

SHORTCUTS = (
    ("F5", "Start / stop receiving"), ("Ctrl+N", "Add a channel"), ("Ctrl+P", "Add channels from a preset"),
    ("Ctrl+I", "Import channels from CSV"), ("Ctrl+O / Ctrl+S", "Open / save profile"),
    ("Ctrl+B", "Borderless window on/off"), ("F11", "Full screen on/off"), ("Esc", "Leave full screen"),
    ("Ctrl+M", "Streamlined view: hide menu bar, toolbar and tabs"),
    ("Alt+drag", "Move a borderless window from anywhere"),
    ("Click / Ctrl+click / Shift+click", "Select channels"), ("Ctrl+A", "Select all channels"),
    ("Ctrl+E", "Edit the selected channels together"), ("Alt+Up / Alt+Down", "Move the selected channels"),
    ("Drag a channel", "Reorder channels"), ("Del", "Delete the selected channels"),
    ("Ctrl+L", "Manage channel lists"), ("Ctrl+1 … Ctrl+9", "Monitor channel list 1 … 9"),
    ("Ctrl+`", "Monitor all channels"),
    ("Ctrl+0", "Show the full captured band"), ("Ctrl+=", "Zoom to the enabled channels"),
    ("Mouse wheel / drag", "Zoom / pan the spectrum and waterfall"),
    ("Double-click spectrum", "Add a channel at that frequency"), ("Right-click spectrum", "More actions"),
)


def menu_text(text: str) -> str:
    """User text for a menu entry ('&' would otherwise become a keyboard mnemonic)."""
    return text.replace("&", "&&")


def tooltip_text(text: str) -> str:
    """Rich-text tooltip with user data escaped (Qt would otherwise render HTML in it)."""
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, profile_path: Path | None = None,
                 view: ViewSettings | None = None, persist_view: bool = True):
        super().__init__()
        self.config = config
        self.profile_path = profile_path
        self.view = view if view is not None else ViewSettings.load()
        self._persist_view = persist_view
        self._autosave = True          # save the session as last_profile.json on close
        self.engine = Engine(config)
        self.engine.spectrum_fft = self.view.fft_size
        self.events: list[ActivityEvent] = []
        self._last_band = None
        self._was_running = False
        self._mode_before_fullscreen = "normal"
        self._heard: dict[str, list] = {}          # channel id -> [last end time, count, airtime]
        self._prev_open: dict[str, bool] = {}
        self._alerted: dict[str, float] = {}
        self._tray = None
        self._app_filter = False
        theme.apply(QApplication.instance(), self.view.theme)
        self.menuBar().setNativeMenuBar(False)       # keep it in-window (needed for borderless dragging)
        self.resize(1280, 900)
        self._update_title()

        self._build_actions()
        self._build_ui()
        self._build_window_controls()
        self.rebuild_channels()
        self._apply_view(initial=True)
        self._update_plan_preview()
        self._show_load_warnings(config)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)
        self.timeline_timer = QTimer(self)
        self.timeline_timer.timeout.connect(self._slow_tick)
        self.timeline_timer.start(500)

    # ================================================================ building
    def _action(self, text, slot=None, shortcut=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        if slot is not None:
            (a.toggled if checkable else a.triggered).connect(slot)
        self.addAction(a)            # shortcuts keep working when menu/toolbar are hidden
        return a

    def _build_actions(self) -> None:
        A = self._action
        self.act_start = A("▶ Start", self.toggle_running, "F5", tip="Start / stop receiving")
        self.act_add = A("＋ Add channel…", lambda: self.add_channel(), "Ctrl+N")
        self.act_presets = A("☰ Add from preset…", self.add_from_preset, "Ctrl+P",
                             tip="FRS, GMRS, MURS, CB, NOAA weather, marine, PMR446, aviation…")
        self.act_import = A("⇪ Import CSV…", self.import_channels, "Ctrl+I",
                            tip="Add channels from a CSV file (including CHIRP exports)")
        self.act_export_ch = A("Export channels to CSV…", self.export_channels)
        self.act_settings = A("⚙ Radio settings…", self.open_settings)
        self.act_iq = A("● Record IQ", self._toggle_iq, checkable=True,
                        tip="Record the raw wideband IQ to a SigMF file for later playback")
        self.act_edit_sel = A("Edit selected channels…", self.edit_selected, "Ctrl+E",
                              tip="Change one or more settings of several channels at once")
        self.act_select_all = A("Select all channels", lambda: self.channel_list.select_all(), "Ctrl+A")
        self.act_move_up = A("Move up", lambda: self.move_selected("up"), "Alt+Up")
        self.act_move_down = A("Move down", lambda: self.move_selected("down"), "Alt+Down")
        self.act_move_top = A("Move to top", lambda: self.move_selected("top"))
        self.act_move_bottom = A("Move to bottom", lambda: self.move_selected("bottom"))
        self.act_duplicate = A("Duplicate selected", self.duplicate_selected)
        self.act_delete_sel = A("Delete selected…", self.delete_selected, "Del")
        self.act_lists = A("Manage channel lists…", self.manage_lists, "Ctrl+L",
                           tip="Named groups of channels you can switch between")
        self.act_streamlined = A("Streamlined view (hide bars)", lambda v: self._set_view("streamlined", v),
                                 "Ctrl+M", checkable=True,
                                 tip="Hide the menu bar, toolbar and panel tabs - press Ctrl+M to bring them back")

        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        self.toolbar = tb
        self.addToolBar(tb)
        for a in (self.act_start, self.act_add, self.act_presets, self.act_import, self.act_settings, self.act_iq):
            tb.addAction(a)
        tb.addSeparator()
        tb.addWidget(QLabel(" Monitor: "))
        self.list_combo = QComboBox()
        self.list_combo.setMinimumWidth(200)
        self.list_combo.setToolTip("Which channel list to receive (Ctrl+1…9, Ctrl+` for all)")
        self.list_combo.activated.connect(self._on_list_combo)
        tb.addWidget(self.list_combo)

        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(A("&Open profile…", self.open_profile, QKeySequence.Open))
        m.addAction(A("&Save profile", self.save_profile, QKeySequence.Save))
        m.addAction(A("Save profile &as…", self.save_profile_as))
        m.addSeparator()
        m.addAction(self.act_import)
        m.addAction(self.act_export_ch)
        m.addAction(A("Export activity &log (CSV)…", self.export_log))
        m.addAction(A("Open recordings folder", self.open_recordings_folder))
        m.addSeparator()
        m.addAction(A("E&xit", self.close, "Ctrl+Q"))

        m = mb.addMenu("&Channels")
        m.addAction(self.act_add)
        m.addAction(self.act_presets)
        m.addAction(self.act_import)
        m.addAction(self.act_export_ch)
        m.addSeparator()
        m.addAction(self.act_lists)
        m.addSeparator()
        for a in (self.act_select_all, self.act_edit_sel, self.act_move_up, self.act_move_down,
                  self.act_move_top, self.act_move_bottom, self.act_duplicate, self.act_delete_sel):
            m.addAction(a)
        m.addSeparator()
        m.addAction(A("Enable all", lambda: self._set_all_enabled(True)))
        m.addAction(A("Disable all", lambda: self._set_all_enabled(False)))
        m.addAction(A("Sort by frequency", self.sort_channels))
        m.addAction(A("Set squelch for all…", self.set_all_squelch))
        m.addSeparator()
        m.addAction(A("Remove all channels…", self.remove_all_channels))

        m = mb.addMenu("&Radio")
        for a in (self.act_start, self.act_settings, self.act_iq):
            m.addAction(a)

        m = mb.addMenu("&View")
        sub = m.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for key, label in (("dark", "Dark"), ("light", "Light")):
            a = A(label, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            self.theme_group.addAction(a)
            sub.addAction(a)
            self.theme_actions[key] = a
        sub = m.addMenu("Layout")
        self.layout_group = QActionGroup(self)
        self.layout_actions = {}
        for key, label in (("vertical", "Spectrum above channels"), ("horizontal", "Side by side")):
            a = A(label, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_layout(k))
            self.layout_group.addAction(a)
            sub.addAction(a)
            self.layout_actions[key] = a
        m.addSeparator()
        self.act_show_spec = A("Show spectrum", lambda v: self._set_view("show_spectrum", v), checkable=True)
        self.act_show_wf = A("Show waterfall", lambda v: self._set_view("show_waterfall", v), checkable=True)
        self.act_show_panel = A("Show channel panel", lambda v: self._set_view("show_panel", v), checkable=True)
        self.act_show_tb = A("Show toolbar", lambda v: self._set_view("show_toolbar", v), checkable=True)
        self.act_show_sb = A("Show status bar", lambda v: self._set_view("show_statusbar", v), checkable=True)
        self.act_compact = A("Compact channel strips", lambda v: self._set_view("compact_strips", v),
                             checkable=True)
        for a in (self.act_show_spec, self.act_show_wf, self.act_show_panel, self.act_show_tb,
                  self.act_show_sb, self.act_compact, self.act_streamlined):
            m.addAction(a)
        sub = m.addMenu("Channel labels")
        self.label_group = QActionGroup(self)
        self.label_actions = {}
        for key, label in LABEL_MODES:
            a = A(label, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self._set_view("channel_labels", k))
            self.label_group.addAction(a)
            sub.addAction(a)
            self.label_actions[key] = a
        sub = m.addMenu("Waterfall colours")
        self.cmap_group = QActionGroup(self)
        self.cmap_actions = {}
        for key, label in COLORMAPS:
            a = A(label, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self._set_view("colormap", k))
            self.cmap_group.addAction(a)
            sub.addAction(a)
            self.cmap_actions[key] = a
        self.act_peak = A("Peak hold", lambda v: self._set_view("peak_hold", v), checkable=True)
        m.addAction(self.act_peak)
        m.addSeparator()
        m.addAction(A("Zoom to channels", self.zoom_to_channels, "Ctrl+="))
        m.addAction(A("Show full band", lambda: self.spectrum.zoom_full(), "Ctrl+0"))
        m.addSeparator()
        sub = m.addMenu("Window mode")
        self.mode_group = QActionGroup(self)
        self.mode_actions = {}
        for key, label, sc in (("normal", "Normal window", None), ("borderless", "Borderless window", "Ctrl+B"),
                               ("fullscreen", "Full screen", "F11")):
            # Shortcut shown in the menu; bound once below as a toggle (avoids ambiguous shortcuts).
            a = A(f"{label}\t{sc}" if sc else label, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self._on_mode_action(k))
            self.mode_group.addAction(a)
            sub.addAction(a)
            self.mode_actions[key] = a
        # Ctrl+B / F11 act as toggles rather than plain selections.
        QShortcut(QKeySequence("Ctrl+B"), self, lambda: self.set_window_mode(
            "normal" if self.view.window_mode == "borderless" else "borderless"))
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, self._escape)
        for n in range(1, 10):
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, lambda n=n: self._switch_list_number(n))
        QShortcut(QKeySequence("Ctrl+`"), self, lambda: self.set_active_list(""))
        self.act_on_top = A("Always on top", self.set_always_on_top, checkable=True)
        m.addAction(self.act_on_top)
        m.addSeparator()
        m.addAction(A("Display settings…", self.open_display_settings))

        m = mb.addMenu("&Help")
        m.addAction(A("Keyboard shortcuts", self.show_shortcuts))
        m.addAction(A("About", self.show_about))

    def _build_ui(self) -> None:
        self.spectrum = SpectrumView(self.view)
        self.spectrum.frequencyDoubleClicked.connect(lambda f: self.add_channel(f))
        self.spectrum.addChannelRequested.connect(lambda f: self.add_channel(f))
        self.spectrum.zoomToChannelsRequested.connect(self.zoom_to_channels)
        self.spectrum.extra_actions = [self.act_streamlined]

        self.channel_list = ChannelList()
        self.channel_list.reorderRequested.connect(self.reorder_channels)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.channel_list)

        self.timeline = ActivityTimeline()
        tl_scroll = QScrollArea()
        tl_scroll.setWidgetResizable(True)
        tl_scroll.setWidget(self.timeline)
        self.timeline_minutes = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.timeline_minutes.setValue(self.config.timeline_minutes)
        self.timeline_minutes.valueChanged.connect(self._on_timeline_minutes)
        clear_tl = QPushButton("Clear")
        clear_tl.clicked.connect(lambda: (self.engine.clear_history(), self._refresh_timeline()))
        tl_box = QWidget()
        tl_lay = QVBoxLayout(tl_box)
        tl_lay.setContentsMargins(2, 2, 2, 2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show last"))
        row.addWidget(self.timeline_minutes)
        row.addStretch(1)
        row.addWidget(clear_tl)
        tl_lay.addLayout(row)
        tl_lay.addWidget(tl_scroll)

        self.log_table = QTableWidget(0, len(LOG_COLUMNS))
        self.log_table.setHorizontalHeaderLabels(LOG_COLUMNS)
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.cellDoubleClicked.connect(self._log_double_clicked)
        self.log_table.setToolTip("Double-click a row with a recording to play it")
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export_log)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_log)
        log_box = QWidget()
        lay = QVBoxLayout(log_box)
        lay.setContentsMargins(2, 2, 2, 2)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(clear)
        row.addWidget(export)
        lay.addLayout(row)
        lay.addWidget(self.log_table)

        self.messages = QPlainTextEdit(readOnly=True)
        self.messages.setMaximumBlockCount(1000)

        self.tabs = QTabWidget()
        self.tabs.addTab(scroll, "Channels")
        self.tabs.addTab(tl_box, "Activity timeline")
        self.tabs.addTab(log_box, "Activity log")
        self.tabs.addTab(self.messages, "Messages")

        self.main_split = QSplitter(Qt.Vertical)
        self.main_split.addWidget(self.spectrum)
        self.main_split.addWidget(self.tabs)
        self.main_split.setSizes([420, 480])
        self.main_split.setChildrenCollapsible(False)
        self.setCentralWidget(self.main_split)

        self.status_src = plain_label("Stopped")
        self.status_plan = plain_label("")
        self.status_load = plain_label("")
        sb = self.statusBar()
        sb.addWidget(self.status_src)
        sb.addWidget(self.status_plan, 1)
        sb.addPermanentWidget(self.status_load)

    def _build_window_controls(self) -> None:
        """Title + minimise/maximise/close in the menu bar corner, shown in borderless mode."""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 2, 0)
        lay.setSpacing(0)
        self._title_label = plain_label(APP_TITLE)
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents)   # drags reach the menu bar
        self._title_label.setStyleSheet("padding: 0 12px")
        lay.addWidget(self._title_label)
        for text, tip, slot in (("—", "Minimise", self.showMinimized),
                                ("▢", "Maximise / restore", self._toggle_maximized),
                                ("✕", "Close", self.close)):
            b = QToolButton(text=text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setFixedSize(34, 24)
            b.clicked.connect(slot)
            lay.addWidget(b)
        self._win_controls = w
        self._title_label.setText(self.windowTitle())
        self.menuBar().setCornerWidget(w, Qt.TopRightCorner)
        self.menuBar().installEventFilter(self)
        w.hide()

    # ================================================================ view options
    def _apply_view(self, initial: bool = False) -> None:
        v = self.view
        self.theme_actions[v.theme].setChecked(True)
        self.layout_actions[v.layout].setChecked(True)
        self.label_actions[v.channel_labels].setChecked(True)
        self.cmap_actions[v.colormap].setChecked(True)
        self.mode_actions[v.window_mode].setChecked(True)
        for act, val in ((self.act_show_spec, v.show_spectrum), (self.act_show_wf, v.show_waterfall),
                         (self.act_show_panel, v.show_panel), (self.act_show_tb, v.show_toolbar),
                         (self.act_show_sb, v.show_statusbar), (self.act_compact, v.compact_strips),
                         (self.act_peak, v.peak_hold), (self.act_on_top, v.always_on_top),
                         (self.act_streamlined, v.streamlined)):
            act.blockSignals(True)
            act.setChecked(val)
            act.blockSignals(False)
        self.main_split.setOrientation(Qt.Horizontal if v.layout == "horizontal" else Qt.Vertical)
        self.spectrum.set_parts(v.show_spectrum, v.show_waterfall)
        self.spectrum.setVisible(v.show_spectrum or v.show_waterfall)
        self.tabs.setVisible(v.show_panel)
        # Streamlined view: no menu bar, toolbar or tab bar - just spectrum, waterfall and channels.
        self.menuBar().setVisible(not v.streamlined)
        self.toolbar.setVisible(v.show_toolbar and not v.streamlined)
        self.tabs.tabBar().setVisible(not v.streamlined)
        self.statusBar().setVisible(v.show_statusbar)
        self._update_app_filter()
        self.spectrum.set_colormap(v.colormap)
        self.spectrum.set_options(v.averaging, v.waterfall_speed, v.auto_levels, v.level_min,
                                  v.level_max, v.peak_hold, v.channel_labels)
        self.engine.spectrum_fft = v.fft_size
        if initial:
            if v.geometry:
                self.restoreGeometry(QByteArray.fromBase64(v.geometry.encode("ascii")))
            if v.splitter:
                self.main_split.restoreState(QByteArray.fromBase64(v.splitter.encode("ascii")))
            self.tabs.setCurrentIndex(v.tab)
            self._apply_window_flags(show=False)

    def _set_view(self, field: str, value) -> None:
        setattr(self.view, field, value)
        v = self.view
        if not (v.show_spectrum or v.show_waterfall or v.show_panel):
            v.show_panel = True                 # never hide everything
        if field == "compact_strips":
            self.rebuild_channels()
        self._apply_view()
        if field == "streamlined" and value:
            self.statusBar().showMessage("Streamlined view - press Ctrl+M (or right-click the spectrum) to "
                                         "show the menus again", 8000)

    def set_theme(self, name: str) -> None:
        self.view.theme = name
        theme.apply(QApplication.instance(), name)
        self.spectrum.apply_theme()
        self.channel_list.refresh_all()
        self.timeline.update()
        self.update()
        self._apply_view()

    def set_layout(self, name: str) -> None:
        self.view.layout = name
        self._apply_view()

    def open_display_settings(self) -> None:
        dlg = DisplayDialog(self.view, self.engine.sample_rate or self.config.radio.sample_rate, self)
        if dlg.exec():
            dlg.apply()
            self._apply_view()

    def zoom_to_channels(self) -> None:
        chans = self.config.monitored_channels()
        if not chans:
            self.spectrum.zoom_full()
            return
        lo = min(c.frequency - c.bandwidth / 2 for c in chans)
        hi = max(c.frequency + c.bandwidth / 2 for c in chans)
        self.spectrum.zoom_to(lo, hi)

    # ---------------------------------------------------------------- window modes
    def present(self) -> None:
        """Show the window in its saved mode (used at start-up)."""
        self._apply_window_flags(show=True)

    def set_window_mode(self, mode: str) -> None:
        if mode == "fullscreen" and self.view.window_mode != "fullscreen":
            self._mode_before_fullscreen = self.view.window_mode
        self.view.window_mode = mode
        self.mode_actions[mode].setChecked(True)
        self._apply_window_flags(show=True)

    def _on_mode_action(self, mode: str) -> None:
        self.set_window_mode(mode)

    def toggle_fullscreen(self) -> None:
        if self.view.window_mode == "fullscreen":
            self.set_window_mode(self._mode_before_fullscreen or "normal")
        else:
            self.set_window_mode("fullscreen")

    def _escape(self) -> None:
        if self.view.window_mode == "fullscreen":
            self.toggle_fullscreen()

    def set_always_on_top(self, on: bool) -> None:
        self.view.always_on_top = bool(on)
        self._apply_window_flags(show=self.isVisible())

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _apply_window_flags(self, show: bool) -> None:
        mode = self.view.window_mode
        flags = Qt.Window
        if mode == "borderless":
            flags |= Qt.FramelessWindowHint
        if self.view.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)          # hides the window; shown again below
        self._win_controls.setVisible(mode == "borderless")
        self._update_margins()
        self.setMouseTracking(mode == "borderless")
        self._update_app_filter()
        if not show:
            return
        if mode == "fullscreen":
            self.showFullScreen()
        elif self.isFullScreen():
            self.showNormal()
        else:
            self.show()

    def _update_margins(self) -> None:
        m = RESIZE_MARGIN if (self.view.window_mode == "borderless" and not self.isMaximized()) else 0
        self.setContentsMargins(m, m, m, m)

    def changeEvent(self, ev) -> None:
        if ev.type() == QEvent.WindowStateChange:
            self._update_margins()
        super().changeEvent(ev)

    def _edges_at(self, pos: QPoint):
        edges = Qt.Edge(0)
        if self.view.window_mode != "borderless" or self.isMaximized():
            return edges
        r = self.rect()
        if pos.x() <= RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= r.width() - RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= r.height() - RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, ev) -> None:
        e = self._edges_at(ev.position().toPoint())
        L, R, T, B = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
        if e in (L | T, R | B):
            self.setCursor(Qt.SizeFDiagCursor)
        elif e in (R | T, L | B):
            self.setCursor(Qt.SizeBDiagCursor)
        elif e in (L, R):
            self.setCursor(Qt.SizeHorCursor)
        elif e in (T, B):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev) -> None:
        self.unsetCursor()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev) -> None:
        e = self._edges_at(ev.position().toPoint())
        if ev.button() == Qt.LeftButton and e != Qt.Edge(0) and self.windowHandle():
            self.windowHandle().startSystemResize(e)
            return
        super().mousePressEvent(ev)

    def _update_app_filter(self) -> None:
        """Alt+drag moves a borderless window from anywhere (needed when the menu bar is hidden)."""
        want = self.view.window_mode == "borderless"
        app = QApplication.instance()
        if want and not self._app_filter:
            app.installEventFilter(self)
        elif not want and self._app_filter:
            app.removeEventFilter(self)
        self._app_filter = want

    def eventFilter(self, obj, ev) -> bool:
        if (self._app_filter and ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton
                and ev.modifiers() & Qt.AltModifier and isinstance(obj, QWidget) and obj.window() is self
                and self.windowHandle()):
            self.windowHandle().startSystemMove()
            return True
        if obj is self.menuBar() and self.view.window_mode == "borderless":
            t = ev.type()
            if t in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and ev.button() == Qt.LeftButton:
                if self.menuBar().actionAt(ev.position().toPoint()) is None:
                    if t == QEvent.MouseButtonDblClick:
                        self._toggle_maximized()
                    elif self.windowHandle():
                        self.windowHandle().startSystemMove()
                    return True
        return super().eventFilter(obj, ev)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self.view.window_mode == "borderless" and not self.isMaximized():
            p = QPainter(self)
            p.setPen(QPen(theme.color("border"), 1))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # ================================================================ channels
    def rebuild_channels(self) -> None:
        visible = self.config.visible_channels()
        empty = ""
        if self.config.active_group and not visible:
            empty = (f"The channel list '{html.escape(self.config.active_group)}' is empty. Add channels to it "
                     "with <b>Channels → Manage channel lists…</b> or the channel's right-click menu.")
        self.channel_list.rebuild(visible, self._connect_strip, self.view.compact_strips, empty)
        self.spectrum.set_channels(self.config.monitored_channels())
        self._refresh_list_combo()
        self._refresh_heard()

    def _connect_strip(self, s: ChannelStrip) -> None:
        s.structureChanged.connect(self._channels_changed)
        s.editRequested.connect(self.edit_channel)
        s.deleteRequested.connect(self.delete_channel)
        s.contextRequested.connect(self._strip_menu)

    def _channels_changed(self) -> None:
        self.engine.channels_changed()
        self.spectrum.set_channels(self.config.monitored_channels())
        self._update_plan_preview()

    def _ask(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Yes | QMessageBox.No, self)
        box.setTextFormat(Qt.PlainText)
        return box.exec() == QMessageBox.Yes

    def _notify(self, title: str, text: str, icon=QMessageBox.Information, details: str = "") -> None:
        box = QMessageBox(icon, title, text, QMessageBox.Ok, self)
        box.setTextFormat(Qt.PlainText)
        if details:
            box.setDetailedText(details)
        box.exec()

    def add_channel(self, freq: float | None = None) -> None:
        if len(self.config.channels) >= MAX_CHANNELS:
            self._notify("Add channel", f"The channel list is full ({MAX_CHANNELS} channels).")
            return
        n = len(self.config.channels) + 1
        if freq is None:
            freq = self.config.channels[-1].frequency + 25_000 if self.config.channels else (
                self.engine.center or self.config.radio.manual_center)
        ch = ChannelConfig(name=f"Channel {n}", frequency=float(freq), color=self.config.next_color())
        active = self.config.group(self.config.active_group)
        if active is not None:
            active.ids.append(ch.id)              # new channels join the list being monitored
        dlg = ChannelDialog(ch, self, "Add channel", self.config.groups)
        if dlg.exec():
            dlg.apply()
            self.config.channels.append(ch)
            self.rebuild_channels()
            self._channels_changed()
        elif active is not None:
            active.ids.remove(ch.id)

    def edit_channel(self, ch_id: str) -> None:
        ch = self.config.channel(ch_id)
        if ch is None:
            return
        dlg = ChannelDialog(ch, self, f"Edit {ch.name}", self.config.groups)
        if dlg.exec():
            dlg.apply()
            if ch_id in self.channel_list.strips and ch in self.config.visible_channels():
                self.channel_list.strips[ch_id].refresh_from_config()
            else:
                self.rebuild_channels()            # it left the list being shown
            self._refresh_list_combo()
            self._channels_changed()

    def _selection_or(self, cid: str | None = None) -> list[str]:
        sel = list(self.channel_list.selected)
        if cid is not None and cid not in sel:
            sel = [cid]
            self.channel_list.set_selection(sel)
        return sel

    def delete_channel(self, ch_id: str) -> None:
        ch = self.config.channel(ch_id)
        if ch is None or not self._ask("Delete channel", f"Delete '{ch.name}'?"):
            return
        self._remove_channels([ch_id])

    def delete_selected(self) -> None:
        ids = self._selection_or()
        if not ids:
            return
        names = [self.config.channel(i).name for i in ids if self.config.channel(i)]
        text = f"Delete '{names[0]}'?" if len(names) == 1 else f"Delete {len(names)} channels?"
        if self._ask("Delete channels", text):
            self._remove_channels(ids)

    def _remove_channels(self, ids) -> None:
        drop = set(ids)
        self.config.channels[:] = [c for c in self.config.channels if c.id not in drop]
        self.config.prune_groups()
        for cid in drop:
            self.engine.mute_until.pop(cid, None)
        self.channel_list.set_selection([])
        self.rebuild_channels()
        self._channels_changed()

    def duplicate_selected(self) -> None:
        ids = self._selection_or()
        new_ids = []
        for cid in ids:
            src = self.config.channel(cid)
            if src is None or len(self.config.channels) >= MAX_CHANNELS:
                continue
            dup = src.copy(id=ChannelConfig().id, name=(src.name + " copy")[:64], color=self.config.next_color())
            self.config.channels.insert(self.config.channels.index(src) + 1, dup)
            for g in self.config.groups:
                if cid in g.ids:
                    g.ids.append(dup.id)
            new_ids.append(dup.id)
        self.rebuild_channels()
        self.channel_list.set_selection(new_ids)
        self._channels_changed()

    def edit_selected(self) -> None:
        ids = list(self.channel_list.selected)
        dlg = BulkEditDialog(self.config, ids, self, noise_of=self._noise_of)
        if dlg.exec():
            n = dlg.apply()
            self.rebuild_channels()
            self.channel_list.set_selection(dlg.changed_ids)
            self._channels_changed()
            self.statusBar().showMessage(f"Updated {n} channel(s)", 5000)

    def _noise_of(self, cid: str):
        strip = self.channel_list.strips.get(cid)
        return strip.noise_db if strip is not None else None

    # ---------------------------------------------------------------- ordering
    def _visible_ids(self) -> list[str]:
        return [c.id for c in self.config.visible_channels()]

    def _apply_visible_order(self, new_ids: list[str]) -> None:
        """Reorder the shown channels; channels hidden by the active list keep their places."""
        wanted = set(new_ids)
        slots = [i for i, c in enumerate(self.config.channels) if c.id in wanted]
        by_id = {c.id: c for c in self.config.channels}
        for slot, cid in zip(slots, new_ids):
            self.config.channels[slot] = by_id[cid]
        keep = list(self.channel_list.selected)
        self.rebuild_channels()
        self.channel_list.set_selection(keep)

    def reorder_channels(self, ids: list[str], index: int) -> None:
        vis = self._visible_ids()
        moving = [i for i in vis if i in set(ids)]
        if not moving:
            return
        rest = [i for i in vis if i not in set(moving)]
        insert_at = len([i for i in vis[:index] if i not in set(moving)])
        self._apply_visible_order(rest[:insert_at] + moving + rest[insert_at:])

    def move_selected(self, how: str) -> None:
        vis = self._visible_ids()
        sel = set(self.channel_list.selected)
        if not sel:
            return
        if how == "top":
            new = [i for i in vis if i in sel] + [i for i in vis if i not in sel]
        elif how == "bottom":
            new = [i for i in vis if i not in sel] + [i for i in vis if i in sel]
        else:
            new = list(vis)
            rng = range(1, len(new)) if how == "up" else range(len(new) - 2, -1, -1)
            step = -1 if how == "up" else 1
            for i in rng:
                j = i + step
                if new[i] in sel and new[j] not in sel:
                    new[i], new[j] = new[j], new[i]
        if new != vis:
            self._apply_visible_order(new)

    # ---------------------------------------------------------------- channel lists
    def _refresh_list_combo(self) -> None:
        c = self.list_combo
        c.blockSignals(True)
        c.clear()
        c.addItem(f"All channels ({len(self.config.channels)})", "")
        for g in self.config.groups:
            c.addItem(f"{g.name} ({len(g.ids)})", g.name)
        c.insertSeparator(c.count())
        c.addItem("Manage lists…", MANAGE_LISTS)
        c.setCurrentIndex(max(0, c.findData(self.config.active_group)))
        c.blockSignals(False)

    def _on_list_combo(self, index: int) -> None:
        data = self.list_combo.itemData(index)
        if data == MANAGE_LISTS:
            self._refresh_list_combo()
            self.manage_lists()
        else:
            self.set_active_list(data or "")

    def _switch_list_number(self, n: int) -> None:
        if 1 <= n <= len(self.config.groups):
            self.set_active_list(self.config.groups[n - 1].name)

    def set_active_list(self, name: str) -> None:
        g = self.config.group(name) if name else None
        self.config.active_group = g.name if g is not None else ""
        self.channel_list.set_selection([])
        self.rebuild_channels()
        self._channels_changed()
        label = f"'{g.name}' ({len(g.ids)} channels)" if g is not None else "all channels"
        self.statusBar().showMessage(f"Monitoring {label}", 5000)

    def manage_lists(self) -> None:
        dlg = ListsDialog(self.config, self, select=self.config.active_group)
        if dlg.exec():
            self.config.groups = dlg.result_groups()
            if self.config.active_group and self.config.group(self.config.active_group) is None:
                self.config.active_group = ""
            self.rebuild_channels()
            self._channels_changed()

    def _add_to_list(self, ids: list[str], name: str) -> None:
        g = self.config.group(name)
        if g is None:
            if len(self.config.groups) >= MAX_GROUPS:
                return
            g = ChannelGroup(unique_group_name(self.config, name))
            self.config.groups.append(g)
        for cid in ids:
            if cid not in g.ids:
                g.ids.append(cid)
        self._refresh_list_combo()
        if self.config.active_group == g.name:
            self.rebuild_channels()
            self._channels_changed()

    def _new_list_from(self, ids: list[str]) -> None:
        text, ok = QInputDialog.getText(self, "New channel list", "List name:")
        if ok and text.strip():
            self._add_to_list(ids, text)

    def _remove_from_active_list(self, ids: list[str]) -> None:
        g = self.config.group(self.config.active_group)
        if g is not None:
            g.ids = [i for i in g.ids if i not in set(ids)]
            self.rebuild_channels()
            self._channels_changed()

    # ---------------------------------------------------------------- strip context menu
    def _strip_menu(self, cid: str, pos) -> None:
        ch = self.config.channel(cid)
        if ch is None:
            return
        ids = self._selection_or(cid)
        n = len(ids)
        menu = QMenu(self)
        menu.addAction("Edit…", lambda: self.edit_channel(cid))
        menu.addAction(f"Edit {n} channels together…" if n > 1 else "Edit with bulk editor…", self.edit_selected)
        menu.addSeparator()
        for act in (self.act_move_up, self.act_move_down, self.act_move_top, self.act_move_bottom):
            menu.addAction(act)
        menu.addSeparator()
        sub = menu.addMenu("Add to channel list")
        for g in self.config.groups:
            a = sub.addAction(menu_text(g.name), lambda name=g.name: self._add_to_list(ids, name))
            a.setCheckable(True)
            a.setChecked(all(i in g.ids for i in ids))
        if self.config.groups:
            sub.addSeparator()
        sub.addAction("New list…", lambda: self._new_list_from(ids))
        if self.config.active_group:
            menu.addAction(f"Remove from '{menu_text(self.config.active_group)}'",
                           lambda: self._remove_from_active_list(ids))
        menu.addSeparator()
        sub = menu.addMenu("Mute for")
        for minutes in (5, 15, 60):
            sub.addAction(f"{minutes} minutes", lambda m=minutes: self.mute_for(ids, m * 60))
        if any(self.engine.mute_until.get(i, 0) > time.time() for i in ids):
            menu.addAction("Unmute now", lambda: self.mute_for(ids, 0))
        strip = self.channel_list.strips.get(cid)
        st = strip.last_status if strip is not None else None
        if ch.mode == MODE_FM and st is not None and (st.dcs or st.tone):
            label = f"DCS {st.dcs}" if st.dcs else f"CTCSS {st.tone:.1f}"
            menu.addAction(f"Use detected {label} as tone squelch", lambda: self.learn_tone(cid))
        menu.addAction("Set squelch from noise floor", lambda: self.auto_squelch(ids))
        menu.addSeparator()
        menu.addAction(self.act_duplicate)
        menu.addAction(self.act_delete_sel)
        if self.view.streamlined:
            menu.addSeparator()
            menu.addAction(self.act_streamlined)
        menu.exec(pos)

    def mute_for(self, ids: list[str], seconds: float) -> None:
        until = time.time() + seconds
        for cid in ids:
            if seconds > 0:
                self.engine.mute_until[cid] = until
            else:
                self.engine.mute_until.pop(cid, None)
        self._refresh_temp_mutes()

    def learn_tone(self, cid: str) -> None:
        ch = self.config.channel(cid)
        strip = self.channel_list.strips.get(cid)
        st = strip.last_status if strip is not None else None
        if ch is None or st is None:
            return
        if st.dcs:
            ch.dcs_code, ch.dcs_polarity, ch.ctcss_hz = st.dcs[:3], st.dcs[3:] or "N", None
        elif st.tone:
            ch.ctcss_hz, ch.dcs_code = st.tone, None
        strip.refresh_from_config()
        self.statusBar().showMessage(f"{ch.name}: tone squelch set to {ch.tone_text()}", 5000)

    def auto_squelch(self, ids: list[str]) -> None:
        for cid in ids:
            strip = self.channel_list.strips.get(cid)
            if strip is not None and strip.noise_db is not None:
                strip.auto_squelch()

    def add_channels(self, channels: list[ChannelConfig], replace: bool = False, list_name: str = "",
                     monitor: bool = False, memberships: dict | None = None) -> int:
        """Add (or replace with) a batch of channels, e.g. from CSV import or a preset."""
        if replace:
            self.config.channels.clear()
            self.config.prune_groups()
        ids = {c.id for c in self.config.channels}
        added: list[str] = []
        id_map: dict[str, str] = {}
        for c in channels:
            if len(self.config.channels) >= MAX_CHANNELS:
                break
            orig = c.id
            if c.id in ids:
                c = c.copy(id=ChannelConfig().id)
            if not c.color:
                c.color = self.config.next_color()
            self.config.channels.append(c)
            ids.add(c.id)
            id_map[orig] = c.id
            added.append(c.id)
        for orig, names in (memberships or {}).items():
            if orig in id_map:
                for name in names:
                    self._add_to_list([id_map[orig]], name)
        active = self.config.group(self.config.active_group)
        if active is not None and not list_name:
            active.ids.extend(i for i in added if i not in active.ids)
        if list_name and added:
            name = unique_group_name(self.config, list_name)
            if len(self.config.groups) < MAX_GROUPS:
                self.config.groups.append(ChannelGroup(name, list(added)))
                if monitor:
                    self.config.active_group = name
        self.rebuild_channels()
        self._channels_changed()
        self.statusBar().showMessage(f"Added {len(added)} channel(s)", 6000)
        return len(added)

    def add_from_preset(self) -> None:
        dlg = ChannelPickerDialog(self.config, self, title="Add channels from a preset list", use_presets=True)
        if dlg.exec():
            name, monitor = dlg.new_list()
            self.add_channels(dlg.selected_channels(), dlg.replace_existing(), name, monitor)

    def import_channels(self, path: str | None = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import channels", "",
                                                  "Channel lists (*.csv *.txt);;All files (*)")
            if not path:
                return
        try:
            res = import_channels_csv(path)
        except (OSError, ValueError) as e:
            self._notify("Import channels", f"Could not read {Path(path).name}:\n{e}", QMessageBox.Warning)
            return
        if not res.channels:
            self._notify("Import channels", f"No usable channels were found in {Path(path).name}.",
                         QMessageBox.Warning, "\n".join(res.errors + res.warnings))
            return
        dlg = ChannelPickerDialog(
            self.config, self, title="Import channels", candidates=res.channels,
            source_text=describe_import(Path(path).name, res.format, len(res.channels), res.rows_read),
            problems=res.errors + res.warnings, list_name=Path(path).stem[:40])
        if dlg.exec():
            name, monitor = dlg.new_list()
            self.add_channels(dlg.selected_channels(), dlg.replace_existing(), name, monitor, res.memberships)

    def export_channels(self) -> None:
        chans = self.config.visible_channels()
        if not chans:
            self._notify("Export channels", "There are no channels to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export channels", "channels.csv", "CSV (*.csv)")
        if path:
            n = export_channels_csv(chans, path, self.config.groups_of)
            self.statusBar().showMessage(f"Exported {n} channels to {path}", 6000)

    def _set_all_enabled(self, on: bool) -> None:
        for c in self.config.visible_channels():
            c.enabled = on
        self.channel_list.refresh_all()
        self._channels_changed()

    def sort_channels(self) -> None:
        vis = sorted(self.config.visible_channels(), key=lambda c: (c.frequency, c.name))
        self._apply_visible_order([c.id for c in vis])

    def set_all_squelch(self) -> None:
        chans = self.config.visible_channels()
        if not chans:
            return
        val, ok = QInputDialog.getDouble(self, "Set squelch for all", "Squelch level (dBFS):",
                                         chans[0].squelch_db, -150, 0, 0)
        if ok:
            for c in chans:
                c.squelch_db = float(val)
            self.channel_list.refresh_all()

    def remove_all_channels(self) -> None:
        if self.config.channels and self._ask("Remove all channels",
                                              f"Remove all {len(self.config.channels)} channels?"):
            self._remove_channels([c.id for c in self.config.channels])

    def _update_plan_preview(self) -> None:
        r = self.config.radio
        if self.engine.running:
            return
        if r.source == SOURCE_FILE:
            self.status_plan.setText("IQ file playback: centre frequency comes from the recording")
            self.status_plan.setToolTip("")
            return
        plan = plan_center(self.config.monitored_channels(), r.sample_rate, r.auto_center, r.manual_center)
        self._show_plan(plan)

    def _show_plan(self, plan) -> None:
        if plan is None:
            return
        text = f"Centre {plan.center / 1e6:.4f} MHz, usable {plan.usable_lo / 1e6:.3f}–{plan.usable_hi / 1e6:.3f} MHz"
        if plan.warnings:
            text = "⚠ " + plan.warnings[0] + (f" (+{len(plan.warnings) - 1} more)" if len(plan.warnings) > 1 else "")
            self.status_plan.setToolTip(tooltip_text("\n".join(plan.warnings)))
        else:
            self.status_plan.setToolTip("")
        if self.status_plan.text() != text:
            self.status_plan.setText(text)

    def _show_load_warnings(self, cfg: AppConfig) -> None:
        if cfg.load_warnings:
            for w in cfg.load_warnings:
                self.messages.appendPlainText(f"Profile: {w}")
            self.statusBar().showMessage(
                f"Profile loaded with {len(cfg.load_warnings)} warning(s) - see the Messages tab", 10000)

    def _update_title(self) -> None:
        name = f" – {self.profile_path.name}" if self.profile_path else ""
        self.setWindowTitle(APP_TITLE + name)
        if hasattr(self, "_title_label"):
            self._title_label.setText(self.windowTitle())

    # ================================================================ running
    def toggle_running(self) -> None:
        if self.engine.running:
            self.engine.stop()
            self.act_iq.setChecked(False)
        else:
            self.engine.start()
        self._sync_run_state()

    def _sync_run_state(self) -> None:
        running = self.engine.running
        self.act_start.setText("■ Stop" if running else "▶ Start")
        if not running:
            self.status_src.setText("Stopped")
            self._update_plan_preview()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self)
        if not dlg.exec():
            return
        restart = dlg.apply()
        if self.engine.running:
            if restart:
                self.engine.stop()
                self.engine.start()
            else:
                self.engine.radio_changed()
        self._update_plan_preview()

    def _toggle_iq(self, on: bool) -> None:
        if on:
            if not self.engine.running:
                self._notify("Record IQ", "Start the receiver first.")
                self.act_iq.setChecked(False)
                return
            d = self.config.effective_recordings_dir()
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._notify("Record IQ", f"Cannot create {d}:\n{e}", QMessageBox.Warning)
                self.act_iq.setChecked(False)
                return
            self.engine.start_iq_recording(str(d / f"iq_{datetime.now():%Y%m%d_%H%M%S}"))
        else:
            self.engine.stop_iq_recording()

    def _tick(self) -> None:
        snap = self.engine.poll()
        if self._was_running and not snap.running:
            self.engine.stop()               # ended by itself (file finished, error): release devices
            self.act_iq.setChecked(False)
        if snap.running != self._was_running:
            self._was_running = snap.running
            self._sync_run_state()
        for m in snap.messages:
            self.messages.appendPlainText(m)
            self.statusBar().showMessage(m.split("\n")[0], 8000)
        if snap.sample_rate > 0:
            band = (snap.spectrum_lo, snap.spectrum_hi, snap.center)
            if band != self._last_band:
                self._last_band = band
                self.spectrum.set_band(*band)
        if snap.spectrum_rows and (self.view.show_spectrum or self.view.show_waterfall):
            self.spectrum.add_rows(snap.spectrum_rows)
        for cid, strip in self.channel_list.strips.items():
            st = snap.channels.get(cid)
            strip.update_status(st if snap.running else None)
        self.spectrum.set_active({cid: st.open for cid, st in snap.channels.items()})
        for cid, st in snap.channels.items():
            was = self._prev_open.get(cid, False)
            if st.open and not was:
                self._on_channel_opened(cid)
            self._prev_open[cid] = st.open
        for ev in snap.events:
            self._add_event(ev)
        if snap.running:
            state = "receiving" if snap.source_ok else "waiting for source"
            self.status_src.setText(f"{state} · {snap.center / 1e6:.4f} MHz · {snap.sample_rate / 1e6:.3f} MS/s"
                                    + (" · ● IQ REC" if snap.iq_recording else ""))
            self.status_load.setText(f"DSP load {snap.dsp_load * 100:.0f}%  dropped {snap.dropped}")
            self._show_plan(snap.plan)
        else:
            self.status_load.setText("")

    def _slow_tick(self) -> None:
        self._refresh_timeline()
        self._refresh_temp_mutes()

    def _refresh_timeline(self) -> None:
        if self.tabs.currentIndex() == 1 and self.tabs.isVisible():
            self.timeline.set_data(self.config.visible_channels(), self.engine.history_snapshot(),
                                   self.timeline_minutes.value())

    def _refresh_temp_mutes(self) -> None:
        now = time.time()
        mutes = self.engine.mute_until
        for cid, until in list(mutes.items()):
            if until <= now:
                mutes.pop(cid, None)
        for cid, strip in self.channel_list.strips.items():
            strip.set_temp_mute(mutes.get(cid))

    # ---------------------------------------------------------------- alerts and "last heard"
    def _on_channel_opened(self, cid: str) -> None:
        ch = self.config.channel(cid)
        if ch is None or not ch.alert:
            return
        now = time.time()
        if now - self._alerted.get(cid, 0.0) < ALERT_REPEAT_S:
            return
        self._alerted[cid] = now
        text = f"{ch.name} is active ({ch.frequency / 1e6:.4f} MHz)"
        self.messages.appendPlainText(f"[{datetime.now():%H:%M:%S}] Alert: {text}")
        shown = False
        if self.config.alert_popup:
            tray = self._ensure_tray()
            if tray is not None:
                tray.showMessage("SDR Scanner", text, QSystemTrayIcon.Information, 6000)
                shown = True
        if not shown:
            self.statusBar().showMessage(f"⚑ {text}", 8000)
        if self.config.alert_sound:
            QApplication.beep()

    def _ensure_tray(self):
        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(QApplication.windowIcon(), self)
            self._tray.setToolTip(APP_TITLE)
            self._tray.activated.connect(lambda *_: (self.showNormal(), self.raise_(), self.activateWindow()))
            self._tray.show()
        return self._tray

    def _note_heard(self, ev: ActivityEvent) -> None:
        h = self._heard.setdefault(ev.channel_id, [0.0, 0, 0.0])
        h[0] = max(h[0], ev.end)
        h[1] += 1
        h[2] += ev.duration

    def _refresh_heard(self) -> None:
        for cid, strip in self.channel_list.strips.items():
            h = self._heard.get(cid)
            if not h:
                strip.set_heard("")
                continue
            last, count, airtime = h
            strip.set_heard(f"last {datetime.fromtimestamp(last):%H:%M:%S} · {count}×",
                            f"{count} transmission(s), {airtime:.0f} s on air this session")

    def _on_timeline_minutes(self, v: int) -> None:
        self.config.timeline_minutes = v
        self._refresh_timeline()

    # ================================================================ log
    def _add_event(self, ev: ActivityEvent) -> None:
        self.events.append(ev)
        if len(self.events) > 50_000:
            del self.events[:10_000]
        row = event_row(ev)
        self.log_table.insertRow(0)
        vals = [row["start"], row["channel"], row["frequency_mhz"], row["mode"], row["duration_s"],
                row["peak_dbfs"], ev.tone_text, row["recording"]]
        for i, v in enumerate(vals):
            item = QTableWidgetItem(v)
            if i == 0:
                item.setData(Qt.UserRole, ev.recording)
            self.log_table.setItem(0, i, item)
        if self.log_table.rowCount() > MAX_LOG_ROWS:
            self.log_table.removeRow(self.log_table.rowCount() - 1)
        self._note_heard(ev)
        self._refresh_heard()

    def _log_double_clicked(self, row: int, _col: int) -> None:
        item = self.log_table.item(row, 0)
        path = item.data(Qt.UserRole) if item is not None else ""
        if not path:
            self.statusBar().showMessage("That transmission was not recorded", 4000)
            return
        p = Path(path)
        if not p.is_file():
            self.statusBar().showMessage(f"Recording not found: {p.name}", 5000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def open_recordings_folder(self) -> None:
        d = self.config.effective_recordings_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._notify("Recordings", f"Cannot create {d}:\n{e}", QMessageBox.Warning)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _clear_log(self) -> None:
        self.events.clear()
        self.log_table.setRowCount(0)

    def export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export activity log", f"activity_{datetime.now():%Y%m%d_%H%M}.csv", "CSV (*.csv)")
        if path:
            n = export_csv(self.events, path)
            self.statusBar().showMessage(f"Exported {n} events to {path}", 6000)

    # ================================================================ help
    def show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td><b>{html.escape(k)}</b>&nbsp;&nbsp;</td><td>{html.escape(v)}</td></tr>"
                       for k, v in SHORTCUTS)
        box = QMessageBox(QMessageBox.NoIcon, "Keyboard shortcuts", f"<table>{rows}</table>", QMessageBox.Ok, self)
        box.setTextFormat(Qt.RichText)
        box.exec()

    def show_about(self) -> None:
        self._notify("About", f"{APP_TITLE} {__version__}\n\nMonitors several FM/AM voice channels at once "
                              "with one RTL-SDR.\nMIT licence.")

    # ================================================================ profiles
    def _replace_config(self, cfg: AppConfig) -> None:
        was_running = self.engine.running
        self.engine.stop()
        self.config = cfg
        self.engine = Engine(cfg)
        self.engine.spectrum_fft = self.view.fft_size
        self.timeline_minutes.setValue(cfg.timeline_minutes)
        self._last_band = None
        self._heard.clear()
        self._prev_open.clear()
        self.channel_list.set_selection([])
        self.rebuild_channels()
        self._update_plan_preview()
        self._show_load_warnings(cfg)
        if was_running:
            self.engine.start()
        self._sync_run_state()

    def open_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open profile", str(self.profile_path or ""), "Profiles (*.json)")
        if not path:
            return
        try:
            cfg = load_profile(path)
        except (OSError, ValueError) as e:
            self._notify("Open profile", f"Could not load profile:\n{e}", QMessageBox.Warning)
            return
        self.profile_path = Path(path)
        self._update_title()
        self._replace_config(cfg)

    def save_profile(self) -> None:
        if self.profile_path is None:
            self.save_profile_as()
            return
        try:
            save_profile(self.config, self.profile_path)
        except OSError as e:
            self._notify("Save profile", f"Could not save:\n{e}", QMessageBox.Warning)
            return
        self.statusBar().showMessage(f"Saved {self.profile_path}", 4000)

    def save_profile_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save profile", str(self.profile_path or "scanner_profile.json"),
                                              "Profiles (*.json)")
        if path:
            self.profile_path = Path(path)
            self._update_title()
            self.save_profile()

    def closeEvent(self, ev) -> None:
        self.engine.stop()
        if self._autosave:
            try:
                save_profile(self.config, last_profile_path())
            except OSError:
                pass
        if self._persist_view:
            v = self.view
            if v.window_mode != "fullscreen":
                v.geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
            v.splitter = bytes(self.main_split.saveState().toBase64()).decode("ascii")
            v.tab = self.tabs.currentIndex()
            try:
                v.save()
            except OSError:
                pass
        super().closeEvent(ev)

"""Main application window."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .. import __version__
from ..activity import ActivityEvent, event_row, export_csv
from ..channel_io import export_channels_csv, import_channels_csv
from ..config import (
    MAX_CHANNELS, SOURCE_FILE, AppConfig, ChannelConfig, last_profile_path, load_profile, save_profile,
)
from ..engine import Engine
from ..planner import plan_center
from . import theme
from .channels import ChannelDialog, ChannelList, ChannelStrip
from .display import DisplayDialog
from .picker import ChannelPickerDialog, describe_import
from .settings import SettingsDialog
from .spectrum import SpectrumView
from .timeline import ActivityTimeline
from .view import COLORMAPS, LABEL_MODES, ViewSettings
from .widgets import plain_label

LOG_COLUMNS = ["Start", "Channel", "Frequency (MHz)", "Mode", "Duration (s)", "Peak (dBFS)", "CTCSS",
               "Recording"]
MAX_LOG_ROWS = 5000
RESIZE_MARGIN = 6
APP_TITLE = "SDR Multi-Channel Scanner"

SHORTCUTS = (
    ("F5", "Start / stop receiving"), ("Ctrl+N", "Add a channel"), ("Ctrl+P", "Add channels from a preset"),
    ("Ctrl+I", "Import channels from CSV"), ("Ctrl+O / Ctrl+S", "Open / save profile"),
    ("Ctrl+B", "Borderless window on/off"), ("F11", "Full screen on/off"), ("Esc", "Leave full screen"),
    ("Ctrl+0", "Show the full captured band"), ("Ctrl+=", "Zoom to the enabled channels"),
    ("Mouse wheel / drag", "Zoom / pan the spectrum and waterfall"),
    ("Double-click spectrum", "Add a channel at that frequency"), ("Right-click spectrum", "More actions"),
)


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
        self.timeline_timer.timeout.connect(self._refresh_timeline)
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

        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        self.toolbar = tb
        self.addToolBar(tb)
        for a in (self.act_start, self.act_add, self.act_presets, self.act_import, self.act_settings, self.act_iq):
            tb.addAction(a)

        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(A("&Open profile…", self.open_profile, QKeySequence.Open))
        m.addAction(A("&Save profile", self.save_profile, QKeySequence.Save))
        m.addAction(A("Save profile &as…", self.save_profile_as))
        m.addSeparator()
        m.addAction(self.act_import)
        m.addAction(self.act_export_ch)
        m.addAction(A("Export activity &log (CSV)…", self.export_log))
        m.addSeparator()
        m.addAction(A("E&xit", self.close, "Ctrl+Q"))

        m = mb.addMenu("&Channels")
        m.addAction(self.act_add)
        m.addAction(self.act_presets)
        m.addAction(self.act_import)
        m.addAction(self.act_export_ch)
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
                  self.act_show_sb, self.act_compact):
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

        self.channel_list = ChannelList()
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
                         (self.act_peak, v.peak_hold), (self.act_on_top, v.always_on_top)):
            act.blockSignals(True)
            act.setChecked(val)
            act.blockSignals(False)
        self.main_split.setOrientation(Qt.Horizontal if v.layout == "horizontal" else Qt.Vertical)
        self.spectrum.set_parts(v.show_spectrum, v.show_waterfall)
        self.spectrum.setVisible(v.show_spectrum or v.show_waterfall)
        self.tabs.setVisible(v.show_panel)
        self.toolbar.setVisible(v.show_toolbar)
        self.statusBar().setVisible(v.show_statusbar)
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
        chans = [c for c in self.config.channels if c.enabled]
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

    def eventFilter(self, obj, ev) -> bool:
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
        self.channel_list.rebuild(self.config.channels, self._connect_strip, self.view.compact_strips)
        self.spectrum.set_channels(self.config.channels)

    def _connect_strip(self, s: ChannelStrip) -> None:
        s.structureChanged.connect(self._channels_changed)
        s.editRequested.connect(self.edit_channel)
        s.deleteRequested.connect(self.delete_channel)

    def _channels_changed(self) -> None:
        self.engine.channels_changed()
        self.spectrum.set_channels(self.config.channels)
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
        dlg = ChannelDialog(ch, self, "Add channel")
        if dlg.exec():
            dlg.apply()
            self.config.channels.append(ch)
            self.rebuild_channels()
            self._channels_changed()

    def edit_channel(self, ch_id: str) -> None:
        ch = self.config.channel(ch_id)
        if ch is None:
            return
        dlg = ChannelDialog(ch, self, f"Edit {ch.name}")
        if dlg.exec():
            dlg.apply()
            self.channel_list.strips[ch_id].refresh_from_config()
            self._channels_changed()

    def delete_channel(self, ch_id: str) -> None:
        ch = self.config.channel(ch_id)
        if ch is None or not self._ask("Delete channel", f"Delete '{ch.name}'?"):
            return
        self.config.channels.remove(ch)
        self.rebuild_channels()
        self._channels_changed()

    def add_channels(self, channels: list[ChannelConfig], replace: bool = False) -> int:
        """Add (or replace with) a batch of channels, e.g. from CSV import or a preset."""
        if replace:
            self.config.channels.clear()
        ids = {c.id for c in self.config.channels}
        added = 0
        for c in channels:
            if len(self.config.channels) >= MAX_CHANNELS:
                break
            if c.id in ids:
                c = c.copy(id=ChannelConfig().id)
            if not c.color:
                c.color = self.config.next_color()
            self.config.channels.append(c)
            ids.add(c.id)
            added += 1
        self.rebuild_channels()
        self._channels_changed()
        self.statusBar().showMessage(f"Added {added} channel(s)", 6000)
        return added

    def add_from_preset(self) -> None:
        dlg = ChannelPickerDialog(self.config, self, title="Add channels from a preset list", use_presets=True)
        if dlg.exec():
            self.add_channels(dlg.selected_channels(), dlg.replace_existing())

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
            problems=res.errors + res.warnings)
        if dlg.exec():
            self.add_channels(dlg.selected_channels(), dlg.replace_existing())

    def export_channels(self) -> None:
        if not self.config.channels:
            self._notify("Export channels", "There are no channels to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export channels", "channels.csv", "CSV (*.csv)")
        if path:
            n = export_channels_csv(self.config.channels, path)
            self.statusBar().showMessage(f"Exported {n} channels to {path}", 6000)

    def _set_all_enabled(self, on: bool) -> None:
        for c in self.config.channels:
            c.enabled = on
        self.channel_list.refresh_all()
        self._channels_changed()

    def sort_channels(self) -> None:
        self.config.channels.sort(key=lambda c: (c.frequency, c.name))
        self.rebuild_channels()

    def set_all_squelch(self) -> None:
        if not self.config.channels:
            return
        cur = self.config.channels[0].squelch_db
        val, ok = QInputDialog.getDouble(self, "Set squelch for all", "Squelch level (dBFS):", cur, -150, 0, 0)
        if ok:
            for c in self.config.channels:
                c.squelch_db = float(val)
            self.channel_list.refresh_all()

    def remove_all_channels(self) -> None:
        if self.config.channels and self._ask("Remove all channels",
                                              f"Remove all {len(self.config.channels)} channels?"):
            self.config.channels.clear()
            self.rebuild_channels()
            self._channels_changed()

    def _update_plan_preview(self) -> None:
        r = self.config.radio
        if self.engine.running:
            return
        if r.source == SOURCE_FILE:
            self.status_plan.setText("IQ file playback: centre frequency comes from the recording")
            self.status_plan.setToolTip("")
            return
        plan = plan_center(self.config.channels, r.sample_rate, r.auto_center, r.manual_center)
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

    def _refresh_timeline(self) -> None:
        if self.tabs.currentIndex() == 1 and self.tabs.isVisible():
            self.timeline.set_data(self.config.channels, self.engine.history_snapshot(),
                                   self.timeline_minutes.value())

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
                row["peak_dbfs"], row["ctcss_hz"], row["recording"]]
        for i, v in enumerate(vals):
            self.log_table.setItem(0, i, QTableWidgetItem(v))
        if self.log_table.rowCount() > MAX_LOG_ROWS:
            self.log_table.removeRow(self.log_table.rowCount() - 1)

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

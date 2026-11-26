"""Main application window."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from ..activity import ActivityEvent, event_row, export_csv
from ..config import (
    SOURCE_FILE, AppConfig, ChannelConfig, last_profile_path, load_profile, save_profile,
)
from ..engine import Engine
from ..planner import plan_center
from .channels import ChannelDialog, ChannelList, ChannelStrip
from .settings import SettingsDialog
from .spectrum import SpectrumView
from .timeline import ActivityTimeline

LOG_COLUMNS = ["Start", "Channel", "Frequency (MHz)", "Duration (s)", "Peak (dBFS)", "CTCSS", "Recording"]
MAX_LOG_ROWS = 5000


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, profile_path: Path | None = None):
        super().__init__()
        self.config = config
        self.profile_path = profile_path
        self.engine = Engine(config)
        self.events: list[ActivityEvent] = []
        self._last_band = None
        self._autosave = True          # save the session as last_profile.json on close
        self.setWindowTitle("SDR Multi-Channel Scanner")
        self.resize(1280, 900)

        self._build_actions()
        self._build_ui()
        self.rebuild_channels()
        self._update_plan_preview()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)
        self.timeline_timer = QTimer(self)
        self.timeline_timer.timeout.connect(self._refresh_timeline)
        self.timeline_timer.start(500)

    # ---------------------------------------------------------------- UI
    def _build_actions(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.act_start = QAction("▶ Start", self, shortcut=QKeySequence("F5"))
        self.act_start.triggered.connect(self.toggle_running)
        self.act_add = QAction("＋ Add channel", self, shortcut=QKeySequence("Ctrl+N"))
        self.act_add.triggered.connect(lambda: self.add_channel())
        self.act_settings = QAction("⚙ Radio settings", self)
        self.act_settings.triggered.connect(self.open_settings)
        self.act_iq = QAction("● Record IQ", self, checkable=True)
        self.act_iq.setToolTip("Record the raw wideband IQ to a SigMF file for later playback")
        self.act_iq.toggled.connect(self._toggle_iq)
        for a in (self.act_start, self.act_add, self.act_settings, self.act_iq):
            tb.addAction(a)

        m = self.menuBar().addMenu("&File")
        a = m.addAction("&Open profile…", self.open_profile)
        a.setShortcut(QKeySequence.Open)
        a = m.addAction("&Save profile", self.save_profile)
        a.setShortcut(QKeySequence.Save)
        m.addAction("Save profile &as…", self.save_profile_as)
        m.addSeparator()
        m.addAction("&Export activity log (CSV)…", self.export_log)
        m.addSeparator()
        m.addAction("E&xit", self.close)
        m = self.menuBar().addMenu("&Radio")
        m.addAction(self.act_start)
        m.addAction(self.act_settings)
        m.addAction(self.act_add)
        m.addAction(self.act_iq)

    def _build_ui(self) -> None:
        self.spectrum = SpectrumView()
        self.spectrum.frequencyDoubleClicked.connect(lambda f: self.add_channel(f))

        self.channel_list = ChannelList()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.channel_list)

        # Timeline tab
        self.timeline = ActivityTimeline()
        tl_scroll = QScrollArea()
        tl_scroll.setWidgetResizable(True)
        tl_scroll.setWidget(self.timeline)
        self.timeline_minutes = QSpinBox(minimum=1, maximum=240, suffix=" min")
        self.timeline_minutes.setValue(self.config.timeline_minutes)
        self.timeline_minutes.valueChanged.connect(self._on_timeline_minutes)
        tl_box = QWidget()
        tl_lay = QVBoxLayout(tl_box)
        tl_lay.setContentsMargins(2, 2, 2, 2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show last"))
        row.addWidget(self.timeline_minutes)
        row.addStretch(1)
        tl_lay.addLayout(row)
        tl_lay.addWidget(tl_scroll)

        # Log tab
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

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.spectrum)
        split.addWidget(self.tabs)
        split.setSizes([420, 480])
        self.setCentralWidget(split)

        self.status_src = QLabel("Stopped")
        self.status_plan = QLabel("")
        self.status_load = QLabel("")
        sb = self.statusBar()
        sb.addWidget(self.status_src)
        sb.addWidget(self.status_plan, 1)
        sb.addPermanentWidget(self.status_load)

    # ---------------------------------------------------------------- channels
    def rebuild_channels(self) -> None:
        self.channel_list.rebuild(self.config.channels, self._connect_strip)
        self.spectrum.set_channels(self.config.channels)

    def _connect_strip(self, s: ChannelStrip) -> None:
        s.structureChanged.connect(self._channels_changed)
        s.editRequested.connect(self.edit_channel)
        s.deleteRequested.connect(self.delete_channel)

    def _channels_changed(self) -> None:
        self.engine.channels_changed()
        self.spectrum.set_channels(self.config.channels)
        self._update_plan_preview()

    def add_channel(self, freq: float | None = None) -> None:
        n = len(self.config.channels) + 1
        if freq is None:
            snap_center = self.engine.poll().center if self.engine.running else 0
            freq = self.config.channels[-1].frequency + 25_000 if self.config.channels else (
                snap_center or self.config.radio.manual_center)
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
        if ch is None:
            return
        if QMessageBox.question(self, "Delete channel", f"Delete '{ch.name}'?") != QMessageBox.Yes:
            return
        self.config.channels.remove(ch)
        self.rebuild_channels()
        self._channels_changed()

    def _update_plan_preview(self) -> None:
        r = self.config.radio
        if self.engine.running:
            return
        if r.source == SOURCE_FILE:
            self.status_plan.setText("IQ file playback: centre frequency comes from the recording")
            return
        plan = plan_center(self.config.channels, r.sample_rate, r.auto_center, r.manual_center)
        self._show_plan(plan)

    def _show_plan(self, plan) -> None:
        if plan is None:
            return
        text = f"Centre {plan.center / 1e6:.4f} MHz, usable {plan.usable_lo / 1e6:.3f}–{plan.usable_hi / 1e6:.3f} MHz"
        if plan.warnings:
            text = "⚠ " + plan.warnings[0] + (f" (+{len(plan.warnings) - 1} more)" if len(plan.warnings) > 1 else "")
            self.status_plan.setToolTip("\n".join(plan.warnings))
        else:
            self.status_plan.setToolTip("")
        self.status_plan.setText(text)

    # ---------------------------------------------------------------- running
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
                QMessageBox.information(self, "Record IQ", "Start the receiver first.")
                self.act_iq.setChecked(False)
                return
            d = self.config.effective_recordings_dir()
            d.mkdir(parents=True, exist_ok=True)
            base = d / f"iq_{datetime.now():%Y%m%d_%H%M%S}"
            self.engine.start_iq_recording(str(base))
        else:
            self.engine.stop_iq_recording()

    def _tick(self) -> None:
        snap = self.engine.poll()
        if snap.running != (self.act_start.text().startswith("■")):
            self._sync_run_state()
            if not snap.running:
                self.act_iq.setChecked(False)
        for m in snap.messages:
            self.messages.appendPlainText(m)
            self.statusBar().showMessage(m.split("\n")[0], 8000)
        if snap.sample_rate > 0:
            band = (snap.spectrum_lo, snap.spectrum_hi, snap.center)
            if band != self._last_band:
                self._last_band = band
                self.spectrum.set_band(*band)
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
        self.timeline.set_data(self.config.channels, self.engine.history_snapshot(),
                               self.timeline_minutes.value())

    def _on_timeline_minutes(self, v: int) -> None:
        self.config.timeline_minutes = v
        self._refresh_timeline()

    # ---------------------------------------------------------------- log
    def _add_event(self, ev: ActivityEvent) -> None:
        self.events.append(ev)
        row = event_row(ev)
        self.log_table.insertRow(0)
        vals = [row["start"], row["channel"], row["frequency_mhz"], row["duration_s"],
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

    # ---------------------------------------------------------------- profiles
    def _replace_config(self, cfg: AppConfig) -> None:
        was_running = self.engine.running
        self.engine.stop()
        self.config = cfg
        self.engine = Engine(cfg)
        self.timeline_minutes.setValue(cfg.timeline_minutes)
        self._last_band = None
        self.rebuild_channels()
        self._update_plan_preview()
        if was_running:
            self.engine.start()
        self._sync_run_state()

    def open_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open profile", str(self.profile_path or ""), "Profiles (*.json)")
        if not path:
            return
        try:
            cfg = load_profile(path)
        except Exception as e:
            QMessageBox.warning(self, "Open profile", f"Could not load profile:\n{e}")
            return
        self.profile_path = Path(path)
        self._replace_config(cfg)

    def save_profile(self) -> None:
        if self.profile_path is None:
            self.save_profile_as()
            return
        save_profile(self.config, self.profile_path)
        self.statusBar().showMessage(f"Saved {self.profile_path}", 4000)

    def save_profile_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save profile", str(self.profile_path or "scanner_profile.json"),
                                              "Profiles (*.json)")
        if path:
            self.profile_path = Path(path)
            self.save_profile()

    def closeEvent(self, ev) -> None:
        self.engine.stop()
        if self._autosave:
            try:
                save_profile(self.config, last_profile_path())
            except OSError:
                pass
        super().closeEvent(ev)

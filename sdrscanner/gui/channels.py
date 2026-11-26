"""Channel strip (live controls), channel edit dialog and the strip list.

The strip list supports selection (click, Ctrl+click, Shift+click) and drag and
drop reordering; the main window acts on the selection (bulk edit, move, delete).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QDrag
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from .. import dcs
from ..audio import list_output_devices
from ..config import (
    AM_BANDWIDTHS, CTCSS_TONES, DEFAULT_BANDWIDTH, FM_BANDWIDTHS, MAX_BANDWIDTH, MAX_FREQ, MIN_BANDWIDTH,
    MIN_FREQ, MODE_AM, MODE_FM, ChannelConfig, ChannelGroup, clean_color, clean_text,
)
from ..engine import ChannelStatus
from . import theme
from .widgets import Led, SignalMeter

DRAG_MIME = "application/x-sdrscanner-channels"
POLARITY_LABELS = (("N", "Normal (N)"), ("I", "Inverted (I)"), ("either", "Either polarity"))


def _slider(lo: int, hi: int, value: int, width: int = 110) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setFixedWidth(width)
    return s


def bandwidth_label(bw: float) -> str:
    return f"{bw / 1e3:g}k"


def channel_summary(c: ChannelConfig) -> str:
    tone = c.tone_text()
    return f"{c.frequency / 1e6:.4f} MHz · {c.mode} {bandwidth_label(c.bandwidth)}" + (f" · {tone}" if tone else "")


def status_tone_text(st: Optional[ChannelStatus]) -> str:
    if st is None:
        return ""
    if st.dcs:
        return f"DCS {st.dcs}"
    if st.tone:
        return f"CTCSS {st.tone:.1f}"
    return ""


class ChannelStrip(QFrame):
    """One row in the channel list with live controls (full or compact layout)."""

    changed = Signal()                      # live setting changed (no DSP rebuild needed)
    structureChanged = Signal()             # enable toggled (DSP rebuild)
    editRequested = Signal(str)
    deleteRequested = Signal(str)
    pressed = Signal(str, object)           # channel id, keyboard modifiers
    dragRequested = Signal(str)
    contextRequested = Signal(str, QPoint)

    def __init__(self, cfg: ChannelConfig, compact: bool = False, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.compact = compact
        self.selected = False
        self.last_status: Optional[ChannelStatus] = None
        self._press_pos = None
        self._noise = -120.0
        self._temp_mute: Optional[float] = None
        self.setObjectName("strip")
        self.setFrameShape(QFrame.StyledPanel)

        self.swatch = QLabel()
        self.swatch.setFixedSize(6, 20 if compact else 34)
        self.swatch.setToolTip("Drag to reorder")
        self.swatch.setCursor(Qt.OpenHandCursor)
        self.enabled = QCheckBox()
        self.enabled.setToolTip("Enable channel")
        self.name = QLabel()
        self.name.setTextFormat(Qt.RichText)        # we escape the user-supplied parts
        self.name.setFixedWidth(250 if compact else 230)
        self.led = Led()
        self.meter = SignalMeter(height=16 if compact else 18)
        if compact:
            self.meter.setMinimumWidth(90)
        self.tone = QLabel()
        self.tone.setTextFormat(Qt.PlainText)
        self.tone.setMinimumWidth(86)
        self.heard = QLabel()
        self.heard.setTextFormat(Qt.PlainText)
        self.heard.setMinimumWidth(120 if not compact else 0)

        self.squelch = _slider(-110, 0, int(cfg.squelch_db), 90 if compact else 110)
        self.squelch.setToolTip("Squelch level (dBFS)")
        self.squelch_lbl = QLabel()
        self.squelch_lbl.setFixedWidth(46)
        self.auto_btn = QToolButton(text="Auto")
        self.auto_btn.setToolTip("Set squelch 8 dB above the current noise floor")
        self.volume = _slider(0, 200, int(cfg.volume * 100), 90)
        self.pan = _slider(-100, 100, int(cfg.pan * 100), 80)
        self.pan.setToolTip("Left / right fade (double-click to centre)")
        self.pan_lbl = QLabel()
        self.pan_lbl.setFixedWidth(46)
        self.mute = QToolButton(text="M", checkable=True)
        self.mute.setToolTip("Mute")
        self.solo = QToolButton(text="S", checkable=True)
        self.solo.setToolTip("Solo")
        self.rec = QToolButton(text="REC", checkable=True)
        self.rec.setToolTip("Record each transmission to WAV")
        edit = QToolButton(text="Edit")
        edit.setToolTip("Edit all channel settings")
        delete = QToolButton(text="✕")
        delete.setToolTip("Delete channel")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 2 if compact else 3, 6, 2 if compact else 3)
        outer.addWidget(self.swatch)
        if compact:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            for w in (self.enabled, self.led, self.name, self.meter, self.tone, self.squelch,
                      self.squelch_lbl, self.mute, self.solo, self.rec, edit, delete):
                row.addWidget(w)
            row.setStretch(3, 1)
            for w in (self.auto_btn, self.volume, self.pan, self.pan_lbl, self.heard):
                w.hide()
            outer.addLayout(row, 1)
        else:
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            for w in (self.enabled, self.led, self.name, self.meter, self.tone, self.heard):
                top.addWidget(w)
            top.setStretch(3, 1)
            bottom = QHBoxLayout()
            bottom.setContentsMargins(0, 0, 0, 0)
            bottom.addWidget(QLabel("Sql"))
            bottom.addWidget(self.squelch)
            bottom.addWidget(self.squelch_lbl)
            bottom.addWidget(self.auto_btn)
            bottom.addSpacing(10)
            bottom.addWidget(QLabel("Vol"))
            bottom.addWidget(self.volume)
            bottom.addSpacing(10)
            bottom.addWidget(QLabel("L"))
            bottom.addWidget(self.pan)
            bottom.addWidget(QLabel("R"))
            bottom.addSpacing(4)
            bottom.addWidget(self.pan_lbl)
            bottom.addStretch(1)
            for w in (self.mute, self.solo, self.rec, edit, delete):
                bottom.addWidget(w)
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            col.addLayout(top)
            col.addLayout(bottom)
            outer.addLayout(col, 1)

        self.refresh_from_config()
        self.enabled.toggled.connect(self._on_enabled)
        self.squelch.valueChanged.connect(self._on_squelch)
        self.auto_btn.clicked.connect(self.auto_squelch)
        self.volume.valueChanged.connect(self._on_volume)
        self.pan.valueChanged.connect(self._on_pan)
        self.pan.mouseDoubleClickEvent = lambda e: self.pan.setValue(0)
        self.mute.toggled.connect(lambda v: self._set("mute", v))
        self.solo.toggled.connect(lambda v: self._set("solo", v))
        self.rec.toggled.connect(lambda v: self._set("record", v))
        edit.clicked.connect(lambda: self.editRequested.emit(self.cfg.id))
        delete.clicked.connect(lambda: self.deleteRequested.emit(self.cfg.id))

    # ---------------------------------------------------------------- appearance
    def refresh_from_config(self) -> None:
        c = self.cfg
        widgets = (self.enabled, self.squelch, self.volume, self.pan, self.mute, self.solo, self.rec)
        for w in widgets:
            w.blockSignals(True)
        self.enabled.setChecked(c.enabled)
        self.squelch.setValue(int(round(c.squelch_db)))
        self.volume.setValue(int(round(c.volume * 100)))
        self.pan.setValue(int(round(c.pan * 100)))
        self.mute.setChecked(c.mute)
        self.solo.setChecked(c.solo)
        self.rec.setChecked(c.record)
        for w in widgets:
            w.blockSignals(False)
        self.swatch.setStyleSheet(f"background:{clean_color(c.color) or '#888888'}")
        t = theme.tokens()
        name = html.escape(c.name)
        badges = ""
        if c.priority:
            badges += f" <span style='color:{t['warn']}' title='priority'>★</span>"
        if c.alert:
            badges += f" <span style='color:{t['warn']}'>⚑</span>"
        summary = html.escape(channel_summary(c))
        sep = " " if self.compact else "<br>"
        self.name.setText(f"<b>{name}</b>{badges}{sep}<span style='color:{t['muted']}'>{summary}</span>")
        tips = [name, summary]
        if c.priority:
            tips.append("Priority channel: other channels are turned down while it is active")
        if c.alert:
            tips.append("Alert when this channel becomes active")
        self.name.setToolTip("<qt>" + "<br>".join(tips) + "</qt>")
        for lbl in (self.tone, self.pan_lbl, self.heard):
            lbl.setStyleSheet(f"color:{t['muted']}")
        self.squelch_lbl.setText(f"{c.squelch_db:.0f} dB")
        self.volume.setToolTip(f"Volume {c.volume * 100:.0f}%")
        self.pan_lbl.setText("ctr" if abs(c.pan) < 0.01 else f"{'L' if c.pan < 0 else 'R'} {abs(c.pan) * 100:.0f}%")
        self.meter.squelch = c.squelch_db
        self.meter.update()
        self._apply_selection_style()

    def set_selected(self, on: bool) -> None:
        if on != self.selected:
            self.selected = on
            self._apply_selection_style()

    def _apply_selection_style(self) -> None:
        if self.selected:
            hl = QColor(theme.tokens()["highlight"])
            bg = QColor(hl)
            bg.setAlpha(55)
            self.setStyleSheet(f"QFrame#strip {{ border: 2px solid {hl.name()}; "
                               f"background-color: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()}); }}")
        else:
            self.setStyleSheet("")

    def set_heard(self, text: str, tooltip: str = "") -> None:
        if self.heard.text() != text:
            self.heard.setText(text)
        self.heard.setToolTip(tooltip)
        if self.compact:
            self.tone.setToolTip(tooltip)

    def set_temp_mute(self, until: Optional[float]) -> None:
        if until == self._temp_mute:
            return
        self._temp_mute = until
        if until:
            self.mute.setText("M⏱")
            self.mute.setToolTip(f"Muted until {datetime.fromtimestamp(until):%H:%M:%S} (temporary)")
        else:
            self.mute.setText("M")
            self.mute.setToolTip("Mute")

    def update_status(self, st: ChannelStatus | None) -> None:
        self.last_status = st
        if st is None or st.state in ("disabled", "out_of_band", "stopped", "not_in_list", "scanning"):
            self.led.set_state(False)
            self.meter.set_values(-120, -120, self.cfg.squelch_db, False, dimmed=True)
            text = {"out_of_band": "out of band", "disabled": "disabled"}.get(st.state if st else "", "")
            if st is not None and st.state == "scanning":
                # Waiting for the scanner to reach this channel's set.
                text = f"scanning · set {st.scan_set}" if st.scan_set else "scanning"
            self.tone.setText(text)
            return
        self._noise = st.noise_db
        self.led.set_state(st.open, clean_color(self.cfg.color) or "#66ff66")
        self.meter.set_values(st.rssi_db, st.noise_db, self.cfg.squelch_db, st.open)
        text = status_tone_text(st)
        self.tone.setText(f"♪ {text}" if text else "")

    @property
    def noise_db(self) -> Optional[float]:
        st = self.last_status
        return st.noise_db if st is not None and st.state in ("idle", "active") else None

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._press_pos = ev.position()
            self.pressed.emit(self.cfg.id, ev.modifiers())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._press_pos is not None and ev.buttons() & Qt.LeftButton:
            if (ev.position() - self._press_pos).manhattanLength() >= QApplication.startDragDistance():
                self._press_pos = None
                self.dragRequested.emit(self.cfg.id)
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:
        self.editRequested.emit(self.cfg.id)

    def contextMenuEvent(self, ev) -> None:
        self.contextRequested.emit(self.cfg.id, ev.globalPos())

    # ---------------------------------------------------------------- controls
    def _set(self, attr: str, value) -> None:
        setattr(self.cfg, attr, value)
        self.refresh_from_config()
        self.changed.emit()

    def _on_enabled(self, v: bool) -> None:
        self.cfg.enabled = v
        self.structureChanged.emit()

    def _on_squelch(self, v: int) -> None:
        self._set("squelch_db", float(v))

    def auto_squelch(self, margin_db: float = 8.0) -> None:
        self.squelch.setValue(int(round(min(0.0, self._noise + margin_db))))

    def _on_volume(self, v: int) -> None:
        self._set("volume", v / 100.0)

    def _on_pan(self, v: int) -> None:
        self._set("pan", v / 100.0)


class ToneSquelchEditor(QWidget):
    """Off / CTCSS tone / DCS code+polarity selector (shared by the channel and bulk dialogs)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = QComboBox()
        self.kind.addItem("Off (carrier squelch)", "off")
        self.kind.addItem("CTCSS tone", "ctcss")
        self.kind.addItem("DCS code", "dcs")
        self.ctcss = QComboBox()
        for t in CTCSS_TONES:
            self.ctcss.addItem(f"{t:.1f} Hz", t)
        self.dcs = QComboBox()
        for code in dcs.STANDARD_CODES:
            self.dcs.addItem(f"{code:03o}", f"{code:03o}")
        self.polarity = QComboBox()
        for k, label in POLARITY_LABELS:
            self.polarity.addItem(label, k)
        self.polarity.setToolTip("Normal = a 1 bit raises the carrier frequency. If a known DCS "
                                 "channel does not open, try 'Either polarity'.")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        for w in (self.kind, self.ctcss, self.dcs, self.polarity):
            lay.addWidget(w)
        lay.addStretch(1)
        self.kind.currentIndexChanged.connect(self._sync)
        self._sync()

    def set_value(self, ctcss: Optional[float], dcs_code: Optional[str], polarity: str) -> None:
        if dcs_code:
            self.kind.setCurrentIndex(2)
            i = self.dcs.findData(dcs_code)
            if i < 0:
                self.dcs.addItem(dcs_code, dcs_code)
                i = self.dcs.count() - 1
            self.dcs.setCurrentIndex(i)
            self.polarity.setCurrentIndex(max(0, self.polarity.findData(polarity)))
        elif ctcss:
            self.kind.setCurrentIndex(1)
            self.ctcss.setCurrentIndex(max(0, self.ctcss.findData(min(CTCSS_TONES, key=lambda t: abs(t - ctcss)))))
        else:
            self.kind.setCurrentIndex(0)
        self._sync()

    def value(self) -> tuple[Optional[float], Optional[str], str]:
        kind = self.kind.currentData()
        if kind == "ctcss":
            return float(self.ctcss.currentData()), None, "N"
        if kind == "dcs":
            return None, self.dcs.currentData(), self.polarity.currentData()
        return None, None, "N"

    def _sync(self) -> None:
        kind = self.kind.currentData()
        self.ctcss.setVisible(kind == "ctcss")
        self.dcs.setVisible(kind == "dcs")
        self.polarity.setVisible(kind == "dcs")


class ChannelDialog(QDialog):
    """Add / edit every setting of a channel."""

    def __init__(self, cfg: ChannelConfig, parent=None, title="Channel",
                 groups: Optional[list[ChannelGroup]] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.cfg = cfg
        self._color = clean_color(cfg.color)
        self._groups = groups or []

        self.name = QLineEdit(cfg.name)
        self.name.setMaxLength(64)
        self.freq = QDoubleSpinBox(decimals=5, minimum=MIN_FREQ / 1e6, maximum=MAX_FREQ / 1e6,
                                   singleStep=0.0125, suffix=" MHz")
        self.freq.setValue(cfg.frequency / 1e6)
        self.mode = QComboBox()
        self.mode.addItem("FM (voice)", MODE_FM)
        self.mode.addItem("AM (aviation, CB)", MODE_AM)
        self.mode.setCurrentIndex(1 if cfg.mode == MODE_AM else 0)
        self.bw = QComboBox()
        self._fill_bandwidths(cfg.mode, cfg.bandwidth)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(cfg.enabled)

        self.squelch = QDoubleSpinBox(decimals=1, minimum=-150, maximum=0, suffix=" dBFS")
        self.squelch.setValue(cfg.squelch_db)
        self.hyst = QDoubleSpinBox(decimals=1, minimum=0, maximum=40, suffix=" dB")
        self.hyst.setValue(cfg.hysteresis_db)
        self.hang = QDoubleSpinBox(decimals=2, minimum=0, maximum=60, singleStep=0.1, suffix=" s")
        self.hang.setValue(cfg.hang_time)
        self.tone_sq = ToneSquelchEditor()
        self.tone_sq.set_value(cfg.ctcss_hz, cfg.dcs_code, cfg.dcs_polarity)
        self.deemph = QComboBox()
        self.deemph.addItem("750 µs (standard land-mobile)", 750.0)
        self.deemph.addItem("Off", 0.0)
        if cfg.deemphasis_us not in (0.0, 750.0):
            self.deemph.addItem(f"{cfg.deemphasis_us:g} µs", float(cfg.deemphasis_us))
            self.deemph.setCurrentIndex(2)
        else:
            self.deemph.setCurrentIndex(0 if cfg.deemphasis_us > 0 else 1)

        self.volume = QDoubleSpinBox(decimals=0, minimum=0, maximum=200, suffix=" %")
        self.volume.setValue(cfg.volume * 100)
        self.pan = QDoubleSpinBox(decimals=0, minimum=-100, maximum=100, suffix="  (-100 L … +100 R)")
        self.pan.setValue(cfg.pan * 100)
        self.device = QComboBox()
        self.device.addItem("System default", "")
        for key, label in list_output_devices():
            self.device.addItem(label, key)
        i = self.device.findData(cfg.audio_device)
        if i < 0 and cfg.audio_device:
            self.device.addItem(f"{cfg.audio_device} (not connected)", cfg.audio_device)
            i = self.device.count() - 1
        self.device.setCurrentIndex(max(0, i))
        self.priority = QCheckBox("Priority: turn other channels down while this one is active")
        self.priority.setChecked(cfg.priority)
        self.alert = QCheckBox("Alert (pop-up / sound) when this channel becomes active")
        self.alert.setChecked(cfg.alert)
        self.record = QCheckBox("Record each transmission to WAV")
        self.record.setChecked(cfg.record)
        self.hold = QDoubleSpinBox(decimals=1, minimum=0, maximum=30, singleStep=0.5, suffix=" s")
        self.hold.setValue(cfg.hold_time)
        self.hold.setToolTip("If the signal returns within this time it continues the same recording and "
                             "log entry (e.g. a repeater's morse ID or a short fade).")
        self.color_btn = QPushButton()
        self.color_btn.clicked.connect(self._pick_color)
        self._update_color_btn()
        self.lists = QListWidget()
        self.lists.setMaximumHeight(110)
        for g in self._groups:
            item = QListWidgetItem(g.name)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if cfg.id in g.ids else Qt.Unchecked)
            self.lists.addItem(item)

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Frequency", self.freq)
        form.addRow("Mode", self.mode)
        form.addRow("Bandwidth", self.bw)
        form.addRow("", self.enabled)
        form.addRow(QLabel("<b>Squelch</b>"))
        form.addRow("Open level", self.squelch)
        form.addRow("Hysteresis", self.hyst)
        form.addRow("Hang time", self.hang)
        form.addRow("Tone squelch", self.tone_sq)
        form.addRow("De-emphasis", self.deemph)
        form.addRow(QLabel("<b>Audio</b>"))
        form.addRow("Volume", self.volume)
        form.addRow("Pan / fade", self.pan)
        form.addRow("Output device", self.device)
        form.addRow("", self.priority)
        form.addRow("", self.alert)
        form.addRow(QLabel("<b>Recording &amp; log</b>"))
        form.addRow("", self.record)
        form.addRow("Transmission hold", self.hold)
        form.addRow("Colour", self.color_btn)
        if self._groups:
            form.addRow("Channel lists", self.lists)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._on_mode)
        self._update_mode_widgets()

    def _fill_bandwidths(self, mode: str, current: float) -> None:
        self.bw.blockSignals(True)
        self.bw.clear()
        choices = AM_BANDWIDTHS if mode == MODE_AM else FM_BANDWIDTHS
        notes = {12_500.0: " (narrow FM)", 25_000.0: " (wide FM)", 6_000.0: " (narrow AM)",
                 8_000.0: " (AM voice)", 10_000.0: " (wide AM)"}
        for bw in choices:
            self.bw.addItem(f"{bw / 1e3:g} kHz{notes.get(bw, '')}", bw)
        if MIN_BANDWIDTH <= current <= MAX_BANDWIDTH and current not in choices:
            self.bw.addItem(f"{current / 1e3:g} kHz (custom)", float(current))
        i = self.bw.findData(float(current))
        self.bw.setCurrentIndex(i if i >= 0 else self.bw.findData(DEFAULT_BANDWIDTH[mode]))
        self.bw.blockSignals(False)

    def _on_mode(self) -> None:
        mode = self.mode.currentData()
        self._fill_bandwidths(mode, DEFAULT_BANDWIDTH[mode])
        self._update_mode_widgets()

    def _update_mode_widgets(self) -> None:
        fm = self.mode.currentData() == MODE_FM
        self.tone_sq.setEnabled(fm)
        self.deemph.setEnabled(fm)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color or "#ffffff"), self)
        if c.isValid():
            self._color = c.name()
            self._update_color_btn()

    def _update_color_btn(self) -> None:
        self.color_btn.setText(self._color or "auto")
        self.color_btn.setStyleSheet(f"background:{self._color or '#888888'}; color:black")

    def selected_groups(self) -> set[str]:
        return {self.lists.item(i).text() for i in range(self.lists.count())
                if self.lists.item(i).checkState() == Qt.Checked}

    def apply(self) -> None:
        c = self.cfg
        c.name = clean_text(self.name.text()) or "Channel"
        c.frequency = float(round(self.freq.value() * 1e6))
        c.mode = self.mode.currentData()
        c.bandwidth = float(self.bw.currentData())
        c.enabled = self.enabled.isChecked()
        c.squelch_db = self.squelch.value()
        c.hysteresis_db = self.hyst.value()
        c.hang_time = self.hang.value()
        if c.mode == MODE_FM:
            c.ctcss_hz, c.dcs_code, c.dcs_polarity = self.tone_sq.value()
            c.deemphasis_us = float(self.deemph.currentData())
        else:
            c.ctcss_hz, c.dcs_code, c.deemphasis_us = None, None, 0.0
        c.volume = self.volume.value() / 100
        c.pan = self.pan.value() / 100
        c.audio_device = self.device.currentData() or ""
        c.priority = self.priority.isChecked()
        c.alert = self.alert.isChecked()
        c.record = self.record.isChecked()
        c.hold_time = self.hold.value()
        c.color = clean_color(self._color)
        if self._groups:
            chosen = self.selected_groups()
            for g in self._groups:
                if g.name in chosen and c.id not in g.ids:
                    g.ids.append(c.id)
                elif g.name not in chosen and c.id in g.ids:
                    g.ids.remove(c.id)


class ChannelList(QWidget):
    """Vertical stack of channel strips with selection and drag-and-drop reordering."""

    reorderRequested = Signal(list, int)     # channel ids, insertion index in display order
    selectionChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(2, 2, 2, 2)
        self.lay.setSpacing(2)
        self.lay.addStretch(1)
        self.strips: dict[str, ChannelStrip] = {}
        self.order: list[str] = []
        self.selected: list[str] = []
        self._anchor: Optional[str] = None
        self.empty = QLabel("No channels yet. Use <b>Add channel</b>, <b>Channels → Add from preset…</b> "
                            "or <b>Import CSV…</b>, or double-click the spectrum.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.lay.insertWidget(0, self.empty)
        self._indicator = QFrame(self)
        self._indicator.setFixedHeight(3)
        self._indicator.hide()
        self.setAcceptDrops(True)

    def rebuild(self, channels, connect, compact: bool = False, empty_text: str = "") -> None:
        for s in self.strips.values():
            s.setParent(None)
            s.deleteLater()
        self.strips.clear()
        self.order = [c.id for c in channels]
        for c in channels:
            s = ChannelStrip(c, compact)
            connect(s)
            s.pressed.connect(self._on_pressed)
            s.dragRequested.connect(self._start_drag)
            self.lay.insertWidget(self.lay.count() - 1, s)
            self.strips[c.id] = s
        if empty_text:
            self.empty.setText(empty_text)
        self.empty.setVisible(not self.strips)
        self.selected = [i for i in self.selected if i in self.strips]
        self._apply_selection()

    def refresh_all(self) -> None:
        for s in self.strips.values():
            s.refresh_from_config()

    # ---------------------------------------------------------------- selection
    def set_selection(self, ids) -> None:
        wanted = set(ids)
        self.selected = [i for i in self.order if i in wanted]
        self._apply_selection()
        self.selectionChanged.emit(list(self.selected))

    def select_all(self) -> None:
        self.set_selection(self.order)

    def _apply_selection(self) -> None:
        sel = set(self.selected)
        for cid, s in self.strips.items():
            s.set_selected(cid in sel)

    def _on_pressed(self, cid: str, mods) -> None:
        if mods & Qt.ShiftModifier and self._anchor in self.order:
            a, b = self.order.index(self._anchor), self.order.index(cid)
            lo, hi = min(a, b), max(a, b)
            self.set_selection(self.order[lo:hi + 1])
            return
        if mods & Qt.ControlModifier:
            sel = set(self.selected)
            sel.symmetric_difference_update({cid})
            self._anchor = cid
            self.set_selection(sel)
            return
        self._anchor = cid
        if cid not in self.selected or len(self.selected) > 1:
            self.set_selection([cid])

    # ---------------------------------------------------------------- drag and drop
    def _start_drag(self, cid: str) -> None:
        ids = list(self.selected) if cid in self.selected else [cid]
        mime = QMimeData()
        mime.setData(DRAG_MIME, ",".join(ids).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        strip = self.strips.get(cid)
        if strip is not None:
            pm = strip.grab()
            drag.setPixmap(pm.scaledToWidth(min(pm.width(), 420)))
        drag.exec(Qt.MoveAction)
        self._indicator.hide()

    def drop_index(self, y: float) -> int:
        for i, cid in enumerate(self.order):
            g = self.strips[cid].geometry()
            if y < g.center().y():
                return i
        return len(self.order)

    def _show_indicator(self, index: int) -> None:
        if not self.order:
            return
        if index < len(self.order):
            y = self.strips[self.order[index]].geometry().top() - 2
        else:
            y = self.strips[self.order[-1]].geometry().bottom() + 1
        self._indicator.setStyleSheet(f"background:{theme.tokens()['highlight']}")
        self._indicator.setGeometry(4, max(0, y), self.width() - 8, 3)
        self._indicator.show()
        self._indicator.raise_()

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasFormat(DRAG_MIME):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:
        if ev.mimeData().hasFormat(DRAG_MIME):
            self._show_indicator(self.drop_index(ev.position().y()))
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev) -> None:
        self._indicator.hide()

    def dropEvent(self, ev) -> None:
        self._indicator.hide()
        if not ev.mimeData().hasFormat(DRAG_MIME):
            return
        raw = bytes(ev.mimeData().data(DRAG_MIME)).decode("ascii", "ignore")
        ids = [i for i in raw.split(",") if i in self.strips]
        if ids:
            self.reorderRequested.emit(ids, self.drop_index(ev.position().y()))
            ev.acceptProposedAction()


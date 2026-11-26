"""Channel strip (live controls) and channel edit dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider,
    QToolButton, QVBoxLayout, QWidget,
)

from ..audio import list_output_devices
from ..config import ChannelConfig
from ..dsp import CTCSS_TONES
from ..engine import ChannelStatus
from .widgets import Led, SignalMeter


def _slider(lo: int, hi: int, value: int, width: int = 110) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    s.setFixedWidth(width)
    return s


class ChannelStrip(QFrame):
    """One row in the channel list with live controls."""

    changed = Signal()               # live setting changed (no DSP rebuild needed)
    structureChanged = Signal()      # enable toggled (DSP rebuild)
    editRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, cfg: ChannelConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setFrameShape(QFrame.StyledPanel)
        self._noise = -120.0

        self.swatch = QLabel()
        self.swatch.setFixedSize(6, 34)
        self.enabled = QCheckBox()
        self.enabled.setToolTip("Enable channel")
        self.name = QLabel()
        self.name.setFixedWidth(210)
        self.led = Led()
        self.meter = SignalMeter()
        self.tone = QLabel()
        self.tone.setMinimumWidth(80)
        self.tone.setStyleSheet("color:#aaaaaa")

        self.squelch = _slider(-110, 0, int(cfg.squelch_db))
        self.squelch_lbl = QLabel()
        self.squelch_lbl.setFixedWidth(52)
        self.auto_btn = QToolButton(text="Auto")
        self.auto_btn.setToolTip("Set squelch 8 dB above the current noise floor")
        self.volume = _slider(0, 200, int(cfg.volume * 100), 90)
        self.pan = _slider(-100, 100, int(cfg.pan * 100), 80)
        self.pan.setToolTip("Left / right fade (double-click to centre)")
        self.pan_lbl = QLabel()
        self.pan_lbl.setFixedWidth(46)
        self.pan_lbl.setStyleSheet("color:#aaaaaa")
        self.mute = QToolButton(text="M", checkable=True)
        self.mute.setToolTip("Mute")
        self.solo = QToolButton(text="S", checkable=True)
        self.solo.setToolTip("Solo")
        self.rec = QToolButton(text="REC", checkable=True)
        self.rec.setToolTip("Record each transmission to WAV")
        edit = QToolButton(text="Edit")
        delete = QToolButton(text="✕")
        delete.setToolTip("Delete channel")

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        for w in (self.enabled, self.led, self.name, self.meter, self.tone):
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
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 3, 6, 3)
        outer.addWidget(self.swatch)
        outer.addLayout(col, 1)

        self.refresh_from_config()
        self.enabled.toggled.connect(self._on_enabled)
        self.squelch.valueChanged.connect(self._on_squelch)
        self.auto_btn.clicked.connect(self._on_auto)
        self.volume.valueChanged.connect(self._on_volume)
        self.pan.valueChanged.connect(self._on_pan)
        self.pan.mouseDoubleClickEvent = lambda e: self.pan.setValue(0)
        self.mute.toggled.connect(lambda v: self._set("mute", v))
        self.solo.toggled.connect(lambda v: self._set("solo", v))
        self.rec.toggled.connect(lambda v: self._set("record", v))
        edit.clicked.connect(lambda: self.editRequested.emit(self.cfg.id))
        delete.clicked.connect(lambda: self.deleteRequested.emit(self.cfg.id))

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
        self.swatch.setStyleSheet(f"background:{c.color or '#888'}")
        bw = "12.5k" if c.bandwidth <= 12_500 else "25k"
        tone = f" · CTCSS {c.ctcss_hz:.1f}" if c.ctcss_hz else ""
        self.name.setText(f"<b>{c.name}</b><br><span style='color:#aaa'>"
                          f"{c.frequency / 1e6:.4f} MHz · {bw}{tone}</span>")
        self.squelch_lbl.setText(f"{c.squelch_db:.0f} dB")
        self.volume.setToolTip(f"Volume {c.volume * 100:.0f}%")
        self.pan_lbl.setText("ctr" if abs(c.pan) < 0.01 else f"{'L' if c.pan < 0 else 'R'} {abs(c.pan) * 100:.0f}%")
        self.meter.squelch = c.squelch_db
        self.meter.update()

    def update_status(self, st: ChannelStatus | None) -> None:
        if st is None or st.state in ("disabled", "out_of_band", "stopped"):
            self.led.set_state(False)
            self.meter.set_values(-120, -120, self.cfg.squelch_db, False, dimmed=True)
            self.tone.setText({"out_of_band": "out of band", "disabled": "disabled"}.get(
                st.state if st else "", ""))
            return
        self._noise = st.noise_db
        self.led.set_state(st.open, self.cfg.color or "#66ff66")
        self.meter.set_values(st.rssi_db, st.noise_db, self.cfg.squelch_db, st.open)
        self.tone.setText(f"♪ {st.tone:.1f} Hz" if st.tone else "")

    def _set(self, attr: str, value) -> None:
        setattr(self.cfg, attr, value)
        self.refresh_from_config()
        self.changed.emit()

    def _on_enabled(self, v: bool) -> None:
        self.cfg.enabled = v
        self.structureChanged.emit()

    def _on_squelch(self, v: int) -> None:
        self._set("squelch_db", float(v))

    def _on_auto(self) -> None:
        self.squelch.setValue(int(round(self._noise + 8)))

    def _on_volume(self, v: int) -> None:
        self._set("volume", v / 100.0)

    def _on_pan(self, v: int) -> None:
        self._set("pan", v / 100.0)


class ChannelDialog(QDialog):
    """Add / edit every setting of a channel."""

    def __init__(self, cfg: ChannelConfig, parent=None, title="Channel"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.cfg = cfg
        self._color = cfg.color

        self.name = QLineEdit(cfg.name)
        self.freq = QDoubleSpinBox(decimals=5, minimum=24.0, maximum=1766.0, singleStep=0.0125, suffix=" MHz")
        self.freq.setValue(cfg.frequency / 1e6)
        self.bw = QComboBox()
        self.bw.addItem("12.5 kHz (narrow FM)", 12_500.0)
        self.bw.addItem("25 kHz (wide/legacy FM)", 25_000.0)
        self.bw.setCurrentIndex(0 if cfg.bandwidth <= 12_500 else 1)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(cfg.enabled)

        self.squelch = QDoubleSpinBox(decimals=1, minimum=-120, maximum=0, suffix=" dBFS")
        self.squelch.setValue(cfg.squelch_db)
        self.hyst = QDoubleSpinBox(decimals=1, minimum=0, maximum=20, suffix=" dB")
        self.hyst.setValue(cfg.hysteresis_db)
        self.hang = QDoubleSpinBox(decimals=2, minimum=0, maximum=10, singleStep=0.1, suffix=" s")
        self.hang.setValue(cfg.hang_time)
        self.ctcss = QComboBox()
        self.ctcss.addItem("Off (carrier squelch)", None)
        for t in CTCSS_TONES:
            self.ctcss.addItem(f"{t:.1f} Hz", t)
        if cfg.ctcss_hz:
            i = self.ctcss.findData(min(CTCSS_TONES, key=lambda t: abs(t - cfg.ctcss_hz)))
            self.ctcss.setCurrentIndex(max(0, i))
        self.deemph = QComboBox()
        self.deemph.addItem("750 µs (standard land-mobile)", 750.0)
        self.deemph.addItem("Off", 0.0)
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
        self.record = QCheckBox("Record each transmission to WAV")
        self.record.setChecked(cfg.record)
        self.color_btn = QPushButton()
        self.color_btn.clicked.connect(self._pick_color)
        self._update_color_btn()

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Frequency", self.freq)
        form.addRow("Bandwidth", self.bw)
        form.addRow("", self.enabled)
        form.addRow(QLabel("<b>Squelch</b>"))
        form.addRow("Open level", self.squelch)
        form.addRow("Hysteresis", self.hyst)
        form.addRow("Hang time", self.hang)
        form.addRow("CTCSS tone", self.ctcss)
        form.addRow("De-emphasis", self.deemph)
        form.addRow(QLabel("<b>Audio</b>"))
        form.addRow("Volume", self.volume)
        form.addRow("Pan / fade", self.pan)
        form.addRow("Output device", self.device)
        form.addRow("", self.record)
        form.addRow("Colour", self.color_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color or "#ffffff"), self)
        if c.isValid():
            self._color = c.name()
            self._update_color_btn()

    def _update_color_btn(self) -> None:
        self.color_btn.setText(self._color or "auto")
        self.color_btn.setStyleSheet(f"background:{self._color or '#888'}; color:black")

    def apply(self) -> None:
        c = self.cfg
        c.name = self.name.text().strip() or "Channel"
        c.frequency = round(self.freq.value() * 1e6)
        c.bandwidth = float(self.bw.currentData())
        c.enabled = self.enabled.isChecked()
        c.squelch_db = self.squelch.value()
        c.hysteresis_db = self.hyst.value()
        c.hang_time = self.hang.value()
        c.ctcss_hz = self.ctcss.currentData()
        c.deemphasis_us = float(self.deemph.currentData())
        c.volume = self.volume.value() / 100
        c.pan = self.pan.value() / 100
        c.audio_device = self.device.currentData() or ""
        c.record = self.record.isChecked()
        c.color = self._color


class ChannelList(QWidget):
    """Vertical stack of channel strips."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(2, 2, 2, 2)
        self.lay.setSpacing(3)
        self.lay.addStretch(1)
        self.strips: dict[str, ChannelStrip] = {}

    def rebuild(self, channels, connect) -> None:
        for s in self.strips.values():
            s.setParent(None)
            s.deleteLater()
        self.strips.clear()
        for c in channels:
            s = ChannelStrip(c)
            connect(s)
            self.lay.insertWidget(self.lay.count() - 1, s)
            self.strips[c.id] = s

"""Display settings dialog (spectrum/waterfall resolution, levels, colours)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QSlider, QVBoxLayout,
)

from .view import COLORMAPS, FFT_SIZES, LABEL_MODES, WATERFALL_SPEEDS, ViewSettings


class DisplayDialog(QDialog):
    def __init__(self, view: ViewSettings, sample_rate: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Display settings")
        self.view = view
        self.fft = QComboBox()
        for n in FFT_SIZES:
            res = f" ({sample_rate / n / 1e3:.2f} kHz per bin)" if sample_rate else ""
            self.fft.addItem(f"{n}{res}", n)
        self.fft.setCurrentIndex(self.fft.findData(view.fft_size))
        self.avg = QSlider(Qt.Horizontal)
        self.avg.setRange(0, 95)
        self.avg.setValue(int(round(view.averaging * 100)))
        self.avg_lbl = QLabel()
        self.avg.valueChanged.connect(lambda v: self.avg_lbl.setText(f"{v}%"))
        self.avg_lbl.setText(f"{self.avg.value()}%")
        self.speed = QComboBox()
        for k, label in WATERFALL_SPEEDS:
            self.speed.addItem(label, k)
        self.speed.setCurrentIndex(self.speed.findData(view.waterfall_speed))
        self.cmap = QComboBox()
        for k, label in COLORMAPS:
            self.cmap.addItem(label, k)
        self.cmap.setCurrentIndex(self.cmap.findData(view.colormap))
        self.auto = QCheckBox("Automatic levels")
        self.auto.setChecked(view.auto_levels)
        self.lmin = QDoubleSpinBox(decimals=0, minimum=-160, maximum=0, suffix=" dBFS")
        self.lmin.setValue(view.level_min)
        self.lmax = QDoubleSpinBox(decimals=0, minimum=-160, maximum=0, suffix=" dBFS")
        self.lmax.setValue(view.level_max)
        self.auto.toggled.connect(self._sync)
        self.peak = QCheckBox("Peak hold trace on the spectrum")
        self.peak.setChecked(view.peak_hold)
        self.labels = QComboBox()
        for k, label in LABEL_MODES:
            self.labels.addItem(label, k)
        self.labels.setCurrentIndex(self.labels.findData(view.channel_labels))

        form = QFormLayout()
        form.addRow("FFT size", self.fft)
        form.addRow("Spectrum averaging", self.avg)
        form.addRow("", self.avg_lbl)
        form.addRow("Waterfall speed", self.speed)
        form.addRow("Waterfall colours", self.cmap)
        form.addRow("", self.auto)
        form.addRow("Level floor", self.lmin)
        form.addRow("Level ceiling", self.lmax)
        form.addRow("", self.peak)
        form.addRow("Channel labels", self.labels)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self._sync()

    def _sync(self) -> None:
        manual = not self.auto.isChecked()
        self.lmin.setEnabled(manual)
        self.lmax.setEnabled(manual)

    def apply(self) -> None:
        v = self.view
        v.fft_size = int(self.fft.currentData())
        v.averaging = self.avg.value() / 100.0
        v.waterfall_speed = int(self.speed.currentData())
        v.colormap = self.cmap.currentData()
        v.auto_levels = self.auto.isChecked()
        v.level_min = self.lmin.value()
        v.level_max = self.lmax.value()
        v.peak_hold = self.peak.isChecked()
        v.channel_labels = self.labels.currentData()
        v.validate()

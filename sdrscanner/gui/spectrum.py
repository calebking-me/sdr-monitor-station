"""Wideband spectrum + waterfall with channel markers."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

WATERFALL_ROWS = 300
DISPLAY_BINS = 1024


class SpectrumView(QWidget):
    """Top: averaged spectrum. Bottom: scrolling waterfall. Both in MHz."""

    frequencyDoubleClicked = Signal(float)   # Hz

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, background="#101010", foreground="#cccccc")
        self._lo = 0.0
        self._hi = 1.0
        self._wf = np.full((WATERFALL_ROWS, DISPLAY_BINS), -120.0, dtype=np.float32)
        self._avg = None
        self._levels = None
        self._markers: dict[str, tuple] = {}

        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setLabel("left", "dBFS")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spec_plot.setMouseEnabled(x=False, y=False)
        self.spec_plot.setMenuEnabled(False)
        self.spec_plot.hideButtons()
        self.spec_plot.setYRange(-110, -10)
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#7fd3ff", width=1))
        self.center_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ffffff", style=Qt.DashLine, width=1))
        self.spec_plot.addItem(self.center_line)
        self.spec_plot.scene().sigMouseClicked.connect(self._on_click)

        self.wf_plot = pg.PlotWidget()
        self.wf_plot.setLabel("bottom", "Frequency", units="MHz")
        self.wf_plot.setMouseEnabled(x=False, y=False)
        self.wf_plot.setMenuEnabled(False)
        self.wf_plot.hideButtons()
        self.wf_plot.hideAxis("left")
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_img = pg.ImageItem(axisOrder="row-major")
        self.wf_img.setColorMap(pg.colormap.get("inferno"))
        # Give the image its real shape before any setRect(), or the scaling is computed for 1x1.
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(-120, -40))
        self.wf_plot.addItem(self.wf_img)
        self.wf_plot.invertY(True)
        self.wf_plot.scene().sigMouseClicked.connect(self._on_click)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.spec_plot)
        split.addWidget(self.wf_plot)
        split.setSizes([180, 320])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

    # ---------------------------------------------------------------- data
    def set_band(self, lo_hz: float, hi_hz: float, center_hz: float) -> None:
        lo, hi = lo_hz / 1e6, hi_hz / 1e6
        if (lo, hi) == (self._lo, self._hi):
            return
        self._lo, self._hi = lo, hi
        self._wf.fill(-120.0)
        self._avg = None
        self.spec_plot.setXRange(lo, hi, padding=0)
        self.wf_img.setRect(pg.QtCore.QRectF(lo, 0, hi - lo, WATERFALL_ROWS))
        self.wf_plot.setYRange(0, WATERFALL_ROWS, padding=0)
        self.center_line.setValue(center_hz / 1e6)

    def add_rows(self, rows: list[np.ndarray]) -> None:
        if not rows:
            return
        disp = []
        for r in rows:
            if len(r) != DISPLAY_BINS and len(r) % DISPLAY_BINS == 0:
                r = r.reshape(DISPLAY_BINS, -1).max(axis=1)
            disp.append(r)
        block = np.stack(disp)[::-1]
        k = min(len(block), WATERFALL_ROWS)
        self._wf[k:] = self._wf[:-k]
        self._wf[:k] = block[:k]
        last = disp[-1]
        self._avg = last if self._avg is None or len(self._avg) != len(last) else 0.6 * self._avg + 0.4 * last
        x = np.linspace(self._lo, self._hi, len(self._avg), endpoint=False)
        self.spec_curve.setData(x, self._avg)
        lo = float(np.percentile(last, 20)) - 3
        hi = max(lo + 35.0, float(np.max(last)) + 3)
        if self._levels is None:
            self._levels = [lo, hi]
        else:
            self._levels[0] += 0.1 * (lo - self._levels[0])
            self._levels[1] += 0.1 * (hi - self._levels[1])
        self.wf_img.setImage(self._wf, autoLevels=False, levels=tuple(self._levels))
        self.spec_plot.setYRange(self._levels[0] - 10, self._levels[1] + 10, padding=0)

    # ---------------------------------------------------------------- markers
    def set_channels(self, channels) -> None:
        """channels: iterable of ChannelConfig (enabled ones are drawn)."""
        wanted = {c.id: c for c in channels if c.enabled}
        for cid in list(self._markers):
            if cid not in wanted:
                for item, plot in zip(self._markers.pop(cid)[:3], (self.spec_plot, self.wf_plot, self.spec_plot)):
                    plot.removeItem(item)
        for cid, c in wanted.items():
            lo = (c.frequency - c.bandwidth / 2) / 1e6
            hi = (c.frequency + c.bandwidth / 2) / 1e6
            if cid not in self._markers:
                r1 = pg.LinearRegionItem(values=(lo, hi), movable=False)
                r2 = pg.LinearRegionItem(values=(lo, hi), movable=False)
                label = pg.TextItem(c.name, anchor=(0.5, 0.0))
                self.spec_plot.addItem(r1)
                self.wf_plot.addItem(r2)
                self.spec_plot.addItem(label)
                self._markers[cid] = (r1, r2, label, None)
            r1, r2, label, _ = self._markers[cid]
            r1.setRegion((lo, hi))
            r2.setRegion((lo, hi))
            label.setText(c.name, color=c.color)
            label.setPos(c.frequency / 1e6, self._levels[1] + 9 if self._levels else -20)
            self._markers[cid] = (r1, r2, label, c.color)
            self._style(cid, False)

    def set_active(self, active: dict[str, bool]) -> None:
        for cid, is_open in active.items():
            if cid in self._markers:
                self._style(cid, is_open)
        if self._levels:
            for r1, r2, label, _ in self._markers.values():
                label.setPos(label.pos().x(), self._levels[1] + 9)

    def _style(self, cid: str, is_open: bool) -> None:
        r1, r2, label, color = self._markers[cid]
        col = QColor(color or "#ffffff")
        for r, a_idle, a_open in ((r1, 40, 150), (r2, 25, 110)):
            c = QColor(col)
            c.setAlpha(a_open if is_open else a_idle)
            r.setBrush(pg.mkBrush(c))
            edge = QColor(col)
            edge.setAlpha(255 if is_open else 120)
            for line in r.lines:
                line.setPen(pg.mkPen(edge, width=2 if is_open else 1))
        r1.update()
        r2.update()

    def _on_click(self, ev) -> None:
        if not ev.double():
            return
        for plot in (self.spec_plot, self.wf_plot):
            vb = plot.getViewBox()
            if vb.sceneBoundingRect().contains(ev.scenePos()):
                f = vb.mapSceneToView(ev.scenePos()).x() * 1e6
                step = 6_250.0
                self.frequencyDoubleClicked.emit(round(f / step) * step)
                return

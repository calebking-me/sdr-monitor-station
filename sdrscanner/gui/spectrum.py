"""Wideband spectrum + waterfall with channel markers, zoom and display options."""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QMenu, QSplitter, QVBoxLayout, QWidget

from ..config import clean_color
from . import theme
from .view import ViewSettings

WATERFALL_ROWS = 300
MIN_SPAN_MHZ = 0.005
FREQ_STEP_HZ = 6_250.0
LEVEL_HYSTERESIS_DB = 1.5      # re-range the spectrum only when auto levels move this much
BAND_MEMORY_BYTES = 48 * 1024 * 1024   # waterfalls kept for other tunings (scan sets)


class ChannelBands(pg.GraphicsObject):
    """Every channel's passband in one plot, drawn in a single paint call.

    One item per plot instead of one LinearRegionItem (+ two InfiniteLines) per
    channel keeps the GUI cheap with dozens of channels (e.g. the 40 CB presets).
    """

    def __init__(self, idle_alpha: int, open_alpha: int):
        super().__init__()
        self.idle_alpha, self.open_alpha = idle_alpha, open_alpha
        self._bands: list[tuple[float, float, str, bool]] = []     # (lo MHz, hi MHz, colour, active)
        self._xr = (0.0, 0.0)
        self.setZValue(-10)

    def set_bands(self, bands) -> None:
        self.prepareGeometryChange()
        self._bands = list(bands)
        if self._bands:
            self._xr = (min(b[0] for b in self._bands), max(b[1] for b in self._bands))
        self.update()

    def viewRangeChanged(self) -> None:
        self.prepareGeometryChange()

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        return self._xr if axis == 0 and self._bands else None

    def boundingRect(self) -> QRectF:
        vr = self.viewRect()
        if vr is None or not self._bands:
            return QRectF()
        lo, hi = self._xr
        return QRectF(lo, vr.top(), max(hi - lo, 1e-9), vr.height())

    def paint(self, p, *args) -> None:
        vr = self.viewRect()
        if vr is None:
            return
        top, h = vr.top(), vr.height()
        light = theme.name() == "light"
        for lo, hi, color, active in self._bands:
            base = QColor(color)
            fill = QColor(base)
            fill.setAlpha(self.open_alpha if active else self.idle_alpha + (15 if light else 0))
            p.fillRect(QRectF(lo, top, hi - lo, h), fill)
            edge = QColor(base.darker(140) if light else base)
            edge.setAlpha(255 if active else 140)
            pen = QPen(edge)
            pen.setCosmetic(True)
            pen.setWidthF(2.0 if active else 1.0)
            p.setPen(pen)
            p.drawLine(QPointF(lo, top), QPointF(lo, top + h))
            p.drawLine(QPointF(hi, top), QPointF(hi, top + h))


class SpectrumView(QWidget):
    """Top: averaged spectrum (+ optional peak hold). Bottom: scrolling waterfall. Both in MHz."""

    frequencyDoubleClicked = Signal(float)    # Hz
    addChannelRequested = Signal(float)       # Hz (context menu)
    zoomToChannelsRequested = Signal()

    def __init__(self, view: ViewSettings, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False)
        self._lo, self._hi, self._center = 0.0, 1.0, 0.5
        self._wf = np.full((WATERFALL_ROWS, view.fft_size), -120.0, dtype=np.float32)
        self._avg = None
        self._peak = None
        self._levels = None
        self._pending: list[np.ndarray] = []
        # While scanning, each tuning (scan set) keeps its own waterfall, average and zoom.
        self._band_mem: OrderedDict[tuple[float, float], dict] = OrderedDict()
        self._band_set = False
        self._markers: dict[str, dict] = {}      # cid -> {lo, hi, color, active, label}
        self._applied_range = None
        self._label_mode = view.channel_labels
        self._averaging = view.averaging
        self._speed = view.waterfall_speed
        self._auto_levels = view.auto_levels
        self._manual_levels = (view.level_min, view.level_max)
        self._peak_on = view.peak_hold
        self.extra_actions: list = []          # added to the right-click menu by the main window

        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setLabel("left", "dBFS")
        self.spec_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spec_plot.setMouseEnabled(x=True, y=False)
        self.spec_plot.setMenuEnabled(False)
        self.spec_plot.hideButtons()
        self.spec_plot.setYRange(-110, -10)
        self.spec_curve = self.spec_plot.plot()
        self.peak_curve = self.spec_plot.plot()
        self.peak_curve.setVisible(self._peak_on)
        self.center_line = pg.InfiniteLine(angle=90)
        self.spec_plot.addItem(self.center_line)
        self.spec_plot.scene().sigMouseClicked.connect(self._on_click)
        self.bands_spec = ChannelBands(40, 150)
        self.spec_plot.addItem(self.bands_spec)

        self.wf_plot = pg.PlotWidget()
        self.wf_plot.setLabel("bottom", "Frequency", units="MHz")
        self.wf_plot.setMouseEnabled(x=True, y=False)
        self.wf_plot.setMenuEnabled(False)
        self.wf_plot.hideButtons()
        self.wf_plot.hideAxis("left")
        self.wf_plot.setXLink(self.spec_plot)
        self.wf_img = pg.ImageItem(axisOrder="row-major")
        self.set_colormap(view.colormap)
        # Give the image its real shape before any setRect(), or the scaling is computed for 1x1.
        self.wf_img.setImage(self._wf, autoLevels=False, levels=(-120, -40))
        self.wf_plot.addItem(self.wf_img)
        self.wf_plot.invertY(True)
        self.wf_plot.setYRange(0, WATERFALL_ROWS, padding=0)
        self.wf_plot.scene().sigMouseClicked.connect(self._on_click)
        self.bands_wf = ChannelBands(25, 110)
        self.bands_wf.setZValue(10)              # above the waterfall image
        self.wf_plot.addItem(self.bands_wf)

        self.split = QSplitter(Qt.Vertical)
        self.split.addWidget(self.spec_plot)
        self.split.addWidget(self.wf_plot)
        self.split.setSizes([180, 320])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.split)
        self.set_parts(view.show_spectrum, view.show_waterfall)
        self.apply_theme()

    # ---------------------------------------------------------------- appearance
    def apply_theme(self) -> None:
        t = theme.tokens()
        for plot in (self.spec_plot, self.wf_plot):
            plot.setBackground(t["plot_bg"])
            for name in ("left", "bottom"):
                ax = plot.getAxis(name)
                ax.setPen(pg.mkPen(t["plot_fg"]))
                ax.setTextPen(pg.mkPen(t["plot_fg"]))
        self.spec_curve.setPen(pg.mkPen(t["curve"], width=1))
        self.peak_curve.setPen(pg.mkPen(t["peak"], width=1, style=Qt.DotLine))
        self.center_line.setPen(pg.mkPen(t["center_line"], style=Qt.DashLine, width=1))
        for m in self._markers.values():
            m["label"].setColor(theme.channel_text_color(m["color"]))
        self._refresh_bands()

    def set_colormap(self, name: str) -> None:
        try:
            self.wf_img.setColorMap(pg.colormap.get(name))
        except Exception:
            self.wf_img.setColorMap(pg.colormap.get("inferno"))

    def set_options(self, averaging: float, speed: int, auto_levels: bool, level_min: float,
                    level_max: float, peak_hold: bool, labels: str) -> None:
        self._averaging = averaging
        self._speed = max(1, int(speed))
        self._pending = []
        self._auto_levels = auto_levels
        self._manual_levels = (level_min, level_max)
        if not auto_levels:
            self._levels = [level_min, level_max]
        self.set_peak_hold(peak_hold)
        self.set_label_mode(labels)
        self._apply_levels(force=True)

    def set_peak_hold(self, on: bool) -> None:
        self._peak_on = on
        self._peak = None
        self.peak_curve.setVisible(on)
        if not on:
            self.peak_curve.setData([], [])

    def reset_peak(self) -> None:
        self._peak = None

    def set_label_mode(self, mode: str) -> None:
        self._label_mode = mode
        for m in self._markers.values():
            self._update_label_visibility(m)

    def set_parts(self, spectrum: bool, waterfall: bool) -> None:
        self.spec_plot.setVisible(spectrum)
        self.wf_plot.setVisible(waterfall)

    # ---------------------------------------------------------------- zoom
    def zoom_full(self) -> None:
        self.spec_plot.setXRange(self._lo, self._hi, padding=0)

    def zoom_to(self, lo_hz: float, hi_hz: float) -> None:
        lo, hi = lo_hz / 1e6, hi_hz / 1e6
        margin = max((hi - lo) * 0.08, 0.01)
        lo, hi = max(self._lo, lo - margin), min(self._hi, hi + margin)
        if hi - lo >= MIN_SPAN_MHZ:
            self.spec_plot.setXRange(lo, hi, padding=0)

    def visible_range_hz(self) -> tuple[float, float]:
        lo, hi = self.spec_plot.getViewBox().viewRange()[0]
        return lo * 1e6, hi * 1e6

    # ---------------------------------------------------------------- data
    def set_band(self, lo_hz: float, hi_hz: float, center_hz: float) -> None:
        """Show the band of the current tuning. A band shown before gets its waterfall back."""
        lo, hi = lo_hz / 1e6, hi_hz / 1e6
        self._center = center_hz / 1e6
        self.center_line.setValue(self._center)
        if (lo, hi) == (self._lo, self._hi):
            return
        if self._band_set:
            self._remember_band()
        saved = self._band_mem.pop((lo, hi), None)
        self._lo, self._hi = lo, hi
        self._band_set = True
        view = (lo, hi)
        self._pending = []
        if saved is not None and saved["wf"].shape == self._wf.shape:
            self._wf = saved["wf"].astype(np.float32)
            self._avg, self._peak = saved["avg"], saved["peak"]
            view = saved["view"]
        else:
            self._wf = np.full(self._wf.shape, -120.0, dtype=np.float32)
            self._avg = self._peak = None
        for plot in (self.spec_plot, self.wf_plot):
            plot.getViewBox().setLimits(xMin=lo, xMax=hi, minXRange=MIN_SPAN_MHZ)
        self.spec_plot.setXRange(*view, padding=0)
        self.wf_img.setImage(self._wf, autoLevels=False, levels=self._current_levels())
        self.wf_img.setRect(QRectF(lo, 0, hi - lo, WATERFALL_ROWS))
        x = np.linspace(lo, hi, len(self._avg), endpoint=False) if self._avg is not None else []
        self.spec_curve.setData(x, self._avg if self._avg is not None else [])
        if self._peak_on:
            self.peak_curve.setData(x if self._peak is not None else [], self._peak if self._peak is not None else [])

    def _remember_band(self) -> None:
        key = (self._lo, self._hi)
        lo, hi = self.spec_plot.getViewBox().viewRange()[0]
        self._band_mem[key] = {"wf": self._wf.astype(np.float16), "avg": self._avg, "peak": self._peak,
                               "view": (max(lo, self._lo), min(hi, self._hi))}
        self._band_mem.move_to_end(key)
        total = sum(m["wf"].nbytes for m in self._band_mem.values())
        while total > BAND_MEMORY_BYTES and len(self._band_mem) > 1:
            _, old = self._band_mem.popitem(last=False)
            total -= old["wf"].nbytes

    def forget_bands(self) -> None:
        """Drop the waterfalls kept for other tunings."""
        self._band_mem.clear()

    def _current_levels(self) -> tuple[float, float]:
        if not self._auto_levels or self._levels is None:
            return self._manual_levels if not self._auto_levels else (-120.0, -40.0)
        return tuple(self._levels)

    def add_rows(self, rows: list[np.ndarray]) -> None:
        if not rows:
            return
        width = len(rows[-1])
        if self._wf.shape[1] != width:            # FFT size changed
            self._wf = np.full((WATERFALL_ROWS, width), -120.0, dtype=np.float32)
            self._avg = self._peak = None
            self._pending = []
            self.wf_img.setImage(self._wf, autoLevels=False, levels=self._current_levels())
            self.wf_img.setRect(QRectF(self._lo, 0, self._hi - self._lo, WATERFALL_ROWS))
        rows = [r for r in rows if len(r) == width]
        lines = []
        for r in rows:
            self._pending.append(r)
            if len(self._pending) >= self._speed:
                lines.append(np.mean(self._pending, axis=0) if self._speed > 1 else self._pending[0])
                self._pending = []
        if lines:
            block = np.stack(lines)[::-1]
            k = min(len(block), WATERFALL_ROWS)
            self._wf[k:] = self._wf[:-k]
            self._wf[:k] = block[:k]

        last = rows[-1]
        a = self._averaging
        if self._avg is None or len(self._avg) != len(last) or a <= 0:
            self._avg = last.copy()
        else:
            self._avg = a * self._avg + (1 - a) * last
        x = np.linspace(self._lo, self._hi, len(self._avg), endpoint=False)
        self.spec_curve.setData(x, self._avg)
        if self._peak_on:
            self._peak = self._avg.copy() if self._peak is None else np.maximum(self._peak - 0.15, self._avg)
            self.peak_curve.setData(x, self._peak)

        if self._auto_levels:
            lo = float(np.percentile(last, 20)) - 3
            hi = max(lo + 35.0, float(np.max(last)) + 3)
            if self._levels is None:
                self._levels = [lo, hi]
            else:
                self._levels[0] += 0.1 * (lo - self._levels[0])
                self._levels[1] += 0.1 * (hi - self._levels[1])
        self.wf_img.setImage(self._wf, autoLevels=False, levels=self._current_levels())
        self._apply_levels()

    def _apply_levels(self, force: bool = False) -> None:
        lo, hi = self._current_levels()
        last = self._applied_range
        if not force and last is not None and abs(last[0] - lo) < LEVEL_HYSTERESIS_DB \
                and abs(last[1] - hi) < LEVEL_HYSTERESIS_DB:
            return              # avoid re-ranging (and fully repainting) the plot every frame
        self._applied_range = (lo, hi)
        self.spec_plot.setYRange(lo - 10, hi + 10, padding=0)
        for m in self._markers.values():
            m["label"].setPos(m["label"].pos().x(), hi + 9)

    # ---------------------------------------------------------------- markers
    def set_channels(self, channels) -> None:
        """channels: iterable of ChannelConfig (enabled ones are drawn)."""
        wanted = {c.id: c for c in channels if c.enabled}
        for cid in list(self._markers):
            if cid not in wanted:
                self.spec_plot.removeItem(self._markers.pop(cid)["label"])
        top = (self._applied_range or self._current_levels())[1] + 9
        for cid, c in wanted.items():
            m = self._markers.get(cid)
            if m is None:
                label = pg.TextItem("", anchor=(0.5, 0.0))     # plain text, never HTML
                self.spec_plot.addItem(label)
                m = self._markers[cid] = {"active": False, "label": label}
            m["lo"] = (c.frequency - c.bandwidth / 2) / 1e6
            m["hi"] = (c.frequency + c.bandwidth / 2) / 1e6
            m["color"] = clean_color(c.color) or "#ffffff"
            m["label"].setText(c.name, color=theme.channel_text_color(m["color"]))
            m["label"].setPos(c.frequency / 1e6, top)
            self._update_label_visibility(m)
        self._refresh_bands()

    def set_active(self, active: dict[str, bool]) -> None:
        changed = False
        for cid, is_open in active.items():
            m = self._markers.get(cid)
            if m is not None and m["active"] != is_open:
                m["active"] = is_open
                self._update_label_visibility(m)
                changed = True
        if changed:
            self._refresh_bands()

    def _refresh_bands(self) -> None:
        bands = [(m["lo"], m["hi"], m["color"], m["active"]) for m in self._markers.values()]
        self.bands_spec.set_bands(bands)
        self.bands_wf.set_bands(bands)

    def _update_label_visibility(self, m: dict) -> None:
        mode = self._label_mode
        m["label"].setVisible(mode == "all" or (mode == "active" and m["active"]))

    # ---------------------------------------------------------------- mouse
    def _freq_at(self, ev) -> float | None:
        for plot in (self.spec_plot, self.wf_plot):
            if not plot.isVisible():
                continue
            vb = plot.getViewBox()
            if vb.sceneBoundingRect().contains(ev.scenePos()):
                f = vb.mapSceneToView(ev.scenePos()).x() * 1e6
                return round(f / FREQ_STEP_HZ) * FREQ_STEP_HZ
        return None

    def _on_click(self, ev) -> None:
        f = self._freq_at(ev)
        if f is None:
            return
        if ev.button() == Qt.LeftButton and ev.double():
            self.frequencyDoubleClicked.emit(f)
        elif ev.button() == Qt.RightButton:
            ev.accept()
            menu = QMenu(self)
            menu.addAction(f"Add channel at {f / 1e6:.4f} MHz…", lambda: self.addChannelRequested.emit(f))
            menu.addSeparator()
            menu.addAction("Zoom to channels", self.zoomToChannelsRequested.emit)
            menu.addAction("Show full band", self.zoom_full)
            if self._peak_on:
                menu.addAction("Reset peak hold", self.reset_peak)
            if self.extra_actions:
                menu.addSeparator()
                for a in self.extra_actions:
                    menu.addAction(a)
            menu.popup(ev.screenPos().toPoint())

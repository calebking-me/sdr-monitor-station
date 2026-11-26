"""Per-channel activity timeline: one row per channel, bars for transmissions."""

from __future__ import annotations

import html
import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from . import theme

ROW_H = 22
LABEL_W = 200
AXIS_H = 20


class ActivityTimeline(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.minutes = 10
        self._channels = []
        self._history = []
        self._bars: list[tuple[QRectF, str]] = []
        self.setMinimumHeight(AXIS_H + ROW_H * 2)
        self.setMouseTracking(True)

    def set_data(self, channels, history, minutes: int) -> None:
        self._channels = list(channels)
        self._history = history
        self.minutes = max(1, int(minutes))
        self.setMinimumHeight(AXIS_H + ROW_H * max(2, len(self._channels)) + 4)
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        for rect, text in reversed(self._bars):
            if rect.adjusted(-2, 0, 2, 0).contains(pos):
                # Escape: channel names are user data and must not be rendered as HTML.
                rich = "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"
                QToolTip.showText(ev.globalPosition().toPoint(), rich, self)
                return
        QToolTip.hideText()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, theme.color("tl_bg"))
        now = time.time()
        span = self.minutes * 60.0
        t0 = now - span
        plot_w = max(10, w - LABEL_W - 8)

        def tx(t: float) -> float:
            return LABEL_W + (t - t0) / span * plot_w

        fm = QFontMetrics(p.font())
        ticks = 6 if self.minutes >= 6 else self.minutes
        for i in range(ticks + 1):
            t = t0 + span * i / ticks
            x = tx(t)
            p.setPen(QPen(theme.color("tl_grid"), 1))
            p.drawLine(int(x), AXIS_H, int(x), h)
            p.setPen(theme.color("tl_text"))
            label = "now" if i == ticks else datetime.fromtimestamp(t).strftime("%H:%M:%S")
            tw = fm.horizontalAdvance(label)
            p.drawText(int(min(max(x - tw / 2, LABEL_W), w - tw - 2)), AXIS_H - 6, label)

        rows = {c.id: i for i, c in enumerate(self._channels)}
        for i, c in enumerate(self._channels):
            y = AXIS_H + i * ROW_H
            if i % 2:
                p.fillRect(QRectF(0, y, w, ROW_H), theme.color("tl_alt"))
            p.setPen(theme.channel_text_color(c.color) if c.enabled else theme.color("tl_disabled"))
            label = fm.elidedText(f"{c.name}  {c.frequency / 1e6:.4f}", Qt.ElideRight, LABEL_W - 12)
            p.drawText(QRectF(6, y, LABEL_W - 10, ROW_H), Qt.AlignVCenter | Qt.AlignLeft, label)

        self._bars = []
        for cid, start, end in self._history:
            i = rows.get(cid)
            if i is None:
                continue
            end_t = end if end is not None else now
            if end_t < t0:
                continue
            x1, x2 = tx(max(start, t0)), tx(end_t)
            col = theme.channel_text_color(self._channels[i].color) if theme.name() == "light" \
                else QColor(self._channels[i].color or "#4fc3f7")
            if end is None:
                col = col.lighter(130)
            y = AXIS_H + i * ROW_H + 4
            rect = QRectF(x1, y, max(2.0, x2 - x1), ROW_H - 8)
            p.fillRect(rect, col)
            c = self._channels[i]
            tip = (f"{c.name} ({c.frequency / 1e6:.4f} MHz)\n"
                   f"{datetime.fromtimestamp(start):%H:%M:%S} - "
                   f"{'now' if end is None else datetime.fromtimestamp(end).strftime('%H:%M:%S')}"
                   f"  ({end_t - start:.1f} s)")
            self._bars.append((rect, tip))

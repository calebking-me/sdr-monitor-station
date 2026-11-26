"""Per-channel activity timeline: one row per channel, bars for transmissions."""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

ROW_H = 22
LABEL_W = 200
AXIS_H = 20


class ActivityTimeline(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.minutes = 10
        self._channels = []
        self._history = []
        self.setMinimumHeight(AXIS_H + ROW_H * 2)

    def set_data(self, channels, history, minutes: int) -> None:
        self._channels = list(channels)
        self._history = history
        self.minutes = max(1, int(minutes))
        self.setMinimumHeight(AXIS_H + ROW_H * max(2, len(self._channels)) + 4)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#141414"))
        now = time.time()
        span = self.minutes * 60.0
        t0 = now - span
        plot_w = max(10, w - LABEL_W - 8)

        def tx(t: float) -> float:
            return LABEL_W + (t - t0) / span * plot_w

        # Time grid
        p.setPen(QPen(QColor("#333333"), 1))
        fm = QFontMetrics(p.font())
        ticks = 6 if self.minutes >= 6 else self.minutes
        for i in range(ticks + 1):
            t = t0 + span * i / ticks
            x = tx(t)
            p.setPen(QPen(QColor("#333333"), 1))
            p.drawLine(int(x), AXIS_H, int(x), h)
            p.setPen(QColor("#aaaaaa"))
            label = "now" if i == ticks else datetime.fromtimestamp(t).strftime("%H:%M:%S")
            tw = fm.horizontalAdvance(label)
            p.drawText(int(min(max(x - tw / 2, LABEL_W), w - tw - 2)), AXIS_H - 6, label)

        rows = {c.id: i for i, c in enumerate(self._channels)}
        for c, i in ((c, rows[c.id]) for c in self._channels):
            y = AXIS_H + i * ROW_H
            if i % 2:
                p.fillRect(QRectF(0, y, w, ROW_H), QColor("#1a1a1a"))
            p.setPen(QColor(c.color or "#ffffff") if c.enabled else QColor("#666666"))
            p.drawText(QRectF(6, y, LABEL_W - 10, ROW_H), Qt.AlignVCenter | Qt.AlignLeft,
                       f"{c.name}  {c.frequency / 1e6:.4f}")

        for cid, start, end in self._history:
            i = rows.get(cid)
            if i is None:
                continue
            end_t = end if end is not None else now
            if end_t < t0:
                continue
            x1, x2 = tx(max(start, t0)), tx(end_t)
            col = QColor(self._channels[i].color or "#4fc3f7")
            if end is None:
                col = col.lighter(130)
            y = AXIS_H + i * ROW_H + 4
            p.fillRect(QRectF(x1, y, max(2.0, x2 - x1), ROW_H - 8), col)

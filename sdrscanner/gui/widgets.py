"""Small custom-painted widgets."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

METER_MIN_DB = -110.0
METER_MAX_DB = 0.0


class Led(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self._color = QColor("#66ff66")
        self.setFixedSize(16, 16)

    def set_state(self, on: bool, color: str | None = None) -> None:
        if color:
            self._color = QColor(color)
        if on != self._on or color:
            self._on = on
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._color if self._on else QColor("#333333")
        p.setBrush(c)
        p.setPen(QPen(QColor("#111111"), 1))
        p.drawEllipse(QRectF(2, 2, 12, 12))


class SignalMeter(QWidget):
    """Horizontal level bar with noise-floor tick and squelch threshold marker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rssi = METER_MIN_DB
        self.noise = METER_MIN_DB
        self.squelch = -50.0
        self.open = False
        self.dimmed = False
        self.setMinimumWidth(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(18)

    def sizeHint(self) -> QSize:
        return QSize(200, 18)

    def set_values(self, rssi: float, noise: float, squelch: float, is_open: bool, dimmed=False):
        self.rssi, self.noise, self.squelch, self.open, self.dimmed = rssi, noise, squelch, is_open, dimmed
        self.update()

    def _x(self, db: float) -> float:
        f = (db - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)
        return max(0.0, min(1.0, f)) * (self.width() - 2) + 1

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#1b1b1b"))
        if not self.dimmed:
            color = QColor("#43c463") if self.open else QColor("#3a6ea5")
            p.fillRect(QRectF(1, 3, self._x(self.rssi) - 1, h - 6), color)
            p.setPen(QPen(QColor("#999999"), 1))
            xn = self._x(self.noise)
            p.drawLine(int(xn), 2, int(xn), h - 2)
        p.setPen(QPen(QColor("#ff5252"), 2))
        xs = self._x(self.squelch)
        p.drawLine(int(xs), 0, int(xs), h)
        p.setPen(QColor("#dddddd"))
        text = "--" if self.dimmed else f"{self.rssi:.1f} dBFS"
        p.drawText(QRectF(4, 0, w - 8, h), Qt.AlignVCenter | Qt.AlignRight, text)

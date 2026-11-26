"""Small custom-painted widgets (theme-aware)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme

METER_MIN_DB = -110.0
METER_MAX_DB = 0.0


class Led(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self._color = QColor("#66ff66")
        self.setFixedSize(16, 16)

    def set_state(self, on: bool, color: str | None = None) -> None:
        new_color = QColor(color) if color else self._color
        if on != self._on or new_color != self._color:
            self._on, self._color = on, new_color
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(self._color if self._on else theme.color("led_off"))
        p.setPen(QPen(theme.color("led_edge"), 1))
        p.drawEllipse(QRectF(2, 2, 12, 12))


class SignalMeter(QWidget):
    """Horizontal level bar with noise-floor tick and squelch threshold marker."""

    def __init__(self, parent=None, height: int = 18):
        super().__init__(parent)
        self.rssi = METER_MIN_DB
        self.noise = METER_MIN_DB
        self.squelch = -50.0
        self.open = False
        self.dimmed = False
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(height)

    def sizeHint(self) -> QSize:
        return QSize(200, self.height())

    def set_values(self, rssi: float, noise: float, squelch: float, is_open: bool, dimmed=False):
        new = (rssi, noise, squelch, is_open, dimmed)
        if new != (self.rssi, self.noise, self.squelch, self.open, self.dimmed):
            self.rssi, self.noise, self.squelch, self.open, self.dimmed = new
            self.update()

    def _x(self, db: float) -> float:
        f = (db - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)
        return max(0.0, min(1.0, f)) * (self.width() - 2) + 1

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, theme.color("meter_bg"))
        if not self.dimmed:
            color = theme.color("meter_open" if self.open else "meter_idle")
            p.fillRect(QRectF(1, 3, self._x(self.rssi) - 1, h - 6), color)
            p.setPen(QPen(theme.color("meter_noise"), 1))
            xn = self._x(self.noise)
            p.drawLine(int(xn), 2, int(xn), h - 2)
        p.setPen(QPen(theme.color("meter_squelch"), 2))
        xs = self._x(self.squelch)
        p.drawLine(int(xs), 0, int(xs), h)
        p.setPen(theme.color("meter_text"))
        text = "--" if self.dimmed else f"{self.rssi:.1f} dBFS"
        p.drawText(QRectF(4, 0, w - 8, h), Qt.AlignVCenter | Qt.AlignRight, text)


def plain_label(text: str = "", parent=None):
    """QLabel that never interprets its text as HTML (for user-supplied strings)."""
    from PySide6.QtWidgets import QLabel
    lbl = QLabel(parent)
    lbl.setTextFormat(Qt.PlainText)
    lbl.setText(text)
    return lbl

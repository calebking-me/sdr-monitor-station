"""Light / dark colour themes shared by the Qt palette and custom-painted widgets."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

THEMES: dict[str, dict[str, str]] = {
    "dark": dict(
        window="#2a2a2a", base="#1e1e1e", alt="#2a2a2a", text="#e0e0e0", muted="#aaaaaa",
        button="#333333", highlight="#2f6fb3", highlight_text="#ffffff", disabled="#777777",
        plot_bg="#101010", plot_fg="#cccccc", curve="#7fd3ff", peak="#ffd54f", center_line="#ffffff",
        meter_bg="#1b1b1b", meter_idle="#3a6ea5", meter_open="#43c463", meter_text="#dddddd",
        meter_noise="#999999", meter_squelch="#ff5252", led_off="#333333", led_edge="#111111",
        tl_bg="#141414", tl_alt="#1a1a1a", tl_grid="#333333", tl_text="#aaaaaa", tl_disabled="#666666",
        border="#4a4a4a", warn="#ffb74d", bad="#ef5350", good="#81c784",
    ),
    "light": dict(
        window="#ececec", base="#ffffff", alt="#f4f4f4", text="#1d1d1d", muted="#5a5a5a",
        button="#e2e2e2", highlight="#2f6fb3", highlight_text="#ffffff", disabled="#9a9a9a",
        plot_bg="#ffffff", plot_fg="#333333", curve="#1565c0", peak="#e65100", center_line="#555555",
        meter_bg="#e2e2e2", meter_idle="#6f9fd8", meter_open="#2e9d4f", meter_text="#111111",
        meter_noise="#666666", meter_squelch="#d32f2f", led_off="#c8c8c8", led_edge="#888888",
        tl_bg="#fafafa", tl_alt="#f0f0f0", tl_grid="#d6d6d6", tl_text="#444444", tl_disabled="#9a9a9a",
        border="#a8a8a8", warn="#e65100", bad="#c62828", good="#2e7d32",
    ),
}

_current = "dark"


def name() -> str:
    return _current


def tokens() -> dict[str, str]:
    return THEMES[_current]


def color(token: str) -> QColor:
    return QColor(THEMES[_current][token])


def channel_text_color(hex_color: str) -> QColor:
    """Channel colour made readable as text on the current background."""
    c = QColor(hex_color or "#888888")
    if _current == "light":
        c = c.darker(170)
    return c


def apply(app, theme: str) -> None:
    """Switch the application palette. Widgets repaint themselves via their own hooks."""
    global _current
    _current = theme if theme in THEMES else "dark"
    t = THEMES[_current]
    p = QPalette()
    p.setColor(QPalette.Window, QColor(t["window"]))
    p.setColor(QPalette.WindowText, QColor(t["text"]))
    p.setColor(QPalette.Base, QColor(t["base"]))
    p.setColor(QPalette.AlternateBase, QColor(t["alt"]))
    p.setColor(QPalette.Text, QColor(t["text"]))
    p.setColor(QPalette.PlaceholderText, QColor(t["muted"]))
    p.setColor(QPalette.Button, QColor(t["button"]))
    p.setColor(QPalette.ButtonText, QColor(t["text"]))
    p.setColor(QPalette.Highlight, QColor(t["highlight"]))
    p.setColor(QPalette.HighlightedText, QColor(t["highlight_text"]))
    p.setColor(QPalette.ToolTipBase, QColor(t["alt"]))
    p.setColor(QPalette.ToolTipText, QColor(t["text"]))
    p.setColor(QPalette.Link, QColor(t["highlight"]))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(t["disabled"]))
    app.setPalette(p)

"""Per-user display preferences (independent of channel profiles)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..config import user_config_dir

COLORMAPS = (
    ("inferno", "Inferno"), ("magma", "Magma"), ("plasma", "Plasma"), ("viridis", "Viridis"),
    ("cividis", "Cividis"), ("turbo", "Turbo"), ("CET-L1", "Grayscale"),
)
FFT_SIZES = (1024, 2048, 4096, 8192)
WATERFALL_SPEEDS = ((1, "Fast"), (2, "Normal"), (4, "Slow"), (8, "Very slow"))
LABEL_MODES = (("all", "All channels"), ("active", "Active channels only"), ("off", "Off"))
WINDOW_MODES = ("normal", "borderless", "fullscreen")


@dataclass
class ViewSettings:
    theme: str = "dark"                 # dark | light
    layout: str = "vertical"            # vertical (spectrum above channels) | horizontal
    window_mode: str = "normal"         # normal | borderless | fullscreen
    always_on_top: bool = False
    show_spectrum: bool = True
    show_waterfall: bool = True
    show_panel: bool = True
    show_toolbar: bool = True
    show_statusbar: bool = True
    compact_strips: bool = False
    streamlined: bool = False           # hide menu bar, toolbar and panel tabs (second-monitor view)
    channel_labels: str = "all"
    colormap: str = "inferno"
    fft_size: int = 2048
    averaging: float = 0.6              # spectrum smoothing, 0 (none) .. 0.95
    waterfall_speed: int = 1            # spectrum rows averaged into one waterfall line
    auto_levels: bool = True
    level_min: float = -100.0
    level_max: float = -40.0
    peak_hold: bool = False
    geometry: str = ""                  # base64 QMainWindow.saveGeometry()
    splitter: str = ""                  # base64 QSplitter.saveState()
    tab: int = 0

    def validate(self) -> "ViewSettings":
        d = ViewSettings()
        if self.theme not in ("dark", "light"):
            self.theme = d.theme
        if self.layout not in ("vertical", "horizontal"):
            self.layout = d.layout
        if self.window_mode not in WINDOW_MODES:
            self.window_mode = d.window_mode
        if self.channel_labels not in {k for k, _ in LABEL_MODES}:
            self.channel_labels = d.channel_labels
        if self.colormap not in {k for k, _ in COLORMAPS}:
            self.colormap = d.colormap
        if self.fft_size not in FFT_SIZES:
            self.fft_size = d.fft_size
        if self.waterfall_speed not in {k for k, _ in WATERFALL_SPEEDS}:
            self.waterfall_speed = d.waterfall_speed
        try:
            self.averaging = min(0.95, max(0.0, float(self.averaging)))
            self.level_min = min(0.0, max(-160.0, float(self.level_min)))
            self.level_max = min(0.0, max(-160.0, float(self.level_max)))
        except (TypeError, ValueError):
            self.averaging, self.level_min, self.level_max = d.averaging, d.level_min, d.level_max
        if self.level_max <= self.level_min + 5:
            self.level_max = self.level_min + 5
        for f in ("always_on_top", "show_spectrum", "show_waterfall", "show_panel", "show_toolbar",
                  "show_statusbar", "compact_strips", "streamlined", "auto_levels", "peak_hold"):
            setattr(self, f, bool(getattr(self, f)))
        if not (self.show_spectrum or self.show_waterfall or self.show_panel):
            self.show_panel = True
        self.geometry = self.geometry if isinstance(self.geometry, str) and len(self.geometry) < 4096 else ""
        self.splitter = self.splitter if isinstance(self.splitter, str) and len(self.splitter) < 4096 else ""
        self.tab = self.tab if isinstance(self.tab, int) and 0 <= self.tab < 10 else 0
        return self

    @classmethod
    def path(cls) -> Path:
        return user_config_dir() / "view.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "ViewSettings":
        p = path or cls.path()
        try:
            if p.stat().st_size > 256 * 1024:
                return cls()
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        names = {f.name: f for f in fields(cls)}
        kwargs = {}
        for k, v in data.items():
            f = names.get(k)
            if f is None:
                continue
            default = getattr(cls(), k)
            if isinstance(default, bool):
                if isinstance(v, bool):
                    kwargs[k] = v
            elif isinstance(default, (int, float)) and not isinstance(default, bool):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    kwargs[k] = type(default)(v)
            elif isinstance(v, type(default)):
                kwargs[k] = v
        return cls(**kwargs).validate()

    def save(self, path: Path | None = None) -> None:
        p = path or self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, p)

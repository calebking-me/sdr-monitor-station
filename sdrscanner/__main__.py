"""Entry point: python -m sdrscanner [profile.json]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sdrscanner", description="Multi-channel FM voice scanner for RTL-SDR")
    parser.add_argument("profile", nargs="?", help="channel profile (.json) to load")
    parser.add_argument("--iq", metavar="FILE", help="play back an IQ recording instead of using the dongle")
    parser.add_argument("--start", action="store_true", help="start receiving immediately")
    parser.add_argument("--self-test", nargs="?", const="", metavar="REPORT",
                        help="check the installation (DSP, audio, librtlsdr, GUI) and exit; "
                             "optionally write the report to REPORT")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    from .native import prepare_native_libs
    prepare_native_libs()
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    if args.self_test is not None:
        from .selftest import run
        return run(args.self_test or None)
    from PySide6.QtWidgets import QApplication

    from .config import SOURCE_FILE, AppConfig, last_profile_path, load_profile
    from .gui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("SDR Scanner")
    app.setStyle("Fusion")
    _dark_palette(app)
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "gui" / "icon.png")))

    profile_path = Path(args.profile) if args.profile else None
    cfg = AppConfig()
    source = profile_path or last_profile_path()
    if source.exists():
        try:
            cfg = load_profile(source)
        except Exception as e:  # corrupt autosave should not block startup
            print(f"Could not load {source}: {e}", file=sys.stderr)
    if args.iq:
        cfg.radio.source = SOURCE_FILE
        cfg.radio.file_path = str(Path(args.iq).resolve())

    win = MainWindow(cfg, profile_path)
    win.show()
    if args.start:
        win.toggle_running()
    return app.exec()


def _dark_palette(app) -> None:
    from PySide6.QtGui import QColor, QPalette
    p = QPalette()
    base, alt, text = QColor("#1e1e1e"), QColor("#2a2a2a"), QColor("#e0e0e0")
    p.setColor(QPalette.Window, alt)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor("#333333"))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, QColor("#2f6fb3"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, alt)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#777777"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#777777"))
    app.setPalette(p)


if __name__ == "__main__":
    sys.exit(main())

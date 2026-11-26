"""Entry point: python -m sdrscanner [profile.json]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sdrscanner",
                                     description="Multi-channel FM/AM voice scanner for RTL-SDR")
    parser.add_argument("profile", nargs="?", help="channel profile (.json) to load")
    parser.add_argument("--iq", metavar="FILE", help="play back an IQ recording instead of using the dongle")
    parser.add_argument("--start", action="store_true", help="start receiving immediately")
    parser.add_argument("--import-csv", metavar="CSV", help="open the channel import dialog for this CSV file")
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
        cfg.radio.file_center = cfg.radio.file_rate = 0.0     # take them from the new file

    win = MainWindow(cfg, profile_path)        # applies the saved theme / view settings
    win.present()                               # normal, borderless or full screen
    if args.import_csv:
        win.import_channels(args.import_csv)
    if args.start:
        win.toggle_running()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

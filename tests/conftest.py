import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import tempfile  # noqa: E402

# Keep the GUI's autosaved profile out of the real user config directory.
_cfg_dir = tempfile.mkdtemp(prefix="sdrscanner-test-")
os.environ["XDG_CONFIG_HOME"] = _cfg_dir
os.environ["APPDATA"] = _cfg_dir

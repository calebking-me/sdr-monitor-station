# PyInstaller spec for SDR Scanner.
#
#   pyinstaller packaging/sdrscanner.spec --noconfirm
#
# Produces dist/SDRScanner/SDRScanner.exe (one-folder build). Set
# SDRSCANNER_ONEFILE=1 for a single self-extracting .exe instead (slower to
# start, more often flagged by antivirus). Works on Linux too (for testing).

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEFILE = os.environ.get("SDRSCANNER_ONEFILE") == "1"
IS_WIN = sys.platform.startswith("win")

datas = [(os.path.join(ROOT, "sdrscanner", "gui", "icon.png"), "sdrscanner/gui")]
binaries = []

# librtlsdr shipped by pyrtlsdrlib (statically linked against libusb on Windows).
# It is located at runtime via importlib.resources, so keep the package layout.
try:
    datas += collect_data_files("pyrtlsdrlib", include_py_files=False)
    datas += copy_metadata("pyrtlsdr")
except Exception as e:  # pyrtlsdrlib is optional: users can drop in their own DLL
    print(f"WARNING: pyrtlsdrlib not bundled ({e})")

hiddenimports = [
    "rtlsdr", "rtlsdr.librtlsdr",
    "pyrtlsdrlib", "pyrtlsdrlib.lib", "pyrtlsdrlib.lib.custom_build", "pyrtlsdrlib.platform",
    "scipy.signal", "scipy.fft", "scipy.io.wavfile",
]

excludes = [
    # Other Qt bindings pyqtgraph can use; we only ship PySide6.
    "PyQt5", "PyQt6", "PySide2",
    # Optional pyqtgraph extras and heavy packages we never import.
    "pyqtgraph.opengl", "OpenGL", "matplotlib", "pandas", "h5py", "cupy", "numba",
    "IPython", "jupyter", "notebook", "tkinter", "pytest",
    # PySide6 modules not used by the app.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtPdf",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
    "PySide6.QtNetwork", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtSvgWidgets",
]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(SPECPATH, "sdrscanner.ico") if IS_WIN else None
exe_kwargs = dict(
    name="SDRScanner",
    icon=icon,
    console=False,          # GUI app: no console window. Use --self-test REPORT for diagnostics.
    upx=False,              # UPX-packed Qt DLLs trigger antivirus false positives
    version=os.path.join(SPECPATH, "version_info.txt") if IS_WIN else None,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **exe_kwargs)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="SDRScanner")

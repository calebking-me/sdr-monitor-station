"""Native library discovery (librtlsdr) for source checkouts and frozen builds.

Python 3.8+ on Windows no longer searches PATH or the current directory when a
DLL is loaded by bare name, so a librtlsdr.dll dropped next to the program
would be ignored. `prepare_native_libs()` registers the application folder as a
DLL directory and, when the user supplied their own librtlsdr (e.g. the
RTL-SDR Blog V4 build), makes it take precedence over the copy bundled with
pyrtlsdrlib.

Must run before anything imports `rtlsdr`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

USER_DLL_NAMES = ("rtlsdr.dll", "librtlsdr.dll")
_state = {"user_dll": None, "prepared": False}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dirs() -> list[Path]:
    """Folders where a user-supplied librtlsdr may be placed."""
    dirs = []
    if is_frozen():
        dirs.append(Path(sys.executable).resolve().parent)
    else:
        dirs.append(Path(__file__).resolve().parents[1])   # project root
    cwd = Path.cwd()
    if cwd not in dirs:
        dirs.append(cwd)
    return dirs


def prepare_native_libs() -> None:
    if _state["prepared"]:
        return
    _state["prepared"] = True
    if not sys.platform.startswith("win"):
        return
    for d in app_dirs():
        dll = next((d / n for n in USER_DLL_NAMES if (d / n).exists()), None)
        if dll is None:
            continue
        _state["user_dll"] = dll
        try:
            os.add_dll_directory(str(d))          # dependencies such as libusb-1.0.dll
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        # pyrtlsdr tries the bundled pyrtlsdrlib first; hide it so the user's DLL wins.
        # pyrtlsdr then finds rtlsdr.dll by name, or librtlsdr.dll via PATH (find_library).
        sys.modules.setdefault("pyrtlsdrlib", None)
        break


def librtlsdr_description() -> str:
    """Which librtlsdr gets loaded (for diagnostics)."""
    import importlib
    mod = importlib.import_module("rtlsdr.librtlsdr")
    lib = mod.librtlsdr
    name = getattr(lib, "_name", "?")
    if _state["user_dll"]:
        return f"user-supplied {_state['user_dll']} ({name})"
    return str(name)

"""Version numbers must agree everywhere (the release workflow also checks the tag)."""

import re
from pathlib import Path

import sdrscanner

ROOT = Path(__file__).resolve().parents[1]


def test_versions_consistent():
    v = sdrscanner.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == v
    info = (ROOT / "packaging" / "version_info.txt").read_text()
    nums = tuple(int(x) for x in v.split(".")) + (0,)
    assert f"filevers={nums}" in info and f"prodvers={nums}" in info
    assert f"'FileVersion', '{v}'" in info and f"'ProductVersion', '{v}'" in info

"""Locate versioned KiCad install roots on Windows.

The KiCad Windows installer defaults to ``C:\\Program Files\\KiCad\\<major>.0``
and does not put ``kicad-cli`` on ``PATH``. Rather than hard-coding versions,
enumerate whatever is installed, newest first.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_VERSION_DIR = re.compile(r"^\d+(?:\.\d+)*$")


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name.split("."))


def windows_kicad_base_dirs() -> list[Path]:
    """Return the ``KiCad`` parent directories searched for installs."""
    parents = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),  # noqa: SIM112
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),  # noqa: SIM112
        Path.home() / "AppData" / "Local" / "Programs",
    ]
    return [parent / "KiCad" for parent in parents]


def windows_kicad_install_roots() -> list[Path]:
    """Return versioned KiCad install directories, newest version first.

    Searches ``%ProgramFiles%``, ``%ProgramFiles(x86)%`` and
    ``~/AppData/Local/Programs`` for ``KiCad/<version>`` directories. Entries
    whose name is not a dotted version number are ignored, and versions are
    ordered numerically (``10.0`` before ``9.0``). Unversioned ``KiCad/``
    layouts are not included; callers keep those as a fallback.
    """
    found: list[tuple[tuple[int, ...], int, Path]] = []
    for order, kicad_dir in enumerate(windows_kicad_base_dirs()):
        try:
            children = list(kicad_dir.iterdir())
        except OSError:
            continue
        for child in children:
            if _VERSION_DIR.match(child.name) and child.is_dir():
                found.append((_version_key(child.name), -order, child))
    # Newest version first; for equal versions keep the parent search order.
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in found]

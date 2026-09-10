#!/usr/bin/env python3
"""Initialize KiCad's stock footprint table for headless native checks.

The GUI normally installs this table on first launch. A fresh CI container has
the footprint libraries but no table, causing native DRC library warnings.
Existing configuration is preserved; missing templates are an error.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def initialize() -> Path:
    version = subprocess.check_output(["kicad-cli", "--version"], text=True).strip()
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        raise RuntimeError(f"Cannot determine KiCad configuration version from {version!r}")
    config_version = f"{match[1]}.{match[2]}"
    if "KICAD_CONFIG_HOME" in os.environ:
        root = Path(os.environ["KICAD_CONFIG_HOME"])
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Preferences/kicad"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kicad"
    target = root / config_version / "fp-lib-table"
    if target.exists():
        return target

    candidates = [
        Path("/usr/share/kicad/template/fp-lib-table"),
        Path("/usr/local/share/kicad/template/fp-lib-table"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/template/fp-lib-table"),
    ]
    template = next((path for path in candidates if path.is_file()), None)
    if template is None:
        raise FileNotFoundError(
            "KiCad stock footprint table is unavailable; install the KiCad library "
            "templates. Searched: " + ", ".join(map(str, candidates))
        )
    data = template.read_text()
    if f"KICAD{match[1]}_FOOTPRINT_DIR" not in data:
        raise RuntimeError(f"Stock footprint table {template} does not match KiCad {version}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also preserves configuration created concurrently.
    try:
        with target.open("x") as stream:
            stream.write(data)
    except FileExistsError:
        pass
    return target


if __name__ == "__main__":
    try:
        print(f"KiCad footprint library table: {initialize()}")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Cannot initialize KiCad libraries: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

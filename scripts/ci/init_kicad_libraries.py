#!/usr/bin/env python3
"""Initialize stock footprint and symbol tables for headless native checks.

The GUI normally installs these tables on first launch. Fresh CI containers
need both tables for native DRC/ERC. Preserve existing configuration and fail
if a missing table has no template matching the installed KiCad major version.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def initialize() -> tuple[Path, Path]:
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
    templates = [
        Path("/usr/share/kicad/template"),
        Path("/usr/local/share/kicad/template"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/template"),
    ]
    targets = (root / config_version / "fp-lib-table", root / config_version / "sym-lib-table")
    pending = []
    for target, kind, variable in zip(
        targets, ("footprint", "symbol"), ("FOOTPRINT", "SYMBOL"), strict=True
    ):
        if target.exists():
            continue
        candidates = [directory / target.name for directory in templates]
        available = [path for path in candidates if path.is_file()]
        if not available:
            raise FileNotFoundError(
                f"KiCad stock {kind} table is unavailable; install the KiCad library "
                "templates. Searched: " + ", ".join(map(str, candidates))
            )
        data = next(
            (
                text
                for path in available
                if f"KICAD{match[1]}_{variable}_DIR" in (text := path.read_text())
            ),
            None,
        )
        if data is None:
            raise RuntimeError(f"Stock {kind} tables {available} do not match KiCad {version}")
        pending.append((target, data))
    for target, data in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves configuration created concurrently.
        try:
            with target.open("x") as stream:
                stream.write(data)
        except FileExistsError:
            pass
    return targets


if __name__ == "__main__":
    try:
        for table in initialize():
            print(f"KiCad library table: {table}")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Cannot initialize KiCad libraries: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

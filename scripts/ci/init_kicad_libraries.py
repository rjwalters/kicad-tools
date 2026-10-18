#!/usr/bin/env python3
"""Give headless kicad-cli the pinned library tables (Issues #5860, #6962).

kicad-cli resolves stock footprint/symbol nicknames (``Resistor_SMD:``) through
the *global* ``fp-lib-table`` / ``sym-lib-table`` in its config home.  Hosts
get those from the GUI's first launch, or not at all; container images differ
by user (the ``kicad`` user has them, ``--user 0:0`` does not).  A missing
table turns every stock footprint into a ``lib_footprint_issues`` warning
(board 03 gained 37 of them, #6962) and every stock symbol into an ERC
warning (#5860).

This script no longer reads or writes the user's real config.  It writes the
tables committed under ``src/kicad_tools/data/kicad-config/<version>/`` into an
isolated config home -- through the same helper every evidence producer uses
(``kicad_tools.cli.kicad_toolchain.populate_config_home``) -- and points
``KICAD_CONFIG_HOME`` at it:

* under GitHub Actions it appends ``KICAD_CONFIG_HOME=<dir>`` to
  ``$GITHUB_ENV``, so every later step of the job uses it;
* ``--shell`` prints ``export KICAD_CONFIG_HOME=<dir>`` for ``eval`` (a
  ``docker run`` script, or a local shell);
* ``--config-home DIR`` chooses the directory (default: a fresh one under
  ``$RUNNER_TEMP`` or the system temporary directory).

It runs with a bare ``python3`` before the project environment exists, so it
loads the helper module by file path rather than importing ``kicad_tools``.
It fails if ``kicad-cli`` is not the KiCad release the pinned tables are for.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN = REPO_ROOT / "src" / "kicad_tools" / "cli" / "kicad_toolchain.py"


def load_toolchain():
    """``kicad_tools.cli.kicad_toolchain``, loaded from its file (stdlib only)."""
    spec = importlib.util.spec_from_file_location("kct_kicad_toolchain", TOOLCHAIN)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {TOOLCHAIN}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def kicad_config_version() -> str:
    """``<major>.<minor>`` of the ``kicad-cli`` on PATH."""
    version = subprocess.check_output(["kicad-cli", "--version"], text=True).strip()
    match = re.search(r"(\d+)\.(\d+)", version)
    if not match:
        raise RuntimeError(f"Cannot determine KiCad configuration version from {version!r}")
    return f"{match[1]}.{match[2]}"


def default_config_home() -> Path:
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(tempfile.mkdtemp(prefix="kct-kicad-config-", dir=base))


def initialize(config_home: Path | None = None) -> tuple[Path, tuple[Path, ...]]:
    """Populate an isolated config home; return it and the tables written."""
    toolchain = load_toolchain()
    version = kicad_config_version()
    if version != toolchain.KICAD_CONFIG_VERSION:
        raise RuntimeError(
            f"kicad-cli is KiCad {version}, but the pinned library tables are for "
            f"KiCad {toolchain.KICAD_CONFIG_VERSION} "
            f"(src/kicad_tools/data/kicad-config/{toolchain.KICAD_CONFIG_VERSION})"
        )
    home = toolchain.populate_config_home(config_home or default_config_home(), version)
    tables = tuple(home / version / name for name in toolchain.LIBRARY_TABLES)
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as stream:
            stream.write(f"KICAD_CONFIG_HOME={home}\n")
    os.environ["KICAD_CONFIG_HOME"] = str(home)
    return home, tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-home", type=Path, help="config home to populate")
    parser.add_argument(
        "--shell", action="store_true", help="print only `export KICAD_CONFIG_HOME=...`"
    )
    args = parser.parse_args(argv)
    try:
        home, tables = initialize(args.config_home)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Cannot initialize KiCad libraries: {exc}", file=sys.stderr)
        return 1
    if args.shell:
        print(f"export KICAD_CONFIG_HOME={shlex.quote(str(home))}")
        return 0
    print(f"KICAD_CONFIG_HOME={home}")
    for table in tables:
        print(f"KiCad library table: {table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

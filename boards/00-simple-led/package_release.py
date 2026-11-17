#!/usr/bin/env python3
"""Rebuild board 00's shipped manufacturing package through ``kct readiness``.

Usage::

    uv run python boards/00-simple-led/package_release.py

Run it after ``generate_design.py`` (or
``scripts/boards/regenerate_board_00_6076.py``) has refreshed
``output/*.kicad_sch`` / ``output/*.kicad_pcb``.  It runs the generic
``kct readiness --generate`` producer (refill + save, ``kct check``, native
KiCad DRC on saved and refilled copper, LVS, export, schematic/assembly PDFs,
full-bundle manifest, ``manufacturing.zip`` and the hash-bound
``output/readiness.json``) through the shared ``boards/release_package.py``
helper, and adds what the shipped board-00 package has carried since the
demo-board release (d95b6eff):

* ``check-report.json`` / ``native-drc.json`` -- byte copies of this run's
  readiness gate evidence, so a fab upload carries its own sign-off;
* ``procurement-review.md`` -- the human supplier-identity review (#5098),
  carried forward only while ``bom_jlcpcb.csv`` and ``cpl_jlcpcb.csv`` are
  byte-identical to the ones it reviewed (otherwise a person must redo it).

``README.txt`` keeps the generic readiness layout and appends sign-off
statements derived from the evidence written by the same run.  Nested ZIPs
are written byte-reproducibly.  Nothing is published if the new package would
lack any file the current package ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

BOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOARD_DIR.parent))

from release_package import Release  # noqa: E402

RELEASE = Release(
    board_dir=BOARD_DIR,
    reviewed={"procurement-review.md": ("bom_jlcpcb.csv", "cpl_jlcpcb.csv")},
)

if __name__ == "__main__":
    sys.exit(RELEASE.main())

#!/usr/bin/env python3
"""Rebuild board 01's shipped manufacturing package through ``kct readiness``.

Usage::

    uv run python boards/01-voltage-divider/package_release.py

Run it after ``output/*.kicad_sch`` / ``output/*.kicad_pcb`` change.  It runs
the generic ``kct readiness --generate`` producer (see
``boards/release_package.py``) and adds what the board's package has shipped
since the demo-board release (d95b6eff):

* ``check-report.json`` / ``native-drc.json`` -- byte copies of this run's
  readiness gate evidence, so a fab upload carries its own sign-off;
* ``procurement-review.md`` -- the human supplier-identity review (#5098),
  carried forward only while ``bom_jlcpcb.csv`` and ``cpl_jlcpcb.csv`` are
  byte-identical to the ones it reviewed.

It refuses (nothing is published) if the new package would lack any file the
current package ships.
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

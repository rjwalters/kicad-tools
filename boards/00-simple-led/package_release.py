#!/usr/bin/env python3
"""Rebuild board 00's shipped manufacturing package through ``kct readiness``.

Usage::

    uv run python boards/00-simple-led/package_release.py

Run it after ``generate_design.py`` has refreshed ``output/*.kicad_pcb``.  It
runs the generic ``kct readiness --generate`` producer (refill + save,
``kct check``, native KiCad DRC on saved and refilled copper, LVS, export,
schematic/assembly PDFs, full-bundle manifest, ``manufacturing.zip`` and the
hash-bound ``output/readiness.json``) with two additions that the shipped
board-00 package has carried since the demo-board release (d95b6eff):

* ``check-report.json`` and ``native-drc.json`` -- byte copies of the
  readiness gate evidence (``output/readiness/kct-check.json`` and
  ``output/readiness/native-drc.json``) placed inside the bundle so a fab
  upload carries its own sign-off evidence.
* ``procurement-review.md`` -- the human supplier-identity review (#5098),
  carried forward from the current package.  It is refused if the
  regenerated BOM/CPL no longer match the ones that review covered, because
  then the review would have to be redone by a person, not copied.

``README.txt`` keeps the generic readiness layout and appends the sign-off
statements the package makes, each derived from the evidence written by the
same run (never asserted unconditionally).

Every addition lands *before* the producer writes the README and the
full-bundle manifest, so the manifest, ``manufacturing.zip`` and
``readiness.json`` checksum them like any other bundle file.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from kicad_tools.cli import readiness_cmd

BOARD_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BOARD_DIR / "output"
PACKAGE_DIR = OUTPUT_DIR / "manufacturing"
MANUFACTURER = "jlcpcb"

#: Hand-written evidence carried forward from the current package.
REVIEW_NAME = "procurement-review.md"
#: Files the procurement review vouches for; it may only be carried forward
#: when the regenerated copies are byte-identical.
REVIEWED_FILES = ("bom_jlcpcb.csv", "cpl_jlcpcb.csv")


def _load_reviewed_inputs() -> dict[str, bytes]:
    names = (REVIEW_NAME, *REVIEWED_FILES)
    missing = [name for name in names if not (PACKAGE_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"current package lacks {', '.join(missing)}; cannot carry review forward")
    return {name: (PACKAGE_DIR / name).read_bytes() for name in names}


def _error_count(path: Path, *keys: str) -> int | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, int) else None


def main() -> int:
    reviewed = _load_reviewed_inputs()
    original_write_readme = readiness_cmd._write_readme

    def write_readme(options, warning_counts, tht_refs):  # type: ignore[no-untyped-def]
        out, evidence = options.output_dir, options.evidence_dir
        for name in REVIEWED_FILES:
            if (out / name).read_bytes() != reviewed[name]:
                raise RuntimeError(
                    f"{name} changed since {REVIEW_NAME} was written; re-review procurement "
                    "instead of carrying the old review forward"
                )
        (out / REVIEW_NAME).write_bytes(reviewed[REVIEW_NAME])
        shutil.copyfile(evidence / "kct-check.json", out / "check-report.json")
        shutil.copyfile(evidence / "native-drc.json", out / "native-drc.json")

        readme = original_write_readme(options, warning_counts, tht_refs)

        kct_errors = _error_count(evidence / "kct-check.json", "summary", "errors")
        native_saved = _error_count(evidence / "native-drc.json", "error_count")
        native_refilled = _error_count(
            evidence / "refilled-native" / "native-drc.json", "error_count"
        )
        claims = ["", "Sign-off", "--------"]
        if kct_errors == 0 and native_saved == 0 and native_refilled == 0:
            claims.append(
                "  Fresh kct check and native KiCad DRC (saved and refilled copper): zero errors."
            )
        else:
            claims.append(
                f"  kct check errors: {kct_errors}; native DRC errors (saved/refilled): "
                f"{native_saved}/{native_refilled}."
            )
        claims.append(
            "  Procurement identities reviewed against supplier listings; see "
            f"{REVIEW_NAME} and source project.kct."
        )
        claims.append("")
        readme.write_text(readme.read_text() + "\n".join(claims))
        return readme

    # The pre-readiness package (no ``producer: kct readiness`` manifest) is
    # refused by --generate as recipe-finalised; this script *is* its recipe.
    manifest = PACKAGE_DIR / "manifest.json"
    if manifest.is_file() and json.loads(manifest.read_text()).get("producer") != "kct readiness":
        shutil.rmtree(PACKAGE_DIR)
        (OUTPUT_DIR / "manufacturing.zip").unlink(missing_ok=True)

    readiness_cmd._write_readme = write_readme
    try:
        return readiness_cmd.main([str(BOARD_DIR), "--generate", "--mfr", MANUFACTURER])
    finally:
        readiness_cmd._write_readme = original_write_readme


if __name__ == "__main__":
    sys.exit(main())

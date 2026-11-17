"""Board 06 header sourcing shared by the BOM/LCSC regression tests.

Issue #7422 (PR #7426, operator ruling #7268 Q2 = 3) replaced board 06's
Samtec TSW headers J1-J3 with LCSC-stocked XFCN PZ254V headers. Those spec
assignments are *reviewed* sourcing -- see
``boards/06-diffpair-test/output/manufacturing/procurement-review.md`` -- so
the export must emit exactly them.

The #4995 guard (an explicit MPN-only part never acquires a generic LCSC
substitute) still needs a fixture with an MPN-only part. No board in the fleet
has one any more, so :func:`write_samtec_spec` rebuilds the pre-#7426 board 06
spec in a temporary directory: the same real routed PCB, with J1-J3 put back
to the explicit Samtec MPN-only entries that reproduced #4995 (issue #7445).
"""

from __future__ import annotations

from pathlib import Path

import yaml

BOARD06 = Path(__file__).resolve().parents[1] / "boards" / "06-diffpair-test"
BOARD06_PCB = BOARD06 / "output" / "diffpair_test_routed.kicad_pcb"

#: The reviewed LCSC sourcing #7426 put on J1-J3 (ref -> (MPN, LCSC)).
REVIEWED_HEADERS: dict[str, tuple[str, str]] = {
    "J1": ("PZ254V-11-02P", "C492401"),
    "J2": ("PZ254V-11-08P", "C492407"),
    "J3": ("PZ254V-11-08P", "C492407"),
}

#: The explicit non-LCSC sourcing board 06 had when #4995 was reported.
SAMTEC_HEADERS: dict[str, str] = {
    "J1": "Samtec TSW-102-07-G-S",
    "J2": "Samtec TSW-108-07-G-S",
    "J3": "Samtec TSW-108-07-G-S",
}


def write_samtec_spec(directory: Path) -> Path:
    """Write board 06's spec with J1-J3 as MPN-only Samtec parts; return its path."""
    spec = yaml.safe_load((BOARD06 / "project.kct").read_text())
    entries = spec["bom_entries"]
    seen = set()
    for entry in entries:
        ref = entry["ref"]
        if ref in SAMTEC_HEADERS:
            entry.clear()
            entry.update({"ref": ref, "part": SAMTEC_HEADERS[ref], "source": "Samtec"})
            seen.add(ref)
    assert seen == set(SAMTEC_HEADERS), f"board 06 spec lost header entries: {seen}"
    path = directory / "project.kct"
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    return path

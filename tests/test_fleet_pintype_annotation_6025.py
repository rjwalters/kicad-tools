"""Committed fleet boards carry schematic pad pin types (issue #6025).

Issue #5998 wired board 09 to ``annotate_pcb_file_pintypes``; issue #6025
rolls the same annotation out to the rest of the fleet, one pipeline family
per PR.  For every board listed here, each committed ``.kicad_pcb`` (unrouted
and routed) must already be the annotator's fixed point: a dry run over the
committed file changes nothing and every schematic-backed pad carries a
``pintype``.  A board regenerated without the annotation step, or a schematic
pin edit that was not carried onto the pads, fails this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.operations.pintype import annotate_pcb_file_pintypes
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (board dir, schematic, committed PCBs) for boards whose outputs are annotated.
ANNOTATED_BOARDS = [
    (
        "00-simple-led",
        "simple_led.kicad_sch",
        ("simple_led.kicad_pcb", "simple_led_routed.kicad_pcb"),
    ),
    (
        "01-voltage-divider",
        "voltage_divider.kicad_sch",
        ("voltage_divider.kicad_pcb", "voltage_divider_routed.kicad_pcb"),
    ),
    (
        "02-charlieplex-led",
        "charlieplex_3x3.kicad_sch",
        ("charlieplex_3x3.kicad_pcb", "charlieplex_3x3_routed.kicad_pcb"),
    ),
    (
        "03-usb-joystick",
        "usb_joystick.kicad_sch",
        (
            "usb_joystick.kicad_pcb",
            "usb_joystick_routed.kicad_pcb",
            # Readiness evidence copies are hash-pinned by readiness.json and
            # must carry the same pad pin types as the board they witness.
            "readiness-verification/fill-saved/usb_joystick_routed.kicad_pcb",
            "readiness-verification/fill-refilled/usb_joystick_routed.kicad_pcb",
        ),
    ),
    (
        "09-usbc-pd-power",
        "usbc_pd_power.kicad_sch",
        ("usbc_pd_power.kicad_pcb",),
    ),
]

#: Named mechanical/shield pads with no schematic pin (board 03's J2 joystick
#: mounting tabs are pads ``MP``), so they legitimately carry no ``pintype``.
UNMAPPED_MECHANICAL_PADS = {
    "03-usb-joystick": {"J2.MP"},
}

_CASES = [
    pytest.param(board, sch, pcb, id=f"{board}/{pcb}")
    for board, sch, pcbs in ANNOTATED_BOARDS
    for pcb in pcbs
]


@pytest.mark.parametrize(("board", "sch", "pcb"), _CASES)
def test_committed_board_is_pintype_annotated(board: str, sch: str, pcb: str) -> None:
    out = REPO_ROOT / "boards" / board / "output"
    pcb_path, sch_path = out / pcb, out / sch
    before = pcb_path.read_bytes()

    result = annotate_pcb_file_pintypes(pcb_path, sch_path, dry_run=True)

    assert pcb_path.read_bytes() == before, "dry run must not write"
    assert result.updated == 0, (
        f"{board}/{pcb}: {result.updated} pad(s) lack the schematic pin type; "
        "re-run the board's generator and package producer (issue #6025)"
    )
    assert not result.missing_pads, result.missing_pads
    assert result.annotated > 0

    # Unnumbered pads (mounting/NPTH holes) have no schematic pin.
    untyped = [
        f"{fp.reference}.{pad.number}"
        for fp in PCB.load(pcb_path).footprints
        for pad in fp.pads
        if pad.number
        and not pad.pintype
        and f"{fp.reference}.{pad.number}" not in UNMAPPED_MECHANICAL_PADS.get(board, ())
    ]
    assert not untyped, f"{board}/{pcb}: pads without pintype: {untyped}"

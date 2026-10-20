"""Issue #7602: the oracle completion weld keeps its vias out of the mask-dam zone.

``PourLinkCloser._weld_pads`` called ``run_stitch`` without ``pad_edge_gap``,
so the #7322 guard never applied to oracle-placed vias and a weld via could
land 0.02 mm from an exposed pad (``via_at_pad_edge`` in ``kct check``).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import _pad_copper_gap, find_all_pad_bboxes
from kicad_tools.router.oracle_completion import ClosureAttempt, PourLinkCloser
from kicad_tools.router.via_in_pad_eligibility import via_drill_at_pad_edge
from kicad_tools.sexp import parse_string

from .test_stitch_pad_edge_7322 import CLEARANCE, DRILL, FLOOR, VIA, _foreign_smd, _write


def _weld(pcb: Path, gap: float) -> ClosureAttempt:
    closer = PourLinkCloser(
        via_size=VIA,
        via_drill=DRILL,
        clearance=CLEARANCE,
        trace_width=0.2,
        pad_edge_gap=gap,
    )
    attempt = ClosureAttempt()
    closer._weld_pads(pcb, {"GND": {"C1.1"}}, attempt)
    return attempt


def _zone_hits(pcb: Path, attempt: ClosureAttempt) -> int:
    bboxes = find_all_pad_bboxes(parse_string(pcb.read_text()), smd_only=True)
    return sum(
        via_drill_at_pad_edge(_pad_copper_gap(x, y, x, y, b), drill, FLOOR)
        for x, y, _size, drill in attempt.vias
        for b in bboxes
    )


def _board_with_pad_beside_weld_site(tmp_path: Path) -> Path:
    base = _weld(_write(tmp_path), 0.0)
    assert len(base.vias) == 1
    x, y, _, _ = base.vias[0]
    # Pad edge 0.5 mm east of the via centre: copper-legal, inside the dam zone.
    return _write(tmp_path, _foreign_smd(x + 0.7, y))


def test_control_weld_without_floor_lands_in_the_zone(tmp_path):
    pcb = _board_with_pad_beside_weld_site(tmp_path)
    attempt = _weld(pcb, 0.0)
    assert attempt.vias
    assert _zone_hits(pcb, attempt) > 0, "control must reach the zone"


def test_weld_with_floor_places_no_via_in_the_zone(tmp_path):
    pcb = _board_with_pad_beside_weld_site(tmp_path)
    attempt = _weld(pcb, FLOOR)
    assert attempt.vias, "a legal site outside the zone exists"
    assert _zone_hits(pcb, attempt) == 0

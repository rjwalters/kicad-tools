"""Native parity: filled Edge.Cuts cut-outs, kct vs the pinned kicad-cli (#9690).

Issue #9690's acceptance criterion: for a filled vs ``fill no`` circle and
poly (rect included for free -- it shares the circle's probed default), a
deep-interior and a far-exterior probe track must be flagged identically by
``kct check``'s :class:`EdgeClearanceRule` and by kicad-cli's
``copper_edge_clearance``.

The encodings here are the ones black-box probed on the pinned image
(kicad-cli 10.0.6) when this issue was implemented:

* a bare ``gr_circle`` / ``gr_rect`` (no stroke/fill tokens) is a filled
  cut-out -- KiCad's legacy zero-stroke-width default;
* a ``gr_poly`` is filled only with an explicit filled token;
* ``(fill no)`` keeps stroke-only behaviour.

Requires a kicad-cli (the CI container's, or the pinned image via
``KCT_KICAD_DOCKER=1``); skips everywhere else.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.edge import EdgeClearanceRule

OUTER = (
    "(gr_rect (start 0 0) (end 100 100) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
)

CASES = {
    "circle_filled": '(gr_circle (center 60 50) (end 80 50) (layer "Edge.Cuts"))',
    "circle_fill_no": (
        "(gr_circle (center 60 50) (end 80 50) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
    ),
    "poly_filled": (
        "(gr_poly (pts (xy 40 30) (xy 80 30) (xy 80 70) (xy 40 70)) "
        '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))'
    ),
    "poly_fill_no": (
        "(gr_poly (pts (xy 40 30) (xy 80 30) (xy 80 70) (xy 40 70)) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
    ),
    "rect_filled": '(gr_rect (start 40 30) (end 80 70) (layer "Edge.Cuts"))',
    "rect_fill_no": (
        "(gr_rect (start 40 30) (end 80 70) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))'
    ),
}

#: The probe tracks: deep inside every cut-out above, and far outside both
#: the cut-out and the board edge stroke.
INTERIOR_TRACK = (60.0, 50.0)  # probe start
EXTERIOR_TRACK = (20.0, 10.0)

_BOARD_TMPL = """(kicad_pcb (version 20241229) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
  (net 0 "")
  (net 1 "N1")
  (setup (pad_to_mask_clearance 0))
  {outer}
  {shape}
  (segment (start 60 50) (end 62 50) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 20 10) (end 22 10) (width 0.25) (layer "F.Cu") (net 1))
)"""


def _kicad_cli():
    from kicad_tools.cli.runner import find_kicad_cli

    found = find_kicad_cli()
    if found is None:
        pytest.skip("no kicad-cli available (KCT_KICAD_DOCKER=1 or a host install needed)")
    return found


def _kct_flagged_tracks(board: Path) -> set[tuple[float, float]]:
    """Probe-track starts that kct's EdgeClearanceRule flags."""
    pcb = PCB.load(board)
    results = EdgeClearanceRule().check(
        pcb,
        SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5),
    )
    flagged = set()
    for violation in results.errors:
        if violation.rule_id != "edge_clearance_trace":
            continue
        for probe in (INTERIOR_TRACK, EXTERIOR_TRACK):
            if abs(violation.location[0] - probe[0]) < 1.0 and (
                abs(violation.location[1] - probe[1]) < 1.0
            ):
                flagged.add(probe)
    return flagged


def _kicad_flagged_tracks(board: Path) -> set[tuple[float, float]]:
    """Probe-track starts that kicad-cli's copper_edge_clearance flags."""
    from kicad_tools.cli.runner import run_drc
    from kicad_tools.drc.report import DRCReport
    from kicad_tools.drc.violation import ViolationType

    result = run_drc(board, schematic_parity=False)
    if not result.success:
        pytest.skip(f"kicad-cli DRC unavailable: {result.stderr[:200]}")
    report = DRCReport.load(Path(result.output_path))
    flagged = set()
    for violation in report.violations:
        if violation.type != ViolationType.COPPER_EDGE_CLEARANCE:
            continue
        # Each violation's items name the copper side ("Track [N1] on ...")
        # and the board-edge side; the copper side's location is the track's
        # own witness point, the kct finding's location's counterpart.
        track_locations = [
            loc
            for item, loc in zip(violation.items, violation.locations, strict=False)
            if "Track" in item
        ]
        for loc in track_locations:
            for probe in (INTERIOR_TRACK, EXTERIOR_TRACK):
                if abs(loc.x_mm - probe[0]) < 1.0 and abs(loc.y_mm - probe[1]) < 1.0:
                    flagged.add(probe)
    return flagged


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_filled_edge_cuts_probe_parity(tmp_path, case):
    """kct and kicad-cli agree on which probe tracks each encoding flags."""
    _kicad_cli()
    board = tmp_path / f"parity_{case}.kicad_pcb"
    board.write_text(_BOARD_TMPL.format(outer=OUTER, shape=CASES[case]))

    kct = _kct_flagged_tracks(board)
    native = _kicad_flagged_tracks(board)

    assert kct == native, f"kct flagged {kct} but kicad-cli flagged {native}"

    # The issue's own claim, pinned: a filled cut-out flags the deep-interior
    # probe; an unfilled shape leaves both probes clean (each probe is >15 mm
    # from every stroke, so the stroke clearance never interferes).
    if case.endswith("_filled"):
        assert kct == {INTERIOR_TRACK}
    else:
        assert kct == set()


def test_probe_report_documents_actual_zero(tmp_path):
    """A filled-cut-out finding reports actual 0.0 mm, as kicad-cli does."""
    board = tmp_path / "actual_zero.kicad_pcb"
    board.write_text(_BOARD_TMPL.format(outer=OUTER, shape=CASES["circle_filled"]))
    pcb = PCB.load(board)
    results = EdgeClearanceRule().check(
        pcb,
        SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5),
    )
    traces = [v for v in results.errors if v.rule_id == "edge_clearance_trace"]
    assert len(traces) == 1
    assert traces[0].actual_value == pytest.approx(0.0)

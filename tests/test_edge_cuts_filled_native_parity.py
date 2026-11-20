"""Native parity: filled Edge.Cuts cut-outs, kct vs the pinned kicad-cli (#9690).

Issue #9690's acceptance criterion: for a filled vs ``fill no`` circle and
poly (rect included for free -- it shares the circle's probed default), a
deep-interior and a far-exterior probe track must be flagged identically by
``kct check``'s :class:`EdgeClearanceRule` and by kicad-cli's
``copper_edge_clearance``.

The review of this PR added two probe classes that pin the *boundary*
behaviour (the cut-out edge is board edge for any fill resolution):

* a **near-outside** track, copper 0.125 mm from the boundary -- closer
  than the 0.3 mm rule but with no sample point inside the region;
* a **crossing** track whose start, end and midpoint all sit outside a
  filled circle while its copper straddles the boundary -- only the
  boundary stroke can catch it.

The encodings here are the ones black-box probed on the pinned image
(kicad-cli 10.0.6) when this issue was implemented:

* a bare ``gr_circle`` / ``gr_rect`` (no stroke/fill tokens) is a filled
  cut-out -- KiCad's legacy zero-stroke-width default;
* a ``gr_poly`` is filled only with an explicit filled token;
* ``(fill no)`` keeps stroke-only behaviour.

Requires the pinned kicad-cli (the CI container's, or the pinned image via
``KCT_KICAD_DOCKER=1``).  There is deliberately **no skip**: an
unresolvable toolchain fails the test loudly (operator ruling on added
skips) instead of quietly passing without enforcing parity.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.core.geometry import point_to_segment_distance
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

#: Probe tracks, name -> ((x1, y1), (x2, y2)).  A finding is attributed to a
#: probe when its witness point lands within 1.0 mm of the probe's segment:
#: both engines report a witness *on the track* (kct the endpoint nearer the
#: outline, kicad-cli its own nearest point), not the authored start.
PROBES: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    # Deep inside every cut-out above, and far outside both the cut-out and
    # the board edge stroke.
    "interior": ((60.0, 50.0), (62.0, 50.0)),
    "exterior": ((20.0, 10.0), (22.0, 10.0)),
    # Copper 0.125 mm outside the circle's top point / the poly's top edge
    # (centre-line 0.25 mm away, half-width 0.125): inside the 0.3 mm rule
    # with every sample point outside the region (review probe 1).
    "near": ((59.0, 29.75), (61.0, 29.75)),
    # Crosses the circle/poly boundary with start, end and midpoint all
    # outside the filled circle -- only the boundary stroke can flag it
    # (review probe 2; against the poly the end point also lands inside).
    "cross": ((5.0, 35.0), (74.0, 35.0)),
}

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
  (segment (start 59 29.75) (end 61 29.75) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 5 35) (end 74 35) (width 0.25) (layer "F.Cu") (net 1))
)"""


def _attributed_probes(x: float, y: float) -> set[str]:
    """Probe names whose track passes within 1.0 mm of ``(x, y)``."""
    return {
        name
        for name, (a, b) in PROBES.items()
        if point_to_segment_distance(x, y, a[0], a[1], b[0], b[1]) <= 1.0
    }


def _kct_flagged_tracks(board: Path) -> set[str]:
    """Probe names that kct's EdgeClearanceRule flags."""
    pcb = PCB.load(board)
    results = EdgeClearanceRule().check(
        pcb,
        SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5),
    )
    flagged: set[str] = set()
    for violation in results.errors:
        if violation.rule_id != "edge_clearance_trace":
            continue
        flagged |= _attributed_probes(violation.location[0], violation.location[1])
    return flagged


def _kicad_flagged_tracks(board: Path, tmp_path: Path) -> set[str]:
    """Probe names that kicad-cli's copper_edge_clearance flags.

    Routed through the evidence toolchain
    (:func:`kicad_tools.cli.kicad_toolchain.evidence_toolchain`): with
    ``KCT_KICAD_DOCKER=1`` kicad-cli is the pinned image's Docker shim, and
    any host kicad-cli is version-gated to the pinned release first.  There
    is deliberately **no skip**: an unresolvable toolchain or a failed DRC
    run fails the test loudly (operator ruling on added skips).
    """
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain
    from kicad_tools.cli.runner import run_drc
    from kicad_tools.drc.report import DRCReport
    from kicad_tools.drc.violation import ViolationType

    try:
        with evidence_toolchain(mounts=[tmp_path]):
            result = run_drc(board, schematic_parity=False)
    except KiCadEnvironmentError as exc:
        pytest.fail(f"pinned kicad-cli unavailable for native edge parity: {exc}")
    if not result.success:
        pytest.fail(f"kicad-cli DRC failed: {result.stderr[:200]}")
    report = DRCReport.load(Path(result.output_path))
    flagged: set[str] = set()
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
            flagged |= _attributed_probes(loc.x_mm, loc.y_mm)
    return flagged


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_filled_edge_cuts_probe_parity(tmp_path, case):
    """kct and kicad-cli agree on which probe tracks each encoding flags."""
    board = tmp_path / f"parity_{case}.kicad_pcb"
    board.write_text(_BOARD_TMPL.format(outer=OUTER, shape=CASES[case]))

    kct = _kct_flagged_tracks(board)
    native = _kicad_flagged_tracks(board, tmp_path)

    assert kct == native, f"kct flagged {kct} but kicad-cli flagged {native}"

    # The issue's own claim, pinned: a filled cut-out additionally flags the
    # deep-interior probe.  For every fill resolution the boundary probes
    # (near-outside, crossing) are flagged -- the cut-out boundary is board
    # edge -- and the far-exterior probe stays clean (it is > 15 mm from
    # every stroke).
    if case.endswith("_filled"):
        assert kct == {"interior", "near", "cross"}
    else:
        assert kct == {"near", "cross"}


def test_probe_report_documents_actual_zero(tmp_path):
    """A filled-cut-out finding reports actual 0.0 mm, as kicad-cli does."""
    board = tmp_path / "actual_zero.kicad_pcb"
    board.write_text(_BOARD_TMPL.format(outer=OUTER, shape=CASES["circle_filled"]))
    pcb = PCB.load(board)
    results = EdgeClearanceRule().check(
        pcb,
        SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5),
    )
    interior = [
        v
        for v in results.errors
        if v.rule_id == "edge_clearance_trace" and _attributed_probes(*v.location) == {"interior"}
    ]
    assert len(interior) == 1
    assert interior[0].actual_value == pytest.approx(0.0)

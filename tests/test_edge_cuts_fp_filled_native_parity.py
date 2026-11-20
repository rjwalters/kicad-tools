"""Native parity: footprint-local filled Edge.Cuts cut-outs (#9692).

The fp-local sibling of ``test_edge_cuts_filled_native_parity.py`` (#9690):
an ``fp_circle`` / ``fp_poly`` / ``fp_rect`` on Edge.Cuts inside a footprint
is a filled cut-out under the same KiCad fill resolution as its board-level
``gr_*`` sibling (explicit token wins; with the token absent only a
zero-stroke-width circle/rect fills, never a poly), but its geometry lives
in the footprint's placement frame -- position *and* rotation -- so the
parity boards rotate every footprint to pin the frame transform too.

Each case is a board whose only cut-out lives in a footprint; probe tracks
(deep-interior, far-exterior, near-outside 0.125 mm from the boundary,
boundary-crossing with all sample points outside the region) must be
flagged identically by ``kct check``'s :class:`EdgeClearanceRule` and by
kicad-cli's ``copper_edge_clearance``.

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

#: Shared probe grid for every case whose fp-local shape lands on the
#: 40..80 x 30..70 area (circle at (60, 50) r 20; the rot-90 polys/rects
#: below are authored to cover it, or the part of it the probes touch).
_ALIGNED_PROBES: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "interior": ((60.0, 50.0), (62.0, 50.0)),
    "exterior": ((20.0, 10.0), (22.0, 10.0)),
    "near": ((59.0, 29.75), (61.0, 29.75)),
    "cross": ((5.0, 35.0), (74.0, 35.0)),
}

#: A circle whose local centre is offset from the anchor: footprint at
#: (60, 50) rotated 30 deg, fp_circle centre (5, 0) end (20, 0) -> board
#: centre (60 + 5 cos 30, 50 - 5 sin 30) = (64.3301, 47.5), radius 15.
_OFFSET_PROBES: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "interior": ((64.33, 47.5), (66.33, 47.5)),
    "exterior": ((20.0, 10.0), (22.0, 10.0)),
    # 0.125 mm outside the circle's top point (64.33, 32.5).
    "near": ((63.33, 32.375), (65.33, 32.375)),
}

#: name -> (footprint ``(at ...)`` tail, fp shape body, probes, filled)
CASES: dict[str, tuple[str, str, dict, bool]] = {
    "fp_circle_filled": (
        "60 50 30",
        '(fp_circle (center 0 0) (end 20 0) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        True,
    ),
    "fp_circle_fill_no": (
        "60 50 30",
        "(fp_circle (center 0 0) (end 20 0) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        False,
    ),
    "fp_circle_offset_rotated_filled": (
        "60 50 30",
        '(fp_circle (center 5 0) (end 20 0) (layer "Edge.Cuts"))',
        _OFFSET_PROBES,
        True,
    ),
    # Local (+-20, +-20) square under rot 90 at (60, 50) -> 40..80 x 30..70.
    "fp_poly_filled": (
        "60 50 90",
        "(fp_poly (pts (xy -20 -20) (xy 20 -20) (xy 20 20) (xy -20 20)) "
        '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        True,
    ),
    "fp_poly_fill_no": (
        "60 50 90",
        "(fp_poly (pts (xy -20 -20) (xy 20 -20) (xy 20 20) (xy -20 20)) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        False,
    ),
    # Local (-20,-15)-(20,15) under rot 90 -> ring (45,70),(45,30),(75,30),
    # (75,70): the aligned probes all stay on it (top edge y=30 spans 45..75,
    # the interior and the crossing track's end sit inside).
    "fp_rect_filled": (
        "60 50 90",
        '(fp_rect (start -20 -15) (end 20 15) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        True,
    ),
    "fp_rect_fill_no": (
        "60 50 90",
        "(fp_rect (start -20 -15) (end 20 15) "
        '(stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))',
        _ALIGNED_PROBES,
        False,
    ),
    # Outward polygon arc (review of #9721): the semicircle bulges below the
    # box, so a track at y=72 (board) sits inside the filled region.
    "fp_poly_arc_outward_filled": (
        "60 50 0",
        "(fp_poly (pts (xy -20 -15) (xy 20 -15) (xy 20 15) "
        "(arc (start 15 15) (mid 0 30) (end -15 15)) (xy -20 15)) "
        '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))',
        {"in_arc": ((60.0, 72.0), (61.0, 72.0)), "exterior": ((20.0, 10.0), (22.0, 10.0))},
        True,
    ),
    # Same arc reversed into a notch: the track at y=58 is in the notch, clear.
    "fp_poly_arc_inward_filled": (
        "60 50 0",
        "(fp_poly (pts (xy -20 -15) (xy 20 -15) (xy 20 15) "
        "(arc (start 15 15) (mid 0 0) (end -15 15)) (xy -20 15)) "
        '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))',
        {"in_notch": ((60.0, 58.0), (61.0, 58.0)), "exterior": ((20.0, 10.0), (22.0, 10.0))},
        True,
    ),
    # Rounded rect: a track well inside the removed (12 mm) corner stays clear.
    "fp_rect_rounded_filled": (
        "60 50 0",
        "(fp_rect (start -20 -20) (end 20 20) (radius 12) "
        '(stroke (width 0.05) (type solid)) (fill yes) (layer "Edge.Cuts"))',
        {
            "removed_corner": ((41.0, 31.0), (42.0, 31.0)),
            "interior": ((60.0, 50.0), (62.0, 50.0)),
            "exterior": ((20.0, 10.0), (22.0, 10.0)),
        },
        True,
    ),
}

#: Exact flagged-probe sets for the cases whose probes do not follow the
#: shared aligned-probe invariant (every non-interior/exterior probe flagged):
#: - outward arc: the probe inside the filled bulge is flagged (no "interior");
#: - inward arc: the probe in the notch is clear, so nothing is flagged;
#: - rounded rect: the probe in the removed corner is clear, the deep
#:   interior of the filled region is flagged.
_EXPLICIT_FLAGGED: dict[str, set[str]] = {
    "fp_poly_arc_outward_filled": {"in_arc"},
    "fp_poly_arc_inward_filled": set(),
    "fp_rect_rounded_filled": {"interior"},
}

_BOARD_TMPL = """(kicad_pcb (version 20241229) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
  (net 0 "")
  (net 1 "N1")
  (setup (pad_to_mask_clearance 0))
  {outer}
  (footprint "CUT" (layer "F.Cu") (at {at})
    {shape}
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Mask")))
  {copper}
)"""


def _board_text(case: str) -> str:
    at, shape, probes, _filled = CASES[case]
    copper = "\n  ".join(
        f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.25) (layer "F.Cu") (net 1))'
        for a, b in probes.values()
    )
    return _BOARD_TMPL.format(outer=OUTER, at=at, shape=shape, copper=copper)


def _attributed_probes(
    probes: dict[str, tuple[tuple[float, float], tuple[float, float]]], x: float, y: float
) -> set[str]:
    """Probe names whose track passes within 1.0 mm of ``(x, y)``."""
    return {
        name
        for name, (a, b) in probes.items()
        if point_to_segment_distance(x, y, a[0], a[1], b[0], b[1]) <= 1.0
    }


def _kct_flagged_tracks(board: Path, probes: dict) -> set[str]:
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
        flagged |= _attributed_probes(probes, violation.location[0], violation.location[1])
    return flagged


def _kicad_flagged_tracks(board: Path, tmp_path: Path, probes: dict) -> set[str]:
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
            flagged |= _attributed_probes(probes, loc.x_mm, loc.y_mm)
    return flagged


@pytest.mark.parametrize("case", sorted(CASES), ids=sorted(CASES))
def test_fp_filled_edge_cuts_probe_parity(tmp_path, case):
    """kct and kicad-cli agree on which probe tracks each fp encoding flags."""
    at, _shape, probes, filled = CASES[case]
    board = tmp_path / f"fp_parity_{case}.kicad_pcb"
    board.write_text(_board_text(case))

    kct = _kct_flagged_tracks(board, probes)
    native = _kicad_flagged_tracks(board, tmp_path, probes)

    assert kct == native, f"kct flagged {kct} but kicad-cli flagged {native}"

    # The cut-out boundary is board edge for every fill resolution (the
    # near-outside and crossing probes stay flagged), the far-exterior
    # probe stays clean, and only a filled cut-out flags the deep-interior
    # probe (#9692's own claim, pinned).
    assert "exterior" not in kct
    if case in _EXPLICIT_FLAGGED:
        # Cases with their own probe sets (no shared invariant): the exact
        # flagged set is authored per case, derived from what each asserts.
        assert kct == _EXPLICIT_FLAGGED[case]
        return
    if filled:
        assert "interior" in kct
    else:
        assert "interior" not in kct
    assert set(probes) - {"interior", "exterior"} <= kct


def test_fp_filled_report_documents_actual_zero(tmp_path):
    """A filled fp cut-out finding reports actual 0.0 mm, as kicad-cli does."""
    case = "fp_circle_offset_rotated_filled"
    probes = CASES[case][2]
    board = tmp_path / "fp_actual_zero.kicad_pcb"
    board.write_text(_board_text(case))
    pcb = PCB.load(board)
    results = EdgeClearanceRule().check(
        pcb,
        SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5),
    )
    interior = [
        v
        for v in results.errors
        if v.rule_id == "edge_clearance_trace"
        and _attributed_probes(probes, *v.location) == {"interior"}
    ]
    assert len(interior) == 1
    assert interior[0].actual_value == pytest.approx(0.0)

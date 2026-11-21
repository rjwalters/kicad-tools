"""Issue #8957: stitch vias keep their annulus out of the same-net pad slit band.

``kct stitch --avoid-pad-overlap`` (#3271) only keeps a stitch via's DRILL out
of same-net SMD pad bounding boxes; nothing constrained how far the via's
copper ANNULUS sits from the pad it escapes.  On the board 06 / board 07
recipes that leaves a stable population of ~0.089 mm same-net slits (via
annulus vs its own pad, a stub bridging the middle), which the fab-view
``physical_copper_gap`` grading (issue #8938, PR #8956) reports as errors --
a fab sees copper, not nets.

The stitcher now rejects candidate seats whose annulus-to-same-net-SMD-pad
gap falls in the forbidden band (``0 < gap <`` the stackup's pad *detection*
edge, ``GapBand.detection_edge_mm`` of the ``pad_spacing`` dfm entry -- never
``error_edge_mm``; #9016) and lets the placement ladder keep walking: annulus
overlapping the pad is a copper union (no slit), and a gap at or beyond the
edge is not a fab finding.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadEdgeGuard,
    PadInfo,
    _resolve_mfr_pad_gap_edge,
    calculate_via_stub,
    find_smd_pad_bboxes_on_nets,
    run_stitch,
)
from kicad_tools.manufacturers.base import load_dfm_thresholds
from kicad_tools.router.via_clearance import same_net_pad_gap_acceptable
from kicad_tools.sexp import parse_string

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

VIA = 0.45
DRILL = 0.3
CLEARANCE = 0.1
R = VIA / 2  # 0.225

#: The issue's worked-example pad: 0.3 x 0.7 SMD rect centred at the origin.
PAD_BBOX = (-0.15, -0.35, 0.15, 0.35)
PAD_TOP = 0.35

EDGE_1OZ = 0.15  # jlcpcb 4layer_1oz pad_spacing danger (no warning band)


# -- the shared band predicate (synthetic geometry, direct) ------------------


def _gap(via_y: float) -> float:
    return via_y - PAD_TOP - R


def test_band_rejects_the_measured_gap():
    # The board 07 stamped pattern: 0.0891 mm of air between annulus edge and
    # its own pad.
    y = PAD_TOP + R + 0.0891
    assert math.isclose(_gap(y), 0.0891, abs_tol=1e-9)
    assert not same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], EDGE_1OZ)


def test_tangent_annulus_is_rejected():
    # Issue #9124: gap == 0 is a one-point touch -- the receding arc leaves a
    # sub-edge wedge the fab-view checker grades, so it is NOT a union.
    assert not same_net_pad_gap_acceptable(0.0, PAD_TOP + R, VIA, [PAD_BBOX], EDGE_1OZ)


def test_overlapping_annulus_is_acceptable():
    # gap < 0: seated inward onto its own pad (drill still outside the bbox).
    assert same_net_pad_gap_acceptable(0.0, PAD_TOP + R - 0.075, VIA, [PAD_BBOX], EDGE_1OZ)


def test_at_and_beyond_edge_is_acceptable():
    assert same_net_pad_gap_acceptable(0.0, PAD_TOP + R + EDGE_1OZ, VIA, [PAD_BBOX], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(0.0, PAD_TOP + R + 0.2, VIA, [PAD_BBOX], EDGE_1OZ)


def test_diagonal_corner_gap_in_band_is_rejected():
    # Rect distance is measured to the pad corner, not the axis projection.
    d = R + 0.0891
    g = d / math.sqrt(2.0)
    assert not same_net_pad_gap_acceptable(
        PAD_BBOX[2] + g, PAD_BBOX[3] + g, VIA, [PAD_BBOX], EDGE_1OZ
    )
    # One notch further out along the same diagonal clears the edge.
    d2 = R + EDGE_1OZ
    g2 = d2 / math.sqrt(2.0)
    assert same_net_pad_gap_acceptable(
        PAD_BBOX[2] + g2, PAD_BBOX[3] + g2, VIA, [PAD_BBOX], EDGE_1OZ
    )


def test_any_pad_in_band_rejects():
    far = (10.0, 10.0, 10.4, 10.4)
    y = PAD_TOP + R + 0.0891
    assert not same_net_pad_gap_acceptable(0.0, y, VIA, [far, PAD_BBOX], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(0.0, y, VIA, [far], EDGE_1OZ)


def test_disabled_when_edge_zero_or_no_pads():
    y = PAD_TOP + R + 0.0891
    assert same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], 0.0)
    assert same_net_pad_gap_acceptable(0.0, y, VIA, [], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(0.0, y, VIA, None, EDGE_1OZ)


def test_two_oz_gap_between_error_and_detection_edge_is_rejected():
    # jlcpcb 4layer_2oz pad_spacing: danger 0.15, warning 0.13 -> error edge
    # 0.13, detection edge 0.15.  A gap in [0.13, 0.15) is graded a
    # physical_copper_gap warning by ``kct check``, so the stitcher (which
    # uses the detection edge, #9016) must reject it too.
    edge = _resolve_mfr_pad_gap_edge("jlcpcb", 4, 2.0)
    for gap in (0.13, 0.14, 0.12):
        y = PAD_TOP + R + gap
        assert not same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], edge)
    # At and beyond the detection edge the seat is acceptable.
    y = PAD_TOP + R + 0.15
    assert same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], edge)


# -- the edge comes from the profile table, never a literal ------------------


def test_pad_gap_edge_resolves_from_jlcpcb_table_1oz():
    # 4layer_1oz pad_spacing records danger 0.15 and NO warning band, so the
    # detection edge is the danger value itself.
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(EDGE_1OZ)


def test_pad_gap_edge_resolves_from_jlcpcb_table_2oz():
    # 4layer_2oz pad_spacing records danger 0.15 AND warning 0.13; the
    # detection edge is the looser of the two (#9016) -- the error edge
    # (0.13) would let the stitcher seat gaps the checker warns about.
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 2.0) == pytest.approx(0.15)


def test_pad_gap_edge_is_none_without_a_table():
    # pcbway ships no dfm_thresholds table -> no band constraint to derive.
    assert load_dfm_thresholds("pcbway") is None
    assert _resolve_mfr_pad_gap_edge("pcbway", 4, 1.0) is None


# -- the placement ladder honours the band -----------------------------------

PAD = PadInfo(
    reference="Q1",
    pad_number="1",
    net_number=1,
    net_name="GND",
    x=0.0,
    y=0.0,
    layer="F.Cu",
    width=0.3,
    height=0.7,
)

#: Foreign-net pad walls east / west / south of the test pad, so the only
#: live ladder direction is north and every candidate order is pinned.
FOREIGN_BBOX_E = (0.6, -1.0, 1.6, 1.0, 2, "X1", 1.0, 2.0)
FOREIGN_BBOX_W = (-1.6, -1.0, -0.6, 1.0, 2, "X2", 1.0, 2.0)
FOREIGN_BBOX_S = (-1.0, -1.6, 1.0, -0.6, 2, "X3", 2.0, 1.0)
FOREIGN_BBOXES = [FOREIGN_BBOX_E, FOREIGN_BBOX_W, FOREIGN_BBOX_S]


def _stub(**kw) -> tuple[float, float, tuple[float, float] | None] | None:
    base: dict[str, Any] = {
        "pad": PAD,
        "offset": 0.2,
        "via_size": VIA,
        "existing_vias": [],
        "clearance": CLEARANCE,
        "other_net_tracks": [],
        "other_net_vias": [],
        "other_net_pads": [],
        "trace_width": 0.2,
        "other_net_filled_polygons": [],
        "other_net_pad_bboxes": FOREIGN_BBOXES,
        "via_drill": DRILL,
    }
    base.update(kw)
    return calculate_via_stub(**base)


def _annulus_gap(via_x: float, via_y: float) -> float:
    """Annulus-edge gap from the seat to the test pad's rectangle."""
    dx = max(0.0, PAD_BBOX[0] - via_x, via_x - PAD_BBOX[2])
    dy = max(0.0, PAD_BBOX[1] - via_y, via_y - PAD_BBOX[3])
    return math.hypot(dx, dy) - R


def test_control_no_guards_seats_a_union_on_its_own_pad():
    # Baseline, no guards at all: the ladder's first live rung north overlaps
    # its own pad -- drill outside the bbox, annulus unioned with the pad.
    stub = _stub(pad_edge_guard=None)
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    assert _annulus_gap(stub[0], stub[1]) <= 1e-9


def test_control_mask_dam_only_seats_in_the_band():
    # Today's production interplay on the board 06/07 recipes: the #7322
    # mask-dam guard pushes the seat one rung out (gap 0.075) -- a slit the
    # fab-view grading reports as an error.  This is the defect #8957 fixes.
    stub = _stub(pad_edge_guard=PadEdgeGuard([PAD_BBOX], 0.1))
    assert stub is not None
    assert stub[:2] == (0.0, 0.65)
    gap = _annulus_gap(stub[0], stub[1])
    assert 0.0 < gap < EDGE_1OZ, f"mask-dam-only control must seat in the band, got gap {gap}"


def test_band_guard_relocates_the_seat_out_of_the_band():
    stub = _stub(
        pad_edge_guard=PadEdgeGuard([PAD_BBOX], 0.1),
        same_net_pad_bboxes=[PAD_BBOX],
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    assert stub is not None
    assert stub[:2] == (0.0, 0.75)
    assert _annulus_gap(stub[0], stub[1]) >= EDGE_1OZ - 1e-9


def test_band_guard_accepts_an_inward_union_seat():
    # With the band guard but no mask-dam floor, the ladder's first live
    # rung north overlaps the pad (drill still outside the bbox): a copper
    # union, not a slit -- the guard must accept it rather than push the
    # via further out.
    stub = _stub(pad_edge_guard=None, same_net_pad_bboxes=[PAD_BBOX], same_net_pad_gap_mm=EDGE_1OZ)
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    assert _annulus_gap(stub[0], stub[1]) <= 1e-9


# -- run_stitch keeps every committed via out of the band ---------------------


def _pcb_text(extra: str = "") -> str:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    return text[:-1] + extra + ")\n"


def _smd_pad_bboxes(pcb: Path) -> list[tuple[float, float, float, float]]:
    sexp = parse_string(pcb.read_text())
    return [(b[0], b[1], b[2], b[3]) for b in find_smd_pad_bboxes_on_nets(sexp, {1})]


def _band_violations(pcb: Path, result, edge: float) -> list[float]:
    bad = []
    for b in _smd_pad_bboxes(pcb):
        for v in result.vias_added:
            dx = max(0.0, b[0] - v.via_x, v.via_x - b[2])
            dy = max(0.0, b[1] - v.via_y, v.via_y - b[3])
            gap = math.hypot(dx, dy) - v.size / 2
            if 1e-9 < gap < edge - 1e-9:
                bad.append(gap)
    return bad


def _stitch(pcb: Path, gap_edge: float):
    return run_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        offset=0.2,
        dry_run=True,
        avoid_pad_overlap=True,
        pad_edge_gap=0.1,
        same_net_pad_gap_mm=gap_edge,
    )


def test_run_stitch_control_seats_a_slit_without_the_edge(tmp_path):
    # The production interplay: --mfr jlcpcb supplies the mask-dam floor
    # (0.1), which pushes the seat one rung west of the pad -- annulus gap
    # 0.025, inside the fab-view error band.  No band edge -> committed.
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_pcb_text())
    result = _stitch(pcb, 0.0)
    assert result.vias_added
    assert _band_violations(pcb, result, EDGE_1OZ), "control must seat a slit"


def test_run_stitch_keeps_vias_out_of_the_band(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_pcb_text())
    result = _stitch(pcb, EDGE_1OZ)
    assert result.vias_added, "a seat outside the band exists; connectivity must stay"
    assert _band_violations(pcb, result, EDGE_1OZ) == []


# -- Issue #9846: the emitted stub TRACK keeps out of the band --------------
#
# The annulus guard above seats the VIA; nothing constrained the 0.2 mm
# pad-to-via TRACK it writes.  On board 06's seed-42 fixture the stitch step
# added two GND tracks sitting 0.125 mm (U2.B4) / 0.100 mm (U3.48) beside
# same-net pads -- the via annulus was a copper union with its own pad, so
# the #8957 annulus check accepted the seat while the track left the slit
# the fab-view physical_copper_gap grading reports.  The stitcher now runs
# the #9012 two-region segment predicate over every emitted leg, against
# same-net SMD pads on the trace's layer.

from kicad_tools.cli.stitch_cmd import trace_points  # noqa: E402
from kicad_tools.router.via_clearance import (  # noqa: E402
    same_net_segment_pad_gap_acceptable,
)

TRACK_HALF = 0.1  # the recipe's 0.2 mm stub, half width


def test_segment_band_rejects_measured_track_gaps():
    # The issue's measured gaps: a 0.2 mm track whose centerline passes
    # 0.100 / 0.125 mm of air from a same-net pad is inside the
    # (0, detection edge) band -> rejected.
    for gap in (0.100, 0.125):
        west = TRACK_HALF + gap
        bbox = (west, -0.35, west + 0.3, 0.35)
        assert not same_net_segment_pad_gap_acceptable(
            (0.0, 0.0), (0.0, 3.0), TRACK_HALF, [bbox], EDGE_1OZ
        ), f"gap {gap} must be rejected"
    # A gap at the detection edge is not a fab finding.
    west = TRACK_HALF + EDGE_1OZ
    assert same_net_segment_pad_gap_acceptable(
        (0.0, 0.0), (0.0, 3.0), TRACK_HALF, [(west, -0.35, west + 0.3, 0.35)], EDGE_1OZ
    )
    # Copper touching copper (the track edge reaches the pad) is a union.
    west = TRACK_HALF - 0.05
    assert same_net_segment_pad_gap_acceptable(
        (0.0, 0.0), (0.0, 3.0), TRACK_HALF, [(west, -0.35, west + 0.3, 0.35)], EDGE_1OZ
    )
    # A leg leaving its OWN pad starts inside the pad: centerline reaches
    # the rectangle -> union, never a slit.
    assert same_net_segment_pad_gap_acceptable(
        (0.0, 0.0), (0.0, 3.0), TRACK_HALF, [PAD_BBOX], EDGE_1OZ
    )
    # Guard off / no pads: disabled.
    west = TRACK_HALF + 0.1
    assert same_net_segment_pad_gap_acceptable(
        (0.0, 0.0), (0.0, 3.0), TRACK_HALF, [(west, -0.35, west + 0.3, 0.35)], 0.0
    )
    assert same_net_segment_pad_gap_acceptable((0.0, 0.0), (0.0, 3.0), TRACK_HALF, None, EDGE_1OZ)


@pytest.mark.parametrize("gap", [0.100, 0.125])
def test_stub_track_band_refuses_the_in_band_rungs(gap):
    # Same-net neighbour beside the north ladder: its west edge sits
    # ``gap`` mm of air from the emitted leg, and because every leg starts
    # at the pad centre, EVERY north rung's leg passes the neighbour at
    # the same gap (the annulus is a union with both pads, so only the
    # TRACK check can reject them).
    west = TRACK_HALF + gap
    neighbor = (west, 0.45, west + 0.3, 0.56)
    # Control (guard off): the ladder's first live rung north seats with
    # the leg in the band -- the U2.B4 / U3.48 defect.
    stub = _stub(same_net_stub_pad_bboxes=[neighbor])
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    # Guard on: every straight rung's leg falls in the band and the
    # strategy refuses them all (the annulus pool stays empty so the
    # refusal is the leg check's doing alone); run_stitch then walks the
    # dog-leg / extended ladders to a legal alternative.
    stub = _stub(
        same_net_stub_pad_bboxes=[neighbor],
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    assert stub is None


def test_stub_track_band_accepts_copper_union_leg():
    # The leg crosses onto the neighbour pad (centerline reaches the
    # rectangle): one copper union, no slit -- the rung stays.
    neighbor = (0.05, 0.4, 0.35, 0.9)
    stub = _stub(
        same_net_stub_pad_bboxes=[neighbor],
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)


def test_stub_track_band_accepts_gap_at_detection_edge():
    # West edge placed so the first rung's leg gap is exactly the 0.15 mm
    # detection edge: at the edge is not a finding.
    west = TRACK_HALF + EDGE_1OZ
    neighbor = (west, 0.45, west + 0.3, 0.56)
    stub = _stub(
        same_net_stub_pad_bboxes=[neighbor],
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)


# Board fixture for the run_stitch path: A (the pad under stitch) at
# (109.49, 110) as in TWO_LAYER_GND_ZONE_PCB, a same-net GND neighbour B
# north of it whose south edge leaves 0.100 mm of air from the first north
# rung's emitted leg, and a foreign (net 0) wall east blocking the east
# rung so the north rung is the ladder's first live candidate.
_TRACK_NEIGHBOR_FOOTPRINT = """  (footprint "Test:Neighbor" (layer "{layer}") (uuid "00000000-0000-0000-0000-000000000200")
    (at 109.49 110.96)
    (property "Reference" "B1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-2"))
    (pad "1" smd rect (at 0 0) (size 0.3 0.48) (layers "{layer}" "F.Mask") (net 1 "GND"))
  )
"""
_NEIGHBOR_VIA = """  (via (at 109.49 111.35) (size 0.45) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
"""
_FOREIGN_EAST_WALL = """  (footprint "Test:Wall" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000300")
    (at 110.6 110)
    (property "Reference" "W1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-3"))
    (pad "1" smd rect (at 0 0) (size 0.8 1.0) (layers "F.Cu" "F.Mask") (net 0 ""))
  )
"""


def _track_pcb(neighbor_layer: str = "F.Cu", neighbor_via: bool = False) -> str:
    extra = _TRACK_NEIGHBOR_FOOTPRINT.format(layer=neighbor_layer)
    if neighbor_via:
        extra += _NEIGHBOR_VIA
    extra += _FOREIGN_EAST_WALL
    return _pcb_text(extra)


def _leg_gap(segment: tuple[tuple[float, float], tuple[float, float]], bbox) -> float:
    """Hand-rolled exact air gap for the fixture's axis-aligned legs."""
    (sx, sy), (ex, ey) = segment
    assert sx == ex or sy == ey, "fixture legs are axis-aligned"
    dx = max(bbox[0] - max(sx, ex), min(sx, ex) - bbox[2], 0.0)
    dy = max(bbox[1] - max(sy, ey), min(sy, ey) - bbox[3], 0.0)
    return math.hypot(dx, dy) - TRACK_HALF


def _leg_band_violations(pcb: Path, result, edge: float) -> list[float]:
    """In-band gaps between every EMITTED stub leg and every same-net SMD pad.

    The legs are the exact vertices the writer emits (``trace_points``),
    so the measurement sees the copper that reaches the board.
    """
    bad = []
    for b in _smd_pad_bboxes(pcb):
        for trace in result.traces_added:
            pts = trace_points(trace)
            for leg in zip(pts, pts[1:], strict=False):
                gap = _leg_gap(leg, b)
                if 1e-9 < gap < edge - 1e-9:
                    bad.append(gap)
    return bad


def _stitch_tracks(pcb: Path, gap_edge: float):
    return run_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        offset=0.2,
        dry_run=True,
        avoid_pad_overlap=True,
        same_net_pad_gap_mm=gap_edge,
    )


def test_run_stitch_track_band_control_seats_the_slit(tmp_path):
    # Guard off: the first north rung seats -- via annulus unioned with its
    # own pad (accepted) while the emitted leg leaves a 0.100 mm slit
    # beside the same-net neighbour.  This is the measured U3.48 defect.
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_track_pcb())
    result = _stitch_tracks(pcb, 0.0)
    assert result.vias_added
    assert _leg_band_violations(pcb, result, EDGE_1OZ), "control must seat a track slit"


def test_run_stitch_keeps_stub_tracks_out_of_the_band(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_track_pcb())
    result = _stitch_tracks(pcb, EDGE_1OZ)
    # Connectivity must survive: either a legal seat or an explicit,
    # reported same_net_pad_gap drop (never a silent loss).
    assert result.vias_added or any(
        "same_net_pad_gap" in reason for _pad, reason in result.pads_skipped
    )
    assert _leg_band_violations(pcb, result, EDGE_1OZ) == []


def test_run_stitch_track_band_ignores_opposite_layer_pad(tmp_path):
    # Passing control: the same neighbour geometry on B.Cu shares no copper
    # with the F.Cu stub -- the leg check must not see it, and the first
    # north rung (leg 0.100 mm from the B.Cu pad's bbox in xy) commits.
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_track_pcb(neighbor_layer="B.Cu"))
    result = _stitch_tracks(pcb, EDGE_1OZ)
    assert any(
        v.via_x == pytest.approx(109.49) and v.via_y == pytest.approx(110.52)
        for v in result.vias_added
    ), "an opposite-layer pad must not block the F.Cu stub leg"


def test_run_stitch_track_band_applies_to_connected_pads(tmp_path):
    # The U3.48 shape: the neighbour pad already has its own via (a
    # connection through the pad / via elsewhere), so stitch skips it as
    # connected -- but its copper still bounds every other pad's legs: the
    # 0.100 mm slit is refused exactly as for an unconnected neighbour.
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_track_pcb(neighbor_via=True))
    control = _stitch_tracks(pcb, 0.0)
    assert control.vias_added
    assert _leg_band_violations(pcb, control, EDGE_1OZ), "control must seat a track slit"
    guard = _stitch_tracks(pcb, EDGE_1OZ)
    assert guard.vias_added or any(
        "same_net_pad_gap" in reason for _pad, reason in guard.pads_skipped
    )
    assert _leg_band_violations(pcb, guard, EDGE_1OZ) == []

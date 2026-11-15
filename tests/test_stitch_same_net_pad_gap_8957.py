"""Issue #8957: stitch vias keep their annulus out of the same-net pad slit band.

``kct stitch --avoid-pad-overlap`` (#3271) only keeps a stitch via's DRILL out
of same-net SMD pad bounding boxes; nothing constrained how far the via's
copper ANNULUS sits from the pad it escapes.  On the board 06 / board 07
recipes that leaves a stable population of ~0.089 mm same-net slits (via
annulus vs its own pad, a stub bridging the middle), which the fab-view
``physical_copper_gap`` grading (issue #8938, PR #8956) reports as errors --
a fab sees copper, not nets.

The stitcher now rejects candidate seats whose annulus-to-same-net-SMD-pad
gap falls in the forbidden band (``0 < gap <`` the stackup's pad error edge,
``GapBand.error_edge_mm`` of the ``pad_spacing`` dfm entry) and lets the
placement ladder keep walking: annulus overlapping the pad is a copper union
(no slit), and a gap at or beyond the edge is not a fab finding.
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


def test_touching_annulus_is_acceptable():
    # gap == 0: annulus and pad form one copper union, no slit.
    assert same_net_pad_gap_acceptable(0.0, PAD_TOP + R, VIA, [PAD_BBOX], EDGE_1OZ)


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


def test_two_oz_edge_bites_below_one_oz_edge():
    # jlcpcb 4layer_2oz pad_spacing: danger 0.15, warning 0.13 -> the error
    # band shrinks to (0, 0.13) and 0.13-0.15 downgrades to a warning.  A
    # seat at gap 0.14 is an error on 1 oz (rejected) and only a warning on
    # 2 oz (accepted) -- a hardcoded 0.15 edge would over-reject 2 oz
    # boards, a hardcoded 0.13 would under-reject 1 oz boards.
    y = PAD_TOP + R + 0.14
    assert not same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], 0.13)
    # Below the 2oz warning band both stackups reject.
    y2 = PAD_TOP + R + 0.12
    assert not same_net_pad_gap_acceptable(0.0, y2, VIA, [PAD_BBOX], EDGE_1OZ)
    assert not same_net_pad_gap_acceptable(0.0, y2, VIA, [PAD_BBOX], 0.13)


# -- the edge comes from the profile table, never a literal ------------------


def test_pad_gap_edge_resolves_from_jlcpcb_table_1oz():
    # 4layer_1oz pad_spacing records danger 0.15 and NO warning band, so the
    # error edge is the danger value itself.
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(EDGE_1OZ)


def test_pad_gap_edge_resolves_from_jlcpcb_table_2oz():
    # 4layer_2oz pad_spacing records danger 0.15 AND warning 0.13; the error
    # edge is the tighter of the two -- a hardcoded 0.15 would silently
    # under-constrain this stackup.
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 2.0) == pytest.approx(0.13)


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

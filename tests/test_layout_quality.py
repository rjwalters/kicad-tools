"""Tests for layout-quality metrics (issue #9594).

Covers the shared ``analyze_layout_quality`` metric set on synthetic PCBs
with known counts, the via classification rule, the buried-vertex
exemption, arcs, the KiCad-10 name-only net dialect, rerun stability, and
the fleet-router benchmark's fingerprint integration (the block is
reported per case and deliberately excluded from the placement
comparison).
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
from pathlib import Path

import pytest

from kicad_tools.analysis import analyze_layout_quality
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent

# One of each corner class, each on its own net, plus vias of each kind
# and a differential pair with a known skew. Corner geometry:
#   SIG  -- 90 deg interior at (15, 5)
#   BEND -- 45 deg direction change (135 deg interior): not a corner
#   FOLD -- 135 deg direction change (45 deg interior): acute
#   TURN -- ~5.2 deg interior at (35, 25): turn-back
KNOWN_COUNTS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG")
  (net 2 "BEND")
  (net 3 "FOLD")
  (net 4 "TURN")
  (net 5 "GND")
  (net 6 "USB_D+")
  (net 7 "USB_D-")

  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 5 5) (end 15 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 15 5) (end 15 15) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 5 25) (end 12 25) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 12 25) (end 15.5355 21.4645) (width 0.2) (layer "F.Cu") (net 2))

  (segment (start 25 5) (end 35 5) (width 0.2) (layer "F.Cu") (net 3))
  (segment (start 35 5) (end 27.93 12.07) (width 0.2) (layer "F.Cu") (net 3))

  (segment (start 25 25) (end 35 25) (width 0.2) (layer "F.Cu") (net 4))
  (segment (start 35 25) (end 25.2 25.9) (width 0.2) (layer "F.Cu") (net 4))

  (segment (start 5 15) (end 10 15) (width 0.2) (layer "F.Cu") (net 6))
  (segment (start 5 17) (end 15 17) (width 0.2) (layer "F.Cu") (net 7))

  (via (at 45 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 5))
  (via (at 45 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 45 15) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu"))
)
"""

# A 90 deg join whose vertex sits at the centre of a same-net pad: a
# joint, not a bend -- skipped like sharp_trace_corner skips it. A second
# join on the same net away from copper stays counted.
BURIED_VERTEX_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "PNET")

  (gr_rect (start 0 0) (end 50 40) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "TestPoint:TestPoint_Pad"
    (layer "F.Cu")
    (at 20 20)
    (property "Reference" "TP1")
    (pad "1" smd rect (at 0 0) (size 1.5 1.5) (layers "F.Cu") (net 1 "PNET"))
  )

  (segment (start 15 20) (end 20 20) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 20 20) (end 20 28) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 30 10) (end 40 10) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 40 10) (end 40 20) (width 0.2) (layer "F.Cu") (net 1))
)
"""

# Arcs contribute length but no corners; collinear pass-throughs (180 deg
# interior) are not corners either.
ARCS_AND_PASSTHROUGH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "ARCNET")
  (net 2 "STRAIGHT")

  (gr_rect (start 0 0) (end 60 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 0 0) (end 5 0) (width 0.2) (layer "F.Cu") (net 1))
  (arc (start 45 15) (mid 40 20) (end 35 15) (width 0.2) (layer "F.Cu") (net 1))

  (segment (start 10 25) (end 15 25) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 15 25) (end 20 25) (width 0.2) (layer "F.Cu") (net 2))
)
"""

# KiCad-10 name-only dialect: segments carry (net "NAME") with no number,
# so grouping must key on the name -- two "NA" segments form one corner
# while an "NB" segment touching the same vertex stays a different net.
NAME_ONLY_PCB = """(kicad_pcb
  (version 20250114)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "NA")
  (net 2 "NB")

  (gr_rect (start 0 0) (end 40 40) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") (net "NA"))
  (segment (start 20 10) (end 20 20) (width 0.2) (layer "F.Cu") (net "NA"))
  (segment (start 20 10) (end 30 5) (width 0.2) (layer "F.Cu") (net "NB"))
)
"""

# A 100x50 gr_rect outline with a 10x10 gr_line-ring cutout inside: the
# cutout must subtract. Regression for the review finding on #9614: the
# old add/subtract parity never fired because polygonize_full returns the
# outer face with the hole already punched, so every face sat at depth 0
# and the gross 5000 mm^2 was reported.
CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 20 10) (end 30 10) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 30 10) (end 30 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 30 20) (end 20 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 20 20) (end 20 10) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# A 20x20 cutout with a 5x5 island inside it: outer - cutout + island.
NESTED_ISLAND_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 15 5) (end 35 5) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 35 5) (end 35 25) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 35 25) (end 15 25) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 15 25) (end 15 5) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_line (start 22 12) (end 27 12) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 27 12) (end 27 17) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 27 17) (end 22 17) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 22 17) (end 22 12) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# A gr_circle hole is measured with the certified circle tessellation
# (~1e-5 mm chord error), so the published area is the rect minus the
# true disc, not the gross rect the circle would silently vanish into.
CIRCLE_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_circle (center 25 15) (end 30 15) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# Footprint-owned Edge.Cuts graphics are part of the outline: a 10x10
# fp_rect cutout inside a footprint placed at the board centre subtracts
# exactly like a top-level one (review finding on #9614: the old code
# only looked at pcb.graphics, so footprint cutouts vanished).
FP_RECT_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "J9")
    (fp_rect (start -5 -5) (end 5 5) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""

# A rotated footprint's fp_rect cutout: the four corners are transformed
# from footprint space, not synthesized as an axis-aligned box from two
# rotated opposite corners (review finding on #9614: at 30 deg that box
# understated a 24 mm^2 cutout to 3.34, and a square at 45 deg collapsed
# to a zero-height line, dropping the area to None).
FP_RECT_ROTATED_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 40 20 {rot})
    (property "Reference" "J9")
    (fp_rect (start {lx1} {ly1}) (end {lx2} {ly2}) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""

# fp_curve on Edge.Cuts is not parsed into the footprint model at all,
# so the contour cannot be measured: refuse the area rather than report
# the gross rect (review finding on #9614).
FP_CURVE_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "J9")
    (fp_curve (pts (xy -5 -5) (xy -2 3) (xy 2 -3) (xy 5 5)) (layer "Edge.Cuts"))
  )
)
"""

# A closed lens cutout built from two gr_curve elements (each the exact
# degree-elevation of a quadratic Bezier with peak h=3, so the lens
# encloses exactly 20 mm^2). Curved contours are measured with the
# certified cubic tessellation, not dropped (review finding on #9614).
CURVE_LENS_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_curve
    (pts (xy 20 10) (xy 23.333333 12) (xy 26.666667 12) (xy 30 10))
    (stroke (width 0.1)) (layer "Edge.Cuts")
  )
  (gr_curve
    (pts (xy 30 10) (xy 26.666667 8) (xy 23.333333 8) (xy 20 10))
    (stroke (width 0.1)) (layer "Edge.Cuts")
  )
)
"""

# A radius-5 mm circular cutout authored as two semicircular gr_arc
# elements: arcs must be tessellated to their certified chord accuracy,
# not reduced to two chords each (which measured 4950 instead of
# 4921.460 -- review finding on #9614).
ARC_PAIR_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (gr_arc (start 20 15) (mid 25 20) (end 30 15) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_arc (start 30 15) (mid 25 10) (end 20 15) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# The same radius-5 mm disc authored as two semicircular fp_arc elements
# inside a footprint placed at the disc's centre: the footprint
# representation must measure what its gr_arc twins measure (review
# finding on #9614: the fp_arc branch zipped each tessellated chain
# against its own shorter tail with strict=True, which raised for every
# footprint arc and silently dropped outline_area_mm2).
FP_ARC_PAIR_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 25 15 {rot})
    (property "Reference" "J9")
    (fp_arc (start -5 0) (mid 0 5) (end 5 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
    (fp_arc (start 5 0) (mid 0 -5) (end -5 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""

# A 10x10 gr_rect with a 2 mm corner radius as the whole board: the
# corners are arcs, so the area is the rectangle minus four
# r*r*(1 - pi/4) corners = 84 + 4*pi ~= 96.566 mm^2 (review finding on
# #9614: the routing-outline helper the area delegated to squared the
# corners off and published the gross 100).
ROUNDED_GR_RECT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 10 10) (radius 2) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# The same rounded rectangle as a cutout inside the 100x50 board, so the
# footprint twin can be compared against the gr_rect representation.
ROUNDED_GR_RECT_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_rect (start 20 10) (end 30 20) (radius 2) (stroke (width 0.1)) (layer "Edge.Cuts"))
)
"""

# A footprint-owned rounded-rect cutout at the same place: must subtract
# what its gr_rect twin subtracts at any footprint orientation.
ROUNDED_FP_RECT_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 25 15 {rot})
    (property "Reference" "J9")
    (fp_rect (start -5 -5) (end 5 5) (radius 2) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""

# A 10x10 gr_poly whose right edge bulges out as a 5 mm semicircle arc
# entry inside pts: 100 + 12.5*pi ~= 139.270 mm^2 (review finding on
# #9614: only the xy run was read and the arc vanished into the 100).
GR_POLY_SEMICIRCLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_poly
    (pts
      (xy 0 0) (xy 10 0)
      (arc (start 10 0) (mid 15 5) (end 10 10))
      (xy 10 10) (xy 0 10)
    )
    (stroke (width 0.1)) (layer "Edge.Cuts")
  )
)
"""

# The same shape as a gr_poly cutout inside the 100x50 board.
GR_POLY_SEMICIRCLE_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_poly
    (pts
      (xy 20 10) (xy 30 10)
      (arc (start 30 10) (mid 35 15) (end 30 20))
      (xy 30 20) (xy 20 20)
    )
    (stroke (width 0.1)) (layer "Edge.Cuts")
  )
)
"""

# The footprint twin of the semicircle poly cutout: identical local pts
# inside a (possibly rotated) footprint at the same board position.
FP_POLY_SEMICIRCLE_CUTOUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_rect (start 0 0) (end 100 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:BoardCutout"
    (layer "F.Cu")
    (at 25 15 {rot})
    (property "Reference" "J9")
    (fp_poly
      (pts
        (xy -5 -5) (xy 5 -5)
        (arc (start 5 -5) (mid 10 0) (end 5 5))
        (xy 5 5) (xy -5 5)
      )
      (stroke (width 0.1)) (layer "Edge.Cuts")
    )
  )
)
"""

# Two length-matched nets with a known length difference: with a
# net-class map declaring the match group, the block reports its skew.
MATCH_GROUP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "MA")
  (net 2 "MB")

  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 5 5) (end 15 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 5 15) (end 5 27.5) (width 0.2) (layer "F.Cu") (net 2))
)
"""

# MATCH_GROUP_PCB with a declared B.Cu: a sidecar whose layer tokens
# depend on the board's copper count must resolve against the same text
# the fingerprint parsed (review finding on #9614: the sidecar loader
# reopened the PCB by path for stack resolution).
MATCH_GROUP_TWO_LAYER_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "MA")
  (net 2 "MB")

  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 5 5) (end 15 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 5 15) (end 5 27.5) (width 0.2) (layer "F.Cu") (net 2))
)
"""


def _load_pcb(text: str) -> PCB:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb") as fh:
        fh.write(text)
        fh.flush()
        return PCB.load(fh.name)


def test_known_counts():
    report = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB))

    assert (report.right_angle_corners, report.acute_corners, report.turn_back_corners) == (1, 1, 1)
    assert report.corners_per_net == {
        "SIG": {"right_angle": 1, "acute": 0, "turn_back": 0},
        "FOLD": {"right_angle": 0, "acute": 1, "turn_back": 0},
        "TURN": {"right_angle": 0, "acute": 0, "turn_back": 1},
    }
    assert "BEND" not in report.corners_per_net  # 135 deg interior: not a corner

    assert (report.track_vias, report.ground_vias) == (2, 1)  # netless via is a track via

    expected = (
        math.dist((5, 5), (15, 5))
        + math.dist((15, 5), (15, 15))
        + math.dist((5, 25), (12, 25))
        + math.dist((12, 25), (15.5355, 21.4645))
        + math.dist((25, 5), (35, 5))
        + math.dist((35, 5), (27.93, 12.07))
        + math.dist((25, 25), (35, 25))
        + math.dist((35, 25), (25.2, 25.9))
        + math.dist((5, 15), (10, 15))
        + math.dist((5, 17), (15, 17))
    )
    assert report.total_track_length_mm == pytest.approx(expected, abs=1e-6)
    assert report.outline_area_mm2 == pytest.approx(50 * 30, abs=1e-6)


def test_to_dict_shape_and_rounding():
    data = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB)).to_dict()
    assert data["via"] == {"track": 2, "ground": 1, "total": 3}
    assert data["corners"]["right_angle"] == 1
    assert data["corners"]["acute"] == 1
    assert data["corners"]["turn_back"] == 1
    assert set(data["corners"]["per_net"]) == {"FOLD", "SIG", "TURN"}  # sorted keys
    assert data["total_track_length_mm"] == round(data["total_track_length_mm"], 3)
    assert data["outline_area_mm2"] == 1500.0
    assert len(data["diff_pair_skew"]) == 1
    skew = data["diff_pair_skew"][0]
    assert skew["net_positive"] == "USB_D+"
    assert skew["net_negative"] == "USB_D-"
    assert skew["length_positive_mm"] == pytest.approx(5.0, abs=1e-6)
    assert skew["length_negative_mm"] == pytest.approx(10.0, abs=1e-6)
    assert skew["skew_mm"] == pytest.approx(5.0, abs=1e-6)


def test_buried_vertex_skipped_open_corner_counted():
    report = analyze_layout_quality(_load_pcb(BURIED_VERTEX_PCB))
    # The pad-centre joint is exempt; the free bend on the same net counts.
    assert report.corners_per_net == {"PNET": {"right_angle": 1, "acute": 0, "turn_back": 0}}
    assert report.right_angle_corners == 1


def test_arcs_add_length_no_corners_and_passthrough_not_counted():
    report = analyze_layout_quality(_load_pcb(ARCS_AND_PASSTHROUGH_PCB))
    assert report.corners_per_net == {}
    assert (report.right_angle_corners, report.acute_corners, report.turn_back_corners) == (0, 0, 0)
    # 5 mm segment + semicircular arc (r=5 -> 5*pi mm) + 10 mm collinear pair.
    assert report.total_track_length_mm == pytest.approx(5 + 5 * math.pi + 10, abs=1e-6)
    assert report.outline_area_mm2 == pytest.approx(60 * 30, abs=1e-6)


def test_name_only_net_dialect_groups_by_name():
    report = analyze_layout_quality(_load_pcb(NAME_ONLY_PCB))
    # Exactly one 90 deg corner on NA; the NB leg meeting at the same
    # vertex is a different net and joins nothing.
    assert report.corners_per_net == {"NA": {"right_angle": 1, "acute": 0, "turn_back": 0}}


def test_outline_cutout_subtracts():
    area = analyze_layout_quality(_load_pcb(CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 10 * 10, abs=1e-6)


def test_nested_island_inside_cutout_adds_back():
    area = analyze_layout_quality(_load_pcb(NESTED_ISLAND_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 20 * 20 + 5 * 5, abs=1e-6)


def test_circle_cutout_measured():
    # The certified circle tessellation (~1e-5 mm sagitta) measures the
    # hole instead of dropping it or refusing the area.
    area = analyze_layout_quality(_load_pcb(CIRCLE_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - math.pi * 25, abs=1e-3)


def test_footprint_rect_cutout_subtracts():
    # Footprint-owned Edge.Cuts graphics are part of the outline.
    area = analyze_layout_quality(_load_pcb(FP_RECT_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 10 * 10, abs=1e-6)


@pytest.mark.parametrize("rot", [0, 30, 45, 60, 90, 135, 180, 270])
def test_footprint_rect_cutout_rotation_invariant(rot):
    # A rigid rotation cannot change a cutout's area: a 6x4 fp_rect at
    # any footprint orientation subtracts exactly 24 mm^2. The old rect
    # branch synthesized two corners in board space, which understated
    # the cutout at every non-multiple of 90 deg (3.34 at 30 deg).
    pcb = _load_pcb(FP_RECT_ROTATED_CUTOUT_PCB.format(rot=rot, lx1=-3, ly1=-2, lx2=3, ly2=2))
    assert analyze_layout_quality(pcb).outline_area_mm2 == pytest.approx(100 * 50 - 24.0, abs=1e-6)


MIXED_JOIN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_line (start 40 20) (end 45 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 45 20) (end 45 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 45 30) (end {close_x} 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:EdgePiece"
    (layer "F.Cu")
    (at 40 30 {rot})
    (property "Reference" "J9")
    (fp_line (start 0 0) (end 10 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""


def test_rotated_footprint_edge_joins_board_lines():
    # A footprint at 90 deg maps local (10, 0) to (40 + 6e-16, 20) rather
    # than exactly (40, 20); the shared corner must still close the
    # contour (5 x 10 = 50 mm^2).
    pcb = _load_pcb(MIXED_JOIN_PCB.format(rot=90, close_x=40))
    assert analyze_layout_quality(pcb).outline_area_mm2 == pytest.approx(50.0, abs=1e-6)


MIXED_JOIN_TRIANGLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_line (start {ex} {ey}) (end {ex} 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start {ex} 30) (end 40 30) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:EdgePiece"
    (layer "F.Cu")
    (at 40 30 {rot})
    (property "Reference" "J9")
    (fp_line (start 0 0) (end 10 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
  )
)
"""


@pytest.mark.parametrize("rot", [30, 45, 60])
def test_rotated_footprint_edge_joins_board_lines_at_kicad_precision(rot):
    # The board graphic declares the shared corner at KiCad's 1 nm file
    # precision while the footprint endpoint is computed in float; the
    # contour must still close at rotations that are not multiples of 90.
    rad = math.radians(rot)
    ex = 40 + 10 * math.cos(rad)
    ey = 30 - 10 * math.sin(rad)
    pcb = _load_pcb(MIXED_JOIN_TRIANGLE_PCB.format(rot=rot, ex=f"{ex:.6f}", ey=f"{ey:.6f}"))
    expected = 0.5 * abs(ex - 40) * abs(30 - ey)
    assert analyze_layout_quality(pcb).outline_area_mm2 == pytest.approx(expected, abs=1e-4)


TRANSFORM_JOIN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")

  (gr_line (start {p1}) (end {p2}) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start {p2}) (end {p3}) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (footprint "Mech:EdgePiece"
    (layer "F.Cu")
    (at 40 30 {rot})
    (property "Reference" "J9")
    {graphic}
  )
)
"""

# KiCad-saved coordinates (6 decimals) of a board triangle closed by one
# rotated footprint edge. The shared corners come from two float paths
# (footprint rotation + board-origin subtraction vs. the written value), so
# the copies straddle a fixed-grid rounding boundary (review of #9614).
_TRANSFORM_JOIN_CASES = {
    45: (
        "47.778175 22.221825",
        "47.778175 29.292893",
        "40.707107 29.292893",
        (1, 0),
        11.000001,
        (6, -1),
    ),
    15: (
        "53.522962 26.376533",
        "53.522962 28.964724",
        "43.863703 28.964724",
        (4, 0),
        14.000001,
        (9, -1),
    ),
}
_TRANSFORM_JOIN_AREAS = {
    (45, "line"): 25.000001,
    (15, "line"): 12.500004,
    # Triangle + the exact circular segment (chord 10, sagitta 1, r=13:
    # 169*asin(5/13) - 60). KiCad's own polygonization of the arc is coarser
    # (31.712014 / 19.211992), so the exact value is the reference here.
    (45, "arc"): 25.000001 + 169 * math.asin(5 / 13) - 60,
    (15, "arc"): 12.500004 + 169 * math.asin(5 / 13) - 60,
}


@pytest.mark.parametrize("kind", ["line", "arc"])
@pytest.mark.parametrize("rot", [45, 15])
def test_rotated_footprint_edge_joins_at_transform_rounding_boundary(rot, kind):
    p1, p2, p3, (sx, sy), ex, mid = _TRANSFORM_JOIN_CASES[rot]
    if kind == "line":
        graphic = (
            f'(fp_line (start {sx} {sy}) (end {ex} 0) (stroke (width 0.1)) (layer "Edge.Cuts"))'
        )
    else:
        graphic = (
            f"(fp_arc (start {sx} {sy}) (mid {mid[0]} {mid[1]}) (end {ex} 0) "
            '(stroke (width 0.1)) (layer "Edge.Cuts"))'
        )
    pcb = _load_pcb(TRANSFORM_JOIN_PCB.format(p1=p1, p2=p2, p3=p3, rot=rot, graphic=graphic))
    area = analyze_layout_quality(pcb).outline_area_mm2
    assert area is not None
    assert area == pytest.approx(_TRANSFORM_JOIN_AREAS[(rot, kind)], abs=2e-3)


def test_endpoint_clustering_joins_straddling_pair_but_not_a_real_gap():
    from kicad_tools.analysis.layout_quality import _cluster_endpoints

    # 7.071068 / 7.071069 land in different 1e-6 cells yet are one corner.
    joined = _cluster_endpoints([((0.0, 0.0), (7.071068, 0.0)), ((7.071069, 0.0), (7.0, 5.0))])
    assert joined[0][1] == joined[1][0]
    # A 1e-3 mm gap is a real gap and must survive.
    gap = _cluster_endpoints([((0.0, 0.0), (7.071, 0.0)), ((7.072, 0.0), (7.0, 5.0))])
    assert gap[0][1] != gap[1][0]


def test_open_contour_still_none_after_snapping():
    # Control: a genuinely open contour (gap of 1 mm) stays unmeasured.
    pcb = _load_pcb(MIXED_JOIN_PCB.format(rot=90, close_x=41))
    assert analyze_layout_quality(pcb).outline_area_mm2 is None


@pytest.mark.parametrize("rot", [30, 45])
def test_footprint_square_cutout_rotated_not_degenerate(rot):
    # A square at a non-multiple of 90 deg used to collapse to a
    # zero-height line (two points, not four), leaving dangles that
    # dropped the whole area to None; it must subtract 36 mm^2.
    pcb = _load_pcb(FP_RECT_ROTATED_CUTOUT_PCB.format(rot=rot, lx1=-3, ly1=-3, lx2=3, ly2=3))
    assert analyze_layout_quality(pcb).outline_area_mm2 == pytest.approx(100 * 50 - 36.0, abs=1e-4)


def test_footprint_curve_edge_cuts_returns_none():
    # fp_curve is absent from the parsed footprint model, so the contour
    # cannot be measured: refuse rather than report the gross rect.
    assert analyze_layout_quality(_load_pcb(FP_CURVE_CUTOUT_PCB)).outline_area_mm2 is None


def test_gr_curve_cutout_measured():
    # Two gr_curve elements close a 20 mm^2 lens: measured with the
    # certified cubic tessellation, not silently dropped.
    area = analyze_layout_quality(_load_pcb(CURVE_LENS_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - 20.0, abs=0.05)


def test_arc_pair_cutout_measured_to_tessellation_accuracy():
    # Two semicircular gr_arcs form a r=5 disc: the tessellated area sits
    # within the certified chord bound (perimeter x 1e-4 mm) of pi r^2,
    # where the old two-chord reduction reported 4950.
    area = analyze_layout_quality(_load_pcb(ARC_PAIR_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - math.pi * 25, abs=0.01)


@pytest.mark.parametrize("rot", [0, 30, 90])
def test_footprint_arc_pair_cutout_matches_gr_arc_representation(rot):
    # The same r=5 disc as two fp_arcs inside a rotated footprint must
    # measure what its gr_arc twins measure: the old fp_arc branch zipped
    # each chain against its shorter tail (strict=True), which raised for
    # every footprint arc and dropped outline_area_mm2 to None.
    gr = analyze_layout_quality(_load_pcb(ARC_PAIR_CUTOUT_PCB)).outline_area_mm2
    fp = analyze_layout_quality(_load_pcb(FP_ARC_PAIR_CUTOUT_PCB.format(rot=rot))).outline_area_mm2
    assert fp == pytest.approx(gr, abs=1e-4)
    assert fp == pytest.approx(100 * 50 - math.pi * 25, abs=0.01)


def test_rounded_gr_rect_area_measured():
    # A 10x10 rectangle with 2 mm corner radius: 84 + 4*pi ~= 96.566,
    # not the gross 100 the square-corner flattening published (the
    # corner arcs are tessellated to the certified 1e-4 mm chord bound).
    area = analyze_layout_quality(_load_pcb(ROUNDED_GR_RECT_PCB)).outline_area_mm2
    assert area == pytest.approx(84.0 + 4.0 * math.pi, abs=0.01)


def test_rounded_gr_rect_cutout_subtracts():
    area = analyze_layout_quality(_load_pcb(ROUNDED_GR_RECT_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - (84.0 + 4.0 * math.pi), abs=0.01)


@pytest.mark.parametrize("rot", [0, 30, 90])
def test_rounded_fp_rect_cutout_matches_gr_rect_representation(rot):
    # The rounded fp_rect keeps its corner arcs from the authored node
    # (the model parses only start/end), so it subtracts what its gr_rect
    # twin subtracts at any footprint orientation.
    gr = analyze_layout_quality(_load_pcb(ROUNDED_GR_RECT_CUTOUT_PCB)).outline_area_mm2
    fp = analyze_layout_quality(
        _load_pcb(ROUNDED_FP_RECT_CUTOUT_PCB.format(rot=rot))
    ).outline_area_mm2
    assert fp == pytest.approx(gr, abs=1e-4)
    assert fp == pytest.approx(100 * 50 - (84.0 + 4.0 * math.pi), abs=0.01)


def test_gr_poly_semicircle_area_measured():
    # A 10x10 square whose right edge is a 5 mm semicircle arc entry:
    # 100 + 12.5*pi ~= 139.270, not the 100 the xy-only reading
    # published.
    area = analyze_layout_quality(_load_pcb(GR_POLY_SEMICIRCLE_PCB)).outline_area_mm2
    assert area == pytest.approx(100.0 + 12.5 * math.pi, abs=0.01)


def test_gr_poly_semicircle_cutout_subtracts():
    area = analyze_layout_quality(_load_pcb(GR_POLY_SEMICIRCLE_CUTOUT_PCB)).outline_area_mm2
    assert area == pytest.approx(100 * 50 - (100.0 + 12.5 * math.pi), abs=0.01)


@pytest.mark.parametrize("rot", [0, 30, 90])
def test_fp_poly_semicircle_cutout_matches_gr_poly_representation(rot):
    # The fp_poly's pts arc entries are read from the authored node (the
    # model keeps only the xy run), so the bulge survives any footprint
    # rotation and subtracts what its gr_poly twin subtracts.
    gr = analyze_layout_quality(_load_pcb(GR_POLY_SEMICIRCLE_CUTOUT_PCB)).outline_area_mm2
    fp = analyze_layout_quality(
        _load_pcb(FP_POLY_SEMICIRCLE_CUTOUT_PCB.format(rot=rot))
    ).outline_area_mm2
    assert fp == pytest.approx(gr, abs=1e-4)
    assert fp == pytest.approx(100 * 50 - (100.0 + 12.5 * math.pi), abs=0.01)


def test_match_group_skew_reported_with_net_class_map():
    from kicad_tools.router.rules import NetClassRouting

    nc = NetClassRouting(name="GRP", length_match_group="GRP")
    pcb = _load_pcb(MATCH_GROUP_PCB)

    without_map = analyze_layout_quality(pcb).to_dict()
    assert "match_group_skew" not in without_map

    data = analyze_layout_quality(pcb, {"MA": nc, "MB": nc}).to_dict()
    assert data["match_group_skew"] == {"GRP": 2.5}


def test_rerun_stability():
    pcb = _load_pcb(KNOWN_COUNTS_PCB)
    assert analyze_layout_quality(pcb).to_dict() == analyze_layout_quality(pcb).to_dict()


def test_ground_via_classification():
    data = analyze_layout_quality(_load_pcb(KNOWN_COUNTS_PCB)).to_dict()
    # The partition always totals the via count, netless vias included.
    assert data["via"]["total"] == data["via"]["track"] + data["via"]["ground"] == 3


# ---------------------------------------------------------------------------
# Fleet-router benchmark integration
# ---------------------------------------------------------------------------

BENCH_SCRIPT = REPO_ROOT / "benchmarks" / "external" / "fleet_routers" / "run.py"


def _load_bench():
    name = "fleet_routers_run_layout_quality_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, BENCH_SCRIPT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def bench():
    return _load_bench()


def _write_pcb(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bench.kicad_pcb"
    path.write_text(text)
    return path


def test_fingerprint_reports_layout_quality_and_is_stable(tmp_path, bench):
    path = _write_pcb(tmp_path, KNOWN_COUNTS_PCB)
    fp1 = bench.fingerprint(path)
    fp2 = bench.fingerprint(path)
    assert fp1["layout_quality"] == fp2["layout_quality"]
    assert fp1["layout_quality"]["corners"]["right_angle"] == 1
    assert fp1["layout_quality"]["via"] == {"track": 2, "ground": 1, "total": 3}


def test_fingerprint_reports_match_group_skew_from_sidecar(tmp_path, bench):
    import json

    path = _write_pcb(tmp_path, MATCH_GROUP_PCB)
    sidecar = tmp_path / "net_class_map.json"
    sidecar.write_text(
        json.dumps(
            {
                "MA": {"name": "GRP", "length_match_group": "GRP"},
                "MB": {"name": "GRP", "length_match_group": "GRP"},
            }
        )
    )
    with_map = bench.fingerprint(path, sidecar)
    assert with_map["layout_quality"]["match_group_skew"] == {"GRP": 2.5}
    without_map = bench.fingerprint(path)
    assert "match_group_skew" not in without_map["layout_quality"]


def test_fingerprint_resolves_layer_sidecar_without_reopening_the_pcb(tmp_path, bench, monkeypatch):
    # A sidecar whose layer tokens depend on the board's copper count
    # ("B.Cu") must be resolved from the same text the fingerprint
    # parsed: the old loader reopened the PCB by path for stack
    # resolution, a second read that diverges from the parsed board
    # (review finding on #9614).
    import json
    from pathlib import Path

    path = _write_pcb(tmp_path, MATCH_GROUP_TWO_LAYER_PCB)
    sidecar = tmp_path / "net_class_map.json"
    sidecar.write_text(
        json.dumps(
            {
                "MA": {
                    "name": "GRP",
                    "length_match_group": "GRP",
                    "preferred_layers": ["B.Cu"],
                },
                "MB": {
                    "name": "GRP",
                    "length_match_group": "GRP",
                    "preferred_layers": ["B.Cu"],
                },
            }
        )
    )
    opens: list[str] = []
    real_read_bytes, real_read_text = Path.read_bytes, Path.read_text

    def counting_read_bytes(self: Path) -> bytes:
        opens.append(self.name)
        return real_read_bytes(self)

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        opens.append(self.name)
        return real_read_text(self, *args, **kwargs)  # type: ignore[call-arg]

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(Path, "read_text", counting_read_text)
    with_map = bench.fingerprint(path, sidecar)
    assert with_map["layout_quality"]["match_group_skew"] == {"GRP": 2.5}
    assert opens.count("bench.kicad_pcb") == 1


def test_fingerprint_rejects_non_pcb_root(tmp_path, bench):
    # A different KiCad file type saved as .kicad_pcb must raise, not
    # fingerprint as an empty board: replacing PCB.load with direct
    # construction had dropped the root-tag check, and the empty result
    # sent the case down the placement-violation branch instead of the
    # fingerprint-error path (review finding on #9614).
    from kicad_tools.exceptions import FileFormatError

    path = tmp_path / "bad.kicad_pcb"
    path.write_text('(kicad_sch (version 20250114) (generator "test"))\n')
    with pytest.raises(FileFormatError):
        bench.fingerprint(path)


def test_compare_fingerprints_ignores_layout_quality(tmp_path, bench):
    path = _write_pcb(tmp_path, KNOWN_COUNTS_PCB)
    before = bench.fingerprint(path)
    after = bench.fingerprint(path)
    # Routing legitimately changes the quality numbers: a router that adds
    # copper is not a placement violation.
    after["layout_quality"]["corners"]["right_angle"] += 7
    after["layout_quality"]["total_track_length_mm"] += 123.456
    assert bench.compare_fingerprints(before, after) == []
    # The comparison still guards the invariants beside it.
    after["pad_nets"]["TP1:1"] = "Hijacked"
    assert bench.compare_fingerprints(before, after) != []


# ---------------------------------------------------------------------------
# _count_corners candidate bucketing (issue #9658)
# ---------------------------------------------------------------------------


def _count_corners_pairwise_reference(pcb: PCB):
    """The pre-#9658 all-pairs junction search, verbatim, as an oracle."""
    from kicad_tools.analysis.layout_quality import (
        _CORNER_CLASSES,
        _VERTEX_DECIMALS,
        _classify_angle,
        _net_display_name,
        _net_raw_name,
    )
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
    from kicad_tools.validate.rules.sharp_trace_corner import _copper_discs, _is_buried

    check = AcidTrapCheck()
    discs = _copper_discs(pcb)
    groups: dict[tuple[int, str, str], list] = {}
    for seg in pcb.segments:
        groups.setdefault((seg.net_number, seg.net_name, seg.layer), []).append(seg)
    vertices: dict[tuple, tuple[tuple[float, float], list]] = {}
    for key, segments in groups.items():
        for i, seg1 in enumerate(segments):
            for seg2 in segments[i + 1 :]:
                junction = check._find_junction(seg1, seg2)
                if junction is None:
                    continue
                vkey = (
                    key,
                    round(junction[0], _VERTEX_DECIMALS),
                    round(junction[1], _VERTEX_DECIMALS),
                )
                entry = vertices.setdefault(vkey, (junction, []))
                legs = entry[1]
                if seg1 not in legs:
                    legs.append(seg1)
                if seg2 not in legs:
                    legs.append(seg2)
    totals: dict[str, int] = dict.fromkeys(_CORNER_CLASSES, 0)
    per_net: dict[str, dict[str, int]] = {}
    for vkey, (junction, legs) in vertices.items():
        net_number, seg_net_name, layer = vkey[0]
        raw_name = _net_raw_name(pcb, net_number, seg_net_name)
        if _is_buried(discs, raw_name, layer, junction):
            continue
        distinct: list = []
        seen_edges: set = set()
        for seg in legs:
            pa = (round(seg.start[0], _VERTEX_DECIMALS), round(seg.start[1], _VERTEX_DECIMALS))
            pb = (round(seg.end[0], _VERTEX_DECIMALS), round(seg.end[1], _VERTEX_DECIMALS))
            edge = (pa, pb) if pa <= pb else (pb, pa)
            if edge not in seen_edges:
                seen_edges.add(edge)
                distinct.append(seg)
        if len(distinct) < 2:
            continue
        display = _net_display_name(pcb, net_number, raw_name)
        for i, seg1 in enumerate(distinct):
            for seg2 in distinct[i + 1 :]:
                cls = _classify_angle(check._calculate_angle(seg1, seg2, junction))
                if cls is None:
                    continue
                totals[cls] += 1
                per_net.setdefault(display, dict.fromkeys(_CORNER_CLASSES, 0))[cls] += 1
    return totals, per_net


def _jittered_endpoint(rng: random.Random, point: tuple[float, float]) -> tuple[float, float]:
    # Per-axis offset inside _find_junction's 0.01 mm tolerance, fine
    # enough to straddle the 2-decimal vertex-rounding boundary.
    return (
        point[0] + rng.uniform(-0.0049, 0.0049),
        point[1] + rng.uniform(-0.0049, 0.0049),
    )


def _random_corner_pcb_text(rng: random.Random) -> str:
    """Chains and T-junctions on mixed nets/layers with every corner class."""
    lines = [
        '(kicad_pcb (version 20240108) (generator "test")',
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '  (net 0 "") (net 1 "RA") (net 2 "RB")',
        '  (gr_rect (start 0 0) (end 500 500) (stroke (width 0.1)) (layer "Edge.Cuts"))',
    ]
    declared: set[str] = set()

    def emit(seg, net_field: str) -> None:
        if net_field.startswith('(net "'):
            declared.add(net_field.split('"')[1])
        lines.append(
            f"  (segment (start {seg[0][0]:.6f} {seg[0][1]:.6f}) "
            f"(end {seg[1][0]:.6f} {seg[1][1]:.6f}) (width 0.2) "
            f'(layer "{seg[2]}") {net_field})'
        )

    net_fields = ['(net 1 "RA")', '(net 2 "RB")', '(net "NLX")']
    chains: list[tuple[float, float]] = [(5.0, 5.0), (100.0, 5.0), (200.0, 5.0), (300.0, 5.0)]
    for chain_idx, (x, y) in enumerate(chains):
        net = net_fields[chain_idx % 3]
        layer = "F.Cu" if chain_idx % 2 == 0 else "B.Cu"
        nominal = [(x, y)]
        for _ in range(rng.randint(6, 14)):
            turn = math.radians(rng.choice([0, 5, 45, 90, 135, 175, -90, -45]))
            length = rng.uniform(0.5, 3.0)
            nominal.append(
                (
                    nominal[-1][0] + length * math.cos(turn),
                    nominal[-1][1] + length * math.sin(turn),
                )
            )
        jittered = [_jittered_endpoint(rng, p) for p in nominal]
        for a, b in zip(jittered, jittered[1:], strict=False):
            emit((a, b, layer), net)
            if rng.random() < 0.1:  # stacked duplicate (art artifact)
                emit((a, b, layer), net)
            if rng.random() < 0.1:  # zero-length segment at the vertex
                emit(((b[0], b[1]), (b[0], b[1]), layer), net)
            if rng.random() < 0.25:  # T-junction: extra leg off the vertex
                ang = rng.uniform(0, 2 * math.pi)
                far = (
                    b[0] + rng.uniform(0.5, 2.0) * math.cos(ang),
                    b[1] + rng.uniform(0.5, 2.0) * math.sin(ang),
                )
                emit((_jittered_endpoint(rng, b), far, layer), net)
            if rng.random() < 0.2:  # guaranteed near miss: per-axis gap > tolerance
                emit(
                    (
                        (b[0] + rng.uniform(0.02, 0.03), b[1] + rng.uniform(-0.05, 0.05)),
                        (b[0] + rng.uniform(0.5, 1.0), b[1] + rng.uniform(0.5, 1.0)),
                        layer,
                    ),
                    net,
                )
    # The first chain duplicated onto another net and layer: same geometry,
    # separate groups.
    x, y = chains[0]
    nominal = [(x, y)]
    for _ in range(8):
        turn = math.radians(rng.choice([0, 90, -90]))
        nominal.append(
            (nominal[-1][0] + 2.0 * math.cos(turn), nominal[-1][1] + 2.0 * math.sin(turn))
        )
    jittered = [_jittered_endpoint(rng, p) for p in nominal]
    for a, b in zip(jittered, jittered[1:], strict=False):
        emit((a, b, "B.Cu"), '(net 2 "RB")')

    header_nets = ['  (net 0 "")', '  (net 1 "RA")', '  (net 2 "RB")']
    if "NLX" in declared:
        header_nets.append('  (net 3 "NLX")')
    return lines[0] + "\n" + "\n".join(header_nets) + "\n" + "\n".join(lines[1:]) + "\n)\n"


# Endpoints that share a junction yet straddle the 2-decimal rounding
# boundary (1.0149 -> 1.01, 1.0151 -> 1.02), plus a T-junction and a
# zero-length leg at the same vertex.
BOUNDARY_STRADDLING_PCB = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "STR")

  (gr_rect (start 0 0) (end 50 50) (stroke (width 0.1)) (layer "Edge.Cuts"))

  (segment (start 5 5) (end 1.0149 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 1.0151 5) (end 1.0148 9) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 1.0150 5.0001) (end 5 9) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 1.0151 4.9999) (end 1.0151 4.9999) (width 0.2) (layer "F.Cu") (net 1))
)
"""


def _corner_equivalence_cases() -> list[tuple[str, str]]:
    import random as _random

    cases = [("straddling-boundary", BOUNDARY_STRADDLING_PCB)]
    for seed in range(5):
        cases.append((f"random-seed-{seed}", _random_corner_pcb_text(_random.Random(seed))))
    return cases


@pytest.mark.parametrize(
    "name,text", _corner_equivalence_cases(), ids=[c[0] for c in _corner_equivalence_cases()]
)
def test_count_corners_matches_pairwise_reference(name, text):
    from kicad_tools.analysis.layout_quality import _count_corners

    pcb = _load_pcb(text)
    assert _count_corners(pcb) == _count_corners_pairwise_reference(pcb)


def _serpentine_pcb_text(n: int) -> str:
    pts = [(0.0, 0.0)]
    x, y = 0.0, 0.0
    for i in range(n):
        if i % 2 == 0:
            x += 1.0
        else:
            y += 0.5 if (i // 2) % 2 == 0 else -0.5
        pts.append((x, y))
    segs = "\n".join(
        f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) (layer "F.Cu") (net 1))'
        for a, b in zip(pts, pts[1:], strict=False)
    )
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "SERP")\n'
        '  (gr_rect (start 0 0) (end 500 500) (stroke (width 0.1)) (layer "Edge.Cuts"))\n'
        f"{segs}\n)\n"
    )


def test_count_corners_serpentine_4000_under_budget():
    # Issue #9658: the all-pairs junction search cost ~3 s (and ~x4 per
    # doubling) at n=4000 same-net/same-layer segments; the bucketed
    # candidate search must stay well under a second.
    import time

    from kicad_tools.analysis.layout_quality import _count_corners

    pcb = _load_pcb(_serpentine_pcb_text(4000))
    assert len(pcb.segments) == 4000
    t0 = time.perf_counter()
    totals, per_net = _count_corners(pcb)
    elapsed = time.perf_counter() - t0
    assert totals["right_angle"] == 3999  # every serpentine vertex is a 90 deg bend
    assert elapsed < 1.0, f"_count_corners took {elapsed:.3f}s at n=4000"

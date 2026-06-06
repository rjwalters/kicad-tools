"""Regression tests for the zone-priority overlap fix (issue #3043).

The outline allocator (``_compute_pour_outlines``) used to compute each
zone's bbox independently.  On real boards with spatially-interleaved
power-net pads (e.g. board 06 with ``+3V3``/``+1V8``/``+1V2`` all feeding
the same BGA region), the bboxes overlap.  KiCad's fill resolver then
awards the entire overlap region to the highest-priority zone, so the
lower-priority siblings get zero copper despite being declared in the
file.  See the issue body for the full diagnosis.

These tests instantiate the exact failure scenarios and assert that the
fix produces disjoint per-net outlines with positive area for every zone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.net_class import NetClass
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.generator import (
    _assign_layers_for_pour_nets,
    _compute_pour_outlines,
    auto_create_zones_for_pour_nets,
)

pytest.importorskip(
    "shapely",
    reason=(
        "shapely is required for the zone-priority outline allocator; "
        "install with: pip install kicad-tools[geometry]"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _polygon_area(polygon: list[tuple[float, float]]) -> float:
    """Shoelace formula for polygon area (mm²)."""
    n = len(polygon)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def _polygons_overlap_area(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
) -> float:
    """Return the area of the intersection of two polygons (mm²)."""
    from shapely.geometry import Polygon

    pa = Polygon(a)
    pb = Polygon(b)
    if not pa.is_valid or not pb.is_valid:
        return 0.0
    return pa.intersection(pb).area


# ---------------------------------------------------------------------------
# Fixtures: synthetic boards reproducing the board-06 failure shape
# ---------------------------------------------------------------------------


def _make_2net_overlap_pcb(tmp_path: Path) -> Path:
    """Build a 2-layer PCB with two power nets whose pads are interleaved.

    Both ``+5V`` and ``+3V3`` have pads spread across the same region of
    the board.  Without the fix, both nets would compute identical
    bounding boxes; with the fix, the higher-priority zone keeps its
    bbox and the lower-priority zone has it carved out.
    """
    pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+5V")
  (net 3 "+3V3")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 20 25)
    (uuid "fp-u1-uuid")
    (property "Reference" "U1"
      (at 0 -2 0) (layer "F.SilkS") (uuid "u1-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "u1-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "+5V"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "+3V3"))
  )
  (footprint "Test:U2"
    (layer "F.Cu")
    (at 50 25)
    (uuid "fp-u2-uuid")
    (property "Reference" "U2"
      (at 0 -2 0) (layer "F.SilkS") (uuid "u2-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "u2-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "+5V"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "+3V3"))
  )
  (footprint "Test:U3"
    (layer "F.Cu")
    (at 80 25)
    (uuid "fp-u3-uuid")
    (property "Reference" "U3"
      (at 0 -2 0) (layer "F.SilkS") (uuid "u3-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "u3-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "+5V"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "+3V3"))
  )
)
"""
    p = tmp_path / "two_net_overlap.kicad_pcb"
    p.write_text(pcb_text)
    return p


def _make_3net_nested_pcb(tmp_path: Path) -> Path:
    """Build a 4-layer PCB modeling the board-06 ``+3V3``/``+1V8``/``+1V2`` shape.

    ``+3V3`` pads span a wide region (10mm..90mm), ``+1V8`` pads sit in a
    narrow vertical strip inside (15mm..25mm), and ``+1V2`` pads sit in
    another narrow strip on the other side (60mm..80mm).  Without the
    fix, all three bboxes overlap (``+3V3``'s bbox encloses both the
    others), and KiCad's fill resolver awards everything to the highest
    priority.  With the fix, ``+3V3`` becomes a carved-out polygon that
    excludes the two narrow strips.
    """
    pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VBUS")
  (net 3 "+3V3")
  (net 4 "+1V8")
  (net 5 "+1V2")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
"""
    rows = []

    def fp(ref: str, x: float, y: float, net_num: int, net_name: str) -> str:
        return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} {y})
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0) (layer "F.SilkS") (uuid "{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net_num} "{net_name}"))
  )"""

    # +3V3 pads spread across the board
    rows.append(fp("U1", 10, 25, 3, "+3V3"))
    rows.append(fp("U2", 30, 25, 3, "+3V3"))
    rows.append(fp("U3", 50, 25, 3, "+3V3"))
    rows.append(fp("U4", 70, 25, 3, "+3V3"))
    rows.append(fp("U5", 90, 25, 3, "+3V3"))
    # +1V8 pads in a narrow strip inside +3V3's region
    rows.append(fp("U6", 15, 20, 4, "+1V8"))
    rows.append(fp("U7", 25, 30, 4, "+1V8"))
    # +1V2 pads in another narrow strip
    rows.append(fp("U8", 65, 20, 5, "+1V2"))
    rows.append(fp("U9", 75, 30, 5, "+1V2"))
    # VBUS sole on one inner layer (gets a single power net assignment)
    rows.append(fp("U10", 50, 10, 2, "VBUS"))
    # GND pads at every footprint position (return-path style)
    rows.append(fp("U11", 50, 40, 1, "GND"))

    pcb_text += "\n".join(rows)
    pcb_text += "\n)\n"

    p = tmp_path / "three_net_nested.kicad_pcb"
    p.write_text(pcb_text)
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestZonePriorityOverlapResolution:
    """Acceptance tests for issue #3043.

    Each test asserts the post-fix invariant: zones sharing a layer must
    have *zero area overlap* AND every zone must retain *positive area*.
    """

    def test_two_overlapping_power_nets_become_disjoint(self, tmp_path: Path):
        """The reduction of the curator's investigation plan: 2 zones, 1 layer."""
        pcb_path = _make_2net_overlap_pcb(tmp_path)

        pour_nets = [
            ("GND", NetClass.GROUND),
            ("+5V", NetClass.POWER),
            ("+3V3", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(pcb_path, pour_nets)
        assert count == 3

        pcb = PCB.load(str(pcb_path))
        f_cu_zones = [z for z in pcb.zones if z.layer == "F.Cu"]
        assert len(f_cu_zones) == 2, (
            f"expected 2 power zones on F.Cu, got {[z.net_name for z in f_cu_zones]}"
        )

        # Every F.Cu zone must have positive area.
        for z in f_cu_zones:
            area = _polygon_area(z.polygon)
            assert area > 0.0, (
                f"zone {z.net_name} on F.Cu has zero area (fix regressed)"
            )

        # The two zones must not overlap (the fix's core invariant).
        overlap = _polygons_overlap_area(
            f_cu_zones[0].polygon, f_cu_zones[1].polygon
        )
        assert overlap < 1e-6, (
            f"zones {f_cu_zones[0].net_name} and {f_cu_zones[1].net_name} "
            f"still overlap by {overlap:.3f} mm² (fix not applied)"
        )

    def test_three_nested_power_nets_each_get_copper(self, tmp_path: Path):
        """Direct reproduction of the board-06 failure shape (issue #3043)."""
        pcb_path = _make_3net_nested_pcb(tmp_path)

        pour_nets = [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+3V3", NetClass.POWER),
            ("+1V8", NetClass.POWER),
            ("+1V2", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(pcb_path, pour_nets)
        assert count == 5

        pcb = PCB.load(str(pcb_path))
        f_cu_zones = [z for z in pcb.zones if z.layer == "F.Cu"]
        assert len(f_cu_zones) == 3, (
            f"expected 3 power zones on F.Cu, got {[z.net_name for z in f_cu_zones]}"
        )

        # Acceptance criterion: every zone has *non-zero* copper area.
        # This is the fundamental "all 3 zones emit zero copper" check
        # from the issue body.
        zone_areas = {z.net_name: _polygon_area(z.polygon) for z in f_cu_zones}
        for net_name, area in zone_areas.items():
            assert area > 0.0, (
                f"zone {net_name} has zero area; issue #3043 regressed"
            )

        # And all three must be pairwise disjoint (no overlap at all).
        polys = {z.net_name: z.polygon for z in f_cu_zones}
        names = list(polys)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                overlap = _polygons_overlap_area(polys[a], polys[b])
                assert overlap < 1e-6, (
                    f"zones {a} and {b} overlap by {overlap:.3f} mm²; "
                    f"outline allocator did not partition correctly"
                )

    def test_compute_pour_outlines_returns_carved_polygon_for_lowest_priority(
        self, tmp_path: Path
    ):
        """Direct unit test: lower-priority zone gets a carved-out outline.

        With 3 power nets on F.Cu (priorities 1/2/3), the highest-priority
        zone (+1V2, priority 3) keeps its raw bbox.  The middle zone
        (+1V8, priority 2) gets +1V2 subtracted.  The lowest-priority
        zone (+3V3, priority 1) gets *both* +1V8 and +1V2 subtracted.

        The key invariant: +3V3's outline must be *disjoint* from both
        +1V8 and +1V2 (zero overlap area), and the outline must have
        *positive area* so KiCad's fill resolver awards copper.
        """
        from kicad_tools.zones import ZoneGenerator

        pcb_path = _make_3net_nested_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(pcb_path)
        assignments = _assign_layers_for_pour_nets(4, [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+3V3", NetClass.POWER),
            ("+1V8", NetClass.POWER),
            ("+1V2", NetClass.POWER),
        ])
        outlines = _compute_pour_outlines(gen.pcb, assignments, gen.board_outline)

        # GND and VBUS are sole on their inner layers.
        assert outlines["GND"] is None
        assert outlines["VBUS"] is None

        # +1V2 has the highest priority on F.Cu -- keep raw bbox (4 vertices).
        assert outlines["+1V2"] is not None
        assert len(outlines["+1V2"]) == 4

        # +1V8 has next priority.  +1V2 sits in a disjoint x-range, so
        # subtraction is a no-op and +1V8 also keeps its 4-vertex bbox.
        assert outlines["+1V8"] is not None
        assert _polygon_area(outlines["+1V8"]) > 0.0

        # +3V3 (lowest priority) must be carved.  Both +1V8 and +1V2
        # sit inside +3V3's raw bbox, so the result is the largest
        # connected piece after subtraction (the central strip between
        # the two higher-priority zones, since KiCad zones do not support
        # disjoint multi-polygon outlines).
        assert outlines["+3V3"] is not None
        assert _polygon_area(outlines["+3V3"]) > 0.0, (
            "+3V3 outline collapsed to zero area; the lower-priority "
            "zone must retain *some* copper region"
        )

        # The fix's core invariant: zero overlap with higher-priority zones.
        assert _polygons_overlap_area(outlines["+3V3"], outlines["+1V8"]) < 1e-6, (
            "+3V3 outline still overlaps +1V8 -- carve-out failed"
        )
        assert _polygons_overlap_area(outlines["+3V3"], outlines["+1V2"]) < 1e-6, (
            "+3V3 outline still overlaps +1V2 -- carve-out failed"
        )

    def test_no_spurious_overlap_warning_emitted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """``_check_overlap`` must not warn when carved polygons are truly disjoint.

        With the AABB-only overlap test, the carved +3V3 polygon would
        still appear to overlap +1V8/+1V2 (their bboxes intersect).
        After the Shapely-aware upgrade, no warning should fire.
        """
        pcb_path = _make_3net_nested_pcb(tmp_path)
        pour_nets = [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+3V3", NetClass.POWER),
            ("+1V8", NetClass.POWER),
            ("+1V2", NetClass.POWER),
        ]
        auto_create_zones_for_pour_nets(pcb_path, pour_nets)

        captured = capsys.readouterr()
        assert "WARNING" not in captured.err, (
            f"spurious overlap warning(s) on disjoint carved polygons:\n"
            f"{captured.err}"
        )


# ---------------------------------------------------------------------------
# Issue #3240: nested-in-both-axes power-net failure shape
# ---------------------------------------------------------------------------

# The fixtures above (board-06 reproduction) place the lower-priority
# zone's pad cluster in a strip that's narrower in y but wider in x than
# the higher-priority zones; the carve subtraction then leaves a
# connected central region for the lower-priority zone.  Issue #3240
# describes a different shape: the chorus-test-revA case where +5V's pads
# sit *inside* the +3.3V bbox in BOTH x and y.  When the union of
# higher-priority bboxes fully covers a lower-priority bbox, the Shapely
# difference returns empty and the legacy code returned the raw bbox --
# producing zero copper for the failing net.  The fix (pad-safe fallback
# + ZonePartitionError) is verified here.


def _make_3net_nested_in_xy_pcb(tmp_path: Path) -> Path:
    """Build a 4-layer PCB modelling the chorus-test-revA failure shape.

    Layout (issue #3240 reproduction):
    - +3.3V pads cluster in a wide region but with a non-pad gap in the
      middle (15-40 in x at y=15, plus 60-85 in x at y=35).  This mirrors
      the real chorus layout where +3.3V is a major rail but not
      uniformly distributed.
    - +5V pads cluster in the middle (45mm..55mm in x, 22mm..28mm in y)
      -- strictly inside the +3.3V raw bbox in both dimensions.
    - +3.3VA pads cluster in a different region (15mm..30mm in x,
      33mm..38mm in y) -- also fully inside +3.3V's raw bbox.
    - VBUS sits alone on one inner layer.
    - GND on its own inner layer.

    Without the fix (issue #3240), +5V and +3.3VA's raw bboxes are fully
    inside +3.3V's raw bbox.  When +3.3V wins overlap (highest priority),
    the Shapely difference of +5V/+3.3VA minus +3.3V returns empty and
    the legacy ``_subtract_polygon`` returned the original raw bbox --
    so the priority resolver awarded their full area to +3.3V, leaving
    +5V and +3.3VA with zero copper.

    With the fix:
    1. +3.3V's effective subtrahend is downgraded to its pad-safe bbox
       (tight 0.3 mm margin around the +3.3V pads only) because the
       raw bbox would over-claim space lower-priority siblings need.
    2. +3.3VA carves its raw bbox against +3.3V's pad-safe bbox --
       producing a disjoint outline with positive area.
    3. +5V does the same against the union of +3.3V's pad-safe bbox and
       +3.3VA's effective subtrahend.
    """
    pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VBUS")
  (net 3 "+3.3V")
  (net 4 "+5V")
  (net 5 "+3.3VA")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
"""

    rows = []

    def fp(ref: str, x: float, y: float, net_num: int, net_name: str) -> str:
        return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} {y})
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0) (layer "F.SilkS") (uuid "{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net_num} "{net_name}"))
  )"""

    # +3.3V pads clustered in a smallish region (not the whole board).
    # This mirrors the chorus-test-revA layout where +3.3V is dense in
    # the MCU area but doesn't span the entire PCB.  Raw bbox is
    # 18.5..51.5 x 13.5..36.5; pad-safe is 19.7..50.3 x 14.7..35.3.
    rows.append(fp("U1", 20, 15, 3, "+3.3V"))
    rows.append(fp("U2", 50, 15, 3, "+3.3V"))
    rows.append(fp("U3", 20, 35, 3, "+3.3V"))
    rows.append(fp("U4", 50, 35, 3, "+3.3V"))
    rows.append(fp("U5", 35, 25, 3, "+3.3V"))
    # +5V pads clustered NEAR the right edge of +3.3V's region, with
    # raw bbox extending OUTSIDE +3.3V's pad-safe.  +5V raw =
    # 43.5..56.5 x 20.5..29.5; the right edge (51.5..56.5) sits OUTSIDE
    # +3.3V's pad-safe (which ends at 50.3 in x).  That sliver is what
    # the carve preserves -- without the issue #3240 fix, the whole +5V
    # bbox would be returned and the priority resolver would give it
    # zero copper.
    rows.append(fp("U6", 45, 22, 4, "+5V"))
    rows.append(fp("U7", 55, 22, 4, "+5V"))
    rows.append(fp("U8", 50, 28, 4, "+5V"))
    # +3.3VA pads near the BOTTOM of +3.3V's region; +3.3VA raw bbox
    # extends BELOW +3.3V's pad-safe.  +3.3VA raw = 18.5..36.5 x
    # 31.5..39.5; the lower strip (35.3..39.5) sits OUTSIDE +3.3V's
    # pad-safe (which ends at 35.3 in y).
    rows.append(fp("U9", 20, 33, 5, "+3.3VA"))
    rows.append(fp("U10", 35, 33, 5, "+3.3VA"))
    rows.append(fp("U11", 27, 38, 5, "+3.3VA"))
    # VBUS sole on inner layer.
    rows.append(fp("U12", 80, 45, 2, "VBUS"))
    # GND pad (sole on the other inner layer).
    rows.append(fp("U13", 80, 5, 1, "GND"))

    pcb_text += "\n".join(rows)
    pcb_text += "\n)\n"

    p = tmp_path / "three_net_nested_xy.kicad_pcb"
    p.write_text(pcb_text)
    return p


def _make_3net_coincident_pads_pcb(tmp_path: Path) -> Path:
    """Build a PCB where all three power-net pads share the same location.

    Degenerate "unsolvable" case for the partition algorithm: every net's
    pad-safe bbox covers the same point, so no carve can produce disjoint
    outlines.  The fix should raise ``ZonePartitionError`` listing all
    three nets rather than silently producing zero-copper zones.
    """
    pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VBUS")
  (net 3 "+3.3V")
  (net 4 "+5V")
  (net 5 "+3.3VA")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
"""

    rows = []

    def fp(ref: str, x: float, y: float, net_num: int, net_name: str) -> str:
        return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} {y})
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0) (layer "F.SilkS") (uuid "{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net_num} "{net_name}"))
  )"""

    # All three power-net pads at the SAME location (50, 25).  Each net's
    # single-pad-fallback bbox is the 4x4 mm square centered at (50, 25)
    # -- identical for all three.  No partition exists.
    rows.append(fp("U1", 50, 25, 3, "+3.3V"))
    rows.append(fp("U2", 50, 25, 4, "+5V"))
    rows.append(fp("U3", 50, 25, 5, "+3.3VA"))
    # Aux nets so the assignment path goes through the multi-power branch.
    rows.append(fp("U4", 50, 45, 2, "VBUS"))
    rows.append(fp("U5", 50, 5, 1, "GND"))

    pcb_text += "\n".join(rows)
    pcb_text += "\n)\n"

    p = tmp_path / "three_net_coincident.kicad_pcb"
    p.write_text(pcb_text)
    return p


class TestZonePartitionErrorAndPadSafeFallback:
    """Regression tests for issue #3240.

    Issue #3240 reported that on chorus-test-revA, three overlapping power
    zones (``+5V``, ``+3.3VA``, ``+3.3V``) on F.Cu produced "zone will get
    zero copper" warnings -- the lower-priority +3.3V net was fully covered
    by the union of +5V and +3.3VA, the Shapely difference returned empty,
    and the legacy ``_subtract_polygon`` silently fell back to the raw
    +3.3V bbox.

    The fix:
    1. ``_subtract_polygon`` returns ``None`` instead of the raw fallback
       on empty/degenerate diff results.
    2. ``_compute_pour_outlines`` catches the ``None`` and tries a
       pad-safe fallback (tight 0.3 mm bbox around just the net's pads).
    3. When even the pad-safe fallback overlaps the winners union with
       no positive non-overlap area, ``ZonePartitionError`` is raised
       with an actionable message naming the failing net and competing
       nets.
    """

    def test_three_power_nets_nested_in_xy_get_carved_to_pad_safe(
        self, tmp_path: Path
    ):
        """The chorus-test-revA failure shape: +3.3V fully covered by +5V/+3.3VA bboxes.

        With +5V and +3.3VA both having higher priority than +3.3V, the
        union of their inflated (1.5 mm) bboxes might fully cover the
        +3.3V raw bbox region in the contested area.  The fix's pad-safe
        fallback ensures +3.3V still receives copper around its actual
        pads, even when the carve subtraction returns empty.
        """
        from kicad_tools.zones import ZoneGenerator
        from kicad_tools.zones.generator import _compute_pour_outlines

        pcb_path = _make_3net_nested_in_xy_pcb(tmp_path)

        # Order matters: +3.3V is listed LAST so it gets the lowest priority
        # on F.Cu.  In the chorus case, +5V was priority 1 (lowest) and
        # +3.3V was priority 3 (highest), but the failure shape is
        # symmetric -- whichever net gets the lowest priority will have
        # its bbox carved away by the higher-priority ones.
        pour_nets = [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3VA", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(pcb_path, pour_nets)
        assert count == 5

        pcb = PCB.load(str(pcb_path))
        f_cu_zones = [z for z in pcb.zones if z.layer == "F.Cu"]
        assert len(f_cu_zones) == 3, (
            f"expected 3 power zones on F.Cu, got "
            f"{[z.net_name for z in f_cu_zones]}"
        )

        # CORE ASSERTION (issue #3240 acceptance criterion 1, Option A):
        # every zone has *non-zero* copper area.  Before the fix, the
        # lowest-priority zone had its raw bbox returned (overlapping the
        # winners), so KiCad's fill resolver would award it zero copper.
        zone_areas = {z.net_name: _polygon_area(z.polygon) for z in f_cu_zones}
        for net_name, area in zone_areas.items():
            assert area > 0.0, (
                f"zone {net_name} has zero area; issue #3240 regressed"
            )

        # Disjointness: no two power zones may overlap (otherwise the
        # priority resolver would still silently win one and zero the other).
        polys = {z.net_name: z.polygon for z in f_cu_zones}
        names = list(polys)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                overlap = _polygons_overlap_area(polys[a], polys[b])
                assert overlap < 1e-6, (
                    f"zones {a} and {b} overlap by {overlap:.3f} mm²; "
                    f"#3240 fix should have produced disjoint outlines"
                )

        # Direct unit-level check: the unit-level outline allocator must
        # produce a non-None outline for the lowest-priority net.
        gen = ZoneGenerator.from_pcb(pcb_path)
        from kicad_tools.zones.generator import _assign_layers_for_pour_nets

        assignments = _assign_layers_for_pour_nets(4, pour_nets)
        outlines = _compute_pour_outlines(gen.pcb, assignments, gen.board_outline)
        # Find the lowest-priority F.Cu net by inspecting assignments.
        f_cu_assignments = [
            (n, p) for n, layer, p in assignments if layer == "F.Cu"
        ]
        if f_cu_assignments:
            lowest = min(f_cu_assignments, key=lambda np: np[1])[0]
            assert outlines.get(lowest) is not None, (
                f"lowest-priority net {lowest} got None outline; "
                f"pad-safe fallback should have returned a polygon"
            )

    def test_no_zero_copper_warning_for_xy_nested_layout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """Issue #3240 acceptance criterion 2: no zero-copper warnings.

        After the fix, the literal warning text "zero copper because the
        other zone has equal or higher priority" must not appear in
        stderr for the xy-nested layout.
        """
        pcb_path = _make_3net_nested_in_xy_pcb(tmp_path)
        pour_nets = [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3VA", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
        ]
        auto_create_zones_for_pour_nets(pcb_path, pour_nets)

        captured = capsys.readouterr()
        assert "zero copper" not in captured.err.lower(), (
            f"#3240 fix did not eliminate the zero-copper warning:\n"
            f"{captured.err}"
        )

    def test_zone_partition_error_on_coincident_pad_layout(self, tmp_path: Path):
        """Issue #3240 acceptance criterion 4 (Option B fallback).

        When the failure is truly unsolvable -- three nets whose pads all
        share the exact same location -- the fix raises
        ``ZonePartitionError`` rather than silently producing zero-copper
        zones.  The error message must name the failing net AND list the
        higher-priority nets that fully cover it.
        """
        from kicad_tools.zones import ZonePartitionError

        pcb_path = _make_3net_coincident_pads_pcb(tmp_path)
        pour_nets = [
            ("GND", NetClass.GROUND),
            ("VBUS", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3VA", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
        ]
        with pytest.raises(ZonePartitionError) as exc_info:
            auto_create_zones_for_pour_nets(pcb_path, pour_nets)

        err = exc_info.value
        # The error must name a failing net and at least one covering net.
        assert err.failing_net in {"+5V", "+3.3VA", "+3.3V"}, (
            f"failing net should be one of the power nets, got {err.failing_net}"
        )
        assert err.layer == "F.Cu", (
            f"layer should be F.Cu (the contested layer), got {err.layer}"
        )
        assert len(err.covering_nets) >= 1, (
            f"covering_nets should list at least one higher-priority net, "
            f"got {err.covering_nets}"
        )
        # The error message must be actionable.
        msg = str(err)
        assert "zero copper" in msg.lower() or "would receive" in msg.lower(), (
            f"error message must mention zero copper / would receive: {msg}"
        )

    def test_subtract_polygon_returns_none_on_empty_diff(self):
        """Unit test: ``_subtract_polygon`` returns ``None`` (not fallback) when fully covered.

        This is the API-level guarantee that backs the
        ``ZonePartitionError`` plumbing -- ``_compute_pour_outlines``
        relies on this signal to decide between pad-safe fallback and
        partition error.
        """
        from kicad_tools.zones.generator import _subtract_polygon

        # Minuend is a 10x10 square at origin; subtrahend is a 20x20
        # square that fully covers it.
        minuend = [(0, 0), (10, 0), (10, 10), (0, 10)]
        subtrahend = [(-5, -5), (15, -5), (15, 15), (-5, 15)]

        result = _subtract_polygon(minuend, subtrahend)
        assert result is None, (
            f"_subtract_polygon must return None on empty diff "
            f"(legacy fallback path is the #3240 bug); got {result!r}"
        )

    def test_subtract_polygon_returns_carved_polygon_on_partial_diff(self):
        """Unit test: ``_subtract_polygon`` returns the carved polygon when partial."""
        from kicad_tools.zones.generator import _subtract_polygon

        # Subtrahend covers only the right half of the minuend.
        # Left half of 10x10 = 5 mm wide × 10 mm tall = 50 mm².
        minuend = [(0, 0), (10, 0), (10, 10), (0, 10)]
        subtrahend = [(5, -1), (11, -1), (11, 11), (5, 11)]

        result = _subtract_polygon(minuend, subtrahend)
        assert result is not None, (
            "_subtract_polygon must return a carved polygon when "
            "diff is non-empty"
        )
        # The result must have positive area strictly smaller than the
        # original 100 mm² minuend.
        from shapely.geometry import Polygon

        result_area = Polygon(result).area
        assert 0 < result_area < 100.0, (
            f"carved polygon should be smaller than the full 10x10 "
            f"(100 mm²); got {result_area}"
        )
        # The carved region must be disjoint from the subtrahend.
        overlap = Polygon(result).intersection(Polygon(subtrahend)).area
        assert overlap < 1e-6, (
            f"carved polygon still overlaps the subtrahend by {overlap}"
        )

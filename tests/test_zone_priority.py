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

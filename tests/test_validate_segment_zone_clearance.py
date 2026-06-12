"""Tests for the segment-vs-foreign-zone-fill DRC rule (Issue #3527).

A trace routed through another net's committed ``filled_polygon`` copper
is a hard manufacturing short, but before ``SegmentZoneClearanceRule``
no rule in ``kct check`` compared segments to zone fill geometry (PR
#3526's judge found a PWR_LED trace crossing ~3.1 mm of +3V3 fill on
board 05 with zero blocking violations reported).

Covers the four acceptance scenarios from the issue:

1. Synthetic short (trace centerline inside a foreign fill)
2. Clearance graze (positive gap below the manufacturer minimum)
3. Same-net pass-through (legal -- a trace inside its own pour)
4. Knocked-out fill (legal -- the fill has a corridor with adequate gap)

plus schema-level tests for the new per-fill layer tracking on
:class:`~kicad_tools.schema.pcb.Zone`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from kicad_tools.schema.pcb import Zone
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.clearance import SegmentZoneClearanceRule

# ---------------------------------------------------------------------------
# Minimal stubs mirroring the fields the rule reads
# ---------------------------------------------------------------------------


@dataclass
class _FakeNet:
    number: int
    name: str


@dataclass
class _FakeSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "SIG"
    uuid: str = "deadbeef-0000"


@dataclass
class _FakeZone:
    net_number: int = 2
    net_name: str = "+3V3"
    layer: str = "F.Cu"
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    filled_polygon_layers: list[str] = field(default_factory=list)

    def filled_polygon_layer(self, index: int) -> str:
        if index < len(self.filled_polygon_layers) and self.filled_polygon_layers[index]:
            return self.filled_polygon_layers[index]
        return self.layer


def _make_pcb(zones, segments, nets):
    pcb = MagicMock()
    pcb.zones = zones
    pcb.nets = {n.number: n for n in nets}
    pcb.segments_on_layer = lambda layer: iter([s for s in segments if s.layer == layer])
    return pcb


def _make_design_rules(min_clearance: float = 0.127):
    rules = MagicMock()
    rules.min_clearance_mm = min_clearance
    return rules


# A 10x10 mm square fill on net 2 (+3V3) at (100,100)..(110,110)
_SQUARE_FILL = [(100.0, 100.0), (110.0, 100.0), (110.0, 110.0), (100.0, 110.0)]

_NETS = [_FakeNet(1, "SIG"), _FakeNet(2, "+3V3")]


def _run(zones, segments, min_clearance=0.127):
    pcb = _make_pcb(zones, segments, _NETS)
    rule = SegmentZoneClearanceRule()
    return rule.check(pcb, _make_design_rules(min_clearance))


# ---------------------------------------------------------------------------
# Scenario 1: hard short -- trace centerline inside a foreign fill
# ---------------------------------------------------------------------------


class TestShortDetection:
    def test_trace_through_foreign_fill_is_short(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0))
        results = _run([zone], [seg])

        shorts = [v for v in results.violations if v.rule_id == "clearance_segment_zone"]
        assert len(shorts) == 1
        v = shorts[0]
        assert v.severity == "error"
        assert "Short" in v.message
        # Negative actual value = overlap depth (centerline runs through
        # the middle of the fill, so depth is substantial).
        assert v.actual_value is not None and v.actual_value < 0
        assert v.nets == ("SIG", "+3V3")
        assert v.layer == "F.Cu"

    def test_trace_fully_inside_foreign_fill_is_short(self):
        """Board 05 regression shape: segment entirely within the fill."""
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(102.0, 105.0), end=(108.0, 105.0))
        results = _run([zone], [seg])

        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" in v.message
        # Depth >= distance to nearest edge (2mm) -- definitely < -1.
        assert v.actual_value < -1.0

    def test_copper_edge_overlap_without_centerline_inside_is_short(self):
        """Centerline outside the fill but trace width overlaps the edge."""
        # Centerline 0.05mm left of the fill edge; half-width 0.1mm.
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(99.95, 102.0), end=(99.95, 108.0), width=0.2)
        results = _run([zone], [seg])

        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" in v.message
        assert v.actual_value < 0


# ---------------------------------------------------------------------------
# Scenario 2: clearance graze (gap > 0 but < minimum)
# ---------------------------------------------------------------------------


class TestClearanceGraze:
    def test_sub_minimum_gap_is_flagged(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Centerline 0.15mm left of the fill edge; half-width 0.1mm
        # -> copper gap 0.05mm < 0.127mm minimum.
        seg = _FakeSegment(start=(99.85, 102.0), end=(99.85, 108.0), width=0.2)
        results = _run([zone], [seg])

        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.severity == "error"
        assert "Short" not in v.message
        assert abs(v.actual_value - 0.05) < 1e-6
        assert v.required_value == 0.127

    def test_adequate_gap_passes(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        # Centerline 0.30mm left of the fill edge -> copper gap 0.20mm.
        seg = _FakeSegment(start=(99.70, 102.0), end=(99.70, 108.0), width=0.2)
        results = _run([zone], [seg])
        assert results.violations == []


# ---------------------------------------------------------------------------
# Scenario 3: same-net pass-through is legal
# ---------------------------------------------------------------------------


class TestSameNet:
    def test_trace_through_own_pour_passes(self):
        zone = _FakeZone(net_number=1, net_name="SIG", filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0))
        results = _run([zone], [seg])
        assert results.violations == []

    def test_net0_segment_skipped(self):
        zone = _FakeZone(filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), net_number=0)
        results = _run([zone], [seg])
        assert results.violations == []

    def test_net0_unnamed_zone_skipped(self):
        zone = _FakeZone(net_number=0, net_name="", filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0))
        results = _run([zone], [seg])
        assert results.violations == []

    def test_name_only_zone_net_resolved(self):
        """KiCad 9 name-only zone net resolves through the net table."""
        zone = _FakeZone(net_number=0, net_name="SIG", filled_polygons=[_SQUARE_FILL])
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0))
        # Same net after resolution -> legal.
        results = _run([zone], [seg])
        assert results.violations == []


# ---------------------------------------------------------------------------
# Scenario 4: knocked-out fill (corridor with adequate gap) is legal
# ---------------------------------------------------------------------------


class TestKnockedOutFill:
    def test_fill_with_corridor_around_trace_passes(self):
        """A properly refilled zone leaves a knockout around the trace.

        The fill outline traces a 0.6mm-wide corridor around a vertical
        trace at x=105 (0.2mm wide trace + 0.2mm gap each side), the way
        KiCad's filler knocks out foreign copper through the exterior
        ring.
        """
        corridor_fill = [
            (100.0, 100.0),
            (110.0, 100.0),
            (110.0, 110.0),
            (105.3, 110.0),
            (105.3, 100.5),  # corridor right wall (trace edge + 0.2 gap)
            (104.7, 100.5),  # corridor left wall
            (104.7, 110.0),
            (100.0, 110.0),
        ]
        zone = _FakeZone(filled_polygons=[corridor_fill])
        # Vertical trace down the corridor, 0.2mm wide -> copper edges at
        # x = 104.9 / 105.1; gap to fill walls = 0.2mm >= 0.127mm.
        seg = _FakeSegment(start=(105.0, 101.0), end=(105.0, 109.0), width=0.2)
        results = _run([zone], [seg])
        assert results.violations == []

    def test_fill_with_too_tight_corridor_flagged(self):
        """Same corridor shape but only 0.05mm of gap -> violation."""
        corridor_fill = [
            (100.0, 100.0),
            (110.0, 100.0),
            (110.0, 110.0),
            (105.15, 110.0),
            (105.15, 100.5),
            (104.85, 100.5),
            (104.85, 110.0),
            (100.0, 110.0),
        ]
        zone = _FakeZone(filled_polygons=[corridor_fill])
        seg = _FakeSegment(start=(105.0, 101.0), end=(105.0, 109.0), width=0.2)
        results = _run([zone], [seg])
        assert len(results.violations) == 1
        v = results.violations[0]
        assert "Short" not in v.message
        assert 0 < v.actual_value < 0.127


# ---------------------------------------------------------------------------
# Layer discipline
# ---------------------------------------------------------------------------


class TestLayers:
    def test_fill_on_other_layer_ignored(self):
        zone = _FakeZone(
            filled_polygons=[_SQUARE_FILL],
            filled_polygon_layers=["B.Cu"],
        )
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), layer="F.Cu")
        results = _run([zone], [seg])
        assert results.violations == []

    def test_multilayer_zone_uses_per_fill_layer(self):
        """Multi-layer zone: only the B.Cu fill collides with a B.Cu trace."""
        zone = _FakeZone(
            layer="",  # multi-layer zones have no single zone-level layer
            filled_polygons=[_SQUARE_FILL, _SQUARE_FILL],
            filled_polygon_layers=["F.Cu", "B.Cu"],
        )
        seg = _FakeSegment(start=(95.0, 105.0), end=(115.0, 105.0), layer="B.Cu")
        results = _run([zone], [seg])
        assert len(results.violations) == 1
        assert results.violations[0].layer == "B.Cu"


# ---------------------------------------------------------------------------
# Zone schema: per-fill layer parsing (Issue #3527 schema change)
# ---------------------------------------------------------------------------


_ZONE_SEXP = """
(zone
  (net 2)
  (net_name "+3V3")
  (layer "F.Cu")
  (uuid "11111111-2222-3333-4444-555555555555")
  (fill yes)
  (polygon (pts (xy 100 100) (xy 110 100) (xy 110 110) (xy 100 110)))
  (filled_polygon
    (layer "F.Cu")
    (pts (xy 100 100) (xy 110 100) (xy 110 110) (xy 100 110)))
  (filled_polygon
    (layer "B.Cu")
    (pts (xy 100 100) (xy 110 100) (xy 110 110) (xy 100 110)))
)
"""


class TestZoneSchemaFillLayers:
    def test_filled_polygon_layers_parsed(self):
        zone = Zone.from_sexp(parse_string(_ZONE_SEXP))
        assert len(zone.filled_polygons) == 2
        assert zone.filled_polygon_layers == ["F.Cu", "B.Cu"]
        assert zone.filled_polygon_layer(0) == "F.Cu"
        assert zone.filled_polygon_layer(1) == "B.Cu"

    def test_fallback_to_zone_layer_for_programmatic_zones(self):
        zone = Zone(net_number=1, net_name="GND", layer="In1.Cu")
        zone.filled_polygons.append(_SQUARE_FILL)
        # No filled_polygon_layers entry -> falls back to zone.layer
        assert zone.filled_polygon_layer(0) == "In1.Cu"

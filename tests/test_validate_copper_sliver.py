"""Tests for the copper-sliver DRC rule (Issue #3843).

``CopperSliverRule`` detects thin filaments of copper narrower than the
manufacturer's minimum reproducible copper width via a per-layer
morphological open (``buffer(-r).buffer(r)`` with
``r = min_trace_width_mm / 2``); the residual ``original - opened`` is the
sliver set.  ``kicad-cli pcb drc`` flags ``copper_sliver`` warnings that
``kct check`` previously missed because no rule inspected the *internal
width* of a single copper region.

Covers the acceptance scenarios from the issue:

- Synthetic thin sliver (two blobs joined by a sub-min-width bridge) ->
  flagged.
- Normal solid pour -> not flagged.
- Normal (>= min-width) isthmus -> not flagged.
- Threshold boundary (neck at ~min_width passes, clearly below fails).
- Empty PCB / layer with no copper / ``min_trace_width_mm == 0`` -> no
  violations, no exceptions.
- Violation metadata (rule_id / severity / layer / location).
- ``ViolationType.from_string("copper_sliver")`` resolves to
  ``COPPER_SLIVER`` with category ``MANUFACTURING``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from kicad_tools.drc.violation import ViolationCategory, ViolationType
from kicad_tools.validate.rules.copper_sliver import CopperSliverRule

# ---------------------------------------------------------------------------
# Minimal stubs mirroring the fields the rule reads
# ---------------------------------------------------------------------------


@dataclass
class _FakeNet:
    number: int
    name: str


@dataclass
class _FakeLayer:
    name: str


@dataclass
class _FakeZone:
    net_number: int = 1
    net_name: str = "GND"
    layer: str = "F.Cu"
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    filled_polygon_layers: list[str] = field(default_factory=list)

    def filled_polygon_layer(self, index: int) -> str:
        if index < len(self.filled_polygon_layers) and self.filled_polygon_layers[index]:
            return self.filled_polygon_layers[index]
        return self.layer


def _make_pcb(
    zones=None,
    segments=None,
    footprints=None,
    vias=None,
    nets=None,
    copper_layers=("F.Cu", "B.Cu"),
):
    zones = zones or []
    segments = segments or []
    footprints = footprints or []
    vias = vias or []
    nets = nets or [_FakeNet(1, "GND")]
    pcb = MagicMock()
    pcb.zones = zones
    pcb.nets = {n.number: n for n in nets}
    pcb.footprints = footprints
    pcb.vias = vias
    pcb.copper_layers = [_FakeLayer(name) for name in copper_layers]
    pcb.segments_on_layer = lambda layer: iter([s for s in segments if s.layer == layer])
    return pcb


def _make_design_rules(min_trace_width: float = 0.127):
    rules = MagicMock()
    rules.min_trace_width_mm = min_trace_width
    return rules


def _run(min_trace_width=0.127, **pcb_kwargs):
    pcb = _make_pcb(**pcb_kwargs)
    return CopperSliverRule().check(pcb, _make_design_rules(min_trace_width))


# ---------------------------------------------------------------------------
# Geometry helpers: build fill polygons as a single zone on F.Cu
# ---------------------------------------------------------------------------


def _solid_square(x0=10.0, y0=10.0, size=10.0):
    """A plain solid square pour, no thin features."""
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)]


def _dumbbell(bridge_width: float):
    """Two square blobs joined by a horizontal bridge of ``bridge_width``.

    The bridge is the only thin feature.  Made narrow -> sliver; made
    wide -> clean isthmus.
    """
    half = bridge_width / 2.0
    cy = 15.0
    # Left blob: x in [5,10], y in [10,20].  Right blob: x in [20,25].
    # Bridge: x in [10,20], y in [cy-half, cy+half].
    return [
        (5.0, 10.0),
        (10.0, 10.0),
        (10.0, cy - half),
        (20.0, cy - half),
        (20.0, 10.0),
        (25.0, 10.0),
        (25.0, 20.0),
        (20.0, 20.0),
        (20.0, cy + half),
        (10.0, cy + half),
        (10.0, 20.0),
        (5.0, 20.0),
    ]


def _zone_with_fill(points, layer="F.Cu"):
    return _FakeZone(net_number=1, net_name="GND", layer=layer, filled_polygons=[points])


# ---------------------------------------------------------------------------
# Scenario: synthetic thin sliver is flagged
# ---------------------------------------------------------------------------


class TestSliverDetection:
    def test_thin_bridge_is_flagged(self):
        # bridge 0.05 mm wide, min width 0.127 mm -> sliver
        zone = _zone_with_fill(_dumbbell(bridge_width=0.05))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) >= 1, "thin bridge should be flagged as a sliver"

    def test_thin_bridge_flagged_exactly_once(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.05))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        # A single neck yields a single residual region.
        assert len(slivers) == 1

    def test_violation_metadata(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.05))
        results = _run(min_trace_width=0.127, zones=[zone])
        v = next(v for v in results.violations if v.rule_id == "copper_sliver")
        assert v.severity == "warning"
        assert v.layer == "F.Cu"
        assert v.location is not None
        x, y = v.location
        # Bridge centroid sits near (15, 15).
        assert 10.0 <= x <= 20.0
        assert 14.0 <= y <= 16.0
        assert v.required_value == pytest.approx(0.127)


# ---------------------------------------------------------------------------
# Scenario: normal pours are NOT flagged
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    def test_solid_pour_passes(self):
        zone = _zone_with_fill(_solid_square())
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []

    def test_wide_isthmus_passes(self):
        # bridge 0.5 mm wide, well above the 0.127 mm threshold.
        zone = _zone_with_fill(_dumbbell(bridge_width=0.5))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []

    def test_single_track_passes(self):
        # A lone track of exactly min width is not a sliver.
        seg = MagicMock()
        seg.start = (0.0, 0.0)
        seg.end = (10.0, 0.0)
        seg.width = 0.127
        seg.layer = "F.Cu"
        seg.net_number = 1
        results = _run(min_trace_width=0.127, segments=[seg])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []


# ---------------------------------------------------------------------------
# Scenario: threshold boundary
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    def test_neck_at_min_width_passes(self):
        # bridge exactly at min width -> open preserves it -> no sliver.
        zone = _zone_with_fill(_dumbbell(bridge_width=0.127))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []

    def test_neck_clearly_below_min_width_fails(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.04))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) == 1


# ---------------------------------------------------------------------------
# Scenario: empty / edge cases return zero violations without raising
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_pcb(self):
        results = _run(min_trace_width=0.127)
        assert results.violations == []

    def test_layer_with_no_copper(self):
        # Zone only on F.Cu; B.Cu has nothing.
        zone = _zone_with_fill(_solid_square(), layer="F.Cu")
        results = _run(min_trace_width=0.127, zones=[zone])
        assert [v for v in results.violations if v.layer == "B.Cu"] == []

    def test_zero_min_trace_width_short_circuits(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.05))
        results = _run(min_trace_width=0.0, zones=[zone])
        assert results.violations == []

    def test_negative_min_trace_width_short_circuits(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.05))
        results = _run(min_trace_width=-1.0, zones=[zone])
        assert results.violations == []

    def test_rules_checked_counter(self):
        zone = _zone_with_fill(_solid_square())
        results = _run(min_trace_width=0.127, zones=[zone])
        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("copper_sliver") == 1


# ---------------------------------------------------------------------------
# Scenario: violation-type wiring
# ---------------------------------------------------------------------------


class TestViolationTypeWiring:
    def test_from_string_resolves(self):
        assert ViolationType.from_string("copper_sliver") is ViolationType.COPPER_SLIVER

    def test_from_string_not_unknown(self):
        assert ViolationType.from_string("copper_sliver") is not ViolationType.UNKNOWN

    def test_category_is_manufacturing(self):
        from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP

        assert _TYPE_CATEGORY_MAP[ViolationType.COPPER_SLIVER] is ViolationCategory.MANUFACTURING

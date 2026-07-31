"""Tests for the copper-sliver DRC rule (Issue #3843).

``CopperSliverRule`` detects acute copper tips using KiCad-compatible
angle-and-width semantics.  ``kicad-cli pcb drc`` flags ``copper_sliver`` warnings that
``kct check`` previously missed because no rule inspected the *internal
width* of a single copper region.

Covers the acceptance scenarios from the issue:

- Synthetic acute copper tip -> flagged.
- Long narrow ribbon -> not flagged.
- Normal solid pour -> not flagged.
- Normal (>= min-width) isthmus -> not flagged.
- Native threshold boundary (0.07 mm passes, 0.10 mm is flagged independently
  of the manufacturer's 0.127 mm trace-width floor).
- KiCad's six-vertex guard and its 0.0008 mm tiny-vertex *traversal*
  (component-wise, walk past -- never drop the candidate tip).
- Empty PCB / layer with no copper -> no violations, no exceptions.
- Violation metadata (rule_id / severity / layer / location).
- ``ViolationType.from_string("copper_sliver")`` resolves to
  ``COPPER_SLIVER`` with category ``MANUFACTURING``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_tools.drc.violation import ViolationCategory, ViolationType
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.copper_sliver import (
    KICAD_SLIVER_MINIMUM_LENGTH_MM,
    KICAD_SLIVER_WIDTH_TOLERANCE_MM,
    CopperSliverRule,
)

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


def _acute_tip(base_width: float = 0.2):
    """A rectangle with one long, acute copper tip on its left edge."""
    half = base_width / 2.0
    return [
        (20.0, 10.0),
        (30.0, 10.0),
        (30.0, 20.0),
        (20.0, 20.0),
        (20.0, 15.0 + half),
        (10.0, 15.0),
        (20.0, 15.0 - half),
    ]


def _acute_tip_with_micro_vertices(*offsets: tuple[float, float], base_width: float = 0.2):
    """``_acute_tip`` with numerical micro-vertices next to the acute tip.

    Each ``offsets`` entry is a ``(dx, dy)`` displacement from the tip at
    ``(10, 15)``; the vertices are inserted between the tip and the lower
    arm, i.e. directly adjacent to the tip, exactly like the kink that
    reproduced the native/internal false negative.  The offsets are chosen
    to deviate from the tip-to-arm chord by more than the rule's
    collinear-simplify tolerance so the vertices genuinely survive into
    ``_iter_sliver_tips``.
    """
    points = _acute_tip(base_width)
    tip_index = points.index((10.0, 15.0))
    kinks = [(10.0 + dx, 15.0 + dy) for dx, dy in offsets]
    return [*points[: tip_index + 1], *kinks, *points[tip_index + 1 :]]


def _zone_with_fill(points, layer="F.Cu"):
    return _FakeZone(net_number=1, net_name="GND", layer=layer, filled_polygons=[points])


# ---------------------------------------------------------------------------
# Scenario: synthetic thin sliver is flagged
# ---------------------------------------------------------------------------


class TestSliverDetection:
    def test_acute_tip_is_flagged(self):
        zone = _zone_with_fill(_acute_tip())
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) >= 1, "acute copper tip should be flagged as a sliver"

    def test_acute_tip_flagged_exactly_once(self):
        zone = _zone_with_fill(_acute_tip())
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        # A single acute tip yields one warning.
        assert len(slivers) == 1

    def test_violation_metadata(self):
        zone = _zone_with_fill(_acute_tip())
        results = _run(min_trace_width=0.127, zones=[zone])
        v = next(v for v in results.violations if v.rule_id == "copper_sliver")
        assert v.severity == "warning"
        assert v.layer == "F.Cu"
        assert v.location is not None
        x, y = v.location
        assert x == pytest.approx(10.0)
        assert y == pytest.approx(15.0)
        assert v.required_value == pytest.approx(KICAD_SLIVER_WIDTH_TOLERANCE_MM)


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

    def test_long_narrow_ribbon_passes(self):
        zone = _zone_with_fill(_dumbbell(bridge_width=0.04))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []


# ---------------------------------------------------------------------------
# Scenario: threshold boundary
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    def test_tip_below_native_chord_tolerance_passes(self):
        zone = _zone_with_fill(_acute_tip(base_width=0.07))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert slivers == []

    def test_tip_between_native_and_manufacturer_thresholds_fails(self):
        # KiCad's 0.08 mm sliver tolerance is independent of the 0.127 mm
        # manufacturer trace-width floor.
        zone = _zone_with_fill(_acute_tip(base_width=0.10))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) == 1

    def test_outline_with_at_most_five_vertices_is_ignored(self, monkeypatch):
        monkeypatch.setattr(CopperSliverRule, "_chord_is_locally_inside", lambda *_: True)
        coords = [(1.0, 0.1), (0.0, 0.0), (1.0, -0.1), (2.0, -1.0), (2.0, 1.0)]
        component = SimpleNamespace(
            exterior=SimpleNamespace(coords=[*coords, coords[0]]),
            interiors=[],
        )

        assert list(CopperSliverRule._iter_sliver_tips(component, 0.08)) == []


# ---------------------------------------------------------------------------
# Scenario: KiCad's tiny-vertex traversal (native minimum-length semantics)
#
# Native KiCad does not *drop* a candidate tip whose immediate neighbour is
# closer than 0.0008 mm -- it walks past such micro-vertices (component-wise
# smallness test) until it finds usable arms.  These tests exercise the real
# traversal on real geometry; nothing is monkeypatched away.
# ---------------------------------------------------------------------------


class TestMicroVertexTraversal:
    def test_micro_vertex_adjacent_to_tip_does_not_hide_sliver(self):
        """A 0.0005 mm kink beside the tip must not mask the sliver."""
        zone = _zone_with_fill(_acute_tip_with_micro_vertices((0.0005, -0.0003)))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) == 1

    def test_smallness_predicate_is_component_wise_not_euclidean(self):
        """``abs(dx) < min and abs(dy) < min`` -- hypot would exceed the floor.

        The kink sits 0.00099 mm from the tip by Euclidean distance (above
        the 0.0008 mm floor) but is component-wise tiny in both axes, so
        native KiCad still traverses past it.  A ``hypot`` predicate would
        accept it as a usable arm and lose the sliver.
        """
        offset = (0.0007, -0.0007)
        assert math.hypot(*offset) > KICAD_SLIVER_MINIMUM_LENGTH_MM
        assert max(abs(offset[0]), abs(offset[1])) < KICAD_SLIVER_MINIMUM_LENGTH_MM

        zone = _zone_with_fill(_acute_tip_with_micro_vertices(offset))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) == 1

    def test_micro_vertex_chain_yields_one_marker_at_the_tip(self):
        """A run of micro-vertices is one acute corner, not one per vertex."""
        zone = _zone_with_fill(_acute_tip_with_micro_vertices((0.0003, -0.0003), (0.0006, -0.0006)))
        results = _run(min_trace_width=0.127, zones=[zone])
        slivers = [v for v in results.violations if v.rule_id == "copper_sliver"]
        assert len(slivers) == 1
        x, y = slivers[0].location
        assert (x, y) == pytest.approx((10.0, 15.0), abs=1e-3)

    def test_traversal_resolves_arms_past_tiny_neighbours(self):
        """Unit-level: the resolved arms skip the micro-vertex entirely."""
        coords = [
            (20.0, 10.0),
            (10.0, 15.0),
            (10.0005, 14.9997),
            (20.0, 20.0),
        ]
        assert CopperSliverRule._resolve_arm_index(coords, 1, 1) == 3
        assert CopperSliverRule._resolve_arm_index(coords, 1, -1) == 0
        # From the micro-vertex the traversal walks back over the tip too.
        assert CopperSliverRule._resolve_arm_index(coords, 2, -1) == 0

    def test_degenerate_ring_inside_minimum_length_yields_no_tips(self):
        """A speck smaller than the minimum length everywhere is not a tip."""
        coords = [
            (0.0, 0.0),
            (0.0002, 0.0),
            (0.0004, 0.0001),
            (0.0004, 0.0003),
            (0.0002, 0.0004),
            (0.0, 0.0002),
        ]
        component = SimpleNamespace(
            exterior=SimpleNamespace(coords=[*coords, coords[0]]),
            interiors=[],
        )

        assert CopperSliverRule._resolve_arm_index(coords, 0, 1) is None
        assert list(CopperSliverRule._iter_sliver_tips(component, 0.08)) == []


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

    def test_zero_manufacturer_trace_width_does_not_disable_native_sliver_check(self):
        zone = _zone_with_fill(_acute_tip(base_width=0.10))
        results = _run(min_trace_width=0.0, zones=[zone])
        assert sum(v.rule_id == "copper_sliver" for v in results.violations) == 1

    def test_negative_manufacturer_trace_width_does_not_change_native_threshold(self):
        zone = _zone_with_fill(_acute_tip(base_width=0.10))
        results = _run(min_trace_width=-1.0, zones=[zone])
        assert sum(v.rule_id == "copper_sliver" for v in results.violations) == 1

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


POSITIVE_FIXTURE = Path(__file__).parent / "fixtures" / "copper_sliver_positive.kicad_pcb"
REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.integration
def test_positive_control_matches_native_copper_sliver_count():
    """Every warning in the native positive control remains detected internally."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.drc.geometric import run_geometric_drc

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    native = run_geometric_drc(POSITIVE_FIXTURE, kicad_cli=cli)
    internal = CopperSliverRule().check(PCB.load(POSITIVE_FIXTURE), _make_design_rules())
    internal_count = sum(v.rule_id == "copper_sliver" for v in internal.violations)

    assert native.ran
    assert native.all_by_type.get("copper_sliver", 0) > 0
    assert internal_count >= native.all_by_type["copper_sliver"]


@pytest.mark.integration
def test_point_one_mm_acute_tip_matches_native_count(tmp_path: Path):
    """The native-positive 0.10 mm chord is not lost at a 0.127 mm fab floor."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.drc.geometric import run_geometric_drc

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    threshold_fixture = tmp_path / "copper_sliver_010.kicad_pcb"
    threshold_fixture.write_text(
        POSITIVE_FIXTURE.read_text()
        .replace("(xy 20 25.1)", "(xy 20 25.05)")
        .replace("(xy 20 24.9)", "(xy 20 24.95)")
    )
    native = run_geometric_drc(threshold_fixture, kicad_cli=cli)
    internal = CopperSliverRule().check(PCB.load(threshold_fixture), _make_design_rules(0.127))
    internal_count = sum(v.rule_id == "copper_sliver" for v in internal.violations)

    assert native.ran
    assert native.all_by_type.get("copper_sliver", 0) == 1
    assert internal_count == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kink", "label"),
    [
        # Sub-0.0008 mm kink adjacent to the acute tip: the exact false
        # negative that a "drop the candidate" minimum-length guard loses
        # (internal/native 0/1) and that the native traversal keeps at 1/1.
        ("        (xy 10.0005 24.9997)\n", "single micro-vertex"),
        # Component-wise smallness: 0.00099 mm away by hypot -- above the
        # 0.0008 mm floor -- yet tiny in both axes, so native walks past it.
        ("        (xy 10.0007 24.9993)\n", "diagonal micro-vertex"),
        # A run of micro-vertices is still one acute corner, one marker.
        (
            "        (xy 10.0003 24.9997)\n        (xy 10.0006 24.9994)\n",
            "micro-vertex chain",
        ),
    ],
)
def test_micro_vertex_beside_tip_matches_native_count(kink: str, label: str, tmp_path: Path):
    """Micro-vertices next to the committed positive tip stay 1/1 vs kicad-cli."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.drc.geometric import run_geometric_drc

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    source = POSITIVE_FIXTURE.read_text()
    tip = "        (xy 10 25)\n"
    assert tip in source
    fixture = tmp_path / "copper_sliver_micro_vertex.kicad_pcb"
    fixture.write_text(source.replace(tip, tip + kink))

    native = run_geometric_drc(fixture, kicad_cli=cli)
    internal = CopperSliverRule().check(PCB.load(fixture), _make_design_rules())
    internal_count = sum(v.rule_id == "copper_sliver" for v in internal.violations)

    assert native.ran
    assert native.all_by_type.get("copper_sliver", 0) == 1, label
    assert internal_count == 1, label


@pytest.mark.integration
@pytest.mark.parametrize(
    "board",
    [
        "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
        "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb",
        "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb",
    ],
)
def test_repository_board_count_matches_native(board: str):
    """Known divergent boards agree with refill-aware native warning counts."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.drc.geometric import run_geometric_drc

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")

    path = REPO_ROOT / board
    native = run_geometric_drc(path, kicad_cli=cli)
    internal = CopperSliverRule().check(PCB.load(path), _make_design_rules())
    internal_count = sum(v.rule_id == "copper_sliver" for v in internal.violations)

    assert native.ran
    assert internal_count == native.all_by_type.get("copper_sliver", 0)

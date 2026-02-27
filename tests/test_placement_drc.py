"""Tests for placement DRC clearance checker.

Tests cover:
- Courtyard clearance violations between component bounding boxes
- Pad-to-pad clearance violations for pads on different nets
- Per-net-class clearance overrides
- Well-spaced placements returning 0 violations
- Violation distance scaling linearly with overlap amount
- Edge cases: single component, opposite sides, same-net pads
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.placement.cost import Net
from kicad_tools.placement.drc import (
    ClearanceViolation,
    DrcResult,
    check_placement_drc,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
)
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_placed(
    ref: str,
    x: float,
    y: float,
    rotation: float = 0.0,
    side: int = 0,
    pads: tuple[TransformedPad, ...] = (),
) -> PlacedComponent:
    return PlacedComponent(reference=ref, x=x, y=y, rotation=rotation, side=side, pads=pads)


def _make_def(
    ref: str,
    width: float = 2.0,
    height: float = 2.0,
    pads: tuple[PadDef, ...] = (),
) -> ComponentDef:
    return ComponentDef(reference=ref, width=width, height=height, pads=pads)


def _default_rules(clearance: float = 0.2) -> DesignRules:
    return DesignRules(trace_clearance=clearance)


# ---------------------------------------------------------------------------
# Courtyard clearance tests
# ---------------------------------------------------------------------------


class TestCourtyardClearance:
    """Tests for courtyard (bounding-box) clearance between components."""

    def test_well_spaced_no_violations(self):
        """Two components far apart should produce zero violations."""
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 10.0, 0.0),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 0
        assert result.total_violation_distance == 0.0
        assert result.violations == ()

    def test_exactly_at_minimum_clearance(self):
        """Components exactly at minimum clearance should pass (no violation)."""
        # Component width = 2.0mm, centered at origin => box from -1 to +1.
        # Second component needs box starting at 1.0 + 0.2 = 1.2, center at 2.2.
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 2.2, 0.0),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 0
        assert result.total_violation_distance == 0.0

    def test_slightly_below_minimum_clearance(self):
        """Components slightly closer than minimum clearance should fail."""
        # Gap = 2.19 - 1.0 - 1.0 = 0.19mm (below 0.2mm minimum)
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 2.19, 0.0),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 1
        assert result.total_violation_distance == pytest.approx(0.01, abs=1e-9)
        assert result.violations[0].kind == "courtyard"
        assert result.violations[0].ref_a == "U1"
        assert result.violations[0].ref_b == "U2"

    def test_overlapping_components(self):
        """Overlapping components should register a violation with gap < 0."""
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 1.0, 0.0),  # overlap: gap = -1.0
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 1
        v = result.violations[0]
        assert v.actual_clearance < 0.0
        # violation_distance = 0.2 - (-1.0) = 1.2
        assert v.violation_distance == pytest.approx(1.2, abs=1e-9)

    def test_opposite_sides_no_violation(self):
        """Components on opposite board sides should not produce violations."""
        placements = [
            _make_placed("U1", 0.0, 0.0, side=0),
            _make_placed("U2", 0.0, 0.0, side=1),  # same position, different side
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 0

    def test_single_component_no_violations(self):
        """A single component should produce zero violations."""
        placements = [_make_placed("U1", 0.0, 0.0)]
        defs = [_make_def("U1")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 0

    def test_empty_placement_no_violations(self):
        """Empty placement list should produce zero violations."""
        result = check_placement_drc([], [], _default_rules())

        assert result.violation_count == 0
        assert result.total_violation_distance == 0.0

    def test_violation_distance_scales_linearly(self):
        """Violation distance should scale linearly with overlap amount."""
        rules = _default_rules(0.2)
        defs = [_make_def("U1"), _make_def("U2")]

        # Gap = 0.1 (violation distance = 0.1)
        placements_a = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 2.1, 0.0),
        ]
        result_a = check_placement_drc(placements_a, defs, rules)

        # Gap = 0.0 (violation distance = 0.2)
        placements_b = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 2.0, 0.0),
        ]
        result_b = check_placement_drc(placements_b, defs, rules)

        # Double the shortfall => double the violation distance
        assert result_b.total_violation_distance == pytest.approx(
            2.0 * result_a.total_violation_distance, abs=1e-9
        )

    def test_rotated_component_courtyard(self):
        """Rotation should be accounted for in courtyard clearance.

        A 2x4mm component rotated 90 degrees becomes 4x2mm.
        """
        # U1: 2x4 at (0,0) => box [-1,-2,1,2]
        # U2: 2x4 rotated 90 at (3.2, 0) => 4x2 => box [1.2,-1,5.2,1]
        # gap_x = max(-1,1.2) - min(1,5.2) = 1.2 - 1.0 = 0.2 (exactly at clearance)
        placements = [
            _make_placed("U1", 0.0, 0.0, rotation=0.0),
            _make_placed("U2", 3.2, 0.0, rotation=90.0),
        ]
        defs = [
            _make_def("U1", width=2.0, height=4.0),
            _make_def("U2", width=2.0, height=4.0),
        ]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 0

    def test_three_components_multiple_violations(self):
        """Three components all too close should produce three violations."""
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 1.5, 0.0),  # too close to U1
            _make_placed("U3", 0.0, 1.5),  # too close to U1
        ]
        # Width/height = 2.0 each, so gap between U1-U2 = 1.5 - 1.0 - 1.0 = -0.5
        # U2-U3: corner case, gap_x = max(-1,0.5) - min(1,2.5) = 0.5-1.0 = -0.5
        # and gap_y = max(-1,0.5) - min(1,2.5) = 0.5-1.0 = -0.5
        # both negative => gap = max(-0.5, -0.5) = -0.5 => violation
        defs = [_make_def("U1"), _make_def("U2"), _make_def("U3")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 3

    def test_diagonal_corner_clearance(self):
        """Two boxes separated diagonally should use Euclidean distance."""
        # U1 at (0,0) width=2 => box [-1,-1,1,1]
        # U2 at (3,3) width=2 => box [2,2,4,4]
        # gap_x = 2-1 = 1, gap_y = 2-1 = 1 => Euclidean = sqrt(2) ~ 1.414
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 3.0, 3.0),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(1.5)  # Just above sqrt(2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 1
        v = result.violations[0]
        assert v.actual_clearance == pytest.approx(math.sqrt(2), abs=1e-9)
        assert v.violation_distance == pytest.approx(1.5 - math.sqrt(2), abs=1e-9)


# ---------------------------------------------------------------------------
# Pad-to-pad clearance tests
# ---------------------------------------------------------------------------


class TestPadClearance:
    """Tests for pad-to-pad clearance between pads on different nets."""

    def test_pads_different_nets_well_spaced(self):
        """Pads on different nets that are well spaced produce no violations."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=5.0, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 5.0, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)
        nets = [
            Net(name="VCC", pins=[("U1", "1")]),
            Net(name="GND", pins=[("U2", "1")]),
        ]

        result = check_placement_drc(placements, defs, rules, nets=nets)

        # Courtyard check passes too (far apart), only care about pad result
        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 0

    def test_pads_different_nets_too_close(self):
        """Pads on different nets too close together should produce a violation."""
        # Pads at (0.5, 0) and (1.0, 0), each 0.5mm wide => gap = 0.25mm
        pads_u1 = (TransformedPad(name="1", x=0.5, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=1.0, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 2.5, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.3)  # require 0.3mm clearance
        nets = [
            Net(name="NET_A", pins=[("U1", "1")]),
            Net(name="NET_B", pins=[("U2", "1")]),
        ]

        # Pad1 box: [0.25, -0.25, 0.75, 0.25]
        # Pad2 box: [0.75, -0.25, 1.25, 0.25]
        # gap_x = 0.75 - 0.75 = 0.0, gap_y = -0.25 - 0.25 = -0.5
        # separated on neither axis both negative? gap_x = 0, gap_y = -0.5
        # gap_x <= 0 and gap_y <= 0 => max(0, -0.5) = 0.0
        # So gap = 0.0 < 0.3 => violation_dist = 0.3

        result = check_placement_drc(placements, defs, rules, nets=nets)

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 1
        assert pad_violations[0].pad_a == "1"
        assert pad_violations[0].pad_b == "1"

    def test_pads_same_net_no_violation(self):
        """Pads on the same net should not produce pad-to-pad violations."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=0.5, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 0.5, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1", width=0.5), _make_def("U2", width=0.5)]
        rules = _default_rules(0.2)
        # Both pads on the same net -- should be skipped
        nets = [
            Net(name="VCC", pins=[("U1", "1"), ("U2", "1")]),
        ]

        result = check_placement_drc(placements, defs, rules, nets=nets)

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 0

    def test_no_nets_skips_pad_checks(self):
        """When nets=None, pad checks should be skipped entirely."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=0.1, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 5.0, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        # No nets => no pad checks
        result = check_placement_drc(placements, defs, rules, nets=None)

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 0

    def test_pads_on_same_component_skipped(self):
        """Pads on the same component should not be checked against each other."""
        pads_u1 = (
            TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),
            TransformedPad(name="2", x=0.3, y=0.0, size_x=0.5, size_y=0.5),
        )

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
        ]
        defs = [_make_def("U1")]
        rules = _default_rules(0.2)
        nets = [
            Net(name="NET_A", pins=[("U1", "1")]),
            Net(name="NET_B", pins=[("U1", "2")]),
        ]

        result = check_placement_drc(placements, defs, rules, nets=nets)

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 0


# ---------------------------------------------------------------------------
# Net-class clearance tests
# ---------------------------------------------------------------------------


class TestNetClassClearance:
    """Tests for per-net-class clearance overrides in pad checks."""

    def test_stricter_net_class_clearance_applied(self):
        """When a net class requires more clearance, it should be used."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=1.0, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 5.0, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)
        nets = [
            Net(name="HI_SPEED", pins=[("U1", "1")]),
            Net(name="GND", pins=[("U2", "1")]),
        ]

        # Pad gap: pad1 at [-0.25,-0.25,0.25,0.25], pad2 at [0.75,-0.25,1.25,0.25]
        # gap_x = 0.75 - 0.25 = 0.5, gap_y = -0.25 - 0.25 = -0.5
        # separated on x only => gap = 0.5mm
        # Default clearance = 0.2 => pass.  But net class = 0.6 => fail.
        net_class_map = {
            "HI_SPEED": NetClassRouting(name="HighSpeed", clearance=0.6),
        }

        result = check_placement_drc(
            placements, defs, rules, nets=nets, net_class_map=net_class_map
        )

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 1
        assert pad_violations[0].required_clearance == pytest.approx(0.6)
        assert pad_violations[0].actual_clearance == pytest.approx(0.5)
        assert pad_violations[0].violation_distance == pytest.approx(0.1)

    def test_net_class_clearance_both_nets(self):
        """When both nets have net classes, the stricter one should be used."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=1.0, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 5.0, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)
        nets = [
            Net(name="NET_A", pins=[("U1", "1")]),
            Net(name="NET_B", pins=[("U2", "1")]),
        ]
        # gap between pads = 0.5mm (see above)
        # NET_A clearance = 0.3, NET_B clearance = 0.4 => max = 0.4 => pass
        net_class_map = {
            "NET_A": NetClassRouting(name="ClassA", clearance=0.3),
            "NET_B": NetClassRouting(name="ClassB", clearance=0.4),
        }

        result = check_placement_drc(
            placements, defs, rules, nets=nets, net_class_map=net_class_map
        )

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 0

    def test_no_net_class_map_uses_default(self):
        """Without net class map, default trace_clearance should be used."""
        pads_u1 = (TransformedPad(name="1", x=0.0, y=0.0, size_x=0.5, size_y=0.5),)
        pads_u2 = (TransformedPad(name="1", x=0.5, y=0.0, size_x=0.5, size_y=0.5),)

        placements = [
            _make_placed("U1", 0.0, 0.0, pads=pads_u1),
            _make_placed("U2", 5.0, 0.0, pads=pads_u2),
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)
        nets = [
            Net(name="NET_A", pins=[("U1", "1")]),
            Net(name="NET_B", pins=[("U2", "1")]),
        ]

        # gap = 0.5 - 0.25 - 0.25 = 0.0? No.
        # pad1 box: [-0.25,-0.25,0.25,0.25], pad2 box: [0.25,-0.25,0.75,0.25]
        # gap_x = 0.25 - 0.25 = 0.0, gap_y = -0.25 - 0.25 = -0.5
        # both <= 0 => gap = max(0.0, -0.5) = 0.0 < 0.2 => violation
        result = check_placement_drc(placements, defs, rules, nets=nets, net_class_map=None)

        pad_violations = [v for v in result.violations if v.kind == "pad"]
        assert len(pad_violations) == 1
        assert pad_violations[0].required_clearance == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for input validation and error handling."""

    def test_mismatched_lengths_raises(self):
        """Mismatched placements and component_defs should raise ValueError."""
        placements = [_make_placed("U1", 0.0, 0.0)]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules()

        with pytest.raises(ValueError, match="placements has 1 items"):
            check_placement_drc(placements, defs, rules)

    def test_result_type(self):
        """check_placement_drc should return a DrcResult instance."""
        result = check_placement_drc([], [], _default_rules())
        assert isinstance(result, DrcResult)

    def test_violation_fields(self):
        """ClearanceViolation should have all expected fields."""
        placements = [
            _make_placed("U1", 0.0, 0.0),
            _make_placed("U2", 1.5, 0.0),  # gap = -0.5
        ]
        defs = [_make_def("U1"), _make_def("U2")]
        rules = _default_rules(0.2)

        result = check_placement_drc(placements, defs, rules)

        assert result.violation_count == 1
        v = result.violations[0]
        assert isinstance(v, ClearanceViolation)
        assert v.ref_a == "U1"
        assert v.ref_b == "U2"
        assert v.kind == "courtyard"
        assert v.required_clearance == pytest.approx(0.2)
        assert v.actual_clearance < 0.0  # overlapping
        assert v.violation_distance > 0.0
        assert v.pad_a is None
        assert v.pad_b is None

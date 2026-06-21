"""Tests for DRC tolerance on clearance and dimension checks.

Verifies that clearance shortfalls within DRC_TOLERANCE (0.005mm) are
not flagged as violations, while genuine violations are still caught.

Regression test for issue #1803: segment-to-via clearance 0.101mm
flagged against 0.102mm minimum (0.001mm false positive).
"""

import pytest

from kicad_tools.validate.rules.base import DRC_TOLERANCE
from kicad_tools.validate.rules.clearance import ClearanceRule
from kicad_tools.validate.rules.dimensions import DimensionRules

# ---------------------------------------------------------------------------
# Mock objects (minimal, matching the interfaces used by the rules)
# ---------------------------------------------------------------------------


class MockDesignRules:
    def __init__(self, **kwargs):
        self.min_clearance_mm = kwargs.get("min_clearance_mm", 0.102)
        self.min_trace_width_mm = kwargs.get("min_trace_width_mm", 0.127)
        self.min_via_drill_mm = kwargs.get("min_via_drill_mm", 0.3)
        self.min_via_diameter_mm = kwargs.get("min_via_diameter_mm", 0.6)
        self.min_annular_ring_mm = kwargs.get("min_annular_ring_mm", 0.15)
        # Drill-to-drill spec (#3842). Mirror min_clearance_mm when unset so
        # these tolerance tests keep treating it as the drill threshold.
        self.min_hole_to_hole_mm = kwargs.get(
            "min_hole_to_hole_mm", self.min_clearance_mm
        )


class MockNet:
    def __init__(self, number, name):
        self.number = number
        self.name = name


class MockSegment:
    def __init__(self, start, end, width, layer, net_number, net_name="", uuid="seg0001"):
        self.start = start
        self.end = end
        self.width = width
        self.layer = layer
        self.net_number = net_number
        self.net_name = net_name
        self.uuid = uuid


class MockVia:
    def __init__(self, position, size, drill, layers, net_number, net_name="", uuid="via0001"):
        self.position = position
        self.size = size
        self.drill = drill
        self.layers = layers
        self.net_number = net_number
        self.net_name = net_name
        self.uuid = uuid


class MockPad:
    def __init__(
        self, position, size, layers, net_number, net_name="", number="1", type="smd", drill=0
    ):
        self.position = position
        self.size = size
        self.layers = layers
        self.net_number = net_number
        self.net_name = net_name
        self.number = number
        self.type = type
        self.drill = drill


class MockFootprint:
    def __init__(self, position, rotation, pads, reference="U1"):
        self.position = position
        self.rotation = rotation
        self.pads = pads
        self.reference = reference


class MockLayer:
    def __init__(self, name):
        self.name = name


class MockPCB:
    def __init__(self, segments=None, vias=None, footprints=None, nets=None):
        self.segments = segments or []
        self.vias = vias or []
        self.footprints = footprints or []
        self._nets = {n.number: n for n in (nets or [])}
        self.copper_layers = [MockLayer("F.Cu")]

    @property
    def nets(self):
        """Net definitions (mirrors schema.pcb.PCB.nets)."""
        return self._nets

    def segments_on_layer(self, layer_name):
        return [s for s in self.segments if s.layer == layer_name]

    def get_net(self, number):
        return self._nets.get(number)


# ---------------------------------------------------------------------------
# Tests for the DRC_TOLERANCE constant
# ---------------------------------------------------------------------------


class TestDRCToleranceConstant:
    def test_tolerance_is_positive(self):
        assert DRC_TOLERANCE > 0

    def test_tolerance_value(self):
        assert pytest.approx(0.005) == DRC_TOLERANCE


# ---------------------------------------------------------------------------
# Tests for clearance tolerance (issue #1803 regression)
# ---------------------------------------------------------------------------


class TestClearanceRuleTolerance:
    """Verify the ClearanceRule respects DRC_TOLERANCE."""

    def _make_segment_via_pcb(self, gap_mm: float) -> tuple:
        """Create a PCB with a segment and via separated by gap_mm edge-to-edge.

        The segment is horizontal, width 0.2mm, centered at y=0.
        The via is circular, diameter 0.6mm (radius 0.3mm), centered below.
        We position them so edge-to-edge clearance is exactly gap_mm.

        Edge clearance = center_distance - seg_half_width - via_radius
        center_distance = gap_mm + 0.1 + 0.3 = gap_mm + 0.4
        """
        seg_width = 0.2
        via_size = 0.6
        center_dist = gap_mm + (seg_width / 2) + (via_size / 2)

        seg = MockSegment(
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=seg_width,
            layer="F.Cu",
            net_number=1,
        )
        via = MockVia(
            position=(5.0, center_dist),
            size=via_size,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=2,
        )
        nets = [MockNet(1, "VCC"), MockNet(2, "GND")]
        pcb = MockPCB(segments=[seg], vias=[via], nets=nets)
        rules = MockDesignRules(min_clearance_mm=0.102)
        return pcb, rules

    def test_exact_issue_1803_case_passes(self):
        """0.101mm clearance vs 0.102mm minimum -- within tolerance, should pass."""
        pcb, rules = self._make_segment_via_pcb(0.101)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0, (
            f"Expected no errors for 0.101mm vs 0.102mm, got: "
            f"{[v.message for v in result.violations]}"
        )

    def test_clearance_within_tolerance_passes(self):
        """Clearance short by less than DRC_TOLERANCE should pass."""
        # 0.098mm clearance vs 0.102mm min => 0.004mm short, within 0.005mm tol
        pcb, rules = self._make_segment_via_pcb(0.098)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0

    def test_clearance_at_tolerance_boundary_passes(self):
        """Clearance short by exactly DRC_TOLERANCE should pass (boundary)."""
        # 0.097mm clearance vs 0.102mm min => 0.005mm short, exactly at tol
        pcb, rules = self._make_segment_via_pcb(0.097)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0

    def test_clearance_beyond_tolerance_fails(self):
        """Clearance short by more than DRC_TOLERANCE should fail."""
        # 0.090mm clearance vs 0.102mm min => 0.012mm short, beyond 0.005mm tol
        pcb, rules = self._make_segment_via_pcb(0.090)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 1

    def test_exact_clearance_passes(self):
        """Clearance exactly at minimum should pass."""
        pcb, rules = self._make_segment_via_pcb(0.102)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0

    def test_generous_clearance_passes(self):
        """Clearance well above minimum should pass."""
        pcb, rules = self._make_segment_via_pcb(0.200)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0


# ---------------------------------------------------------------------------
# Tests for dimension tolerance
# ---------------------------------------------------------------------------


class TestDimensionRulesTolerance:
    """Verify DimensionRules respects DRC_TOLERANCE for all checks."""

    def test_trace_width_within_tolerance_passes(self):
        """Trace 0.124mm vs 0.127mm minimum -- 0.003mm short, within tolerance."""
        seg = MockSegment(
            start=(0, 0),
            end=(10, 0),
            width=0.124,
            layer="F.Cu",
            net_number=1,
        )
        pcb = MockPCB(segments=[seg], nets=[MockNet(1, "VCC")])
        rules = MockDesignRules(min_trace_width_mm=0.127)
        result = DimensionRules().check(pcb, rules)
        width_violations = [v for v in result.violations if v.rule_id == "dimension_trace_width"]
        assert len(width_violations) == 0

    def test_trace_width_beyond_tolerance_fails(self):
        """Trace 0.110mm vs 0.127mm minimum -- 0.017mm short, should fail."""
        seg = MockSegment(
            start=(0, 0),
            end=(10, 0),
            width=0.110,
            layer="F.Cu",
            net_number=1,
        )
        pcb = MockPCB(segments=[seg], nets=[MockNet(1, "VCC")])
        rules = MockDesignRules(min_trace_width_mm=0.127)
        result = DimensionRules().check(pcb, rules)
        width_violations = [v for v in result.violations if v.rule_id == "dimension_trace_width"]
        assert len(width_violations) == 1

    def test_via_drill_within_tolerance_passes(self):
        """Via drill 0.297mm vs 0.300mm minimum -- 0.003mm short, passes."""
        via = MockVia(
            position=(5, 5),
            size=0.8,
            drill=0.297,
            layers=["F.Cu", "B.Cu"],
            net_number=1,
        )
        pcb = MockPCB(vias=[via], nets=[MockNet(1, "VCC")])
        rules = MockDesignRules(
            min_via_drill_mm=0.3, min_via_diameter_mm=0.6, min_annular_ring_mm=0.15
        )
        result = DimensionRules().check(pcb, rules)
        drill_violations = [v for v in result.violations if v.rule_id == "dimension_via_drill"]
        assert len(drill_violations) == 0

    def test_drill_clearance_within_tolerance_passes(self):
        """Drill edge-to-edge 0.124mm vs 0.127mm min -- 0.003mm short, passes."""
        via1 = MockVia(
            position=(0, 0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=1,
        )
        via2 = MockVia(
            position=(0.724, 0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=2,
            uuid="via0002",
        )
        # edge distance = 0.724 - 0.15 - 0.15 = 0.424 ... let me recalculate
        # edge = center_dist - r1 - r2 = 0.724 - 0.15 - 0.15 = 0.424
        # That's too large. Let me use drill/2 values.
        # edge = center_dist - drill1/2 - drill2/2
        # Want edge = 0.124, drill = 0.3, so center = 0.124 + 0.15 + 0.15 = 0.424
        via2 = MockVia(
            position=(0.424, 0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=2,
            uuid="via0002",
        )
        pcb = MockPCB(vias=[via1, via2], nets=[MockNet(1, "VCC"), MockNet(2, "GND")])
        rules = MockDesignRules(min_clearance_mm=0.127)
        result = DimensionRules().check(pcb, rules)
        drill_violations = [
            v for v in result.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(drill_violations) == 0

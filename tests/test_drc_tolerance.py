"""Tests for the DRC numerical guard band on clearance and dimension checks.

``DRC_TOLERANCE`` is a float-rounding guard band -- NOT a manufacturing
tolerance.  Issue #3913 reduced it from 0.005 mm (5 um) to 1e-4 mm
(0.1 um): the old 5 um dead band silently PASSED genuine marginal
violations up to 5 um below the floor (e.g. a 0.0999 mm clearance vs a
0.1016 mm / 4 mil JLCPCB floor is only 1.7 um short).  The guard now
suppresses only IEEE-754 rounding noise (~1e-9 mm at board coordinates)
while marginal-class shortfalls of a few um correctly FAIL.

Historical note: issue #1803 originally motivated a wide dead band to
avoid a 1 um false positive (0.101 mm vs 0.102 mm).  #3913 established
that such a shortfall is a TRUE positive at KiCad IU granularity (1 nm),
so that case is now (correctly) reported.
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
        self.min_hole_to_hole_mm = kwargs.get("min_hole_to_hole_mm", self.min_clearance_mm)


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
        """Guard band is 0.1 um (#3913), tight enough to catch marginal-class
        violations while still suppressing float64 rounding noise."""
        assert pytest.approx(1e-4) == DRC_TOLERANCE

    def test_tolerance_is_sub_micron(self):
        """The band must stay well below the ~1.6 um marginal-violation width
        (0.1000 vs 0.1016 mm) it previously masked, and far above float64
        rounding (~1e-9 mm at board coordinates)."""
        assert DRC_TOLERANCE < 1.6e-3  # below the marginal class it must catch
        assert DRC_TOLERANCE > 1e-9  # above float rounding noise


# ---------------------------------------------------------------------------
# Tests for clearance tolerance (#3913: the dead band is gone)
# ---------------------------------------------------------------------------


class TestClearanceRuleTolerance:
    """Verify the ClearanceRule guard band only suppresses float noise."""

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

    def test_issue_1803_case_now_fails(self):
        """0.101mm vs 0.102mm min (1 um short) is a TRUE positive (#3913).

        The old 5 um dead band passed this; at KiCad IU granularity (1 nm)
        it is a real sub-floor clearance and must now be reported.
        """
        pcb, rules = self._make_segment_via_pcb(0.101)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 1, (
            "0.101mm vs 0.102mm min is 1 um below the floor; with the tightened "
            "0.1 um guard band it must FAIL (was masked by the old 5 um band)."
        )

    def test_marginal_shortfall_fails(self):
        """A 4 um shortfall (inside the old dead band) now fails."""
        # 0.098mm clearance vs 0.102mm min => 4 um short (was passed by 5 um band)
        pcb, rules = self._make_segment_via_pcb(0.098)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 1

    def test_float_rounding_noise_still_passes(self):
        """A shortfall below the 0.1 um guard band (pure float noise) passes."""
        # 0.10195mm clearance vs 0.102mm min => 0.05 um short, below the guard
        pcb, rules = self._make_segment_via_pcb(0.10195)
        result = ClearanceRule().check(pcb, rules)
        assert result.error_count == 0, (
            "A 0.05 um shortfall is within float64 rounding noise and must not "
            "be flagged; only genuine sub-floor clearances should fail."
        )

    def test_clearance_beyond_tolerance_fails(self):
        """Clearance short by more than the guard band should fail."""
        # 0.090mm clearance vs 0.102mm min => 12 um short, well beyond the guard
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
# Tests for dimension tolerance (#3913: same tightened guard band)
# ---------------------------------------------------------------------------


class TestDimensionRulesTolerance:
    """Verify DimensionRules respects the tightened DRC_TOLERANCE guard band."""

    def test_trace_width_marginal_shortfall_fails(self):
        """Trace 0.124mm vs 0.127mm min -- 3 um short, now a TRUE positive."""
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
        assert len(width_violations) == 1, (
            "A 3 um trace-width shortfall (0.124 vs 0.127) is below the floor "
            "and must fail now that the guard band is 0.1 um (#3913)."
        )

    def test_trace_width_float_noise_passes(self):
        """Trace within the 0.1 um guard band of the floor still passes."""
        seg = MockSegment(
            start=(0, 0),
            end=(10, 0),
            width=0.12695,  # 0.05 um below 0.127 floor -> float noise
            layer="F.Cu",
            net_number=1,
        )
        pcb = MockPCB(segments=[seg], nets=[MockNet(1, "VCC")])
        rules = MockDesignRules(min_trace_width_mm=0.127)
        result = DimensionRules().check(pcb, rules)
        width_violations = [v for v in result.violations if v.rule_id == "dimension_trace_width"]
        assert len(width_violations) == 0

    def test_trace_width_beyond_tolerance_fails(self):
        """Trace 0.110mm vs 0.127mm minimum -- 17 um short, should fail."""
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

    def test_via_drill_marginal_shortfall_fails(self):
        """Via drill 0.297mm vs 0.300mm min -- 3 um short, now a TRUE positive."""
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
        assert len(drill_violations) == 1

    def test_drill_clearance_marginal_shortfall_fails(self):
        """Drill edge-to-edge 0.124mm vs 0.127mm min -- 3 um short, now fails."""
        via1 = MockVia(
            position=(0, 0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=1,
        )
        # edge = center_dist - drill1/2 - drill2/2 = 0.424 - 0.15 - 0.15 = 0.124
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
            v for v in result.violations if v.rule_id == "hole_to_hole_clearance"
        ]
        assert len(drill_violations) == 1

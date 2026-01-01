"""Tests for dimension DRC rules (trace width, via drill, annular ring)."""

import pytest

from kicad_tools.validate.rules.dimensions import DimensionRules


class MockDesignRules:
    """Mock design rules for testing."""

    def __init__(
        self,
        min_trace_width_mm: float = 0.127,
        min_via_drill_mm: float = 0.3,
        min_via_diameter_mm: float = 0.6,
        min_annular_ring_mm: float = 0.15,
        min_clearance_mm: float = 0.127,
    ):
        self.min_trace_width_mm = min_trace_width_mm
        self.min_via_drill_mm = min_via_drill_mm
        self.min_via_diameter_mm = min_via_diameter_mm
        self.min_annular_ring_mm = min_annular_ring_mm
        self.min_clearance_mm = min_clearance_mm


class MockNet:
    """Mock net for testing."""

    def __init__(self, number: int, name: str):
        self.number = number
        self.name = name


class MockSegment:
    """Mock trace segment for testing."""

    def __init__(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        width: float,
        layer: str,
        net_number: int,
    ):
        self.start = start
        self.end = end
        self.width = width
        self.layer = layer
        self.net_number = net_number


class MockVia:
    """Mock via for testing."""

    def __init__(
        self,
        position: tuple[float, float],
        size: float,
        drill: float,
        layers: list[str],
        net_number: int,
    ):
        self.position = position
        self.size = size
        self.drill = drill
        self.layers = layers
        self.net_number = net_number


class MockPad:
    """Mock pad for testing."""

    def __init__(
        self,
        number: str,
        type: str,
        position: tuple[float, float],
        drill: float,
        net_name: str,
        net_number: int,
    ):
        self.number = number
        self.type = type
        self.position = position
        self.drill = drill
        self.net_name = net_name
        self.net_number = net_number


class MockFootprint:
    """Mock footprint for testing."""

    def __init__(
        self,
        reference: str,
        position: tuple[float, float],
        pads: list[MockPad],
    ):
        self.reference = reference
        self.position = position
        self.pads = pads


class MockPCB:
    """Mock PCB for testing dimension rules."""

    def __init__(
        self,
        segments: list[MockSegment] | None = None,
        vias: list[MockVia] | None = None,
        footprints: list[MockFootprint] | None = None,
        nets: dict[int, MockNet] | None = None,
    ):
        self.segments = segments or []
        self.vias = vias or []
        self.footprints = footprints or []
        self._nets = nets or {}

    def get_net(self, number: int) -> MockNet | None:
        return self._nets.get(number)


class TestDimensionRulesMetadata:
    """Test DimensionRules class metadata."""

    def test_rule_id(self):
        """Test rule has correct ID."""
        rule = DimensionRules()
        assert rule.rule_id == "dimensions"

    def test_rule_name(self):
        """Test rule has correct name."""
        rule = DimensionRules()
        assert rule.name == "Dimension Rules"

    def test_rule_description(self):
        """Test rule has a description."""
        rule = DimensionRules()
        assert len(rule.description) > 0


class TestTraceWidthCheck:
    """Tests for trace width validation."""

    def test_trace_width_passes(self):
        """Test that valid trace widths pass."""
        pcb = MockPCB(
            segments=[
                MockSegment((0, 0), (10, 0), width=0.2, layer="F.Cu", net_number=1),
                MockSegment((10, 0), (10, 10), width=0.15, layer="F.Cu", net_number=1),
            ],
            nets={1: MockNet(1, "VCC")},
        )
        rules = MockDesignRules(min_trace_width_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        trace_violations = [v for v in results.violations if v.rule_id == "dimension_trace_width"]
        assert len(trace_violations) == 0

    def test_trace_width_undersized(self):
        """Test that undersized traces are detected."""
        pcb = MockPCB(
            segments=[
                MockSegment((0, 0), (10, 0), width=0.10, layer="F.Cu", net_number=1),
            ],
            nets={1: MockNet(1, "SIG")},
        )
        rules = MockDesignRules(min_trace_width_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        trace_violations = [v for v in results.violations if v.rule_id == "dimension_trace_width"]
        assert len(trace_violations) == 1
        assert trace_violations[0].severity == "error"
        assert trace_violations[0].actual_value == pytest.approx(0.10)
        assert trace_violations[0].required_value == pytest.approx(0.127)
        assert trace_violations[0].layer == "F.Cu"
        assert "SIG" in trace_violations[0].items

    def test_multiple_undersized_traces(self):
        """Test that multiple undersized traces are all detected."""
        pcb = MockPCB(
            segments=[
                MockSegment((0, 0), (10, 0), width=0.05, layer="F.Cu", net_number=1),
                MockSegment((10, 0), (10, 10), width=0.08, layer="B.Cu", net_number=2),
                MockSegment((10, 10), (20, 10), width=0.20, layer="F.Cu", net_number=1),  # Valid
            ],
            nets={1: MockNet(1, "VCC"), 2: MockNet(2, "GND")},
        )
        rules = MockDesignRules(min_trace_width_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        trace_violations = [v for v in results.violations if v.rule_id == "dimension_trace_width"]
        assert len(trace_violations) == 2


class TestViaDimensionChecks:
    """Tests for via dimension validation."""

    def test_via_passes_all_checks(self):
        """Test that valid vias pass all checks."""
        pcb = MockPCB(
            vias=[
                MockVia((5, 5), size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "VCC")},
        )
        rules = MockDesignRules(
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
        )
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        via_violations = [v for v in results.violations if v.rule_id.startswith("dimension_via")]
        annular_violations = [
            v for v in results.violations if v.rule_id == "dimension_annular_ring"
        ]
        assert len(via_violations) == 0
        assert len(annular_violations) == 0

    def test_via_drill_undersized(self):
        """Test that undersized via drill is detected."""
        pcb = MockPCB(
            vias=[
                MockVia((5, 5), size=0.5, drill=0.2, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "NET1")},
        )
        rules = MockDesignRules(min_via_drill_mm=0.3)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        drill_violations = [v for v in results.violations if v.rule_id == "dimension_via_drill"]
        assert len(drill_violations) == 1
        assert drill_violations[0].actual_value == pytest.approx(0.2)
        assert drill_violations[0].required_value == pytest.approx(0.3)

    def test_via_diameter_undersized(self):
        """Test that undersized via outer diameter is detected."""
        pcb = MockPCB(
            vias=[
                MockVia((5, 5), size=0.5, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "NET1")},
        )
        rules = MockDesignRules(min_via_diameter_mm=0.6)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        diameter_violations = [
            v for v in results.violations if v.rule_id == "dimension_via_diameter"
        ]
        assert len(diameter_violations) == 1
        assert diameter_violations[0].actual_value == pytest.approx(0.5)
        assert diameter_violations[0].required_value == pytest.approx(0.6)

    def test_annular_ring_undersized(self):
        """Test that undersized annular ring is detected."""
        # Via with size=0.5, drill=0.3 has annular ring = (0.5-0.3)/2 = 0.1mm
        pcb = MockPCB(
            vias=[
                MockVia((5, 5), size=0.5, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "NET1")},
        )
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        annular_violations = [
            v for v in results.violations if v.rule_id == "dimension_annular_ring"
        ]
        assert len(annular_violations) == 1
        assert annular_violations[0].actual_value == pytest.approx(0.1)
        assert annular_violations[0].required_value == pytest.approx(0.15)


class TestDrillClearanceCheck:
    """Tests for drill-to-drill clearance validation."""

    def test_drills_with_sufficient_clearance(self):
        """Test that well-spaced drills pass."""
        # Two vias 10mm apart with 0.4mm drills = 10 - 0.4 = 9.6mm edge clearance
        pcb = MockPCB(
            vias=[
                MockVia((0, 0), size=0.6, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=1),
                MockVia((10, 0), size=0.6, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=2),
            ],
            nets={1: MockNet(1, "NET1"), 2: MockNet(2, "NET2")},
        )
        rules = MockDesignRules(min_clearance_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        clearance_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(clearance_violations) == 0

    def test_drills_too_close(self):
        """Test that close drills are detected."""
        # Two vias 0.5mm apart with 0.3mm drills = 0.5 - 0.3 = 0.2mm edge clearance
        # But actually edge-to-edge = 0.5 - (0.15 + 0.15) = 0.2mm
        pcb = MockPCB(
            vias=[
                MockVia((0, 0), size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=1),
                MockVia((0.4, 0), size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=2),
            ],
            nets={1: MockNet(1, "NET1"), 2: MockNet(2, "NET2")},
        )
        # center distance = 0.4, edge distance = 0.4 - 0.15 - 0.15 = 0.1mm
        rules = MockDesignRules(min_clearance_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        clearance_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(clearance_violations) == 1
        assert clearance_violations[0].actual_value == pytest.approx(0.1)
        assert "NET1" in clearance_violations[0].items
        assert "NET2" in clearance_violations[0].items

    def test_through_hole_pad_clearance(self):
        """Test that through-hole pad clearance is checked."""
        # Via at origin, through-hole pad nearby
        pcb = MockPCB(
            vias=[
                MockVia((0, 0), size=0.6, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            footprints=[
                MockFootprint(
                    reference="R1",
                    position=(0.6, 0),  # Footprint position
                    pads=[
                        MockPad(
                            number="1",
                            type="thru_hole",
                            position=(0, 0),  # Pad relative to footprint
                            drill=0.4,
                            net_name="NET2",
                            net_number=2,
                        ),
                    ],
                ),
            ],
            nets={1: MockNet(1, "NET1"), 2: MockNet(2, "NET2")},
        )
        # Via at (0,0) with drill=0.4, pad at (0.6, 0) with drill=0.4
        # center distance = 0.6mm, edge distance = 0.6 - 0.2 - 0.2 = 0.2mm
        rules = MockDesignRules(min_clearance_mm=0.25)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        clearance_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(clearance_violations) == 1

    def test_smd_pads_not_checked(self):
        """Test that SMD pads are not included in drill clearance check."""
        pcb = MockPCB(
            vias=[
                MockVia((0, 0), size=0.6, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            footprints=[
                MockFootprint(
                    reference="C1",
                    position=(0.5, 0),
                    pads=[
                        MockPad(
                            number="1",
                            type="smd",  # SMD - no drill
                            position=(0, 0),
                            drill=0,  # No drill for SMD
                            net_name="NET2",
                            net_number=2,
                        ),
                    ],
                ),
            ],
            nets={1: MockNet(1, "NET1"), 2: MockNet(2, "NET2")},
        )
        rules = MockDesignRules(min_clearance_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        clearance_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(clearance_violations) == 0


class TestEmptyPCB:
    """Tests for edge cases with empty PCB."""

    def test_empty_pcb(self):
        """Test that empty PCB produces no violations."""
        pcb = MockPCB()
        rules = MockDesignRules()
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        assert len(results.violations) == 0
        assert results.rules_checked == 5

    def test_pcb_with_no_vias(self):
        """Test PCB with traces but no vias."""
        pcb = MockPCB(
            segments=[
                MockSegment((0, 0), (10, 0), width=0.2, layer="F.Cu", net_number=1),
            ],
            nets={1: MockNet(1, "VCC")},
        )
        rules = MockDesignRules()
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        via_violations = [v for v in results.violations if "via" in v.rule_id.lower()]
        assert len(via_violations) == 0

    def test_pcb_with_no_traces(self):
        """Test PCB with vias but no traces."""
        pcb = MockPCB(
            vias=[
                MockVia((5, 5), size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "VCC")},
        )
        rules = MockDesignRules()
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        trace_violations = [v for v in results.violations if v.rule_id == "dimension_trace_width"]
        assert len(trace_violations) == 0


class TestRulesCheckedCount:
    """Tests for rules_checked count."""

    def test_rules_checked_count(self):
        """Test that 5 rule categories are counted."""
        pcb = MockPCB()
        rules = MockDesignRules()
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        # 5 rules: trace width, via drill, via diameter, annular ring, drill clearance
        assert results.rules_checked == 5


class TestViolationLocation:
    """Tests for violation location reporting."""

    def test_trace_violation_location(self):
        """Test that trace violation reports segment start position."""
        pcb = MockPCB(
            segments=[
                MockSegment((15.5, 22.3), (25.0, 22.3), width=0.05, layer="F.Cu", net_number=1),
            ],
            nets={1: MockNet(1, "NET")},
        )
        rules = MockDesignRules(min_trace_width_mm=0.127)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        violation = results.violations[0]
        assert violation.location == (15.5, 22.3)

    def test_via_violation_location(self):
        """Test that via violation reports via position."""
        pcb = MockPCB(
            vias=[
                MockVia((42.5, 18.2), size=0.4, drill=0.2, layers=["F.Cu", "B.Cu"], net_number=1),
            ],
            nets={1: MockNet(1, "NET")},
        )
        rules = MockDesignRules(min_via_drill_mm=0.3)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        violation = results.violations[0]
        assert violation.location == (42.5, 18.2)

    def test_drill_clearance_violation_location(self):
        """Test that drill clearance violation reports midpoint."""
        pcb = MockPCB(
            vias=[
                MockVia((0, 0), size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=1),
                MockVia((0.4, 0), size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=2),
            ],
            nets={1: MockNet(1, "NET1"), 2: MockNet(2, "NET2")},
        )
        rules = MockDesignRules(min_clearance_mm=0.15)
        rule = DimensionRules()

        results = rule.check(pcb, rules)

        clearance_violations = [
            v for v in results.violations if v.rule_id == "dimension_drill_clearance"
        ]
        assert len(clearance_violations) == 1
        # Midpoint of (0,0) and (0.4, 0) is (0.2, 0)
        assert clearance_violations[0].location == pytest.approx((0.2, 0.0))

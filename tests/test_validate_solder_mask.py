"""Tests for solder mask and pad dimension DRC rules."""

import pytest

from kicad_tools.validate.rules.solder_mask import SolderMaskPadRules


# -- Mock classes ----------------------------------------------------------


class MockSetup:
    """Mock PCB setup with board-level mask clearance."""

    def __init__(self, pad_to_mask_clearance: float = 0.0):
        self.pad_to_mask_clearance = pad_to_mask_clearance


class MockDesignRules:
    """Mock design rules for testing."""

    def __init__(
        self,
        min_solder_mask_clearance_mm: float = 0.05,
        min_pad_size_mm: float = 0.25,
        min_annular_ring_mm: float = 0.15,
    ):
        self.min_solder_mask_clearance_mm = min_solder_mask_clearance_mm
        self.min_pad_size_mm = min_pad_size_mm
        self.min_annular_ring_mm = min_annular_ring_mm


class MockPad:
    """Mock pad for testing."""

    def __init__(
        self,
        number: str = "1",
        type: str = "smd",
        position: tuple[float, float] = (0.0, 0.0),
        size: tuple[float, float] = (1.0, 1.0),
        layers: list[str] | None = None,
        drill: float = 0.0,
        net_name: str = "",
        net_number: int = 0,
        solder_mask_margin: float | None = None,
    ):
        self.number = number
        self.type = type
        self.position = position
        self.size = size
        self.layers = layers or ["F.Cu", "F.Paste", "F.Mask"]
        self.drill = drill
        self.net_name = net_name
        self.net_number = net_number
        self.solder_mask_margin = solder_mask_margin


class MockFootprint:
    """Mock footprint for testing."""

    def __init__(
        self,
        reference: str = "U1",
        position: tuple[float, float] = (10.0, 20.0),
        layer: str = "F.Cu",
        pads: list[MockPad] | None = None,
    ):
        self.reference = reference
        self.position = position
        self.layer = layer
        self.pads = pads or []


class MockPCB:
    """Mock PCB for testing solder mask rules."""

    def __init__(
        self,
        footprints: list[MockFootprint] | None = None,
        setup: MockSetup | None = None,
    ):
        self.footprints = footprints or []
        self.setup = setup or MockSetup()


# -- Solder Mask Clearance Tests -------------------------------------------


class TestSolderMaskClearance:
    """Tests for solder mask clearance validation."""

    def test_no_violation_with_zero_clearance(self):
        """Zero mask clearance (KiCad default) should not be flagged."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(solder_mask_margin=None),
                ]),
            ],
            setup=MockSetup(pad_to_mask_clearance=0.0),
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 0

    def test_violation_with_small_per_pad_clearance(self):
        """Per-pad mask clearance below minimum should be flagged."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(solder_mask_margin=0.02),
                ]),
            ],
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 1
        assert mask_violations[0].severity == "warning"
        assert mask_violations[0].actual_value == pytest.approx(0.02)
        assert mask_violations[0].required_value == pytest.approx(0.05)

    def test_violation_with_small_board_level_clearance(self):
        """Board-level mask clearance below minimum should be flagged."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(solder_mask_margin=None),
                ]),
            ],
            setup=MockSetup(pad_to_mask_clearance=0.03),
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 1

    def test_no_violation_with_adequate_clearance(self):
        """Clearance at or above minimum should pass."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(solder_mask_margin=0.05),
                ]),
            ],
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 0

    def test_pads_without_mask_layer_skipped(self):
        """Pads without mask layers should not be checked."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(
                        solder_mask_margin=0.01,
                        layers=["F.Cu"],  # No mask layer
                    ),
                ]),
            ],
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 0

    def test_per_pad_overrides_board_level(self):
        """Per-pad margin should override board-level setting."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(solder_mask_margin=0.06),  # Above min
                ]),
            ],
            setup=MockSetup(pad_to_mask_clearance=0.01),  # Below min
        )
        rules = MockDesignRules(min_solder_mask_clearance_mm=0.05)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        mask_violations = [v for v in results.violations if v.rule_id == "solder_mask_clearance"]
        assert len(mask_violations) == 0


# -- Minimum Pad Size Tests ------------------------------------------------


class TestMinPadSize:
    """Tests for minimum pad size validation."""

    def test_adequate_pad_size_passes(self):
        """Pads at or above minimum should pass."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(size=(0.5, 0.8)),
                ]),
            ],
        )
        rules = MockDesignRules(min_pad_size_mm=0.25)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 0

    def test_undersized_pad_detected(self):
        """Pad with smallest dimension below minimum should be flagged."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(size=(0.2, 0.5)),  # Width 0.2 < 0.25
                ]),
            ],
        )
        rules = MockDesignRules(min_pad_size_mm=0.25)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 1
        assert pad_violations[0].severity == "error"
        assert pad_violations[0].actual_value == pytest.approx(0.2)
        assert pad_violations[0].required_value == pytest.approx(0.25)
        assert "U1-1" in pad_violations[0].items

    def test_npth_pads_skipped(self):
        """Non-plated through holes should not be checked for size."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(
                        type="np_thru_hole",
                        size=(0.1, 0.1),
                        drill=0.1,
                        layers=["*.Cu"],
                    ),
                ]),
            ],
        )
        rules = MockDesignRules(min_pad_size_mm=0.25)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 0

    def test_multiple_undersized_pads(self):
        """Multiple undersized pads should all be reported."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(
                    reference="C1",
                    pads=[
                        MockPad(number="1", size=(0.15, 0.3)),
                        MockPad(number="2", size=(0.15, 0.3)),
                    ],
                ),
            ],
        )
        rules = MockDesignRules(min_pad_size_mm=0.25)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 2

    def test_violation_location_is_absolute(self):
        """Violation location should be absolute (footprint + pad position)."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(
                    position=(100.0, 200.0),
                    pads=[
                        MockPad(position=(5.0, 3.0), size=(0.1, 0.1)),
                    ],
                ),
            ],
        )
        rules = MockDesignRules(min_pad_size_mm=0.25)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 1
        assert pad_violations[0].location == pytest.approx((105.0, 203.0))


# -- PTH Annular Ring Tests ------------------------------------------------


class TestPTHAnnularRing:
    """Tests for PTH pad annular ring validation."""

    def test_adequate_annular_ring_passes(self):
        """Through-hole pad with adequate ring should pass."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(
                        type="thru_hole",
                        size=(1.8, 1.8),
                        drill=1.0,
                        layers=["*.Cu", "*.Mask"],
                        net_name="VCC",
                    ),
                ]),
            ],
        )
        # annular_ring = (1.8 - 1.0) / 2 = 0.4mm > 0.15mm
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 0

    def test_undersized_annular_ring_detected(self):
        """Through-hole pad with thin ring should be flagged."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(
                    reference="R1",
                    pads=[
                        MockPad(
                            type="thru_hole",
                            size=(1.1, 1.1),
                            drill=1.0,
                            layers=["*.Cu", "*.Mask"],
                            net_name="GND",
                        ),
                    ],
                ),
            ],
        )
        # annular_ring = (1.1 - 1.0) / 2 = 0.05mm < 0.15mm
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 1
        assert pth_violations[0].severity == "error"
        assert pth_violations[0].actual_value == pytest.approx(0.05)
        assert pth_violations[0].required_value == pytest.approx(0.15)
        assert "R1-1" in pth_violations[0].items
        assert "GND" in pth_violations[0].items

    def test_smd_pads_not_checked_for_annular_ring(self):
        """SMD pads should not be checked for annular ring."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(type="smd", size=(0.5, 0.5)),
                ]),
            ],
        )
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 0

    def test_oval_pad_uses_smallest_dimension(self):
        """Oval pad should use smallest dimension for annular ring."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(
                    reference="J1",
                    pads=[
                        MockPad(
                            type="thru_hole",
                            size=(2.0, 1.2),  # Oval pad
                            drill=1.0,
                            layers=["*.Cu", "*.Mask"],
                            net_name="SIG",
                        ),
                    ],
                ),
            ],
        )
        # annular_ring = (min(2.0, 1.2) - 1.0) / 2 = (1.2 - 1.0) / 2 = 0.1mm
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 1
        assert pth_violations[0].actual_value == pytest.approx(0.1)

    def test_no_drill_skipped(self):
        """Through-hole pad with no drill should be skipped."""
        pcb = MockPCB(
            footprints=[
                MockFootprint(pads=[
                    MockPad(type="thru_hole", size=(1.0, 1.0), drill=0.0),
                ]),
            ],
        )
        rules = MockDesignRules(min_annular_ring_mm=0.15)
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)

        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 0


# -- Rules Metadata Tests --------------------------------------------------


class TestSolderMaskPadRulesMetadata:
    """Test SolderMaskPadRules class metadata."""

    def test_rule_id(self):
        rule = SolderMaskPadRules()
        assert rule.rule_id == "solder_mask_pad"

    def test_rule_name(self):
        rule = SolderMaskPadRules()
        assert rule.name == "Solder Mask & Pad Rules"

    def test_rules_checked_count(self):
        """Should report 3 rule categories."""
        pcb = MockPCB()
        rules = MockDesignRules()
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)
        assert results.rules_checked == 3

    def test_empty_pcb_no_violations(self):
        """Empty PCB should produce no violations."""
        pcb = MockPCB()
        rules = MockDesignRules()
        rule = SolderMaskPadRules()

        results = rule.check(pcb, rules)
        assert len(results.violations) == 0


# -- Integration Test -------------------------------------------------------


class TestSolderMaskPadIntegration:
    """Integration test with DRCChecker."""

    def test_check_all_includes_solder_mask_rules(self):
        """Verify check_all() includes solder mask/pad rules in results."""
        from kicad_tools.schema.pcb import PCB, Footprint, Pad
        from kicad_tools.sexp import SExp
        from kicad_tools.validate import DRCChecker

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        # Add a footprint with an undersized pad
        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="C1",
            value="100nF",
            pads=[
                Pad(
                    number="1",
                    type="smd",
                    shape="roundrect",
                    position=(0.0, 0.0),
                    size=(0.15, 0.3),  # Width below 0.25mm min
                    layers=["F.Cu", "F.Paste", "F.Mask"],
                ),
            ],
        )
        pcb._footprints.append(fp)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_all()

        # Should find the undersized pad
        pad_violations = [v for v in results.violations if v.rule_id == "min_pad_size"]
        assert len(pad_violations) == 1

        # rules_checked should include solder_mask_pad (3 rules)
        assert results.rules_checked >= 3

    def test_check_solder_mask_pads_method(self):
        """Verify DRCChecker.check_solder_mask_pads() works standalone."""
        from kicad_tools.schema.pcb import PCB, Footprint, Pad
        from kicad_tools.sexp import SExp
        from kicad_tools.validate import DRCChecker

        sexp = SExp(name="kicad_pcb")
        pcb = PCB(sexp)

        fp = Footprint(
            name="TestFP",
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="R1",
            value="10k",
            pads=[
                Pad(
                    number="1",
                    type="thru_hole",
                    shape="circle",
                    position=(0.0, 0.0),
                    size=(1.1, 1.1),
                    layers=["*.Cu", "*.Mask"],
                    drill=1.0,
                    net_name="NET1",
                ),
            ],
        )
        pcb._footprints.append(fp)

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_solder_mask_pads()

        # PTH annular ring = (1.1 - 1.0) / 2 = 0.05 < 0.15
        pth_violations = [v for v in results.violations if v.rule_id == "pth_annular_ring"]
        assert len(pth_violations) == 1
        assert results.rules_checked == 3

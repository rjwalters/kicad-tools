"""Tests for the via-in-pad DRC rule (validate/rules/via_in_pad.py).

Covers issue #2635 acceptance criteria:
- A board with a via fully inside an SMD pad on the same net is flagged
  when the profile has via_in_pad_supported=False.
- The same board produces no violation when via_in_pad_supported=True.
- Vias on a different net than the pad they overlap are NOT flagged
  (clearance rule handles those).
- Vias clearly outside any pad are not flagged.
- Through-hole pads are not treated as SMD pads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

# ---------------------------------------------------------------------------
# Minimal stubs sufficient for the rule (mirror the schema fields it reads)
# ---------------------------------------------------------------------------


@dataclass
class _StubPad:
    number: str = "1"
    type: str = "smd"
    position: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (1.0, 0.5)
    net_number: int = 1
    net_name: str = "DATA"


@dataclass
class _StubFootprint:
    reference: str = "U1"
    position: tuple[float, float] = (10.0, 10.0)
    rotation: float = 0.0
    pads: list[_StubPad] = field(default_factory=list)


@dataclass
class _StubVia:
    position: tuple[float, float] = (10.0, 10.0)
    drill: float = 0.3
    net_number: int = 1
    net_name: str = "DATA"
    uuid: str = "abcdef12"


@dataclass
class _StubPCB:
    footprints: list[_StubFootprint] = field(default_factory=list)
    vias: list[_StubVia] = field(default_factory=list)


@dataclass
class _StubDesignRules:
    via_in_pad_supported: bool = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestViaInPadRule:
    """Tests for ViaInPadRule.check()."""

    def test_via_inside_smd_pad_flagged_when_unsupported(self):
        """Via at the center of an SMD pad on the same net -> violation."""
        # Pad at footprint position (10, 10); pad local position (0,0) so
        # absolute pad center is (10, 10).  Via also at (10, 10).
        # Drill 0.3 fits inside a 1.0 x 0.5 pad easily.
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.3, net_number=1, net_name="DATA")
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))

        violations = [v for v in results.violations if v.rule_id == "via_in_pad"]
        assert len(violations) == 1, results.violations
        v = violations[0]
        assert v.severity == "error"
        assert v.location == (10.0, 10.0)
        assert v.items == ("Via-abcdef12", "U1-1")
        assert v.nets == ("DATA",)
        assert "U1-1" in v.message
        assert "via-in-pad" in v.message.lower()
        assert results.rules_checked == 1

    def test_via_inside_smd_pad_suppressed_when_supported(self):
        """Same board with via_in_pad_supported=True -> no violation."""
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.3, net_number=1, net_name="DATA")
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=True))

        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_via_on_different_net_not_flagged(self):
        """Via overlapping a pad on a DIFFERENT net is out of scope.

        The clearance rule handles inter-net pad-vs-via overlaps; this
        rule is specifically about via-in-pad on the same connected net.
        """
        pad = _StubPad(net_number=1, net_name="DATA")
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(net_number=2, net_name="CLK")  # different net
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_via_outside_pad_not_flagged(self):
        """Via clearly outside the pad bounding box -> no violation."""
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        # Pad spans approximately (9.5, 9.75) - (10.5, 10.25).
        # Place via well outside that box.
        via = _StubVia(position=(20.0, 20.0), drill=0.3, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_through_hole_pad_not_considered_smd(self):
        """Through-hole pads are not flagged -- a via "inside" them is fine."""
        # The pad geometry overlaps, but type=thru_hole so we skip it.
        pad = _StubPad(type="thru_hole", net_number=1, net_name="DATA", size=(1.0, 1.0))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.3, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_via_net_zero_not_flagged(self):
        """Unconnected vias (net 0) cannot have via-in-pad on a same-net pad."""
        pad = _StubPad(net_number=1, net_name="DATA")
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(net_number=0, net_name="")
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_pad_net_zero_not_considered(self):
        """Pads on net 0 (unconnected) are skipped.

        Even if a via at net 0 sits "inside" an unconnected pad, the
        same-net constraint excludes net 0 from both sides.
        """
        pad = _StubPad(net_number=0, net_name="")
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(net_number=0, net_name="")
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_via_partially_overlapping_pad_not_flagged(self):
        """Drill circle that pokes out of the pad edge is NOT in-pad.

        The router considers a via "in-pad" only when the drill is fully
        covered by the pad copper -- partial overlaps would be flagged
        by the clearance rule (if any) but not here.  The DRC tolerance
        gives a small grace zone for fabrication rounding.
        """
        # Pad is 1.0 wide x 0.5 tall.  Place the via at the edge so its
        # drill circle extends well past the pad.
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        # Pad spans x in [-0.5, 0.5] (relative to fp 10,10 -> [9.5, 10.5]).
        # Via at (10.5, 10.0) with drill 0.4 -> circle in x [10.3, 10.7],
        # which extends past the pad's max x of 10.5 by 0.2 mm (well
        # above the DRC tolerance of 0.005 mm).
        via = _StubVia(position=(10.5, 10.0), drill=0.4, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0

    def test_multiple_vias_in_same_pad_each_flagged(self):
        """Two distinct in-pad vias on the same pad produce two violations."""
        # Make a large pad so two vias both fit inside.
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(2.0, 2.0))
        fp = _StubFootprint(pads=[pad])
        via_a = _StubVia(position=(9.7, 10.0), drill=0.3, net_number=1, uuid="aaaa1111")
        via_b = _StubVia(position=(10.3, 10.0), drill=0.3, net_number=1, uuid="bbbb2222")
        pcb = _StubPCB(footprints=[fp], vias=[via_a, via_b])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        violations = [v for v in results.violations if v.rule_id == "via_in_pad"]
        assert len(violations) == 2
        via_refs = {v.items[0] for v in violations}
        assert via_refs == {"Via-aaaa1111", "Via-bbbb2222"}

    def test_rotated_footprint_pad_bbox_swapped(self):
        """A footprint rotated 90 deg swaps pad width/height when computing the bbox."""
        # Pad is 0.5 wide x 1.0 tall at local (0, 0).  Rotated 90 deg,
        # the pad in board coords becomes 1.0 wide x 0.5 tall.
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(0.5, 1.0))
        fp = _StubFootprint(pads=[pad], rotation=90.0)
        # Pad center is still at fp.position because local (0,0) rotates to (0,0).
        # Bbox at (10,10): x in [9.5, 10.5], y in [9.75, 10.25] after the swap.
        # A via at (10.4, 10.0) with drill 0.2 -> x in [10.3, 10.5] (inside)
        # and y in [9.9, 10.1] (inside).
        via = _StubVia(position=(10.4, 10.0), drill=0.2, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])

        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 1

    def test_rules_checked_counts_one(self):
        """Even when there are no vias, rules_checked is 1 (the rule ran)."""
        pcb = _StubPCB()
        results = ViaInPadRule().check(pcb, _StubDesignRules(via_in_pad_supported=False))
        assert results.rules_checked == 1


class TestViaInPadIntegration:
    """Integration tests via DRCChecker.check_via_in_pad()."""

    def test_drc_checker_exposes_check_via_in_pad(self):
        """DRCChecker should have a check_via_in_pad() method wired in."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.create(width=50.0, height=50.0, layers=2)
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        assert hasattr(checker, "check_via_in_pad")
        # On an empty board, the check should run without errors.
        results = checker.check_via_in_pad()
        assert results.rules_checked == 1
        assert len(results.violations) == 0

    def test_design_rules_via_in_pad_supported_default_false(self):
        """DesignRules default for via_in_pad_supported is False."""
        from kicad_tools.manufacturers import DesignRules

        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
        )
        assert rules.via_in_pad_supported is False

    def test_design_rules_to_dict_includes_via_in_pad_supported(self):
        """to_dict() should serialize the new field."""
        from kicad_tools.manufacturers import DesignRules

        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
            via_in_pad_supported=True,
        )
        d = rules.to_dict()
        assert "via_in_pad_supported" in d
        assert d["via_in_pad_supported"] is True

    def test_jlcpcb_tier1_yaml_loads_with_via_in_pad_supported(self):
        """The jlcpcb-tier1 YAML should produce DesignRules with the flag True."""
        from kicad_tools.manufacturers import get_profile

        profile = get_profile("jlcpcb-tier1")
        for layers in (2, 4, 6):
            rules = profile.get_design_rules(layers=layers, copper_oz=1.0)
            assert rules.via_in_pad_supported is True, (
                f"jlcpcb-tier1 ({layers}L) should have via_in_pad_supported=True"
            )

    def test_base_jlcpcb_yaml_via_in_pad_unsupported(self):
        """The base jlcpcb profile should have via_in_pad_supported=False."""
        from kicad_tools.manufacturers import get_profile

        profile = get_profile("jlcpcb")
        for layers in (2, 4, 6):
            rules = profile.get_design_rules(layers=layers, copper_oz=1.0)
            assert rules.via_in_pad_supported is False, (
                f"base jlcpcb ({layers}L) should have via_in_pad_supported=False"
            )

    def test_pcbway_yaml_via_in_pad_supported(self):
        """PCBWay should have via_in_pad_supported=True (matches router)."""
        from kicad_tools.manufacturers import get_profile

        profile = get_profile("pcbway")
        for layers in (2, 4, 6):
            rules = profile.get_design_rules(layers=layers, copper_oz=1.0)
            assert rules.via_in_pad_supported is True, (
                f"pcbway ({layers}L) should have via_in_pad_supported=True"
            )

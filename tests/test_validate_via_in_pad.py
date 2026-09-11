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
    drill: float = 0.0


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
    size: float = 0.6
    net_number: int = 1
    net_name: str = "DATA"
    uuid: str = "abcdef12"


@dataclass
class _StubPCB:
    footprints: list[_StubFootprint] = field(default_factory=list)
    vias: list[_StubVia] = field(default_factory=list)
    copper_layers: list = field(default_factory=lambda: ["F.Cu", "B.Cu"])


@dataclass
class _StubDesignRules:
    via_in_pad_supported: bool = False
    via_in_pad_process_id: str | None = None


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

    def test_via_partially_overlapping_pad_is_flagged(self):
        """A same-net drill crossing the pad edge still requires filled/capped vias."""
        pad = _StubPad(size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.5, 10.0), drill=0.4)
        pcb = _StubPCB(footprints=[fp], vias=[via])
        results = ViaInPadRule().check(pcb, _StubDesignRules())
        assert len(results.violations) == 1

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


class TestViaInPadProcessEligibility:
    """Issue #5009: via_in_pad_supported=True alone must not suppress a
    via-in-pad finding -- a real, eligible FabricationProcess must be
    declared and satisfied.
    """

    def test_missing_process_selection_fails_distinctly(self):
        """Synthetic two-layer 0.15mm via-in-SMT-land, supported=True, no process.

        Reproduces the original board04-style scenario (now fixed on
        board04 itself; this is the synthetic fixture the issue's own
        Verified Corrections note says is required) as a distinct
        ``via_in_pad_process_missing`` finding, NOT the base
        ``via_in_pad`` capability-absent finding.
        """
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.15, size=0.30, net_number=1)
        # Two-layer board (matches the default _StubPCB.copper_layers).
        pcb = _StubPCB(footprints=[fp], vias=[via])

        design_rules = _StubDesignRules(via_in_pad_supported=True, via_in_pad_process_id=None)
        results = ViaInPadRule().check(pcb, design_rules)

        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0
        missing = [v for v in results.violations if v.rule_id == "via_in_pad_process_missing"]
        assert len(missing) == 1, results.violations
        assert "via_in_pad_process_id" in missing[0].message
        assert results.rules_checked == 1

    def test_unrecognized_process_id_treated_as_missing(self):
        """An unrecognized ``via_in_pad_process_id`` behaves like "unset"."""
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.15, size=0.30, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])

        design_rules = _StubDesignRules(
            via_in_pad_supported=True, via_in_pad_process_id="not-a-real-process"
        )
        results = ViaInPadRule().check(pcb, design_rules)

        missing = [v for v in results.violations if v.rule_id == "via_in_pad_process_missing"]
        assert len(missing) == 1
        assert "not-a-real-process" in missing[0].message

    def test_declared_process_ineligible_geometry_fails_distinctly(self):
        """A declared process that the via's real geometry does not satisfy.

        The board only has 2 copper layers and a 0.15mm drill, both below
        the jlcpcb-tier1-pofv-4l process's floor (4 layers, 0.2mm min
        drill) -- this must fail as ``via_in_pad_process_ineligible``,
        distinct from "no process was declared at all".
        """
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.15, size=0.30, net_number=1)
        pcb = _StubPCB(footprints=[fp], vias=[via])  # default 2-layer copper_layers

        design_rules = _StubDesignRules(
            via_in_pad_supported=True, via_in_pad_process_id="jlcpcb-tier1-pofv-4l"
        )
        results = ViaInPadRule().check(pcb, design_rules)

        assert len([v for v in results.violations if v.rule_id == "via_in_pad"]) == 0
        assert (
            len([v for v in results.violations if v.rule_id == "via_in_pad_process_missing"]) == 0
        )
        ineligible = [v for v in results.violations if v.rule_id == "via_in_pad_process_ineligible"]
        assert len(ineligible) == 1, results.violations
        assert "jlcpcb-tier1-pofv-4l" in ineligible[0].message
        assert "0.15" in ineligible[0].message or "layer" in ineligible[0].message

    def test_valid_four_layer_pofv_construction_passes(self):
        """Positive control: a documented, eligible 4-layer POFV via.

        Mirrors boards/03-usb-joystick's reviewed four-layer
        filled-and-capped POFV contract: 4 copper layers, 0.2mm drill,
        0.45mm diameter (0.125mm ring), no nearby component holes.
        """
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        fp = _StubFootprint(pads=[pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.2, size=0.45, net_number=1)
        pcb = _StubPCB(
            footprints=[fp],
            vias=[via],
            copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        )

        design_rules = _StubDesignRules(
            via_in_pad_supported=True, via_in_pad_process_id="jlcpcb-tier1-pofv-4l"
        )
        results = ViaInPadRule().check(pcb, design_rules)

        assert results.violations == []
        assert results.rules_checked == 1

    def test_component_hole_too_close_fails_ineligible(self):
        """A nearby PTH hole closer than the process's floor is flagged."""
        pad = _StubPad(net_number=1, net_name="DATA", position=(0.0, 0.0), size=(1.0, 0.5))
        # A through-hole pad on a DIFFERENT footprint, very close to the via.
        pth_pad = _StubPad(
            number="1",
            type="thru_hole",
            net_number=2,
            net_name="GND",
            position=(0.0, 0.0),
            size=(1.0, 1.0),
            drill=0.3,
        )
        fp = _StubFootprint(pads=[pad])
        pth_fp = _StubFootprint(reference="J1", position=(10.2, 10.0), pads=[pth_pad])
        via = _StubVia(position=(10.0, 10.0), drill=0.2, size=0.45, net_number=1)
        pcb = _StubPCB(
            footprints=[fp, pth_fp],
            vias=[via],
            copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        )

        design_rules = _StubDesignRules(
            via_in_pad_supported=True, via_in_pad_process_id="jlcpcb-tier1-pofv-4l"
        )
        results = ViaInPadRule().check(pcb, design_rules)

        ineligible = [v for v in results.violations if v.rule_id == "via_in_pad_process_ineligible"]
        assert len(ineligible) == 1, results.violations
        assert "component hole" in ineligible[0].message


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


def test_partial_drill_detection_honors_round_pad_outline():
    """A drill can cross a round land, while one near its empty AABB corner is safe."""
    from kicad_tools.schema.pcb import Footprint, Pad

    pad = Pad("1", "smd", "circle", (0, 0), (1, 1), ["F.Cu"], net_number=1)
    fp = Footprint("test", "F.Cu", (10, 10), 0, "U1", "test", pads=[pad])
    pcb = _StubPCB(
        footprints=[fp],
        vias=[
            _StubVia(position=(10.55, 10), drill=0.2, uuid="overlap"),
            _StubVia(position=(10.5, 10.5), drill=0.2, uuid="corner"),
            _StubVia(position=(10.6, 10), drill=0.2, uuid="tangent"),
        ],
    )
    result = ViaInPadRule().check(pcb, _StubDesignRules())
    assert [v.items[0] for v in result.violations] == ["Via-overlap"]


def test_drill_overlap_uses_absolute_pad_angle():
    """A 90-degree pad in an unrotated footprint must use its own copper angle."""
    from kicad_tools.schema.pcb import Footprint, Pad

    pad = Pad("1", "smd", "rect", (0, 0), (1, 0.25), ["F.Cu"], net_number=1, rotation=90)
    fp = Footprint("test", "F.Cu", (10, 10), 0, "U1", "test", pads=[pad])
    pcb = _StubPCB(
        footprints=[fp],
        vias=[
            _StubVia(position=(10, 10.55), drill=0.2, uuid="overlap"),
            _StubVia(position=(10.55, 10), drill=0.2, uuid="outside"),
        ],
    )
    result = ViaInPadRule().check(pcb, _StubDesignRules())
    assert [v.items[0] for v in result.violations] == ["Via-overlap"]

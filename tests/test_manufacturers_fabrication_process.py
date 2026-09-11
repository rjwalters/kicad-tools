"""Tests for the fabrication-process model (issue #5009).

Covers:
- FabricationProcess.to_dict() / ordering_instructions() shape.
- get_fabrication_process() lookup semantics (None/unknown -> None).
- describe_selection() export-ready binding.
- DesignRules.via_in_pad_process_id default/serialization.
- jlcpcb-tier1 YAML: 4+ layer configs carry the POFV process id, 2-layer
  configs deliberately do not (the bug this issue closes).
- pcbway YAML: every layer config carries its via-in-pad process id.
"""

from __future__ import annotations

from kicad_tools.manufacturers import DesignRules, get_profile
from kicad_tools.manufacturers.fabrication_process import (
    FABRICATION_PROCESSES,
    JLCPCB_TIER1_POFV_4L,
    PCBWAY_VIA_IN_PAD,
    describe_selection,
    get_fabrication_process,
)


class TestFabricationProcess:
    def test_to_dict_contains_all_fields(self):
        data = JLCPCB_TIER1_POFV_4L.to_dict()
        assert data["process_id"] == "jlcpcb-tier1-pofv-4l"
        assert data["min_layer_count"] == 4
        assert data["min_via_drill_mm"] == 0.2
        assert data["max_via_drill_mm"] == 0.5
        assert data["requires_filled_and_capped"] is True
        assert "source" in data

    def test_ordering_instructions_mentions_key_parameters(self):
        text = JLCPCB_TIER1_POFV_4L.ordering_instructions()
        assert "4" in text
        assert "0.2" in text
        assert "0.5" in text
        assert "epoxy-filled" in text.lower()
        assert JLCPCB_TIER1_POFV_4L.source in text

    def test_registry_contains_known_processes(self):
        assert "jlcpcb-tier1-pofv-4l" in FABRICATION_PROCESSES
        assert "pcbway-via-in-pad" in FABRICATION_PROCESSES
        assert FABRICATION_PROCESSES["pcbway-via-in-pad"] is PCBWAY_VIA_IN_PAD


class TestGetFabricationProcess:
    def test_none_id_returns_none(self):
        assert get_fabrication_process(None) is None

    def test_empty_id_returns_none(self):
        assert get_fabrication_process("") is None

    def test_unknown_id_returns_none(self):
        assert get_fabrication_process("not-a-real-process") is None

    def test_known_id_returns_process(self):
        process = get_fabrication_process("jlcpcb-tier1-pofv-4l")
        assert process is JLCPCB_TIER1_POFV_4L


class TestDescribeSelection:
    def test_no_process_id_returns_none(self):
        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
        )
        assert describe_selection(rules) is None

    def test_unknown_process_id_returns_none(self):
        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
            via_in_pad_process_id="bogus",
        )
        assert describe_selection(rules) is None

    def test_known_process_id_returns_dict_with_instructions(self):
        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
            via_in_pad_process_id="jlcpcb-tier1-pofv-4l",
        )
        data = describe_selection(rules)
        assert data is not None
        assert data["process_id"] == "jlcpcb-tier1-pofv-4l"
        assert "ordering_instructions" in data

    def test_object_without_attribute_returns_none(self):
        class _NoAttr:
            pass

        assert describe_selection(_NoAttr()) is None


class TestDesignRulesViaInPadProcessId:
    def test_default_is_none(self):
        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
        )
        assert rules.via_in_pad_process_id is None

    def test_to_dict_includes_field(self):
        rules = DesignRules(
            min_trace_width_mm=0.1,
            min_clearance_mm=0.1,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.15,
            via_in_pad_process_id="jlcpcb-tier1-pofv-4l",
        )
        d = rules.to_dict()
        assert d["via_in_pad_process_id"] == "jlcpcb-tier1-pofv-4l"


class TestJlcpcbTier1ProcessAttachment:
    """Issue #5009: only 4+ layer jlcpcb-tier1 configs carry a real process."""

    def test_two_layer_configs_have_no_process(self):
        profile = get_profile("jlcpcb-tier1")
        for oz in (1.0, 2.0):
            rules = profile.get_design_rules(layers=2, copper_oz=oz)
            assert rules.via_in_pad_supported is True
            assert rules.via_in_pad_process_id is None, (
                f"jlcpcb-tier1 2-layer {oz}oz should have NO via-in-pad process "
                "(JLCPCB's POFV process requires 4+ layers)"
            )

    def test_four_and_six_layer_configs_have_pofv_process(self):
        profile = get_profile("jlcpcb-tier1")
        for layers, oz in [(4, 1.0), (4, 2.0), (6, 1.0)]:
            rules = profile.get_design_rules(layers=layers, copper_oz=oz)
            assert rules.via_in_pad_supported is True
            assert rules.via_in_pad_process_id == "jlcpcb-tier1-pofv-4l", (
                f"jlcpcb-tier1 {layers}-layer {oz}oz should carry the POFV process id"
            )


class TestPcbwayProcessAttachment:
    def test_every_layer_config_has_via_in_pad_process(self):
        profile = get_profile("pcbway")
        for layers in (2, 4, 6):
            rules = profile.get_design_rules(layers=layers, copper_oz=1.0)
            assert rules.via_in_pad_supported is True
            assert rules.via_in_pad_process_id == "pcbway-via-in-pad"

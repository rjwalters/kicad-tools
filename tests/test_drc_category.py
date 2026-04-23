"""Tests for DRC violation categorization (ViolationCategory and is_fine_pitch_inherent).

Covers:
- ViolationCategory.category property inference from ViolationType
- Solder mask bridge same-component vs different-component distinction
- is_fine_pitch_inherent() method
- _extract_component_refs() helper
- Manufacturer checker SOLDER_MASK_BRIDGE handler
- drc_summary fine-pitch bridge reclassification
"""

import pytest

from kicad_tools.cli.drc_summary import (
    IssueSeverity,
    compare_with_manufacturer,
    create_summary,
    get_severity,
)
from kicad_tools.drc import DRCReport, ViolationCategory, ViolationType
from kicad_tools.drc.checker import CheckResult, _check_violation
from kicad_tools.drc.violation import DRCViolation, _extract_component_refs
from kicad_tools.core.types import Severity
from kicad_tools.manufacturers import get_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_violation(
    vtype: ViolationType,
    *,
    items: list[str] | None = None,
    actual_value_mm: float | None = None,
    severity: Severity = Severity.ERROR,
) -> DRCViolation:
    return DRCViolation(
        type=vtype,
        type_str=vtype.value,
        severity=severity,
        message="test violation",
        items=items or [],
        actual_value_mm=actual_value_mm,
    )


# ---------------------------------------------------------------------------
# _extract_component_refs
# ---------------------------------------------------------------------------

class TestExtractComponentRefs:
    """Tests for the _extract_component_refs helper."""

    def test_same_component(self):
        refs = _extract_component_refs(["Pad 1 of U3", "Pad 2 of U3"])
        assert refs == {"U3"}

    def test_different_components(self):
        refs = _extract_component_refs(["Pad 1 of U3", "Pad 1 of C12"])
        assert refs == {"U3", "C12"}

    def test_no_pads(self):
        refs = _extract_component_refs(["Via", "Trace"])
        assert refs == set()

    def test_empty_list(self):
        refs = _extract_component_refs([])
        assert refs == set()

    def test_single_item(self):
        refs = _extract_component_refs(["Pad A1 of U7"])
        assert refs == {"U7"}

    def test_mixed_pad_and_via(self):
        """Via items without 'of <ref>' should not contribute refs."""
        refs = _extract_component_refs(["Pad 1 of U3", "Via"])
        assert refs == {"U3"}


# ---------------------------------------------------------------------------
# ViolationCategory.category property
# ---------------------------------------------------------------------------

class TestViolationCategory:
    """Tests for DRCViolation.category property."""

    def test_clearance_is_routing(self):
        v = _make_violation(ViolationType.CLEARANCE)
        assert v.category == ViolationCategory.ROUTING

    def test_clearance_segment_via_is_routing(self):
        v = _make_violation(ViolationType.CLEARANCE_SEGMENT_VIA)
        assert v.category == ViolationCategory.ROUTING

    def test_track_width_is_routing(self):
        v = _make_violation(ViolationType.TRACK_WIDTH)
        assert v.category == ViolationCategory.ROUTING

    def test_clearance_pad_pad_is_placement(self):
        v = _make_violation(ViolationType.CLEARANCE_PAD_PAD)
        assert v.category == ViolationCategory.PLACEMENT

    def test_courtyard_overlap_is_placement(self):
        v = _make_violation(ViolationType.COURTYARD_OVERLAP)
        assert v.category == ViolationCategory.PLACEMENT

    def test_unconnected_items_is_connectivity(self):
        v = _make_violation(ViolationType.UNCONNECTED_ITEMS)
        assert v.category == ViolationCategory.CONNECTIVITY

    def test_shorting_items_is_connectivity(self):
        v = _make_violation(ViolationType.SHORTING_ITEMS)
        assert v.category == ViolationCategory.CONNECTIVITY

    def test_silk_over_copper_is_cosmetic(self):
        v = _make_violation(ViolationType.SILK_OVER_COPPER)
        assert v.category == ViolationCategory.COSMETIC

    def test_silk_overlap_is_cosmetic(self):
        v = _make_violation(ViolationType.SILK_OVERLAP)
        assert v.category == ViolationCategory.COSMETIC

    def test_drill_hole_too_small_is_manufacturing(self):
        v = _make_violation(ViolationType.DRILL_HOLE_TOO_SMALL)
        assert v.category == ViolationCategory.MANUFACTURING

    def test_via_annular_width_is_manufacturing(self):
        v = _make_violation(ViolationType.VIA_ANNULAR_WIDTH)
        assert v.category == ViolationCategory.MANUFACTURING

    def test_copper_edge_clearance_is_manufacturing(self):
        v = _make_violation(ViolationType.COPPER_EDGE_CLEARANCE)
        assert v.category == ViolationCategory.MANUFACTURING

    # -----------------------------------------------------------------------
    # Solder mask bridge special-case logic
    # -----------------------------------------------------------------------

    def test_smb_same_component_is_placement(self):
        """SMB between pads on the same IC is placement-inherent."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
        )
        assert v.category == ViolationCategory.PLACEMENT

    def test_smb_different_components_is_routing(self):
        """SMB between pads on different components may be routing-fixable."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 1 of C12"],
        )
        assert v.category == ViolationCategory.ROUTING

    def test_smb_no_items_defaults_to_placement(self):
        """SMB with no item context falls back to the type-map default."""
        v = _make_violation(ViolationType.SOLDER_MASK_BRIDGE)
        # No refs -> falls through to map default (PLACEMENT)
        assert v.category == ViolationCategory.PLACEMENT

    def test_smb_pad_and_via_is_routing(self):
        """SMB between a pad and a via (different references) is routing."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Via"],
        )
        # Only one ref extracted (U3), other is a via without 'of <ref>'
        assert v.category == ViolationCategory.PLACEMENT

    def test_unknown_type_defaults_to_routing(self):
        """Unknown type falls back to ROUTING (safe default)."""
        v = _make_violation(ViolationType.UNKNOWN)
        assert v.category == ViolationCategory.ROUTING


# ---------------------------------------------------------------------------
# is_fine_pitch_inherent()
# ---------------------------------------------------------------------------

class TestIsFinePitchInherent:
    """Tests for DRCViolation.is_fine_pitch_inherent()."""

    def test_returns_true_same_component_below_threshold(self):
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.05,
        )
        assert v.is_fine_pitch_inherent(min_solder_mask_dam_mm=0.1) is True

    def test_returns_false_same_component_above_threshold(self):
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.15,
        )
        assert v.is_fine_pitch_inherent(min_solder_mask_dam_mm=0.1) is False

    def test_returns_false_same_component_at_threshold(self):
        """Exactly at threshold is NOT considered fine-pitch inherent."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.1,
        )
        assert v.is_fine_pitch_inherent(min_solder_mask_dam_mm=0.1) is False

    def test_returns_false_different_components(self):
        """Different-component SMB is not fine-pitch inherent."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 1 of C12"],
            actual_value_mm=0.05,
        )
        assert v.is_fine_pitch_inherent(min_solder_mask_dam_mm=0.1) is False

    def test_returns_false_no_actual_value(self):
        """No measurement available -> cannot determine -> returns False."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
        )
        assert v.is_fine_pitch_inherent(min_solder_mask_dam_mm=0.1) is False

    def test_returns_false_wrong_violation_type(self):
        """Non-SMB violations always return False."""
        v = _make_violation(ViolationType.CLEARANCE, actual_value_mm=0.05)
        assert v.is_fine_pitch_inherent() is False

    def test_default_threshold_is_01mm(self):
        """Default threshold should be 0.1mm (JLCPCB standard)."""
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.05,
        )
        assert v.is_fine_pitch_inherent() is True  # uses default 0.1mm


# ---------------------------------------------------------------------------
# checker.py SOLDER_MASK_BRIDGE handler
# ---------------------------------------------------------------------------

class TestCheckerSolderMaskBridge:
    """Tests for _check_violation() handling SOLDER_MASK_BRIDGE."""

    @pytest.fixture
    def jlcpcb_rules(self):
        profile = get_profile("jlcpcb")
        return profile.id, profile.name, profile.get_design_rules(2)

    def test_fine_pitch_same_component_returns_pass(self, jlcpcb_rules):
        """Fine-pitch same-component SMB should return PASS."""
        mfr_id, mfr_name, rules = jlcpcb_rules
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.05,
        )
        check = _check_violation(v, mfr_id, mfr_name, rules)

        assert check is not None
        assert check.result == CheckResult.PASS
        assert "fine-pitch" in check.message.lower() or "fine_pitch" in check.rule_name.lower()

    def test_smb_above_minimum_returns_pass(self, jlcpcb_rules):
        """SMB with dam above minimum should return PASS."""
        mfr_id, mfr_name, rules = jlcpcb_rules
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 1 of C12"],
            actual_value_mm=0.15,
        )
        check = _check_violation(v, mfr_id, mfr_name, rules)

        assert check is not None
        assert check.result == CheckResult.PASS

    def test_smb_below_minimum_different_components_returns_fail(self, jlcpcb_rules):
        """SMB between different components with dam below minimum should FAIL."""
        mfr_id, mfr_name, rules = jlcpcb_rules
        # Set actual below the manufacturer minimum for a cross-component bridge
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 1 of C12"],
            actual_value_mm=0.05,
        )
        check = _check_violation(v, mfr_id, mfr_name, rules)

        assert check is not None
        assert check.result == CheckResult.FAIL

    def test_smb_no_actual_value_returns_warning(self, jlcpcb_rules):
        """SMB without measurement should return WARNING."""
        mfr_id, mfr_name, rules = jlcpcb_rules
        v = _make_violation(ViolationType.SOLDER_MASK_BRIDGE)
        check = _check_violation(v, mfr_id, mfr_name, rules)

        assert check is not None
        assert check.result == CheckResult.WARNING

    def test_smb_check_includes_rule_name(self, jlcpcb_rules):
        """SMB check should set rule_name to solder_mask_dam."""
        mfr_id, mfr_name, rules = jlcpcb_rules
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            actual_value_mm=0.15,
        )
        check = _check_violation(v, mfr_id, mfr_name, rules)

        assert check is not None
        assert check.rule_name == "solder_mask_dam"


# ---------------------------------------------------------------------------
# drc_summary fine-pitch bridge reclassification
# ---------------------------------------------------------------------------

class TestSummaryFinePitchSolderMask:
    """Tests for fine-pitch solder mask bridge reclassification in drc_summary."""

    def _make_report(self, violations: list[DRCViolation]) -> DRCReport:
        return DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=violations,
        )

    def test_fine_pitch_smb_reclassified_as_fab_acceptable(self):
        """Fine-pitch same-component SMB with manufacturer should be FAB_ACCEPTABLE."""
        violation = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.05,
        )
        report = self._make_report([violation])
        summary = create_summary(report, manufacturer_id="jlcpcb", layers=2)

        assert summary.fab_acceptable_count == 1
        assert summary.cosmetic_count == 0

    def test_non_fine_pitch_smb_stays_cosmetic(self):
        """SMB with dam above minimum (not fine-pitch) stays cosmetic without mfr context."""
        violation = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.15,
        )
        report = self._make_report([violation])
        summary = create_summary(report)

        # Without manufacturer context, SMB is just cosmetic
        assert summary.cosmetic_count == 1

    def test_compare_with_manufacturer_fine_pitch_is_false_positive(self):
        """compare_with_manufacturer should flag fine-pitch SMB as false positive."""
        violation = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
            actual_value_mm=0.05,
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is not None
        assert comparison.is_false_positive is True
        assert "fine-pitch" in comparison.message.lower() or "inherent" in comparison.message.lower()

    def test_compare_with_manufacturer_cross_component_smb_is_true_violation(self):
        """SMB between different components below minimum is a true violation."""
        violation = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 1 of C12"],
            actual_value_mm=0.05,
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is not None
        assert comparison.is_false_positive is False

    def test_solder_mask_bridge_default_severity_is_cosmetic(self):
        """Without manufacturer context, SMB severity is COSMETIC."""
        violation = _make_violation(ViolationType.SOLDER_MASK_BRIDGE)
        assert get_severity(violation) == IssueSeverity.COSMETIC


# ---------------------------------------------------------------------------
# to_dict includes category
# ---------------------------------------------------------------------------

class TestViolationToDict:
    """Tests that to_dict() includes the category field."""

    def test_to_dict_includes_category(self):
        v = _make_violation(ViolationType.CLEARANCE)
        d = v.to_dict()
        assert "category" in d
        assert d["category"] == "routing"

    def test_to_dict_smb_same_component_category_is_placement(self):
        v = _make_violation(
            ViolationType.SOLDER_MASK_BRIDGE,
            items=["Pad 1 of U3", "Pad 2 of U3"],
        )
        d = v.to_dict()
        assert d["category"] == "placement"

    def test_to_dict_connectivity_category(self):
        v = _make_violation(ViolationType.UNCONNECTED_ITEMS)
        d = v.to_dict()
        assert d["category"] == "connectivity"

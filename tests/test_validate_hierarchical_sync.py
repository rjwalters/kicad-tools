"""Regression tests for hierarchical schematic support in validate --sync/--consistency.

Reproduces and guards against issue #2625, where ``NetlistValidator`` and
``SchematicPCBChecker`` enumerated only the root sheet's symbols and
therefore reported every PCB footprint placed in a sub-sheet as an
orphan / extra component.

The fix is to use ``extract_bom(..., hierarchical=True)`` so all
sub-sheets are walked, mirroring the behaviour of ``pcb sync-netlist``.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from kicad_tools.validate.consistency import SchematicPCBChecker
from kicad_tools.validate.netlist import NetlistValidator

# Fixture root: tests/fixtures/hierarchical/root.kicad_sch references
#   sub_a.kicad_sch (R2, C1) -> nested.kicad_sch (C2)
#   sub_b.kicad_sch (R3, R4)
# and contains R1 itself at the root.
HIERARCHICAL_ROOT_SCH = Path(__file__).parent / "fixtures" / "hierarchical" / "root.kicad_sch"

# PCB with every component that lives anywhere in the hierarchical
# schematic. Refs match: R1 (root), R2/C1 (sub_a), C2 (sub_a/nested),
# R3/R4 (sub_b). When the validator walks hierarchically every footprint
# below should match a schematic symbol -> 0 orphans, 0 extras.
HIERARCHICAL_PCB_MATCHING = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r1") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r2") (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r2-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-c1") (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu") (uuid "fp-c2") (at 130 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c2-ref"))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r3") (at 140 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r3-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r4") (at 150 100)
    (property "Reference" "R4" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r4-ref"))
    (property "Value" "2.2k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r4-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
)
"""

# PCB missing one of the sub-sheet footprints (C1, which lives in
# sub_a.kicad_sch). After the fix the validator should report exactly
# one ``missing_on_pcb`` issue -- proving the hierarchical walker is
# actually being exercised rather than silently disabled.
HIERARCHICAL_PCB_MISSING_C1 = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r1") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r2") (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r2-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu") (uuid "fp-c2") (at 130 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c2-ref"))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r3") (at 140 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r3-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r4") (at 150 100)
    (property "Reference" "R4" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r4-ref"))
    (property "Value" "2.2k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r4-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu"))
  )
)
"""


@pytest.fixture
def hierarchical_matching_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hierarchical_match.kicad_pcb"
    pcb_file.write_text(HIERARCHICAL_PCB_MATCHING)
    return pcb_file


@pytest.fixture
def hierarchical_missing_c1_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hierarchical_missing_c1.kicad_pcb"
    pcb_file.write_text(HIERARCHICAL_PCB_MISSING_C1)
    return pcb_file


class TestNetlistValidatorHierarchical:
    """Regression: NetlistValidator must walk sub-sheets (issue #2625)."""

    def test_in_sync_hierarchical_reports_zero_orphans(
        self, hierarchical_matching_pcb: Path
    ) -> None:
        """An in-sync hierarchical project must report 0 orphaned footprints.

        Before the fix this returned every sub-sheet footprint (R2, C1,
        C2, R3, R4 -- 5 of 6) as orphaned because ``self.schematic.symbols``
        on a hierarchical root contained only R1.
        """
        validator = NetlistValidator(
            HIERARCHICAL_ROOT_SCH,
            hierarchical_matching_pcb,
        )
        result = validator.validate()

        # Every footprint maps to a sub-sheet symbol -> no orphans.
        assert result.orphaned_on_pcb == [], (
            "Hierarchical sub-sheet footprints reported as orphans; "
            f"saw {[i.reference for i in result.orphaned_on_pcb]}"
        )
        # And nothing is missing on the PCB.
        assert result.missing_on_pcb == [], (
            f"Unexpected missing-on-PCB: {[i.reference for i in result.missing_on_pcb]}"
        )

    def test_missing_subsheet_symbol_still_detected(
        self, hierarchical_missing_c1_pcb: Path
    ) -> None:
        """Deleting one sub-sheet footprint must produce exactly one missing issue.

        Proves the hierarchical walker is *actually* being exercised --
        if it silently fell back to root-only enumeration, removing C1
        (which lives in sub_a) would be invisible to the validator.
        """
        validator = NetlistValidator(
            HIERARCHICAL_ROOT_SCH,
            hierarchical_missing_c1_pcb,
        )
        result = validator.validate()

        missing_refs = sorted(i.reference for i in result.missing_on_pcb)
        assert missing_refs == ["C1"], (
            f"Expected exactly C1 missing on PCB, got {missing_refs}. "
            "If the list is empty the hierarchical walker is bypassed; "
            "if R2/C2/R3/R4 also appear the matcher is broken."
        )
        # No spurious orphans either.
        assert result.orphaned_on_pcb == []

    def test_schematic_object_input_warns_on_hierarchical_root(
        self, hierarchical_matching_pcb: Path
    ) -> None:
        """Passing a Schematic *object* on a hierarchical project warns.

        We cannot walk sub-sheets without a path, so the caller must be
        warned that they are getting the legacy root-only behaviour.
        """
        from kicad_tools.schema.schematic import Schematic

        sch = Schematic.load(HIERARCHICAL_ROOT_SCH)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            validator = NetlistValidator(sch, hierarchical_matching_pcb)
            validator.validate()

        runtime_warns = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert runtime_warns, (
            "Expected a RuntimeWarning when NetlistValidator is constructed "
            "with a Schematic object on a hierarchical schematic."
        )
        assert any("hierarchical" in str(w.message).lower() for w in runtime_warns)


class TestSchematicPCBCheckerHierarchical:
    """Regression: SchematicPCBChecker must walk sub-sheets (issue #2625)."""

    def test_consistent_hierarchical_reports_no_component_issues(
        self, hierarchical_matching_pcb: Path
    ) -> None:
        """An in-sync hierarchical project must show 0 component issues."""
        checker = SchematicPCBChecker(
            HIERARCHICAL_ROOT_SCH,
            hierarchical_matching_pcb,
        )
        result = checker.check()

        missing = [i for i in result.component_issues if i.issue_type == "missing"]
        extra = [i for i in result.component_issues if i.issue_type == "extra"]
        assert missing == [], (
            f"Hierarchical sub-sheet symbols reported as missing: {[i.reference for i in missing]}"
        )
        assert extra == [], (
            f"PCB footprints from sub-sheets reported as extra: {[i.reference for i in extra]}"
        )

    def test_missing_subsheet_symbol_still_detected(
        self, hierarchical_missing_c1_pcb: Path
    ) -> None:
        """Removing one sub-sheet footprint must still surface as missing."""
        checker = SchematicPCBChecker(
            HIERARCHICAL_ROOT_SCH,
            hierarchical_missing_c1_pcb,
        )
        result = checker.check()

        missing_refs = sorted(
            i.reference for i in result.component_issues if i.issue_type == "missing"
        )
        assert missing_refs == ["C1"], f"Expected exactly C1 missing, got {missing_refs}"
        extra_refs = sorted(i.reference for i in result.component_issues if i.issue_type == "extra")
        assert extra_refs == []

    def test_schematic_object_input_warns_on_hierarchical_root(
        self, hierarchical_matching_pcb: Path
    ) -> None:
        """Passing a Schematic object on a hierarchical project warns."""
        from kicad_tools.schema.schematic import Schematic

        sch = Schematic.load(HIERARCHICAL_ROOT_SCH)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            checker = SchematicPCBChecker(sch, hierarchical_matching_pcb)
            checker.check()

        runtime_warns = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert runtime_warns, (
            "Expected a RuntimeWarning when SchematicPCBChecker is "
            "constructed with a Schematic object on a hierarchical schematic."
        )

"""Regression tests for hierarchical net checks in validate --consistency.

Reproduces and guards against issue #2633, where
``SchematicPCBChecker._check_nets`` called
``_extract_schematic_pin_nets`` which loaded the schematic via the models
layer and invoked ``extract_netlist()`` -- a root-only walker. On a
hierarchical design every sub-sheet pin was absent from the resulting
pin-net map, so net-name mismatches on sub-sheet pads were silently
skipped by the ``sch_refs & pcb_refs`` intersection.

The fix is to opt ``_extract_schematic_pin_nets`` into the new
``extract_netlist(hierarchical=True)`` mode, which delegates to the
existing hierarchical walker in
``kicad_tools.operations.netlist._collect_hierarchy_components`` and
merges sub-sheet nets, with sheet-pin / hierarchical-label connections
resolving to the parent's net name.

The fixture tree under ``tests/fixtures/hierarchical/`` is reused:

    root.kicad_sch
        R1 (root)
        + sub_a.kicad_sch  -> R2, C1
            + nested.kicad_sch  -> C2
        + sub_b.kicad_sch  -> R3, R4
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schematic.models import Schematic as ModelsSchematic
from kicad_tools.validate.consistency import SchematicPCBChecker

HIERARCHICAL_ROOT_SCH = Path(__file__).parent / "fixtures" / "hierarchical" / "root.kicad_sch"


# PCB matching the hierarchical schematic, with pad ``net_name`` values
# chosen to AGREE with what extract_netlist(hierarchical=True) produces
# for every component (root + every sub-sheet). On the fixtures this is
# the auto-generated Net-(<ref>-<pin>) form for sub-sheet pins and the
# power-symbol names ("VCC", "GND") for the root R1 pins.
HIERARCHICAL_PCB_MATCHING_NETS = """(kicad_pcb
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
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "Net-(R2-1)")
  (net 4 "Net-(R2-2)")
  (net 5 "Net-(C1-1)")
  (net 6 "Net-(C1-2)")
  (net 7 "Net-(C2-1)")
  (net 8 "Net-(C2-2)")
  (net 9 "Net-(R3-1)")
  (net 10 "Net-(R3-2)")
  (net 11 "Net-(R4-1)")
  (net 12 "Net-(R4-2)")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r1") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r2") (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r2-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "Net-(R2-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 4 "Net-(R2-2)"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-c1") (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 5 "Net-(C1-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 6 "Net-(C1-2)"))
  )
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu") (uuid "fp-c2") (at 130 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c2-ref"))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 7 "Net-(C2-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 8 "Net-(C2-2)"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r3") (at 140 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r3-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 9 "Net-(R3-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 10 "Net-(R3-2)"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r4") (at 150 100)
    (property "Reference" "R4" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r4-ref"))
    (property "Value" "2.2k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r4-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 11 "Net-(R4-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 12 "Net-(R4-2)"))
  )
)
"""


# Same PCB as above, but R2's pad 1 has been deliberately rewired to a
# bogus net name ("WRONG_NET"). On a hierarchical-aware checker this
# should produce exactly one ``domain="net"`` mismatch for "R2.1".
# Pre-fix the checker missed this entirely because R2 is in sub_a and
# the root-only extract_netlist() never returned an R2 entry.
HIERARCHICAL_PCB_BAD_SUBSHEET_NET = """(kicad_pcb
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
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "WRONG_NET")
  (net 4 "Net-(R2-2)")
  (net 5 "Net-(C1-1)")
  (net 6 "Net-(C1-2)")
  (net 7 "Net-(C2-1)")
  (net 8 "Net-(C2-2)")
  (net 9 "Net-(R3-1)")
  (net 10 "Net-(R3-2)")
  (net 11 "Net-(R4-1)")
  (net 12 "Net-(R4-2)")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r1") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r2") (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r2-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "WRONG_NET"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 4 "Net-(R2-2)"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-c1") (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c1-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 5 "Net-(C1-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 6 "Net-(C1-2)"))
  )
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu") (uuid "fp-c2") (at 130 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-c2-ref"))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab") (uuid "p-c2-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 7 "Net-(C2-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 8 "Net-(C2-2)"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r3") (at 140 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r3-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 9 "Net-(R3-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 10 "Net-(R3-2)"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "fp-r4") (at 150 100)
    (property "Reference" "R4" (at 0 -1.5 0) (layer "F.SilkS") (uuid "p-r4-ref"))
    (property "Value" "2.2k" (at 0 1.5 0) (layer "F.Fab") (uuid "p-r4-val"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 11 "Net-(R4-1)"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 12 "Net-(R4-2)"))
  )
)
"""


@pytest.fixture
def hierarchical_matching_nets_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hierarchical_match_nets.kicad_pcb"
    pcb_file.write_text(HIERARCHICAL_PCB_MATCHING_NETS)
    return pcb_file


@pytest.fixture
def hierarchical_bad_subsheet_net_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hierarchical_bad_subsheet_net.kicad_pcb"
    pcb_file.write_text(HIERARCHICAL_PCB_BAD_SUBSHEET_NET)
    return pcb_file


class TestExtractNetlistHierarchical:
    """Direct tests of the new hierarchical=True flag on extract_netlist()."""

    def test_default_is_root_only(self) -> None:
        """Default behaviour (no kwarg) is unchanged: root sheet only.

        The fixture root contains R1 + VCC/GND power symbols and one
        global label ("DATA_BUS"). Only R1's two pins should appear; no
        sub-sheet refs (R2, C1, C2, R3, R4).
        """
        sch = ModelsSchematic.load(HIERARCHICAL_ROOT_SCH)
        netlist = sch.extract_netlist()
        refs = {pin.symbol_ref for pins in netlist.values() for pin in pins}
        assert refs == {"R1"}, f"Root-only extract_netlist() returned unexpected refs: {refs}"

    def test_hierarchical_walks_every_subsheet(self) -> None:
        """``hierarchical=True`` returns pins from every sub-sheet.

        Acceptance criterion from issue #2633: six components total
        across the hierarchy -- R1 (root), R2 + C1 (sub_a), C2
        (sub_a/nested), R3 + R4 (sub_b).
        """
        sch = ModelsSchematic.load(HIERARCHICAL_ROOT_SCH)
        netlist = sch.extract_netlist(hierarchical=True)
        refs = {pin.symbol_ref for pins in netlist.values() for pin in pins}
        assert refs == {"R1", "R2", "C1", "C2", "R3", "R4"}, (
            f"Hierarchical extract_netlist() missed sub-sheet pins: {refs}"
        )

    def test_hierarchical_requires_loaded_path(self) -> None:
        """``hierarchical=True`` on an unsaved Schematic raises ValueError.

        Without a source path there is no way to walk relative sheet
        references, so the caller is told explicitly rather than
        silently falling back to root-only.
        """
        sch = ModelsSchematic("Test")
        with pytest.raises(ValueError, match=r"hierarchical=True\)? +requires"):
            sch.extract_netlist(hierarchical=True)


class TestExtractSchematicPinNetsHierarchical:
    """Regression: _extract_schematic_pin_nets() must walk sub-sheets."""

    def test_pin_nets_include_every_subsheet_ref(
        self, hierarchical_matching_nets_pcb: Path
    ) -> None:
        """Internal helper returns entries for every component in the hierarchy.

        Pre-#2633 this dict contained only R1 (the single root-sheet
        symbol). Post-fix every sub-sheet ref is present.
        """
        checker = SchematicPCBChecker(HIERARCHICAL_ROOT_SCH, hierarchical_matching_nets_pcb)
        pin_nets = checker._extract_schematic_pin_nets()
        assert set(pin_nets.keys()) == {"R1", "R2", "C1", "C2", "R3", "R4"}, (
            f"_extract_schematic_pin_nets() missed sub-sheet refs: {sorted(pin_nets.keys())}"
        )
        # Every component should have both pins represented.
        for ref, pins in pin_nets.items():
            assert set(pins.keys()) == {"1", "2"}, (
                f"{ref} pin coverage incomplete: {sorted(pins.keys())}"
            )


class TestCheckNetsHierarchical:
    """Regression: SchematicPCBChecker._check_nets must catch sub-sheet mismatches.

    This is the end-to-end behaviour exposed via
    ``kct validate --consistency`` and is the user-facing bug from
    issue #2633.
    """

    @pytest.mark.xfail(
        reason="fixture wires drawn at pre-#738 rotation pin positions -- see issue #3518",
        strict=False,
    )
    def test_in_sync_hierarchical_reports_no_net_issues(
        self, hierarchical_matching_nets_pcb: Path
    ) -> None:
        """A hierarchical PCB whose pad nets match the schematic emits no net issues."""
        checker = SchematicPCBChecker(HIERARCHICAL_ROOT_SCH, hierarchical_matching_nets_pcb)
        result = checker.check()
        net_issues = result.net_issues
        assert net_issues == [], (
            "In-sync hierarchical net check reported spurious mismatches: "
            f"{[(i.reference, i.schematic_value, i.pcb_value) for i in net_issues]}"
        )

    @pytest.mark.xfail(
        reason="fixture wires drawn at pre-#738 rotation pin positions -- see issue #3518",
        strict=False,
    )
    def test_subsheet_net_mismatch_is_detected(
        self, hierarchical_bad_subsheet_net_pcb: Path
    ) -> None:
        """Deliberately wrong net on a SUB-SHEET pad must be surfaced.

        This is the exact scenario the issue #2633 curator notes called
        out: R2 lives in sub_a.kicad_sch, so pre-fix it was never even
        considered by ``_check_nets``. After the fix the mismatch is
        reported with ``domain="net"`` and ``reference="R2.1"``.
        """
        checker = SchematicPCBChecker(HIERARCHICAL_ROOT_SCH, hierarchical_bad_subsheet_net_pcb)
        result = checker.check()

        net_mismatches = [i for i in result.net_issues if i.issue_type == "mismatch"]
        r2_pin1_mismatches = [i for i in net_mismatches if i.reference == "R2.1"]
        assert r2_pin1_mismatches, (
            "Pre-fix: sub-sheet net mismatch on R2.1 was silently skipped "
            "because extract_netlist() only walked the root sheet. "
            f"All net mismatches seen: "
            f"{[(i.reference, i.pcb_value) for i in net_mismatches]}"
        )
        # The single sub-sheet pin we touched should be the only mismatch.
        assert {i.reference for i in net_mismatches} == {"R2.1"}, (
            "Expected exactly R2.1 net mismatch, got: "
            f"{sorted(i.reference for i in net_mismatches)}"
        )
        # And it points at the wrong net name we injected.
        assert r2_pin1_mismatches[0].pcb_value == "WRONG_NET"

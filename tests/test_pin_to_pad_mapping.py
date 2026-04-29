"""Tests for pin-to-pad mapping in sync-netlist.

Verifies that assign_nets_from_netlist correctly resolves schematic pin
numbers to footprint pad numbers using an explicit pin_to_pad_map.
"""

from pathlib import Path

from kicad_tools.operations.netlist import (
    Netlist,
    NetlistNet,
    NetNode,
    build_pin_to_pad_map,
)
from kicad_tools.schema.pcb import PCB


# ---------------------------------------------------------------------------
# PCB fixture: a 4-pad IC (U1) whose pads are numbered "A", "B", "C", "D"
# instead of sequential "1", "2", "3", "4".
# ---------------------------------------------------------------------------
PCB_WITH_ALPHA_PADS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SDA")
  (net 4 "SCL")
  (footprint "Package_SO:SOIC-4"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at 100 100)
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "IC1" (at 0 1.5 0) (layer "F.Fab"))
    (pad "A" smd roundrect (at -1 -0.5) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "B" smd roundrect (at 1 -0.5) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "C" smd roundrect (at -1 0.5) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "D" smd roundrect (at 1 0.5) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# ---------------------------------------------------------------------------
# Standard PCB: pads match pin numbers (identity mapping)
# ---------------------------------------------------------------------------
PCB_STANDARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""


def _write_pcb(tmp_path: Path, content: str) -> Path:
    """Write PCB content to a temp file and return the path."""
    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(content, encoding="utf-8")
    return pcb_path


class TestAssignNetsWithPinToPadMap:
    """Verify that pin_to_pad_map correctly remaps pin numbers to pad numbers."""

    def test_identity_map_assigns_correctly(self, tmp_path):
        """When pin numbers match pad numbers, mapping is identity."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_STANDARD))

        netlist = Netlist(
            tool="kicad-tools (Python fallback)",
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="R1", pin="1"),
                    NetNode(reference="C1", pin="1"),
                ]),
                NetlistNet(code=2, name="GND", nodes=[
                    NetNode(reference="R1", pin="2"),
                    NetNode(reference="C1", pin="2"),
                ]),
            ],
        )

        # Ensure nets exist
        pcb.add_net("VCC")
        pcb.add_net("GND")

        # Identity map: pin "1" -> pad "1", pin "2" -> pad "2"
        pin_map = {
            ("R1", "1"): "1",
            ("R1", "2"): "2",
            ("C1", "1"): "1",
            ("C1", "2"): "2",
        }

        result = pcb.assign_nets_from_netlist(netlist, pin_to_pad_map=pin_map)
        assert len(result["assigned"]) == 4
        assert len(result["missing_pads"]) == 0

        # Verify correct nets assigned
        r1 = pcb.get_footprint("R1")
        assert r1.pads[0].net_name == "VCC"
        assert r1.pads[1].net_name == "GND"

    def test_remapped_pins_assign_to_correct_pads(self, tmp_path):
        """When pin numbers differ from pad numbers, mapping resolves them."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_WITH_ALPHA_PADS))

        # Netlist uses schematic pin numbers "1", "2", "3", "4"
        # but the footprint has pads "A", "B", "C", "D"
        netlist = Netlist(
            tool="kicad-tools (Python fallback)",
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="U1", pin="1"),
                ]),
                NetlistNet(code=2, name="GND", nodes=[
                    NetNode(reference="U1", pin="2"),
                ]),
                NetlistNet(code=3, name="SDA", nodes=[
                    NetNode(reference="U1", pin="3"),
                ]),
                NetlistNet(code=4, name="SCL", nodes=[
                    NetNode(reference="U1", pin="4"),
                ]),
            ],
        )

        pcb.add_net("VCC")
        pcb.add_net("GND")
        pcb.add_net("SDA")
        pcb.add_net("SCL")

        # Pin-to-pad map: schematic pin "1" -> pad "A", etc.
        pin_map = {
            ("U1", "1"): "A",
            ("U1", "2"): "B",
            ("U1", "3"): "C",
            ("U1", "4"): "D",
        }

        result = pcb.assign_nets_from_netlist(netlist, pin_to_pad_map=pin_map)
        assert len(result["assigned"]) == 4
        assert len(result["missing_pads"]) == 0

        u1 = pcb.get_footprint("U1")
        pad_nets = {p.number: p.net_name for p in u1.pads}
        assert pad_nets["A"] == "VCC"
        assert pad_nets["B"] == "GND"
        assert pad_nets["C"] == "SDA"
        assert pad_nets["D"] == "SCL"

    def test_no_map_uses_pin_as_pad(self, tmp_path):
        """Without pin_to_pad_map, node.pin is used directly as pad number."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_STANDARD))

        netlist = Netlist(
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="R1", pin="1"),
                ]),
            ],
        )

        pcb.add_net("VCC")
        result = pcb.assign_nets_from_netlist(netlist)
        assert "R1.1" in result["assigned"]

    def test_partial_map_falls_back_to_identity(self, tmp_path):
        """When a pin is not in the map, it falls back to using pin as pad."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_STANDARD))

        netlist = Netlist(
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="R1", pin="1"),
                ]),
                NetlistNet(code=2, name="GND", nodes=[
                    NetNode(reference="R1", pin="2"),
                ]),
            ],
        )

        pcb.add_net("VCC")
        pcb.add_net("GND")

        # Only map pin "1", leave pin "2" unmapped
        pin_map = {("R1", "1"): "1"}

        result = pcb.assign_nets_from_netlist(netlist, pin_to_pad_map=pin_map)
        assert len(result["assigned"]) == 2
        assert len(result["missing_pads"]) == 0

    def test_wrong_pin_without_map_reports_missing(self, tmp_path):
        """When pin doesn't match any pad and no map is given, it's reported."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_WITH_ALPHA_PADS))

        # Netlist uses numeric pins but footprint has alpha pads
        netlist = Netlist(
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="U1", pin="1"),
                ]),
            ],
        )

        pcb.add_net("VCC")
        result = pcb.assign_nets_from_netlist(netlist)
        assert len(result["missing_pads"]) == 1
        assert "U1.1" in result["missing_pads"]

    def test_wrong_pin_with_map_succeeds(self, tmp_path):
        """When pin doesn't match any pad but map resolves it, it succeeds."""
        pcb = PCB.load(_write_pcb(tmp_path, PCB_WITH_ALPHA_PADS))

        netlist = Netlist(
            nets=[
                NetlistNet(code=1, name="VCC", nodes=[
                    NetNode(reference="U1", pin="1"),
                ]),
            ],
        )

        pcb.add_net("VCC")
        pin_map = {("U1", "1"): "A"}
        result = pcb.assign_nets_from_netlist(netlist, pin_to_pad_map=pin_map)
        assert len(result["assigned"]) == 1
        assert len(result["missing_pads"]) == 0


class TestBuildPinToPadMap:
    """Tests for build_pin_to_pad_map function."""

    def test_identity_mapping_for_standard_components(self, tmp_path):
        """Standard components where pin numbers match pad numbers."""
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (pin_numbers hide)
      (pin_names (offset 0) hide)
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 2.54 270) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "pin-r1-1"))
    (pin "2" (uuid "pin-r1-2"))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch_content, encoding="utf-8")

        pcb = PCB.load(_write_pcb(tmp_path, PCB_STANDARD))
        pin_map = build_pin_to_pad_map(sch_path, pcb)

        # Should have identity mapping for R1
        assert pin_map.get(("R1", "1")) == "1"
        assert pin_map.get(("R1", "2")) == "2"

    def test_missing_component_skipped(self, tmp_path):
        """Components in schematic but not in PCB are skipped."""
        sch_content = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "Device:R_1_1"
        (pin passive line (at 0 2.54 270) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R99" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "pin-1"))
    (pin "2" (uuid "pin-2"))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch_content, encoding="utf-8")

        pcb = PCB.load(_write_pcb(tmp_path, PCB_STANDARD))
        pin_map = build_pin_to_pad_map(sch_path, pcb)

        # R99 is not in the PCB, so no mapping for it
        assert ("R99", "1") not in pin_map
        assert ("R99", "2") not in pin_map

    def test_kicad_cli_netlist_skips_map(self):
        """Verify kicad-cli netlists are identified by tool string."""
        netlist = Netlist(tool="Eeschema 10.0.0")
        # kicad-cli output does not contain "Python fallback"
        assert "Python fallback" not in (netlist.tool or "")

    def test_python_fallback_netlist_identified(self):
        """Verify Python fallback netlists are identified by tool string."""
        netlist = Netlist(tool="kicad-tools (Python fallback)")
        assert "Python fallback" in (netlist.tool or "")

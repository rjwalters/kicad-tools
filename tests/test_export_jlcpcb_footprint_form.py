"""BOM Footprint and CPL Package converge on the qualified form (#8569)."""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage
from kicad_tools.export.footprint_form import reconcile_bom_items, reconcile_footprint

_PCB = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "C_0402_1005Metric" (layer "F.Cu") (at 10 20) (attr smd)
    (property "Reference" "C1") (property "Value" "100n")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 30 20) (attr smd)
    (property "Reference" "R1") (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu") (at 40 20) (attr smd)
    (property "Reference" "R2") (property "Value" "1k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
)
"""


def _item(ref, fp):
    return SimpleNamespace(reference=ref, footprint=fp)


def test_reconcile_footprint_cases():
    assert reconcile_footprint("Lib:C_0402", "C_0402") == "Lib:C_0402"
    assert reconcile_footprint("C_0402", "Lib:C_0402") == "Lib:C_0402"
    assert reconcile_footprint("Lib:C_0402", "Lib:C_0402") is None
    # different bare names are a genuine mismatch: never rewritten
    assert reconcile_footprint("Lib:C_0402", "C_0603") is None
    assert reconcile_footprint("C_0402", "Lib:C_0603") is None
    # both qualified with different libraries: left alone
    assert reconcile_footprint("A:C_0402", "B:C_0402") is None
    assert reconcile_footprint("", "Lib:C_0402") is None


def test_reconcile_bom_items_both_directions_and_mismatch():
    items = [
        _item("C1", "Capacitor_SMD:C_0402_1005Metric"),  # CPL bare
        _item("R1", "R_0603_1608Metric"),  # BOM bare
        _item("R2", "Resistor_SMD:R_1206_3216Metric"),  # genuine mismatch
    ]
    names = {
        "C1": "C_0402_1005Metric",
        "R1": "Resistor_SMD:R_0603_1608Metric",
        "R2": "Resistor_SMD:R_0805_2012Metric",
    }
    overrides = reconcile_bom_items(items, names)
    assert overrides == {"C1": "Capacitor_SMD:C_0402_1005Metric"}
    assert items[0].footprint == "Capacitor_SMD:C_0402_1005Metric"
    assert items[1].footprint == "Resistor_SMD:R_0603_1608Metric"
    assert items[2].footprint == "Resistor_SMD:R_1206_3216Metric"


def test_cpl_package_uses_overrides(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_PCB)
    pkg = AssemblyPackage(pcb, None, "jlcpcb", AssemblyConfig(bom_source="pcb"))
    pkg._cpl_footprint_overrides = {"C1": "Capacitor_SMD:C_0402_1005Metric"}
    out = pkg._generate_pnp(tmp_path)
    rows = {r["Designator"]: r for r in csv.DictReader(io.StringIO(out.read_text()))}
    assert rows["C1"]["Package"] == "Capacitor_SMD:C_0402_1005Metric"
    assert rows["R1"]["Package"] == "Resistor_SMD:R_0603_1608Metric"
    assert rows["R2"]["Package"] == "Resistor_SMD:R_0805_2012Metric"

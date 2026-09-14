"""Assembly filtering must recognize pad evidence without footprint attributes."""

import csv
import io

import pytest

from kicad_tools.export.pnp import (
    PnPExportConfig,
    export_pnp,
    extract_placements,
    extract_tht_exclusions,
)
from kicad_tools.export.preflight import PreflightChecker
from kicad_tools.schema.bom import BOM, BOMItem
from kicad_tools.schema.pcb import PCB


@pytest.fixture(params=["", "(attr smd)"])
def mixed_pcb(tmp_path, request):
    """A header missing/mislabeling its attr, plus an SMT connector with pegs."""
    path = tmp_path / "mixed.kicad_pcb"
    path.write_text(
        f"""(kicad_pcb (version 20240108) (generator "test")
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (footprint "Header" (layer "F.Cu") (at 10 10) {request.param}
            (property "Reference" "J1") (property "Value" "Header")
            (pad "1" thru_hole circle (at 0 0) (size 2 2)
              (drill 1) (layers "*.Cu" "*.Mask")))
          (footprint "SMTConnector" (layer "F.Cu") (at 20 10) (attr smd)
            (property "Reference" "J2") (property "Value" "Connector")
            (pad "1" smd rect (at 0 0) (size 1 1)
              (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "" np_thru_hole circle (at 2 0) (size 1 1)
              (drill 1) (layers "*.Cu" "*.Mask"))
            (pad "" thru_hole circle (at 4 0) (size 2 2)
              (drill 1) (layers "*.Cu" "*.Mask")))
          (footprint "LegacyTHT" (layer "F.Cu") (at 30 10) (attr through_hole)
            (property "Reference" "J3") (property "Value" "Legacy"))
          (footprint "Unpopulated" (layer "F.Cu") (at 40 10) (attr dnp)
            (property "Reference" "J4") (property "Value" "Optional")
            (pad "1" thru_hole circle (at 0 0) (size 2 2)
              (drill 1) (layers "*.Cu" "*.Mask")))
          (footprint "Omitted" (layer "F.Cu") (at 50 10)
            (attr exclude_from_pos_files)
            (property "Reference" "J5") (property "Value" "Omitted")
            (pad "1" thru_hole circle (at 0 0) (size 2 2)
              (drill 1) (layers "*.Cu" "*.Mask"))))"""
    )
    return PCB.load(path)


def test_cpl_exclusions_and_preflight_use_same_pad_evidence(mixed_pcb, monkeypatch):
    config = PnPExportConfig(exclude_tht=True)
    included = extract_placements(mixed_pcb.footprints, config)
    excluded = extract_tht_exclusions(mixed_pcb.footprints, config)
    assert [p.reference for p in included] == ["J2"]
    assert [p.reference for p in excluded] == ["J1", "J3"]

    # Exercise the actual JLCPCB formatter's default SMT-only policy.
    cpl = export_pnp(mixed_pcb.footprints, manufacturer="jlcpcb")
    assert [row["Designator"] for row in csv.DictReader(io.StringIO(cpl))] == ["J2"]

    checker = PreflightChecker(mixed_pcb.path, manufacturer="jlcpcb")
    checker._pcb = mixed_pcb
    monkeypatch.setattr(
        checker,
        "_load_bom",
        lambda: BOM(
            items=[
                BOMItem(reference=ref, value="Connector", footprint="Connector", lib_id="")
                for ref in ["J1", "J2", "J3"]
            ]
        ),
    )
    tht = checker._check_tht_in_cpl()
    assert tht.status == "OK"
    assert tht.message == "2 through-hole component(s) excluded from CPL"
    assert tht.details == "THT references: J1, J3"
    match = checker._check_bom_cpl_match()
    assert match.status == "OK"
    assert "1 components, 2 THT excluded" in match.message


def test_include_tht_override_keeps_unmarked_header(mixed_pcb):
    config = PnPExportConfig(exclude_tht=False)
    assert {p.reference for p in extract_placements(mixed_pcb.footprints, config)} == {
        "J1",
        "J2",
        "J3",
    }
    assert extract_tht_exclusions(mixed_pcb.footprints, config) == []
    checker = PreflightChecker(mixed_pcb.path, manufacturer="jlcpcb", exclude_tht=False)
    checker._pcb = mixed_pcb
    result = checker._check_tht_in_cpl()
    assert result.status == "WARN"
    assert result.details.startswith("THT references: J1, J3;")


@pytest.mark.parametrize("attribute", ["", "(attr smd)"])
def test_smd_exposed_pad_thermal_vias_remain_in_cpl(tmp_path, monkeypatch, attribute):
    path = tmp_path / "thermal.kicad_pcb"
    path.write_text(
        f"""(kicad_pcb (version 20240108) (generator "test")
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (footprint "HTSSOP_ThermalVias" (layer "F.Cu") (at 10 10) {attribute}
            (property "Reference" "U1") (property "Value" "DRV8313")
            (pad "1" smd rect (at -3 0) (size 1.5 0.45)
              (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "29" smd rect (at 0 0) (size 3 5)
              (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "29" thru_hole circle (at 0 0) (size 0.6 0.6)
              (drill 0.3) (layers "*.Cu"))
            (pad "29" thru_hole circle (at 0 1) (size 0.6 0.6)
              (drill 0.3) (layers "*.Cu"))))"""
    )
    pcb = PCB.load(path)
    config = PnPExportConfig(exclude_tht=True)
    assert extract_tht_exclusions(pcb.footprints, config) == []
    cpl = export_pnp(pcb.footprints, manufacturer="jlcpcb")
    assert [row["Designator"] for row in csv.DictReader(io.StringIO(cpl))] == ["U1"]
    checker = PreflightChecker(path, manufacturer="jlcpcb")
    checker._pcb = pcb
    monkeypatch.setattr(
        checker,
        "_load_bom",
        lambda: BOM(
            items=[
                BOMItem(reference="U1", value="DRV8313", footprint="HTSSOP_ThermalVias", lib_id="")
            ]
        ),
    )
    assert checker._check_tht_in_cpl().message.startswith("No through-hole components")
    assert checker._check_bom_cpl_match().status == "OK"

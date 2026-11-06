"""BOM Footprint and CPL Package use one form in the JLCPCB export (Issue #8538)."""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

from kicad_tools.export.bom_formats import JLCPCBBOMFormatter
from kicad_tools.export.pnp import JLCPCBPnPFormatter, PlacementData, bare_footprint


def _item(ref, footprint):
    return SimpleNamespace(
        reference=ref,
        value="10k",
        footprint=footprint,
        lcsc="C1",
        dnp=False,
        in_bom=True,
        exclude_from_bom=False,
    )


def test_bare_footprint():
    assert bare_footprint("Capacitor_SMD:C_0402_1005Metric") == "C_0402_1005Metric"
    assert bare_footprint(" R_2010_5025Metric ") == "R_2010_5025Metric"


def test_bom_and_cpl_agree_per_ref():
    fps = {
        "C1": "Capacitor_SMD:C_0402",
        "R1": "R_2010_5025Metric",
        "R2": "Resistor_SMD:R_2010_5025Metric",
    }
    bom = JLCPCBBOMFormatter().format([_item(r, f) for r, f in fps.items()])
    placements = [
        PlacementData(
            reference=r, value="10k", footprint=f, x=1.0, y=2.0, rotation=0.0, layer="F.Cu"
        )
        for r, f in fps.items()
    ]
    cpl = JLCPCBPnPFormatter().format(placements)
    bom_fp = {}
    for row in csv.DictReader(io.StringIO(bom)):
        for ref in row["Designator"].split(","):
            bom_fp[ref] = row["Footprint"]
    cpl_fp = {r["Designator"]: r["Package"] for r in csv.DictReader(io.StringIO(cpl))}
    assert bom_fp == cpl_fp
    assert all(":" not in v for v in bom_fp.values())

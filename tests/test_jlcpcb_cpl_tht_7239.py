"""JLCPCB CPL includes through-hole parts and matches ``kicad-cli pcb export pos`` (#7239)."""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.export.pnp import export_pnp
from kicad_tools.schema.pcb import PCB

_PCB = """(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "R_0402" (layer "F.Cu") (at 10 20) (attr smd)
    (property "Reference" "R1") (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "PinHeader_1x02" (layer "F.Cu") (at 30.5 40.25 90) (attr through_hole)
    (property "Reference" "J1") (property "Value" "Conn")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
  (footprint "PinHeader_1x02" (layer "B.Cu") (at 50 60) (attr through_hole)
    (property "Reference" "J2") (property "Value" "Conn")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
  (footprint "PinHeader_1x02" (layer "F.Cu") (at 70 80) (attr through_hole dnp)
    (property "Reference" "J3") (property "Value" "Conn")
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask")))
)
"""


@pytest.fixture
def pcb_path(tmp_path: Path) -> Path:
    path = tmp_path / "tht.kicad_pcb"
    path.write_text(_PCB)
    return path


def _rows(csv_text: str) -> dict[str, dict[str, str]]:
    return {r["Designator"]: r for r in csv.DictReader(io.StringIO(csv_text))}


def _mm(value: str) -> float:
    return float(value.removesuffix("mm"))


def test_jlcpcb_cpl_includes_tht_on_both_sides(pcb_path):
    pcb = PCB.load(pcb_path)
    rows = _rows(export_pnp(pcb.footprints, "jlcpcb", pcb_path=pcb_path))
    assert {"R1", "J1", "J2"} <= set(rows)
    assert rows["J1"]["Layer"] == "Top"
    assert rows["J2"]["Layer"] == "Bottom"


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_jlcpcb_tht_coordinates_match_kicad_cli_pos(pcb_path, tmp_path):
    out = tmp_path / "pos.csv"
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "export",
            "pos",
            "--format",
            "csv",
            "--units",
            "mm",
            "--side",
            "both",
            "--use-drill-file-origin",
            "-o",
            str(out),
            str(pcb_path),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    native = {r["Ref"]: r for r in csv.DictReader(out.open())}
    ours = _rows(export_pnp(PCB.load(pcb_path).footprints, "jlcpcb", pcb_path=pcb_path))

    for ref in ("R1", "J1", "J2"):
        assert ref in native, f"{ref} missing from kicad-cli output"
        assert _mm(ours[ref]["Mid X"]) == pytest.approx(float(native[ref]["PosX"]), abs=0.01)
        assert _mm(ours[ref]["Mid Y"]) == pytest.approx(float(native[ref]["PosY"]), abs=0.01)

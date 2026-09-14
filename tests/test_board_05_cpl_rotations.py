"""The released Board05 CPL must retain manufacturer rotation corrections."""

import csv
from pathlib import Path

from kicad_tools.export.assembly import AssemblyPackage


def test_board05_package_export_and_saved_cpl_apply_rotation_profile(tmp_path):
    output = Path(__file__).resolve().parents[1] / "boards/05-bldc-motor-controller/output"
    package = AssemblyPackage(
        output / "bldc_controller_routed.kicad_pcb",
        output / "bldc_controller.kicad_sch",
        manufacturer="jlcpcb",
    )
    generated = package._generate_pnp(tmp_path)
    with generated.open() as stream:
        rows = {row["Designator"]: row for row in csv.DictReader(stream)}
    assert len(rows) == 36
    assert not {"J1", "J2", "J3", "J4", "J5", "RV1"} & rows.keys()
    for reference, rotation in {"D1": 180, "D2": 180, "D3": 180, "U1": 270}.items():
        assert ":" in rows[reference]["Package"]
        assert float(rows[reference]["Rotation"]) == rotation
    with (output / "manufacturing/cpl_jlcpcb.csv").open() as stream:
        saved = {row["Designator"]: row for row in csv.DictReader(stream)}
    assert saved == rows

"""The released Board05 CPL must retain manufacturer rotation corrections."""

import csv
from pathlib import Path

from kicad_tools.export.assembly import AssemblyPackage

OUTPUT = Path(__file__).resolve().parents[1] / "boards/05-bldc-motor-controller/output"

#: Through-hole parts JLCPCB now assembles (#7239), so they are CPL rows.
THT_REFS = {"J1", "J2", "J3", "J4", "J5", "RV1"}


def _fresh_rows(tmp_path):
    package = AssemblyPackage(
        OUTPUT / "bldc_controller_routed.kicad_pcb",
        OUTPUT / "bldc_controller.kicad_sch",
        manufacturer="jlcpcb",
    )
    generated = package._generate_pnp(tmp_path)
    with generated.open() as stream:
        return {row["Designator"]: row for row in csv.DictReader(stream)}


def test_board05_package_export_applies_rotation_profile(tmp_path):
    rows = _fresh_rows(tmp_path)
    # 36 SMT rows + the six THT parts.
    assert len(rows) == 42
    assert rows.keys() >= THT_REFS
    for reference, rotation in {"D1": 180, "D2": 180, "D3": 180, "U1": 270}.items():
        assert ":" in rows[reference]["Package"]
        assert float(rows[reference]["Rotation"]) == rotation


def test_board05_saved_cpl_matches_fresh_export(tmp_path):
    """The committed CPL is the current export: rotations, THT rows and the
    Gerber/drill frame (#7059) all included (#7241)."""
    rows = _fresh_rows(tmp_path)
    with (OUTPUT / "manufacturing/cpl_jlcpcb.csv").open() as stream:
        saved = {row["Designator"]: row for row in csv.DictReader(stream)}
    assert saved == rows


def test_board05_saved_cpl_includes_tht_rows():
    with (OUTPUT / "manufacturing/cpl_jlcpcb.csv").open() as stream:
        saved = {row["Designator"] for row in csv.DictReader(stream)}
    assert saved >= THT_REFS

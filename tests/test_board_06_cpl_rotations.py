"""The released Board06 CPL must retain manufacturer rotation corrections (#7200)."""

import csv
from pathlib import Path

from kicad_tools.export.assembly import AssemblyPackage

OUTPUT = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/output"


def _rows(path):
    with Path(path).open() as stream:
        return {row["Designator"]: row for row in csv.DictReader(stream)}


def test_board06_package_export_applies_rotation_profile(tmp_path):
    package = AssemblyPackage(
        OUTPUT / "diffpair_test_routed.kicad_pcb",
        OUTPUT / "diffpair_test.kicad_sch",
        manufacturer="jlcpcb",
    )
    rows = _rows(package._generate_pnp(tmp_path))
    assert len(rows) == 33
    for reference, rotation in {"U1": 270, "U2": 270}.items():
        assert ":" in rows[reference]["Package"]
        assert float(rows[reference]["Rotation"]) == rotation


def test_board06_saved_cpl_matches_fresh_export(tmp_path):
    """Saved CPL equals a fresh export, coordinates (#7059 frame) included."""
    package = AssemblyPackage(
        OUTPUT / "diffpair_test_routed.kicad_pcb",
        OUTPUT / "diffpair_test.kicad_sch",
        manufacturer="jlcpcb",
    )
    generated = _rows(package._generate_pnp(tmp_path))
    saved = _rows(OUTPUT / "manufacturing/cpl_jlcpcb.csv")
    assert saved == generated

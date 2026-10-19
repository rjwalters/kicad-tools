"""The released Board06 CPL must retain manufacturer rotation corrections (#7200)."""

import csv
from pathlib import Path

import pytest

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
    # 33 SMT rows + THT headers J1-J3 (JLCPCB assembles THT, #7239).
    assert len(rows) == 36
    assert {"J1", "J2", "J3"} <= rows.keys()
    for reference, rotation in {f"U{n}": 270 for n in range(1, 9)}.items():
        assert ":" in rows[reference]["Package"]
        assert float(rows[reference]["Rotation"]) == rotation


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#7268: the committed board 06 CPL predates THT inclusion (#7239) and lacks "
        "J1-J3; the Samtec headers have no LCSC ID, so a regenerated package fails "
        "the readiness bom gate until procurement assigns parts"
    ),
)
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

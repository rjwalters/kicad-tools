"""The released Board04 CPL must retain manufacturer rotation corrections (#7200)."""

import csv
from pathlib import Path

import pytest

from kicad_tools.export.assembly import AssemblyPackage

OUTPUT = Path(__file__).resolve().parents[1] / "boards/04-stm32-devboard/output"


def _rows(path):
    with Path(path).open() as stream:
        return {row["Designator"]: row for row in csv.DictReader(stream)}


def test_board04_package_export_applies_rotation_profile(tmp_path):
    package = AssemblyPackage(
        OUTPUT / "stm32_devboard_routed.kicad_pcb",
        OUTPUT / "stm32_devboard.kicad_sch",
        manufacturer="jlcpcb",
    )
    rows = _rows(package._generate_pnp(tmp_path))
    # 15 SMT rows + THT J1/Y1 (JLCPCB assembles THT, #7239).
    assert len(rows) == 17
    assert {"J1", "Y1"} <= rows.keys()
    for reference, rotation in {"D1": 180, "U1": 180, "U2": 270}.items():
        assert ":" in rows[reference]["Package"]
        assert float(rows[reference]["Rotation"]) == rotation


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#7268: the committed board 04 CPL predates THT inclusion (#7239) and lacks "
        "J1/Y1; refresh_readiness.py refuses to re-pin it on #7220 thermal_asymmetry "
        "warnings (#7273)"
    ),
)
def test_board04_saved_cpl_matches_fresh_export(tmp_path):
    """Saved CPL equals a fresh export, coordinates (#7059 frame) included."""
    package = AssemblyPackage(
        OUTPUT / "stm32_devboard_routed.kicad_pcb",
        OUTPUT / "stm32_devboard.kicad_sch",
        manufacturer="jlcpcb",
    )
    generated = _rows(package._generate_pnp(tmp_path))
    saved = _rows(OUTPUT / "manufacturing/cpl_jlcpcb.csv")
    assert saved == generated

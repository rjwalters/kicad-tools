"""The released Board05 CPL must retain manufacturer rotation corrections."""

import csv
from pathlib import Path

import pytest

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


def test_board05_saved_cpl_smt_rows_apply_rotation_profile(tmp_path):
    """The committed CPL's SMT rows keep the rotation profile.

    Coordinates are excluded: the committed bundle predates the Gerber-frame
    CPL fix (#7059) and refreshes on the next board regen.
    """
    rows = _fresh_rows(tmp_path)
    with (OUTPUT / "manufacturing/cpl_jlcpcb.csv").open() as stream:
        saved = {row["Designator"]: row for row in csv.DictReader(stream)}
    coords = {"Mid X", "Mid Y"}

    def strip(table):
        return {
            ref: {k: v for k, v in row.items() if k not in coords}
            for ref, row in table.items()
            if ref not in THT_REFS
        }

    assert strip(saved) == strip(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#7268: the committed board 05 CPL predates THT inclusion (#7239) and lacks "
        "J1-J5/RV1; refresh_readiness.py refuses to re-pin it on #7220 "
        "via_at_pad_edge warnings"
    ),
)
def test_board05_saved_cpl_includes_tht_rows():
    with (OUTPUT / "manufacturing/cpl_jlcpcb.csv").open() as stream:
        saved = {row["Designator"] for row in csv.DictReader(stream)}
    assert saved >= THT_REFS

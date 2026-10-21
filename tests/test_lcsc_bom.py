"""BOM-driven LCSC C-number resolution (issue #7686).  Fixture-only, no network."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.cli.pcb_add_3d_models import run_add_3d_models
from kicad_tools.footprints.library_path import LibraryPaths
from kicad_tools.pcb import lcsc_bom
from kicad_tools.pcb.lcsc_bom import (
    build_lcsc_mapping,
    discover_board_inputs,
    merge_with_sidecar,
    resolve_bom_lcsc,
)
from kicad_tools.pcb.lcsc_models import LcscModelEntry

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD03 = REPO_ROOT / "boards" / "03-usb-joystick"
FAKE_STEP = b"ISO-10303-21;\r\nHEADER;\r\n/* fake */\r\nEND-ISO-10303-21;\r\n"


def _fp(lib_id: str, ref: str, extra: str = "") -> str:
    return (
        f'  (footprint "{lib_id}" (layer "F.Cu")\n'
        f'    (property "Reference" "{ref}")\n{extra}'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))\n'
    )


def _pcb(*fps: str) -> str:
    return "(kicad_pcb\n" + "".join(fps) + ")\n"


def _bom(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bom.csv"
    path.write_text(text)
    return path


class TestResolution:
    def test_footprint_property_wins(self):
        pcb = _pcb(_fp("L:A", "U1", '    (property "LCSC Part #" "C111")\n'))
        result = resolve_bom_lcsc(pcb)
        assert result.mapping["L:A"].lcsc == "C111"
        assert result.sources["L:A"] == "footprint property"

    def test_bom_lcsc_column_by_reference(self, tmp_path):
        bom = _bom(tmp_path, "Reference,Value,LCSC Part #\nU1,x,C222\n")
        result = resolve_bom_lcsc(_pcb(_fp("L:A", "U1")), bom_csv=bom)
        assert result.mapping["L:A"].lcsc == "C222"

    def test_comma_separated_reference_cell(self, tmp_path):
        bom = _bom(tmp_path, 'Designator,JLCPCB Part #\n"R1, R2",C333\n')
        result = resolve_bom_lcsc(_pcb(_fp("L:R", "R1"), _fp("L:R", "R2")), bom_csv=bom)
        assert result.mapping["L:R"].lcsc == "C333"

    def test_mpn_via_procurement_review(self, tmp_path):
        bom = _bom(tmp_path, "Reference,MPN\nJ1,ABC-1\n")
        proc = tmp_path / "procurement-review.json"
        proc.write_text(json.dumps({"parts": {"ABC-1": {"lcsc": "C444"}}}))
        result = resolve_bom_lcsc(_pcb(_fp("L:J", "J1")), bom_csv=bom, procurement=proc)
        assert result.mapping["L:J"].lcsc == "C444"
        assert "procurement review" in result.sources["L:J"]

    def test_disagreeing_references_are_a_conflict_not_a_mapping(self, tmp_path):
        bom = _bom(tmp_path, "Reference,LCSC\nR1,C1\nR2,C2\n")
        result = resolve_bom_lcsc(_pcb(_fp("L:R", "R1"), _fp("L:R", "R2")), bom_csv=bom)
        assert "L:R" not in result.mapping
        assert result.conflicts == {"L:R": ["C1", "C2"]}

    def test_part_with_no_c_number_is_absent(self, tmp_path):
        bom = _bom(tmp_path, "Reference,MPN\nU1,NOPE\n")
        result = resolve_bom_lcsc(_pcb(_fp("L:A", "U1")), bom_csv=bom)
        assert result.mapping == {} and result.conflicts == {}

    def test_invalid_c_number_is_ignored(self, tmp_path):
        bom = _bom(tmp_path, "Reference,LCSC\nU1,../../etc\n")
        assert resolve_bom_lcsc(_pcb(_fp("L:A", "U1")), bom_csv=bom).mapping == {}


class TestSidecarOverride:
    def test_sidecar_overrides_and_stale_entries_reported(self, tmp_path):
        bom = _bom(tmp_path, "Reference,LCSC\nU1,C1\nU2,C2\n")
        pcb = _pcb(_fp("L:A", "U1"), _fp("L:B", "U2"))
        result = resolve_bom_lcsc(pcb, bom_csv=bom)
        sidecar = {
            "L:A": LcscModelEntry(lcsc="C999", rotate=(0.0, 0.0, 90.0)),  # disagrees with BOM
            "L:Gone": LcscModelEntry(lcsc="C5"),  # not on the board
        }
        merged, stale = merge_with_sidecar(result, sidecar, pcb)
        assert merged["L:A"].lcsc == "C999" and merged["L:A"].rotate == (0.0, 0.0, 90.0)
        assert merged["L:B"].lcsc == "C2"
        assert set(stale) == {"L:A", "L:Gone"}
        assert "C1" in stale["L:A"] and "no footprint" in stale["L:Gone"]


class TestBoard03:
    """Board 03 resolves from its BOM with no sidecar (acceptance criterion)."""

    PCB = BOARD03 / "output" / "usb_joystick_routed.kicad_pcb"

    def test_resolves_without_sidecar(self):
        plan = build_lcsc_mapping(self.PCB)
        jst = plan.mapping["Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal"]
        assert jst.lcsc == "C265104"
        assert plan.mapping["Package_QFP:TQFP-44_10x10mm_P0.8mm"].lcsc == "C44854"
        assert plan.stale_sidecar == {}

    def test_sidecar_naming_a_lib_id_not_on_the_board_is_reported_stale(self, tmp_path):
        # The committed board-03 sidecar that exercised this was deleted in #7685
        # for exactly this reason (its lib_id is not on the board).
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Module:Joystick_Analog": "C50950"}))
        plan = build_lcsc_mapping(self.PCB, sidecar=sidecar)
        assert "Module:Joystick_Analog" in plan.stale_sidecar

    def test_shared_passive_footprints_are_conflicts(self):
        plan = build_lcsc_mapping(self.PCB)
        assert "Resistor_SMD:R_0603_1608Metric" in plan.bom.conflicts

    def test_discovers_inputs_from_output_dir(self):
        bom, proc = discover_board_inputs(self.PCB)
        assert bom == (BOARD03 / "assembly-bom.csv").resolve()
        assert proc == (BOARD03 / "procurement-review.json").resolve()

    def test_cli_offline_uses_cached_bodies_and_never_fetches(self, tmp_path, monkeypatch, capsys):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C265104.step").write_bytes(FAKE_STEP)
        monkeypatch.setenv("KCT_LCSC_3D_DIR", str(cache))
        monkeypatch.delenv("KCT_LCSC_FETCH", raising=False)
        work = tmp_path / "pcb.kicad_pcb"
        shutil.copy(self.PCB, work)
        # Shadow the installed KiCad libraries so only the LCSC tier can resolve.
        empty = tmp_path / "fp"
        empty.mkdir()
        libs = LibraryPaths(footprints_path=empty, source="config")
        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")),
            mock.patch(
                "kicad_tools.footprints.library_path.detect_kicad_library_path",
                return_value=libs,
            ),
        ):
            rc = run_add_3d_models(
                work,
                lib_path=empty,
                dry_run=True,
                refresh=True,
                output_format="json",
                bom=BOARD03 / "assembly-bom.csv",
                procurement=BOARD03 / "procurement-review.json",
            )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["lcsc_matches"] == {
            "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal": "C265104"
        }
        assert "Resistor_SMD:R_0603_1608Metric" in out["lcsc_bom_conflicts"]

    def test_no_lcsc_bom_flag_disables_bom_resolution(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("KCT_LCSC_3D_DIR", str(tmp_path / "cache"))
        empty = tmp_path / "fp"
        empty.mkdir()
        rc = run_add_3d_models(
            self.PCB,
            lib_path=empty,
            dry_run=True,
            output_format="json",
            lcsc_from_bom=False,
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["lcsc_from_bom"] == {}


def test_module_exports():
    assert hasattr(lcsc_bom, "resolve_bom_lcsc")
    assert pytest is not None

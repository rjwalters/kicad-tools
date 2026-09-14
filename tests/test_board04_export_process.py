"""Board04 export must retain its reviewed paid-drill contract in the package."""

import importlib.util
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import (
    ManufacturingPackage,
    ManufacturingResult,
    _create_project_zip,
)

BOARD = Path(__file__).resolve().parents[1] / "boards/04-stm32-devboard"


def load_recipe():
    spec = importlib.util.spec_from_file_location(
        "board04_export_recipe", BOARD / "generate_design.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def board(tmp_path):
    for name in (
        "stm32_devboard_routed.kicad_pcb",
        "stm32_devboard_routed.kicad_pro",
        "stm32_devboard_routed.kicad_dru",
        "manufacturing-requirements.json",
    ):
        shutil.copy2(BOARD / "output" / name, tmp_path / name)
    return tmp_path / "stm32_devboard_routed.kicad_pcb"


def test_export_preserves_reviewed_floors_in_project_zip(monkeypatch, board):
    recipe = load_recipe()
    schematic = board.parent / "stm32_devboard.kicad_sch"
    shutil.copy2(BOARD / "output/stm32_devboard.kicad_sch", schematic)
    recipe.write_portable_symbols(schematic)

    def export(package, output_dir):
        # Exercise the export's actual constraint writer if enabled, then its
        # actual ZIP writer. Rendering/assembly are independent of this bug.
        result = ManufacturingResult(output_dir=output_dir)
        if package.config.emit_drc_constraints:
            package._write_drc_constraints(result)
        output_dir.mkdir()
        _create_project_zip(package.pcb_path, output_dir)
        return result

    monkeypatch.setattr(ManufacturingPackage, "export", export)
    assert recipe.generate_manufacturing(board, board.parent)
    with zipfile.ZipFile(board.parent / "manufacturing/kicad_project.zip") as archive:
        project = json.loads(archive.read("stm32_devboard_routed.kicad_pro"))
        for name in ("board04_symbols.kicad_sym", "kicad_tools_pwr.kicad_sym", "sym-lib-table"):
            assert archive.read(name) == (board.parent / name).read_bytes()
    rules = project["board"]["design_settings"]["rules"]
    assert rules["min_via_hole"] == 0.15
    assert rules["min_via_diameter"] == 0.30
    assert rules["min_via_annular_width"] == 0.075


def test_export_rejects_unselected_paid_option(monkeypatch, board):
    (board.parent / "manufacturing-requirements.json").write_text("{}")

    def unexpected_export(*args, **kwargs):
        pytest.fail("Export must not run without the reviewed option")

    monkeypatch.setattr(ManufacturingPackage, "export", unexpected_export)
    with pytest.raises(ValueError, match="Select the reviewed paid"):
        load_recipe().generate_manufacturing(board, board.parent)


def test_paid_gate_reports_failure_from_json_when_stderr_empty(monkeypatch, tmp_path):
    script = BOARD.parents[1] / "scripts/ci/check_routed_drc.py"
    spec = importlib.util.spec_from_file_location("board04_paid_gate", script)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    def check(command, **kwargs):
        Path(command[-1]).write_text(
            json.dumps({"meta_checks": {"manifest": {"status": "FAILED", "detail": "STALE"}}})
        )
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", check)
    with pytest.raises(RuntimeError, match="STALE"):
        gate._run_paid_drill_check(tmp_path / "board.kicad_pcb")


def test_portable_capacitor_preserves_cached_symbol_and_instance_fields(tmp_path):
    from kicad_tools.sexp import parse_file, serialize_sexp

    recipe = load_recipe()
    schematic = tmp_path / "stm32_devboard.kicad_sch"
    shutil.copy2(BOARD / "output/stm32_devboard.kicad_sch", schematic)
    before = parse_file(schematic)
    # Exercise a stock binding even once the committed artifact is portable.
    for symbol in before.find_child("lib_symbols").find_children("symbol"):
        if symbol.get_string(0).endswith(":C_Small"):
            symbol.set_atom(0, "Device:C_Small")
    for symbol in before.find_children("symbol"):
        if symbol.find_child("lib_id").get_string(0).endswith(":C_Small"):
            symbol.find_child("lib_id").set_atom(0, "Device:C_Small")
    schematic.write_text(serialize_sexp(before))
    recipe.write_portable_symbols(schematic)
    after = parse_file(schematic)
    for symbol in after.find_child("lib_symbols").find_children("symbol"):
        if symbol.get_string(0) == "board04_symbols:C_Small":
            cached = serialize_sexp(symbol)
            symbol.set_atom(0, "Device:C_Small")
    for symbol in after.find_children("symbol"):
        if symbol.find_child("lib_id").get_string(0) == "board04_symbols:C_Small":
            symbol.find_child("lib_id").set_atom(0, "Device:C_Small")
    assert serialize_sexp(after) == serialize_sexp(before)
    local = parse_file(tmp_path / "board04_symbols.kicad_sym").find_child("symbol")
    local.set_atom(0, "board04_symbols:C_Small")
    assert serialize_sexp(local) == cached
    assert "${KIPRJMOD}/board04_symbols.kicad_sym" in (tmp_path / "sym-lib-table").read_text()
    content = schematic.read_bytes()
    recipe.write_portable_symbols(schematic)
    assert schematic.read_bytes() == content

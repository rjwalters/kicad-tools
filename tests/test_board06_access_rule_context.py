"""Fresh and staged recipes use the same explicit native rule context."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recipe(monkeypatch):
    base = ROOT / "boards/06-diffpair-test"

    def module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        result = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, result)
        spec.loader.exec_module(result)
        return result

    module("generate_pcb", base / "generate_pcb.py")
    monkeypatch.syspath_prepend(str(base))
    return module("board06_access_context", base / "generate_design.py")


def test_fresh_context_creates_factory_rules_and_preserves_source(recipe, tmp_path):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.4}}}})
    )
    before = project.read_bytes()
    rules = recipe._prepare_plane_access_context(source, output)
    assert rules.clearance == 0.4
    assert (
        rules.pth_hole_track,
        rules.inner_pth_hole_copper,
        rules.edge_clearance,
        rules.hole_edge_clearance,
    ) == (0.28, 0.3, 0.3, 0.4)
    assert project.read_bytes() == before == output.with_suffix(".kicad_pro").read_bytes()
    assert not source.with_suffix(".kicad_dru").exists()
    snapshot = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert recipe._prepare_plane_access_context(source, output) == rules
    assert snapshot == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_existing_output_minima_and_bytes_preserved(recipe, tmp_path):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.with_suffix(".kicad_pro").write_text("{}")
    project = output.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_track_width": 0.6}}}})
    )
    before = project.read_bytes()
    rules = recipe._prepare_plane_access_context(source, output)
    assert rules.width == 0.6 and project.read_bytes() == before


@pytest.mark.parametrize("stem", ["input", "output"])
def test_invalid_custom_rules_do_not_create_or_change_sidecars(recipe, tmp_path, stem):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.with_suffix(".kicad_pro").write_text("{}")
    (tmp_path / (stem + ".kicad_dru")).write_text('(version 1)\n(rule "Unsupported")')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="custom DRC"):
        recipe._prepare_plane_access_context(source, output)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_staged_rule_context_is_not_rewritten(recipe, tmp_path):
    import shutil

    spec = importlib.util.spec_from_file_location(
        "context_stager", ROOT / "scripts/ci/board_recipe_artifacts.py"
    )
    staging = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(staging)
    base = ROOT / "boards/06-diffpair-test"
    for name in ("regression-fixture", "regression-input"):
        shutil.copytree(base / name, tmp_path / name)
    folder = staging.recipe_output_dir(tmp_path, prepare=True)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    rules = recipe._prepare_plane_access_context(
        folder / "diffpair_test.kicad_pcb", folder / "diffpair_test_routed.kicad_pcb"
    )
    assert rules.pth_hole_track == 0.28
    assert before == {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}

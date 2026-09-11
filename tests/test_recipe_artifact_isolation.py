"""Synthetic gates must never consume or mutate assembled release artifacts."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("board", "filename", "floor"),
    [
        ("06-diffpair-test", "diffpair_test_routed.kicad_pcb", 18),
        ("07-matchgroup-test", "matchgroup_test_routed.kicad_pcb", 8),
    ],
)
def test_synthetic_baselines_never_apply_to_active_release(monkeypatch, board, filename, floor):
    monkeypatch.chdir(ROOT)
    helper = load("board_recipe_artifacts")
    gate = load("check_diffpair_coverage")
    allowances = gate.load_allowlist(ROOT / ".github/routed-drc-tolerance.yml")
    root = ROOT / "boards" / board
    active = helper.recipe_baseline_key(root / "output" / filename)
    fixture = helper.recipe_baseline_key(root / "regression-fixture" / filename)
    regenerated = helper.recipe_baseline_key(root / "regression-output" / filename)
    assert regenerated == fixture != active
    assert allowances[fixture] == floor
    assert allowances.get(active, 0) == 0


def test_pair_improvement_cannot_hide_new_physical_error():
    gate = load("check_diffpair_coverage")
    key = "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
    errors = {"diffpair_length_skew": 8, "diffpair_routing_continuity": 9, "via_in_pad": 1}
    assert sum(errors.values()) == 18
    assert gate.unexpected_baseline_errors(key, errors) == {"via_in_pad": 1}
    del errors["via_in_pad"]
    assert gate.unexpected_baseline_errors(key, errors) == {}


def test_matchgroup_improvement_cannot_hide_new_physical_error():
    gate = load("check_matchgroup_coverage")
    key = "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
    errors = {
        "diffpair_length_skew": 3,
        "diffpair_routing_continuity": 4,
        "via_in_pad": 1,
        "connectivity": 5,
    }
    assert gate.unexpected_baseline_errors(key, errors) == {"via_in_pad": 1}
    del errors["via_in_pad"]
    assert gate.unexpected_baseline_errors(key, errors) == {}


@pytest.mark.parametrize(
    ("number", "board", "gate"),
    [("06", "06-diffpair-test", "diffpair"), ("07", "07-matchgroup-test", "matchgroup")],
)
def test_e2e_stages_matching_synthetic_bundle(number, board, gate):
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"][f"board-{number}-end-to-end"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step.get("name", "").startswith("Independent synthetic routing gate")
    )
    assert f"cp -a /tmp/board{number}-ci/. boards/{board}/regression-output/" in command
    assert f"check_{gate}_coverage.py" in command
    assert "--skip-route" in command
    assert f"boards/{board}/output/" not in command


@pytest.mark.parametrize("gate_name", ["check_diffpair_coverage", "check_matchgroup_coverage"])
def test_recipe_uses_its_own_pcb_and_sidecar(tmp_path, monkeypatch, gate_name):
    gate = load(gate_name)
    board = tmp_path / "demo"
    (board / "output").mkdir(parents=True)
    fixture = board / "regression-fixture"
    fixture.mkdir()
    (board / "generate_design.py").write_text("# fixture recipe")
    for folder, identity in [(board / "output", "assembled"), (fixture, "synthetic")]:
        (folder / "demo_routed.kicad_pcb").write_text(identity)
        (folder / "net_class_map.json").write_text(identity)
    assert gate.find_routed_pcb(board).parent == fixture
    commands = []

    def run(command, **kwargs):
        from types import SimpleNamespace

        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.re_route_board(board, 42)
    assert commands[0][2] == str(board / "regression-output")
    routed = gate.find_routed_pcb(board)
    assert routed.parent == board / "regression-output"
    assert routed.read_text() == "synthetic"
    assert (board / "output/demo_routed.kicad_pcb").read_text() == "assembled"
    assert (board / "output/net_class_map.json").read_text() == "assembled"
    if hasattr(gate, "find_net_class_map_sidecar"):
        assert gate.find_net_class_map_sidecar(board, routed).parent == routed.parent

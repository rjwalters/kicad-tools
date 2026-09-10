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

"""Board 06's routed ``.kicad_pro`` must declare the recipe's router net classes.

Issue #6763: the recipe routes with ``build_net_class_map()`` (``Power`` 0.2 mm,
...) but the project file declared ``Default`` only, so ``kicad-cli pcb drc``
never enforced the clearance the router used.  The recipe now calls
``write_drc_constraints(router_net_classes=...)`` after routing (as ``kct
route`` does, #6390); these tests pin the result to the recipe map.

Hermetic: runs on the generated (unrouted) PCB -- no router, no kicad-cli.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BOARD_DIR = Path(__file__).resolve().parent.parent / "boards" / "06-diffpair-test"


@pytest.fixture(scope="module")
def recipe():
    sys.path.insert(0, str(BOARD_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "board06_generate_design_6763", BOARD_DIR / "generate_design.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(BOARD_DIR))


@pytest.fixture
def declared(recipe, tmp_path):
    """(project dict, expected {class: (clearance, nets)}) after the recipe's write."""
    pcb = tmp_path / "diffpair_test_routed.kicad_pcb"
    pcb.write_text(recipe.generate_pcb.generate_pcb())
    net_class_map = recipe.build_net_class_map()
    recipe._write_router_netclass_sidecars(pcb, net_class_map)
    project = json.loads(pcb.with_suffix(".kicad_pro").read_text())

    from kicad_tools.manufacturers.project_generator import board_net_names

    board_nets = set(board_net_names(pcb))
    default = next(c for c in project["net_settings"]["classes"] if c["name"] == "Default")
    expected: dict[str, tuple[float, set[str]]] = {}
    for net, routing in net_class_map.items():
        if net not in board_nets or routing.clearance <= default["clearance"] + 1e-6:
            continue
        clearance, nets = expected.setdefault(routing.name, (routing.clearance, set()))
        expected[routing.name] = (max(clearance, routing.clearance), nets | {net})
    return project, expected


def _members(project: dict, class_name: str) -> set[str]:
    out = set()
    for net, value in project["net_settings"]["netclass_assignments"].items():
        values = value if isinstance(value, list) else [value]
        if class_name in values:
            out.add(net)
    return out


def test_power_class_is_declared_at_recipe_clearance(declared):
    project, expected = declared
    assert "Power" in expected, "recipe map should have Power nets on the board"
    classes = {c["name"]: c for c in project["net_settings"]["classes"]}
    assert classes["Power"]["clearance"] == pytest.approx(0.2)


def test_project_net_classes_match_recipe_map(declared):
    project, expected = declared
    classes = {c["name"]: c for c in project["net_settings"]["classes"]}
    for name, (clearance, nets) in expected.items():
        assert classes[name]["clearance"] == pytest.approx(clearance), name
        assert _members(project, name) == nets, name


def test_writing_twice_is_idempotent(recipe, declared, tmp_path):
    project, _ = declared
    pcb = tmp_path / "diffpair_test_routed.kicad_pcb"
    recipe._write_router_netclass_sidecars(pcb, recipe.build_net_class_map())
    assert json.loads(pcb.with_suffix(".kicad_pro").read_text()) == project

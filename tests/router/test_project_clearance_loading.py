"""Original project constraints survive staging and neutral copper ownership."""

import json
from pathlib import Path

import pytest

from kicad_tools.core.project_clearance import ProjectClearanceError, resolve_project_clearances
from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.cache import CacheKey
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import DesignRules
from tests.test_routing_placement_disposition import board_text


def project(default=0.2):
    return {
        "net_settings": {
            "meta": {"version": 5},
            "classes": [
                {"name": "Default", "clearance": default},
                {"name": "Strict", "clearance": 0.6, "priority": 0},
                {"name": "Plane", "clearance": 0.3, "priority": 0},
            ],
            "netclass_assignments": {"BAD": ["Strict"], "PLANE": ["Plane"]},
        }
    }


@pytest.mark.parametrize("name_only", [False, True])
@pytest.mark.parametrize("staged", [False, True])
def test_loader_resolves_original_project_without_mutating_caller(tmp_path, name_only, staged):
    original = tmp_path / "source.kicad_pcb"
    original.write_text(board_text(name_only=name_only))
    sidecar = original.with_suffix(".kicad_pro")
    sidecar.write_text(json.dumps(project()))
    board = tmp_path / "staged.kicad_pcb" if staged else original
    board.write_text(original.read_text())
    if staged:
        board.with_suffix(".kicad_pro").write_text(json.dumps(project(1.0)))
    rules = DesignRules(trace_clearance=0.15, manufacturer="jlcpcb")
    before = dict(rules.net_clearance_floors)
    router, nets = load_pcb_for_routing(
        str(board),
        project_path=sidecar if staged else None,
        rules=rules,
        placement_disposition=analyze_routing_placement(original),
        skip_nets=["PLANE"],
        force_python=True,
        validate_drc=False,
    )
    assert router.rules is not rules
    assert rules.net_clearance_floors == before
    assert router.rules.trace_clearance == 0.15
    assert router.rules.net_clearance_floors[nets["GOOD"]] == 0.2
    assert router.rules.net_clearance_floors[0] == 0.2
    bad = router.pads[("X2", "1")].net
    plane = router.pads[("G1", "1")].net
    assert bad != plane and bad not in nets.values() and plane not in nets.values()
    assert router.pads[("X2", "1")].obstacle_only
    assert router.pads[("G1", "1")].obstacle_only
    assert bad not in router.nets and plane not in router.nets
    assert nets["BAD"] not in router.nets and nets["PLANE"] not in router.nets
    assert router.rules.clearance_for_nets(nets["GOOD"], bad, 0.15) == 0.6
    assert router.rules.clearance_for_nets(nets["GOOD"], plane, 0.15) == 0.3
    assert router.placement_neutral_routes[0].net == bad
    assert router.placement_preserved_routes[0].net == nets["BAD"]
    assert sidecar.read_text() == json.dumps(project())
    assert CacheKey.compute(board.read_bytes(), rules, 0.1) != CacheKey.compute(
        board.read_bytes(), router.rules, 0.1
    )


def test_missing_sidecar_keeps_legacy_rules_and_invalid_sidecar_fails(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text(board_text(invalid=False))
    rules = DesignRules(trace_clearance=0.15)
    router, _ = load_pcb_for_routing(str(board), rules=rules, force_python=True, validate_drc=False)
    assert router.rules.net_clearance_floors == {}
    sidecar = board.with_suffix(".kicad_pro")
    sidecar.write_text('{"net_settings":{"meta":{"version":99}}}')
    with pytest.raises(ProjectClearanceError):
        load_pcb_for_routing(str(board), rules=rules, force_python=True, validate_drc=False)


def test_unnamed_copper_uses_default_without_wildcard_assignment():
    data = project()
    data["net_settings"]["netclass_patterns"] = [{"pattern": "*", "netclass": "Strict"}]
    resolved = resolve_project_clearances(data, ["", "GOOD"])
    assert resolved[""].clearance == 0.2
    assert resolved["GOOD"].clearance == 0.6


def test_neutral_floor_reaches_native_grid(tmp_path):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    board = tmp_path / "board.kicad_pcb"
    board.write_text(board_text())
    board.with_suffix(".kicad_pro").write_text(json.dumps(project()))
    router, nets = load_pcb_for_routing(
        str(board),
        placement_disposition=analyze_routing_placement(board),
        skip_nets=["PLANE"],
        force_python=True,
        validate_drc=False,
    )
    native = CppGrid.from_routing_grid(router.grid)._impl
    assert native.net_clearance_floor(nets["GOOD"], router.pads[("X2", "1")].net) == pytest.approx(
        0.6
    )
    assert native.net_clearance_floor(nets["GOOD"], router.pads[("G1", "1")].net) == pytest.approx(
        0.3
    )


@pytest.mark.parametrize(
    "flags",
    [
        ["--no-auto-layers"],
        [],
        ["--no-auto-layers", "--adaptive-rules"],
        ["--adaptive-rules"],
    ],
)
def test_cli_passes_original_sidecar_after_auto_pour_staging(tmp_path, monkeypatch, flags):
    import kicad_tools.router as router_package
    from kicad_tools.cli.route_cmd import main

    class Loaded(BaseException):
        """Stop after exercising the real loader, before expensive routing."""

    board = tmp_path / "original.kicad_pcb"
    board.write_text(board_text(invalid=False))
    sidecar = board.with_suffix(".kicad_pro")
    sidecar.write_text(json.dumps(project()))
    output = tmp_path / "staged.kicad_pcb"
    output.with_suffix(".kicad_pro").write_text(json.dumps(project(1.0)))
    seen = []

    def capture(path, **kwargs):
        assert Path(path) == output
        assert kwargs["project_path"] == sidecar
        kwargs["force_python"] = True
        router, nets = load_pcb_for_routing(path, **kwargs)
        seen.append(router.rules.net_clearance_floors[nets["GOOD"]])
        raise Loaded()

    monkeypatch.setattr(router_package, "load_pcb_for_routing", capture)
    with pytest.raises(Loaded):
        main([str(board), "-o", str(output), "--force", "--quiet", *flags])
    assert seen == [0.2]

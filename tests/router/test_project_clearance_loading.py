"""The routing loader turns stricter project netclasses into per-net minima (#6243).

Ported from closed PR #5502 onto the item-level design: a net whose routing id
is neutralised (skipped plane, placement-excluded) keeps its authored minimum on
its own pads and copper rather than on a private net id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.core.project_clearance import ProjectClearanceError
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
            "netclass_assignments": {"BAD": ["Strict"], "PLANE": ["Plane"], "GOOD": ["Strict"]},
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
        # A staged copy's sidecar may have been rewritten; the original wins.
        board.with_suffix(".kicad_pro").write_text(json.dumps(project(1.0)))
    rules = DesignRules(trace_clearance=0.15, manufacturer="jlcpcb")
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
    assert rules.net_clearance_floors == {}
    assert router.rules.trace_clearance == 0.15  # fab scalar untouched
    assert router.rules.net_clearance_floors[nets["GOOD"]] == 0.6
    # Default-class nets carry no floor: the base already covers them.
    assert nets["PARTNER"] not in router.rules.net_clearance_floors
    # Neutralised pads keep their class minimum on the copper itself.
    assert router.pads[("X2", "1")].net == 0
    assert router.pads[("X2", "1")].authored_clearance == 0.6
    assert router.pads[("G1", "1")].net == 0
    assert router.pads[("G1", "1")].authored_clearance == 0.3
    assert router.pads[("P1", "1")].authored_clearance == 0.0
    assert router.existing_routes[0].net == 0
    assert router.existing_routes[0].authored_clearance == 0.6
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
    data = project()
    data["net_settings"]["meta"]["version"] = 99
    sidecar.write_text(json.dumps(data))
    with pytest.raises(ProjectClearanceError, match="Unsupported net_settings schema"):
        load_pcb_for_routing(str(board), rules=rules, force_python=True, validate_drc=False)
    sidecar.write_text("{not json")
    with pytest.raises(ProjectClearanceError, match="Cannot read project"):
        load_pcb_for_routing(str(board), rules=rules, force_python=True, validate_drc=False)


def test_default_only_project_is_byte_identical(tmp_path):
    """The fleet shape: one ``Default`` class, even with legacy/missing meta."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text(board_text(invalid=False))
    board.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]}})
    )
    router, _ = load_pcb_for_routing(
        str(board), rules=DesignRules(trace_clearance=0.15), force_python=True, validate_drc=False
    )
    assert router.rules.net_clearance_floors == {}
    assert all(pad.authored_clearance == 0.0 for pad in router.pads.values())
    assert router.grid.authored_violation is not None  # method present, gate dormant
    assert router.grid._authored_item_floors is False


def test_cli_passes_original_sidecar_after_auto_pour_staging(tmp_path, monkeypatch):
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
        assert Path(kwargs["project_path"]) == sidecar
        kwargs["force_python"] = True
        router, nets = load_pcb_for_routing(path, **kwargs)
        seen.append(router.rules.net_clearance_floors.get(nets["GOOD"]))
        raise Loaded()

    monkeypatch.setattr(router_package, "load_pcb_for_routing", capture)
    with pytest.raises(Loaded):
        main([str(board), "-o", str(output), "--force", "--quiet"])
    assert seen == [0.6]

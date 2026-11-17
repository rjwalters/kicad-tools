"""``kct route-auto`` runs the same authored-netclass final pass as ``kct route`` (#6254).

#6243 made ``kct route`` demote any net whose copper breaks a project's named
netclass minimum (``Autorouter.demote_authored_floor_violation_nets``).
route-auto's per-net output gate resolved a multi-class net with its own
largest-class model, so the two commands could disagree.  Pinned here on the
#6243 wall-corridor board: route-auto never ships HV copper below 0.5 mm, the
final pass refuses a result the shared helper would demote, and a net whose
lower-clearance class wins on priority is judged identically by both commands.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.orchestrator import RoutingOrchestrator
from kicad_tools.router.primitives import Segment
from kicad_tools.router.strategies import RoutingResult, RoutingStrategy
from kicad_tools.schema.pcb import PCB
from tests.router.test_authored_netclass_board import (
    HV_CLEARANCE,
    HV_Y,
    _netclass,
    _routed_census,
    project,
    write_board,
)

#: The board's Edge.Cuts starts at (100, 100): the orchestrator is board-relative.
ORIGIN = 100.0


def _orchestrator(path: Path) -> RoutingOrchestrator:
    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules

    text = path.read_text()
    return RoutingOrchestrator(
        pcb=PCB.load(str(path)),  # type: ignore[arg-type]
        rules=parse_pcb_design_rules(text).to_design_rules(),
        layer_stack=detect_layer_stack(text),
    )


def _straight_hv() -> RoutingResult:
    """HV straight through the 0.45 mm corridor: legal at 0.15, illegal at 0.5."""
    y = HV_Y - ORIGIN
    return RoutingResult(
        success=True,
        net="HV",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(3.0, y, 27.0, y, 0.2, Layer.F_CU, 1, "HV")],
    )


def test_route_auto_never_ships_hv_below_authored_minimum(tmp_path: Path) -> None:
    pcb = write_board(tmp_path)
    out = tmp_path / "routed.kicad_pcb"
    subprocess.run(
        [sys.executable, "-m", "kicad_tools.cli", "route-auto", str(pcb)]
        + ["--nets", "HV,SIG", "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert out.exists()  # SIG routes; HV is refused rather than written short
    assert _routed_census(out) == []


def test_final_pass_refuses_what_the_shared_helper_demotes(tmp_path: Path) -> None:
    orch = _orchestrator(write_board(tmp_path))
    result = _straight_hv()
    orch._enforce_authored_floors(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert not result.success
    assert not result.segments
    assert "authored netclass minimum" in result.error_message
    assert result.alternative_strategies[0].strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR


def test_default_only_project_skips_the_final_pass(tmp_path: Path) -> None:
    pcb = write_board(tmp_path)
    data = json.loads(pcb.with_suffix(".kicad_pro").read_text())
    data["net_settings"]["classes"] = [
        c for c in data["net_settings"]["classes"] if c["name"] == "Default"
    ]
    data["net_settings"]["netclass_patterns"] = []
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(data))
    orch = _orchestrator(pcb)
    assert orch._board_has_authored_floors() is False
    result = _straight_hv()
    orch._enforce_authored_floors(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success and result.segments


def test_lower_clearance_class_winning_on_priority_is_judged_identically(tmp_path: Path) -> None:
    """HV matches two classes; the 0.3 mm one has the smaller priority number, so it wins."""
    pcb = write_board(tmp_path)
    data = project("hv")
    data["net_settings"]["classes"].append(_netclass("LOW", 0.3, 0))
    data["net_settings"]["classes"][1]["priority"] = 5  # HV (0.5 mm) loses to LOW
    data["net_settings"]["netclass_patterns"].append({"netclass": "LOW", "pattern": "HV"})
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(data))

    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.router.io import load_pcb_for_routing

    # kct route's floor for HV...
    router, nets = load_pcb_for_routing(str(pcb), force_python=True, validate_drc=False)
    assert router.rules.net_clearance_floors == {nets["HV"]: pytest.approx(0.3)}
    # ...is the one route-auto's gate uses (the old largest-class model said 0.5).
    assert BoardClearanceRules.from_board(pcb).class_clearance_of("HV") == pytest.approx(0.3)

    orch = _orchestrator(pcb)
    result = _straight_hv()  # 0.45 mm gaps: legal at 0.3
    assert not orch._foreign_copper_conflicts(result)
    orch._enforce_authored_floors(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success and result.segments
    assert HV_CLEARANCE > 0.3

"""Smoke test: call every registered MCP tool end to end (Issue #7123).

Three tools shipped broken (#7111, #7114, #7115) because nothing ever called
every registered tool.  This module parametrizes over ``TOOL_REGISTRY`` and
calls each tool through ``MCPServer().call_tool`` on a per-test copy of the
``test_project`` fixture.

Rules:

* ``SMOKE_ARGS`` plus ``SKIP`` must cover the registry exactly, so a newly
  registered tool must opt in here (with args, or a reasoned SKIP entry).
* Exceptions propagate; any exception fails the test.
* Tools in ``EXPECT_SUCCESS`` must not return a failure payload.
* No network: sockets are blocked for the whole module.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("jsonschema")

from kicad_tools.mcp.server import MCPServer
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

FIXTURES = Path(__file__).parent / "fixtures" / "projects"
REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_INTENT_SPEC = REPO_ROOT / "boards" / "01-voltage-divider" / "project.kct"

# Tools that return success: false when kicad-cli is absent (and only these).
NEEDS_KICAD_CLI = {"export_gerbers", "export_assembly", "screenshot_board", "screenshot_schematic"}

# Tools that need an optional extra, mapped to the module to importorskip.
NEEDS_MODULE = {"board_view": "matplotlib"}


class Fixtures:
    """Per-test paths and helpers; everything lives under ``tmp``."""

    def __init__(self, tmp: Path, server: MCPServer) -> None:
        self.tmp = tmp
        self.server = server
        for ext in ("kicad_pcb", "kicad_sch", "kicad_pro"):
            shutil.copy(FIXTURES / f"test_project.{ext}", tmp / f"test_project.{ext}")
        self.pcb = str(tmp / "test_project.kicad_pcb")
        self.sch = str(tmp / "test_project.kicad_sch")
        self.out = tmp / "out"
        self.out.mkdir()

    def out_path(self, name: str) -> str:
        return str(self.out / name)

    def session(self) -> str:
        result = self.server.call_tool("start_session", {"pcb_path": self.pcb})
        return str(result["session_id"])

    def decision(self) -> tuple[str, str]:
        sid = self.session()
        result = self.server.call_tool(
            "record_decision", {"session_id": sid, "action": "move", "target": "R1"}
        )
        return sid, str(result["decision_id"])

    def checkpoint(self) -> tuple[str, str]:
        sid = self.session()
        result = self.server.call_tool("create_checkpoint", {"session_id": sid, "name": "smoke"})
        return sid, str(result["checkpoint_id"])


def _pcb(f: Fixtures) -> dict[str, Any]:
    return {"pcb_path": f.pcb}


def _sid(f: Fixtures) -> dict[str, Any]:
    return {"session_id": f.session()}


def _undo(f: Fixtures) -> dict[str, Any]:
    # Undo needs a pending move to undo.
    sid = f.session()
    f.server.call_tool("apply_move", {"session_id": sid, "ref": "R1", "x": 20.0, "y": 20.0})
    return {"session_id": sid}


def _annotate(f: Fixtures) -> dict[str, Any]:
    sid, did = f.decision()
    return {"session_id": sid, "decision_id": did, "feedback": "ok", "outcome": "success"}


def _restore(f: Fixtures) -> dict[str, Any]:
    sid, cid = f.checkpoint()
    return {"session_id": sid, "checkpoint_id": cid}


SMOKE_ARGS: dict[str, Callable[[Fixtures], dict[str, Any]]] = {
    "validate_assembly_bom": lambda f: {"schematic_path": f.sch},
    "export_gerbers": lambda f: {
        "pcb_path": f.pcb,
        "output_dir": f.out_path("gerbers"),
        "manufacturer": "generic",
    },
    "export_bom": lambda f: {"schematic_path": f.sch, "output_path": f.out_path("bom.csv")},
    # "generic": the default (jlcpcb) makes network calls (46 s with sockets blocked).
    "export_assembly": lambda f: {
        "pcb_path": f.pcb,
        "schematic_path": f.sch,
        "output_dir": f.out_path("assembly"),
        "manufacturer": "generic",
    },
    "placement_analyze": _pcb,
    "placement_place_unplaced": lambda f: {
        "pcb_path": f.pcb,
        "output_path": f.out_path("placed.kicad_pcb"),
    },
    "placement_suggestions": _pcb,
    "start_session": _pcb,
    "query_move": lambda f: {"session_id": f.session(), "ref": "R1", "x": 20.0, "y": 20.0},
    "apply_move": lambda f: {"session_id": f.session(), "ref": "R1", "x": 20.0, "y": 20.0},
    "undo_move": _undo,
    "commit_session": lambda f: {
        "session_id": f.session(),
        "output_path": f.out_path("committed.kicad_pcb"),
    },
    "rollback_session": _sid,
    "declare_interface": lambda f: {
        "session_id": f.session(),
        "interface_type": "i2c_standard",
        "nets": ["NET1", "NET2"],
    },
    "declare_power_rail": lambda f: {
        "session_id": f.session(),
        "net": "VIN",
        "voltage": 5.0,
    },
    "list_intents": _sid,
    "clear_intent": _sid,
    "record_decision": lambda f: {"session_id": f.session(), "action": "move", "target": "R1"},
    "get_decision_history": _sid,
    "annotate_decision": _annotate,
    "get_session_context": _sid,
    # Only session_id and name: #7121 removes score/drc_violation_count.
    "create_checkpoint": lambda f: {"session_id": f.session(), "name": "smoke"},
    "restore_checkpoint": _restore,
    "get_session_summary": _sid,
    "get_design_intent": lambda f: {"spec_path": str(DESIGN_INTENT_SPEC)},
    "measure_clearance": lambda f: {"pcb_path": f.pcb, "item1": "R1", "item2": "C1"},
    "get_drc_violations": _pcb,
    "board_summary": _pcb,
    "board_inspect": lambda f: {"pcb_path": f.pcb, "aspect": "footprints"},
    "get_unrouted_nets": _pcb,
    # NET1: pour nets (VIN/GND) report success but write nothing.
    "route_net": lambda f: {
        "pcb_path": f.pcb,
        "net_name": "NET1",
        "output_path": f.out_path("routed.kicad_pcb"),
    },
    "route_net_auto": lambda f: {
        "pcb_path": f.pcb,
        "net_name": "NET1",
        "output_path": f.out_path("routed_auto.kicad_pcb"),
    },
    "validate_pattern": lambda f: {
        "pcb_path": f.pcb,
        "pattern_type": "ldo",
        "components": {"regulator": "U1", "input_cap": "C1", "output_cap": "C2"},
    },
    "adapt_pattern": lambda f: {"pattern_type": "ldo", "component_mpn": "AMS1117-3.3"},
    "get_component_requirements": lambda f: {"component_mpn": "AMS1117-3.3"},
    "list_pattern_components": lambda f: {},
    "detect_mistakes": _pcb,
    "list_mistake_categories": lambda f: {},
    "optimize_placement": lambda f: {
        "pcb_path": f.pcb,
        "max_iterations": 5,
        "output_path": f.out_path("optimized.kicad_pcb"),
    },
    "evaluate_placement": _pcb,
    "resolve_placement_overlaps": lambda f: {
        "pcb_path": f.pcb,
        "output_path": f.out_path("resolved.kicad_pcb"),
    },
    "screenshot_board": lambda f: {
        "pcb_path": f.pcb,
        "max_size_px": 640,
        "output_path": f.out_path("board.png"),
    },
    "board_view": lambda f: {
        "pcb_path": f.pcb,
        "max_size_px": 640,
        "output_path": f.out_path("view.png"),
    },
    "screenshot_schematic": lambda f: {
        "sch_path": f.sch,
        "max_size_px": 640,
        "output_path": f.out_path("sch.png"),
    },
    "create_pcb_from_schematic": lambda f: {
        "schematic_path": f.sch,
        "output_path": f.out_path("from_sch.kicad_pcb"),
    },
    "get_recent_calls": lambda f: {},
    "ecosystem_list": lambda f: {},
    "ecosystem_show": lambda f: {"project_id": "kicadroutingtools"},
}

# Tools that cannot be called yet.  Every entry needs a reason and an issue number.
SKIP: dict[str, str] = {}

# Every called tool must return a non-failure payload.
EXPECT_SUCCESS = sorted(SMOKE_ARGS)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, *args, **kwargs):
        raise RuntimeError("network access is blocked in the MCP smoke test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _fixture_hashes() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(FIXTURES.glob("test_project.*"))
    }


def _need_kicad_cli() -> None:
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")


def test_every_registered_tool_has_smoke_args():
    registered = set(TOOL_REGISTRY)
    covered = set(SMOKE_ARGS) | set(SKIP)
    assert registered == covered, (
        f"registered tools without SMOKE_ARGS/SKIP: {sorted(registered - covered)}; "
        f"SMOKE_ARGS/SKIP entries for unregistered tools: {sorted(covered - registered)}"
    )
    assert not set(SMOKE_ARGS) & set(SKIP), "a tool is both in SMOKE_ARGS and SKIP"
    assert all(SKIP.values()), "every SKIP entry needs a reason"


@pytest.mark.parametrize("name", sorted(SMOKE_ARGS))
def test_tool_smoke(name, tmp_path):
    if name in NEEDS_KICAD_CLI:
        _need_kicad_cli()
    if name in NEEDS_MODULE:
        pytest.importorskip(NEEDS_MODULE[name])

    before = _fixture_hashes()
    server = MCPServer()
    fx = Fixtures(tmp_path, server)
    args = SMOKE_ARGS[name](fx)

    result = server.call_tool(name, args)  # exceptions propagate

    assert isinstance(result, dict), f"{name} returned {type(result).__name__}"
    if name in EXPECT_SUCCESS:
        assert result.get("success", True) is True and not result.get("error"), (
            f"{name} failed: {result.get('error')!r}"
        )
    assert _fixture_hashes() == before, f"{name} modified the checked-in fixtures"

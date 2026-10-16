"""The deadline/partial-save exit writes a routing-plan sidecar (#6694)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from kicad_tools.cli import route_cmd
from kicad_tools.router.routing_plan import RoutingPlan

_BOARD = '(kicad_pcb\n  (version 20240108)\n  (net 0 "")\n  (net 1 "A")\n)\n'


class _Router:
    def __init__(self, plan):
        self.routes = [object()]
        self.routing_plan = plan
        self.commit_journal = None
        self.net_names = {}

    def to_sexp(self, name_only=False):
        return '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))'

    def get_statistics(self):
        return {"nets_routed": 1, "segments": 1, "vias": 0}


def _arm(router, src, out):
    route_cmd._reset_partial_save_state()
    route_cmd._interrupt_state.update(
        router=router,
        output_path=out,
        pcb_path=src,
        quiet=True,
        interrupted=False,
        best_completed_attempt=False,
        dry_run=False,
    )


def test_partial_save_writes_plan_sidecar(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_BOARD)
    out = tmp_path / "out.kicad_pcb"
    _arm(_Router(RoutingPlan()), src, out)

    assert route_cmd._save_partial_results() is True

    partial = tmp_path / "out_partial.kicad_pcb"
    sidecar = tmp_path / "out_partial.routing_plan.json"
    assert partial.exists() and not out.exists()
    data = json.loads(sidecar.read_text())
    assert data["source"]["pcb"] == str(partial)
    assert RoutingPlan.from_dict(data) is not None


def test_partial_save_without_plan_writes_no_sidecar(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_BOARD)
    out = tmp_path / "out.kicad_pcb"
    _arm(_Router(None), src, out)

    assert route_cmd._save_partial_results() is True
    assert not (tmp_path / "out_partial.routing_plan.json").exists()


def test_plan_failure_does_not_lose_partial_board(tmp_path, monkeypatch):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_BOARD)
    out = tmp_path / "out.kicad_pcb"
    _arm(_Router(RoutingPlan()), src, out)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(route_cmd, "_write_routing_plan_sidecar", boom)
    assert route_cmd._save_partial_results() is True
    assert (tmp_path / "out_partial.kicad_pcb").exists()


def test_fleet_table_prefers_sidecar_with_board(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "routing_plan_fleet_table",
        Path(__file__).resolve().parent.parent / "scripts" / "routing_plan_fleet_table.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload = json.dumps(RoutingPlan().to_dict())
    (tmp_path / "out.routing_plan.json").write_text(payload)  # no sibling board
    (tmp_path / "out_partial.routing_plan.json").write_text(payload)
    (tmp_path / "out_partial.kicad_pcb").write_text(_BOARD)

    _, sidecar = mod._load_plan(tmp_path)
    assert sidecar.name == "out_partial.routing_plan.json"

    (tmp_path / "out_partial.kicad_pcb").unlink()  # nothing measurable: old behaviour
    _, sidecar = mod._load_plan(tmp_path)
    assert sidecar.name == "out.routing_plan.json"

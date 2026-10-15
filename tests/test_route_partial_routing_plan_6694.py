"""The deadline/partial-save exit writes a routing-plan sidecar (#6694)."""

from __future__ import annotations

import json
from types import SimpleNamespace

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

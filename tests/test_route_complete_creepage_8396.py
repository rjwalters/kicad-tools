"""``--complete`` rolls back links that add a pairwise (HV creepage) shortfall (#8396)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kicad_tools.cli.route_cmd import (
    _build_complete_link_report,
    _rollback_complete_creepage,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.primitives import Route, Segment
from tests.test_route_complete_pairwise_8277 import OY, _board, _x


def _router(tmp_path: Path, offsets: list[float]):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_board())
    table = build_pairwise_clearance_table({"/LINE": 150.0, "/NEUT": 0.0}, dru=0.2)
    routes = [
        Route(
            net=1,
            net_name="/LINE",
            segments=[
                Segment(_x(o), OY + 5, _x(o), OY + 15, 0.25, Layer.B_CU, net=1, net_name="/LINE")
            ],
        )
        for o in offsets
    ]
    return SimpleNamespace(
        rules=SimpleNamespace(pairwise_clearance=table, trace_clearance=0.2),
        routes=routes,
        existing_routes=[],
        _emitted_preserved_routes=[],
        _pairwise_attach_zone_pcb_path=str(board),
        _lattice_negotiation_stats=SimpleNamespace(
            elapsed_s=1.0, budget_s=None, deadline_hit=False
        ),
        _lattice_failure_reasons={},
    )


ARGS = SimpleNamespace(complete=True, _pairwise_required={})


def test_violating_link_is_rolled_back_and_clean_link_kept(tmp_path):
    router = _router(tmp_path, [7.15, 9.0])  # 0.825 mm gap vs 1.6 required; second is clear
    refusals = _rollback_complete_creepage(router, ARGS, quiet=True)

    assert len(refusals) == 1
    assert len(router.routes) == 1
    assert router.routes[0].segments[0].x1 == _x(9.0)
    ref = refusals[0]
    assert ref["reason"] == "creepage-rolled-back"
    assert {ref["net"], ref["blocking_net"]} == {"/LINE", "/NEUT"}
    assert ref["required_mm"] == 1.6
    assert ref["measured_mm"] < ref["required_mm"]


def test_refusal_is_listed_in_complete_report(tmp_path):
    router = _router(tmp_path, [7.15])
    _rollback_complete_creepage(router, ARGS, quiet=True)
    report = _build_complete_link_report(router, tmp_path / "b.kicad_pcb")
    assert report is not None
    (entry,) = report["unroutable_links"]
    assert entry["reason"] == "creepage-rolled-back"
    assert entry["blocking_net"] in {"/LINE", "/NEUT"}
    assert entry["required_mm"] == 1.6


def test_clean_links_untouched(tmp_path):
    router = _router(tmp_path, [9.0])
    assert _rollback_complete_creepage(router, ARGS, quiet=True) == []
    assert len(router.routes) == 1


def test_noop_without_complete_or_voltage_map(tmp_path):
    router = _router(tmp_path, [7.15])
    assert (
        _rollback_complete_creepage(
            router, SimpleNamespace(complete=False, _pairwise_required={}), quiet=True
        )
        == []
    )
    assert (
        _rollback_complete_creepage(
            router, SimpleNamespace(complete=True, _pairwise_required=None), quiet=True
        )
        == []
    )
    assert len(router.routes) == 1

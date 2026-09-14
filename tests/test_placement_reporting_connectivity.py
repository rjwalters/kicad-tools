"""Real analyzer/loader regressions for already-connected requested nets."""

from __future__ import annotations

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.benchmark.external import collect_report
from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.adaptive import AdaptiveAutorouter, RoutingResult
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.output import get_routing_diagnostics_json
from kicad_tools.router.reporting import RoutingPlacementReport


def _board(tmp_path, *, mixed=False, preconnected=False):
    names = ["ONE"]
    pads = [(1, "J1", 1, 1)]
    if mixed or preconnected:
        names += ["GOOD"]
        pads += [(2, "J2", 2, 2), (2, "J3", 8, 2)]
    if mixed:
        names += ["BAD", "PAIR"]
        pads += [(3, "J4", 15, 3), (4, "J5", 4, 3)]
    text = '(kicad_pcb (version 20240108) (generator "test") (general (thickness 1.6))\n'
    text += '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
    text += '(net 0 "")\n' + "".join(f'(net {i} "{name}")\n' for i, name in enumerate(names, 1))
    for start, end in [
        ((0, 0), (10, 0)),
        ((10, 0), (10, 10)),
        ((10, 10), (0, 10)),
        ((0, 10), (0, 0)),
    ]:
        text += f'(gr_line (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (layer "Edge.Cuts") (width 0.05))\n'
    for net, ref, x, y in pads:
        text += f'(footprint "Test" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net {net} "{names[net - 1]}")))\n'
    if mixed or preconnected:
        text += '(segment (start 2 2) (end 8 2) (width 0.25) (layer "F.Cu") (net 2))\n'
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text + ")\n")
    disposition = analyze_routing_placement(path, coupled_groups=[("BAD", "PAIR")] if mixed else ())
    router, netmap = load_pcb_for_routing(
        str(path),
        placement_disposition=disposition,
        force_python=True,
        validate_drc=False,
        load_existing_routes=True,
    )
    if mixed:
        # Represent the real fixture's GOOD trace as this run's routed geometry.
        router.routes = [r for r in router.existing_routes if r.net_name == "GOOD"]
        router.existing_routes = [r for r in router.existing_routes if r.net_name != "GOOD"]
    return path, disposition, router, netmap


def _adaptive_result(router, disposition, data):
    adaptive = AdaptiveAutorouter.__new__(AdaptiveAutorouter)
    return RoutingResult(
        routes=router.routes,
        layer_count=2,
        layer_stack=LayerStack.two_layer(),
        nets_requested=0,
        nets_routed=0,
        overflow=0,
        converged=adaptive._check_convergence(router, 0),
        iterations_used=0,
        statistics={},
        placement_report=RoutingPlacementReport(
            disposition, frozenset(data["placement_disposition"]["completed_nets"])
        ),
    )


def test_valid_single_pad_is_complete_without_a_route_or_copper(tmp_path):
    path, disposition, router, netmap = _board(tmp_path)
    assert disposition.requested_nets == frozenset({"ONE"})
    assert [n.net_name for n in NetStatusAnalyzer(path).analyze().complete] == ["ONE"]
    assert router.routes == []
    data = get_routing_diagnostics_json(router, netmap, 0, single_pad_count=1)
    assert data["summary"]["nets_requested"] == data["summary"]["nets_completed"] == 1
    assert data["summary"]["nets_failed"] == 0
    assert data["summary"]["total_nets_on_board"] == 1
    assert data["summary"]["nets_completed_without_new_routes"] == 1
    assert data["successful_routes"][0]["status"] == "already_connected"
    assert data["successful_routes"][0]["length_mm"] == 0
    assert _adaptive_result(router, disposition, data).converged is True
    report = collect_report(
        path,
        board_id="single",
        run_kicad_cli=False,
        route_exit_code=0,
        route_output_exists=True,
        placement_disposition=disposition,
    )
    assert report.placement_disposition.to_dict()["completed_nets"] == ["ONE"]
    assert report.copper.segment_count == report.completion.connections_total == 0


def test_mixed_single_pad_route_and_blocked_coupled_requests(tmp_path):
    path, disposition, router, netmap = _board(tmp_path, mixed=True)
    assert disposition.direct_invalid_nets == frozenset({"BAD"})
    assert disposition.coupled_invalid_nets == frozenset({"PAIR"})
    data = get_routing_diagnostics_json(router, netmap, 1, single_pad_count=1, min_completion=0)
    summary = data["summary"]
    assert summary["nets_requested"] == summary["total_nets_on_board"] == 4
    assert summary["nets_eligible"] == summary["nets_completed"] == 2
    assert summary["nets_placement_blocked"] == summary["nets_failed"] == 2
    assert summary["clean_success"] is False
    assert data["placement_disposition"]["completed_nets"] == ["GOOD", "ONE"]
    assert _adaptive_result(router, disposition, data).converged is False
    report = collect_report(
        path,
        board_id="mixed",
        run_kicad_cli=False,
        route_exit_code=0,
        route_output_exists=True,
        placement_disposition=disposition,
    )
    assert report.placement_disposition.to_dict()["completed_nets"] == ["GOOD", "ONE"]
    assert report.completion.nets_complete == 4  # blocked single pads stay physically complete
    assert report.copper.segment_count == 1
    assert report.route_outcome.outcome == "partial"


def test_preconnected_multipad_uses_existing_copper_without_inventing_routes(tmp_path, monkeypatch):
    _, _, router, netmap = _board(tmp_path, preconnected=True)
    assert router.routes == [] and len(router.existing_routes) == 1
    data = get_routing_diagnostics_json(router, netmap, 1, single_pad_count=1)
    assert data["placement_disposition"]["completed_nets"] == ["GOOD", "ONE"]
    assert data["summary"]["clean_success"] is True
    assert data["summary"]["total_nets_on_board"] == 2
    assert all(r["status"] == "already_connected" for r in data["successful_routes"])
    assert all(r["length_mm"] == 0 for r in data["successful_routes"])

    adaptive = AdaptiveAutorouter(
        width=10, height=10, components=[], net_map=netmap, max_layers=2, verbose=False
    )
    monkeypatch.setattr(adaptive, "_create_autorouter", lambda stack: router)
    monkeypatch.setattr(router, "route_all", lambda: [])
    result = adaptive.route(method="simple")
    assert result.converged is True
    assert result.nets_requested == result.nets_routed == 2
    assert result.routes == []

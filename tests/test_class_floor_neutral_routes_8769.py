"""Kept placement-disposition copper keeps its --net-class-map floor (#8769).

``io.load_pcb_for_routing`` rekeys the copper of a ``preserve_copper_nets``
net to net 0 and stores it in ``router.placement_neutral_routes`` with the
designer floor only.  A class map merged after loading (``--net-class-map``)
must raise (and, when lowered again, drop) that floor like it does for
neutralised pads (#8577).
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.authored_clearance import authored_violations
from kicad_tools.router.class_clearance_floors import refresh_router_class_floors
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

BASE = 0.15
BAD_CLASS = 0.25
KEPT_Y = 103.0
KEPT_W = 0.2


def _board(tmp_path):
    nets = {1: "BAD", 2: "GOOD"}
    table = " ".join(f'(net {i} "{n}")' for i, n in {0: "", **nets}.items())
    parts = []
    for ref, x, y, net in [
        ("X1", 125, 105, 1),  # off-board: makes BAD placement-invalid
        ("X2", 105, 103, 1),
        ("R1", 105, 107, 2),
        ("R2", 115, 107, 2),
    ]:
        parts.append(
            f'(footprint "t" (layer "F.Cu") (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
            f'  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") '
            f'(net {net} "{nets[net]}")))'
        )
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "t")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        f"  (setup (pad_to_mask_clearance 0)) {table}\n"
        "  (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))\n'
        + "\n".join(parts)
        + f"\n(segment (start 105 {KEPT_Y}) (end 115 {KEPT_Y}) (width {KEPT_W}) "
        '(layer "F.Cu") (net 1) (uuid "00000000-0000-4000-8000-000000000001"))\n)\n'
    )
    return path


def _load(tmp_path):
    path = _board(tmp_path)
    disposition = analyze_routing_placement(path)
    assert "BAD" in disposition.preserve_copper_nets
    rules = DesignRules(trace_clearance=BASE, via_clearance=BASE)
    router, net_map = load_pcb_for_routing(
        str(path),
        rules=rules,
        placement_disposition=disposition,
        force_python=True,
        validate_drc=False,
        router_class_floors=False,
    )
    return router, net_map


def _set_class(router, value):
    router.net_class_map["BAD"] = NetClassRouting(name="Bad", clearance=value)
    return refresh_router_class_floors(router)


def test_neutral_route_floor_follows_the_merged_class(tmp_path):
    router, _ = _load(tmp_path)
    (neutral,) = router.placement_neutral_routes
    assert neutral.net == 0
    assert neutral.authored_clearance == 0.0

    assert _set_class(router, BAD_CLASS) is True
    assert neutral.authored_clearance == pytest.approx(BAD_CLASS)
    assert router.grid._authored_item_floors is True


def test_lowering_the_class_drops_the_route_floor(tmp_path):
    router, _ = _load(tmp_path)
    (neutral,) = router.placement_neutral_routes
    _set_class(router, BAD_CLASS)
    _set_class(router, 0.1)
    assert neutral.authored_clearance == 0.0


def test_census_holds_foreign_copper_to_the_kept_route_class(tmp_path):
    router, net_map = _load(tmp_path)
    _set_class(router, BAD_CLASS)
    good = net_map["GOOD"]
    gap = 0.2  # legal at BASE, short of BAD_CLASS
    y = KEPT_Y + KEPT_W / 2 + gap + 0.075
    seg = Segment(108.0, y, 112.0, y, 0.15, Layer.F_CU, net=good, net_name="GOOD")
    hits = authored_violations(
        router.rules.net_clearance_floors,
        router.grid.pads,
        [Route(good, "GOOD", segments=[seg]), *router.existing_routes],
    )
    mine = [h for h in hits if h.net == good]
    assert mine and mine[0].required == pytest.approx(BAD_CLASS)


def test_native_stored_route_sync_restarts_when_the_route_floor_changes(tmp_path):
    """``sync_stored_routes`` is count-keyed; a floor change must force a re-copy."""
    from types import SimpleNamespace

    from kicad_tools.router.cpp_backend import sync_stored_routes

    router, _ = _load(tmp_path)
    grid = router.grid
    calls = []
    cpp = SimpleNamespace(
        _synced_route_count=len(grid.routes),
        invalidate_stored_routes=lambda: (
            calls.append("invalidate"),
            setattr(cpp, "_synced_route_count", 0),
        ),
        _layer_to_index={},
        _off_grid_stored_segments=[],
        _impl=SimpleNamespace(add_stored_segment=lambda *a, **k: calls.append(k)),
    )
    sync_stored_routes(cpp, grid, project=lambda layers: [])
    assert calls == []  # nothing changed: stays incremental

    _set_class(router, BAD_CLASS)
    sync_stored_routes(cpp, grid, project=lambda layers: [])
    assert calls[0] == "invalidate"
    assert cpp._synced_route_count == len(grid.routes)

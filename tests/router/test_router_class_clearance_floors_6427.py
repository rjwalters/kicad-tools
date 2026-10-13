"""Router net classes bind foreign copper at max(class(a), class(b)) (Issue #6427).

Board 02 routed a ``LINE_A`` (router class ``Audio``, 0.15 mm) trace 0.1647 mm
from a ``GND`` (router class ``Power``, 0.2 mm) via.  The project declares only
``Default``, so the #6243 authored floors were empty and every foreign pair was
checked at the 0.15 mm base -- by the trace optimizer, the native validator's
stored copper and the search alike -- while the routed sidecars (#6390)
declare both router classes and KiCad requires 0.2 mm.

These tests pin the wiring: a board with no authored classes gets its router
classes as per-net floors at load time, and the optimizer's two collision
checkers and the native validator then reject a 0.15-class shortcut that lands
0.165 mm from a 0.2-class via while still accepting one at >= 0.2 mm.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.router.class_clearance_floors import (
    class_clearances_by_name,
    merge_named_floors,
    refresh_router_class_floors,
)
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Via
from kicad_tools.router.rules import NET_CLASS_ANALOG, DesignRules, NetClassRouting

# Via at (110, 106), Ø0.8 -> its copper edge is 0.4 mm from the centre.
VIA_X, VIA_Y, VIA_D = 110.0, 106.0, 0.8
TRACE_W = 0.2


def _board_text() -> str:
    nets = {1: "GND", 2: "LINE_A"}
    parts = []
    for ref, x, y, net in [
        ("G1", 104, 104, 1),
        ("G2", 116, 104, 1),
        ("L1", 104, 109, 2),
        ("L2", 116, 109, 2),
    ]:
        parts.append(
            f'''(footprint "test" (layer "F.Cu") (at {x} {y})
          (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
          (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net {net} "{nets[net]}")))'''
        )
    table = "\n".join(f'(net {i} "{n}")' for i, n in nets.items())
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) {table}
      (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
      {" ".join(parts)})"""


@pytest.fixture
def loaded(tmp_path):
    """Load the board with no ``.kicad_pro`` -- no authored classes at all."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_board_text())
    rules = DesignRules(trace_clearance=0.15, via_clearance=0.15)
    router, nets = load_pcb_for_routing(
        str(board), rules=rules, force_python=True, validate_drc=False
    )
    return router, nets, rules


def _gnd_via(net: int) -> Route:
    via = Via(VIA_X, VIA_Y, 0.4, VIA_D, (Layer.F_CU, Layer.B_CU), net, "GND")
    return Route(net, "GND", vias=[via])


def _trace_y(gap: float) -> float:
    """Centreline y of a horizontal trace whose edge is ``gap`` from the via."""
    return VIA_Y + VIA_D / 2 + TRACE_W / 2 + gap


def test_router_classes_become_per_net_floors_without_a_project(loaded):
    router, nets, rules = loaded
    # The router's own classification (name heuristics) is what #6390 declares.
    assert router.net_class_map["GND"].clearance == pytest.approx(0.2)
    assert router.net_class_map["LINE_A"].clearance == pytest.approx(0.15)
    # Only the class stricter than the 0.15 base becomes a floor.
    assert router.rules.net_clearance_floors == {nets["GND"]: pytest.approx(0.2)}
    # The caller's rules object is never mutated, and no authored floor exists.
    assert rules.net_clearance_floors == {}
    assert router._authored_net_clearance_floors == {}
    assert router.rules.clearance_for_nets(nets["GND"], nets["LINE_A"], 0.15) == pytest.approx(0.2)


@pytest.mark.parametrize("gap,clear", [(0.165, False), (0.25, True)])
def test_optimizer_checkers_hold_foreign_trace_to_the_via_class(loaded, gap, clear):
    """The optimizer's narrow phase rejects the board-02 shortcut (0.165 mm)."""
    from kicad_tools.router.optimizer.collision import (
        GridCollisionChecker,
        VectorCollisionChecker,
    )

    router, nets, _ = loaded
    router.grid.mark_route(_gnd_via(nets["GND"]))
    y = _trace_y(gap)
    for checker in (
        GridCollisionChecker(router.grid, ignore_overflow=True),
        VectorCollisionChecker(router.grid),
    ):
        verdict = checker.path_is_clear(106, y, 114, y, Layer.F_CU, TRACE_W, nets["LINE_A"])
        assert verdict is clear, (type(checker).__name__, gap)


def test_without_class_floors_the_optimizer_accepted_the_shortcut(loaded):
    """Control: the pre-#6427 rules (no floors) passed the 0.165 mm shortcut."""
    from kicad_tools.router.optimizer.collision import (
        GridCollisionChecker,
        VectorCollisionChecker,
    )

    router, nets, _ = loaded
    router.rules.net_clearance_floors = {}
    router.grid.mark_route(_gnd_via(nets["GND"]))
    y = _trace_y(0.165)
    for checker in (
        GridCollisionChecker(router.grid, ignore_overflow=True),
        VectorCollisionChecker(router.grid),
    ):
        assert checker.path_is_clear(106, y, 114, y, Layer.F_CU, TRACE_W, nets["LINE_A"])


@pytest.mark.parametrize("gap,valid", [(0.17, False), (0.21, True)])
def test_native_validator_holds_stored_via_to_its_class(loaded, gap, valid):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    from kicad_tools.router import router_cpp

    router, nets, _ = loaded
    native = CppGrid.from_routing_grid(router.grid)._impl
    native.add_stored_via(VIA_X, VIA_Y, 0.4, VIA_D, nets["GND"])
    seg = router_cpp.Segment()
    y = _trace_y(gap)
    seg.x1, seg.y1, seg.x2, seg.y2 = 106.0, y, 114.0, y
    seg.width, seg.layer, seg.net = TRACE_W, 0, nets["LINE_A"]
    result = native.validate_route([seg], [], nets["LINE_A"], [], 0.15, 0.15, 0.15)
    assert result.valid is valid


def test_class_filter_keeps_only_classes_above_the_base():
    strict = NetClassRouting(name="Power", clearance=0.2)
    loose = NetClassRouting(name="Audio", clearance=0.15)
    odd = SimpleNamespace(clearance=float("nan"))
    assert class_clearances_by_name({"GND": strict, "A": loose, "X": odd}, 0.15) == {"GND": 0.2}
    assert class_clearances_by_name({"GND": strict}, 0.2) == {}
    assert class_clearances_by_name(None, 0.1) == {}


def test_merge_keeps_the_stricter_floor_and_neutral_pads():
    rules = DesignRules(trace_clearance=0.15, net_clearance_floors={1: 0.3})
    pads = [{"net": 0, "net_name": "PLANE"}, {"net": 2, "net_name": "GND"}]
    merged = merge_named_floors(
        rules, {"GND": 0.2, "A": 0.25, "PLANE": 0.4}, {"GND": 2, "A": 1}, [{"pads": pads}]
    )
    assert merged is not rules
    assert merged.net_clearance_floors == {1: 0.3, 2: 0.2}
    assert pads[0]["authored_clearance"] == 0.4
    assert "authored_clearance" not in pads[1]
    assert merge_named_floors(rules, {}, {"GND": 2}) is rules


def test_refresh_follows_cli_class_edits(loaded):
    """``--net-class-map`` / ``--analog-nets`` edits re-derive the floors."""
    router, nets, _ = loaded
    before = router.rules.net_clearance_floors
    router.net_class_map["LINE_A"] = NetClassRouting(name="Strict", clearance=0.3)
    assert refresh_router_class_floors(router)
    assert router.rules.net_clearance_floors is not before  # new dict: caches see it
    assert router.rules.net_clearance_floors == {nets["GND"]: 0.2, nets["LINE_A"]: 0.3}
    assert router.grid.rules.net_clearance_floors == router.rules.net_clearance_floors
    # Lowering a class drops the floor again; nothing authored pins it.
    router.net_class_map["LINE_A"] = NET_CLASS_ANALOG
    refresh_router_class_floors(router)
    expected = {nets["GND"]: 0.2}
    if NET_CLASS_ANALOG.clearance > 0.15:
        expected[nets["LINE_A"]] = NET_CLASS_ANALOG.clearance
    assert router.rules.net_clearance_floors == pytest.approx(expected)
    assert not refresh_router_class_floors(router)


def test_authored_index_extends_on_append_and_rebuilds_on_removal():
    """The cached index grows with committed routes instead of rebuilding.

    Router classes make the #6243 gate live on most boards, so a full rebuild
    per committed route was the gate's dominant cost.  An append must still be
    seen (same verdicts as a fresh index); a removal must still rebuild.
    """
    from kicad_tools.router.authored_clearance import AuthoredCopperIndex, grid_authored_index
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.primitives import Segment

    rules = DesignRules(trace_clearance=0.15, via_clearance=0.15, net_clearance_floors={1: 0.2})
    grid = RoutingGrid(20, 20, rules)
    first = Route(1, "GND", vias=[Via(5, 5, 0.4, 0.8, (Layer.F_CU, Layer.B_CU), 1)])
    grid.mark_route(first)
    index = grid_authored_index(grid)
    assert index is not None

    second = Route(1, "GND", vias=[Via(15, 15, 0.4, 0.8, (Layer.F_CU, Layer.B_CU), 1)])
    grid.mark_route(second)
    probe = Segment(13, 15 + 0.4 + 0.1 + 0.165, 17, 15 + 0.4 + 0.1 + 0.165, 0.2, Layer.F_CU, 2)
    assert grid_authored_index(grid) is index, "an append must extend, not rebuild"
    assert grid.authored_violation(probe, 2) is not None
    fresh = AuthoredCopperIndex(rules.net_clearance_floors, grid._pads, grid.routes)
    assert fresh.segment_violation(probe, 2) is not None

    grid.unmark_route(second)
    assert grid_authored_index(grid) is not index, "a removal must rebuild"
    assert grid.authored_violation(probe, 2) is None

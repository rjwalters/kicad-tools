"""Regression tests for Issue #6315: clearance-cell ownership between adjacent
fine-pitch pads of different nets.

Board 05's ``PHASE_C`` (U2.9) was unroutable whenever ``PHASE_B`` (U2.8, the
pad 0.65 mm above it) was routed in the same run, even on an empty board and
even with ``PHASE_C`` routed first.  Both nets are 0.4 mm wide.  The cause was
grid ownership, not routing order:

* ``RoutingGrid._add_pad_unsafe`` gave every clearance cell to the FIRST pad
  that touched it -- including the neighbour's own metal rows -- so the whole
  gap between U2.8 and U2.9, and U2.9's top metal row, belonged to
  ``PHASE_B``; and
* the C++ A* trace disc treats every foreign-owned blocked cell as copper, so
  a 0.4 mm ``PHASE_C`` trace had no legal cell to leave its own pad from,
  although a trace on U2.9's centreline clears both neighbours.

The fix: a pad's own metal belongs to it whatever the add order; a halo cell
reached by two different signal nets' pads is owned by the NEAREST pad and
recorded as shared; and the C++ A* resolves pad-derived cells with the exact
segment-vs-pad clearance test instead of trusting ``cell.net``.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

# Board 05 U2 (TI PWP0028C) geometry: 1.575 x 0.4 mm pads at 0.65 mm pitch,
# 0.065 mm grid, 0.4 mm wide phase traces.
PITCH = 0.65
PAD_W = 1.575
PAD_H = 0.4
RESOLUTION = 0.065
WIDE_TRACE = 0.4
WIDE_CLEARANCE = 0.2
PAD_X = 5.0
PAD_Y0 = 5.0


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=RESOLUTION,
    )


def _u2_pads(net_a: int = 1, net_b: int = 2) -> list[Pad]:
    """Four U2-style pads: unconnected, net A, net B, unconnected."""
    nets = [0, net_a, net_b, 0]
    return [
        Pad(
            x=PAD_X,
            y=PAD_Y0 + i * PITCH,
            width=PAD_W,
            height=PAD_H,
            net=net,
            net_name={0: "", net_a: "PHASE_B", net_b: "PHASE_C"}[net],
            layer=Layer.F_CU,
            ref="U2",
            pin=str(i + 7),
        )
        for i, net in enumerate(nets)
    ]


def _grid_with_pads(order: list[int]) -> tuple[RoutingGrid, list[Pad]]:
    grid = RoutingGrid(
        width=12.0,
        height=12.0,
        rules=_rules(),
        layer_stack=LayerStack.two_layer(),
    )
    pads = _u2_pads()
    for i in order:
        grid.add_pad(pads[i], pin_pitch=PITCH)
    return grid, pads


def _metal_cells(grid: RoutingGrid, pad: Pad) -> list[tuple[int, int]]:
    res = grid.resolution
    gx1 = math.ceil((pad.x - pad.width / 2 - grid.origin_x) / res)
    gx2 = math.floor((pad.x + pad.width / 2 - grid.origin_x) / res)
    gy1 = math.ceil((pad.y - pad.height / 2 - grid.origin_y) / res)
    gy2 = math.floor((pad.y + pad.height / 2 - grid.origin_y) / res)
    return [(gy, gx) for gy in range(gy1, gy2 + 1) for gx in range(gx1, gx2 + 1)]


def _window(grid: RoutingGrid, pads: list[Pad]):
    res = grid.resolution
    gx1 = math.floor((PAD_X - PAD_W / 2 - 1.0 - grid.origin_x) / res)
    gx2 = math.ceil((PAD_X + PAD_W / 2 + 1.0 - grid.origin_x) / res)
    gy1 = math.floor((pads[0].y - 1.0 - grid.origin_y) / res)
    gy2 = math.ceil((pads[-1].y + 1.0 - grid.origin_y) / res)
    return (slice(gy1, gy2 + 1), slice(gx1, gx2 + 1))


class TestPadHaloOwnership:
    """Grid-level ownership rules (Python ``RoutingGrid``, no routing)."""

    @pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]])
    def test_pad_metal_belongs_to_its_own_pad(self, order: list[int]) -> None:
        grid, pads = _grid_with_pads(order)
        for pad in pads:
            nets = {int(grid._net[0, gy, gx]) for gy, gx in _metal_cells(grid, pad)}
            assert nets == {pad.net}, (
                f"U2.{pad.pin} metal carries nets {nets}, expected only {pad.net} (order {order})"
            )

    def test_gap_between_two_signal_pads_is_shared(self) -> None:
        grid, pads = _grid_with_pads([0, 1, 2, 3])
        assert grid._shared_pad_halo, "the U2.8/U2.9 clearance gap must be recorded as shared"
        # Every shared cell lies between the two signal pads' metal.
        res = grid.resolution
        top = pads[1].y + PAD_H / 2
        bottom = pads[2].y - PAD_H / 2
        for key in grid._shared_pad_halo:
            gy = (key // grid.cols) % grid.rows
            wy = grid.origin_y + gy * res
            assert top - res <= wy <= bottom + res

    def test_gap_owner_is_the_nearest_pad(self) -> None:
        """Neither net owns the whole gap: each owns the half next to it."""
        grid, pads = _grid_with_pads([0, 1, 2, 3])
        owners = {int(grid._net.flat[key]) for key in grid._shared_pad_halo}
        assert owners == {pads[1].net, pads[2].net}

    def test_ownership_does_not_depend_on_pad_order(self) -> None:
        """Reverse-order self-check: the same grid whichever pad is added first."""
        grid_fwd, pads = _grid_with_pads([0, 1, 2, 3])
        grid_rev, _ = _grid_with_pads([3, 2, 1, 0])
        win = _window(grid_fwd, pads)
        for name in ("_net", "_blocked", "_pad_blocked", "_is_obstacle"):
            fwd = getattr(grid_fwd, name)[0][win]
            rev = getattr(grid_rev, name)[0][win]
            assert (fwd == rev).all(), f"{name} differs between pad add orders"
        assert grid_fwd._shared_pad_halo == grid_rev._shared_pad_halo


# ---------------------------------------------------------------------------
# End-to-end on the C++ backend
# ---------------------------------------------------------------------------


def _pad_edge_gap(pad: Pad, x1: float, y1: float, x2: float, y2: float) -> float:
    """Distance from segment centreline to an axis-aligned rectangular pad."""
    hw, hh = pad.width / 2, pad.height / 2
    best = math.inf
    steps = 200
    for i in range(steps + 1):
        t = i / steps
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        dx = max(abs(px - pad.x) - hw, 0.0)
        dy = max(abs(py - pad.y) - hh, 0.0)
        best = min(best, math.hypot(dx, dy))
    return best


def _route_two_wide_nets(order: tuple[str, str]):
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=12.0, height=12.0, rules=_rules())
    for name in ("PHASE_B", "PHASE_C"):
        ar.net_class_map[name] = NetClassRouting(
            name="HighCurrent", trace_width=WIDE_TRACE, clearance=WIDE_CLEARANCE
        )
    u2 = [
        {
            "number": p.pin,
            "x": p.x,
            "y": p.y,
            "width": p.width,
            "height": p.height,
            "net": p.net,
            "net_name": p.net_name,
            "layer": Layer.F_CU,
        }
        for p in _u2_pads()
    ]
    ar.add_component("U2", u2)
    ar.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": 1.0,
                "y": 3.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "PHASE_B",
                "layer": Layer.F_CU,
            },
            {
                "number": "2",
                "x": 1.0,
                "y": 8.5,
                "width": 1.0,
                "height": 1.0,
                "net": 2,
                "net_name": "PHASE_C",
                "layer": Layer.F_CU,
            },
        ],
    )
    if ar.backend_info.get("active") != "cpp":
        pytest.skip("Autorouter did not select the C++ backend")
    routes = {}
    net_ids = {"PHASE_B": 1, "PHASE_C": 2}
    for name in order:
        got = ar.route_net(net_ids[name])
        routes[name] = got
    return ar, routes


@requires_cpp
class TestTwoWideNetsOnAdjacentFinePitchPins:
    """AC 2: two 0.4 mm nets on adjacent 0.65 mm-pitch pins both route, legally."""

    @pytest.mark.parametrize("order", [("PHASE_B", "PHASE_C"), ("PHASE_C", "PHASE_B")])
    def test_both_nets_route_with_legal_clearance(self, order: tuple[str, str]) -> None:
        ar, routes = _route_two_wide_nets(order)
        for name in order:
            assert routes[name], f"{name} did not route (order {order})"

        pads = list(ar.pads.values())
        for route in ar.routes:
            for seg in route.segments:
                for pad in pads:
                    if pad.net == route.net or pad.layer != seg.layer:
                        continue
                    gap = _pad_edge_gap(pad, seg.x1, seg.y1, seg.x2, seg.y2) - seg.width / 2
                    assert gap >= WIDE_CLEARANCE - 1e-3, (
                        f"net {route.net} segment ({seg.x1:.3f},{seg.y1:.3f})-"
                        f"({seg.x2:.3f},{seg.y2:.3f}) is {gap:.3f} mm from "
                        f"{pad.ref}.{pad.pin} (needs {WIDE_CLEARANCE})"
                    )

    def test_cpp_grid_flags_pad_cells_and_shared_halo(self) -> None:
        ar, _ = _route_two_wide_nets(("PHASE_B", "PHASE_C"))
        grid = ar.grid
        cpp = getattr(grid, "_cpp_grid", None)
        if cpp is None:
            pytest.skip("no paired C++ grid")
        assert grid._shared_pad_halo
        key = next(iter(grid._shared_pad_halo))
        gx = key % grid.cols
        gy = (key // grid.cols) % grid.rows
        layer = key // (grid.cols * grid.rows)
        cell = cpp._impl.at(gx, gy, layer)
        assert cell.pad_cell
        assert cell.shared_pad_halo


@requires_cpp
def test_pad_geometry_clear_matches_exact_geometry() -> None:
    """``Grid3D.pad_geometry_clear``: centreline exit legal, off-centre not."""
    grid, pads = _grid_with_pads([0, 1, 2, 3])
    from kicad_tools.router.cpp_backend import CppGrid

    cpp = CppGrid.from_routing_grid(grid)
    impl = cpp._impl
    for pad in pads:
        grid_layer = 0
        impl.add_pad(
            pad.x, pad.y, pad.width, pad.height, pad.net, grid_layer, 0, 0.0, False, 0.0, False
        )
    pc = pads[2]  # net 2, between net 1 above and net 0 below
    half = WIDE_TRACE / 2
    # A 0.4 mm trace on U2.9's centreline, leaving to the left: 0.25 mm from
    # both neighbours' metal, legal at 0.2 mm.
    assert impl.pad_geometry_clear(
        pc.x - 1.5, pc.y, pc.x - 0.5, pc.y, 0, pc.net, half, WIDE_CLEARANCE
    )
    # Shifted 0.1 mm toward U2.8 it is only 0.15 mm from U2.8's metal.
    assert not impl.pad_geometry_clear(
        pc.x - 1.5, pc.y - 0.1, pc.x - 0.5, pc.y - 0.1, 0, pc.net, half, WIDE_CLEARANCE
    )


# ---------------------------------------------------------------------------
# Give-up message: a via in the net's OWN pad is not a via-via block
# ---------------------------------------------------------------------------


@requires_cpp
def test_via_in_own_pad_is_not_reported_as_via_via_block() -> None:
    """A net that cannot leave its own SMD pad (via-in-pad disallowed) must not
    fail with ``FAILURE_VIA_VIA_BLOCKED`` naming its own net -- board 05 printed
    "all via candidates blocked by stored-via geometry (blocking net PHASE_C)"
    for ``PHASE_C`` itself."""
    from kicad_tools.router import router_cpp
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
        manufacturer="jlcpcb",
    )
    grid = RoutingGrid(
        width=6.0, height=6.0, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    start = Pad(x=2.0, y=2.0, width=0.8, height=0.8, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=4.5, y=4.5, width=0.8, height=0.8, layer=Layer.B_CU, net=1, net_name="N1")
    grid.add_pad(start)
    grid.add_pad(end)
    cpp_grid = CppGrid.from_routing_grid(grid)
    if cpp_grid._impl.pad_count == 0:
        for p, layer in ((start, 0), (end, 3)):
            cpp_grid._impl.add_pad(
                p.x, p.y, p.width, p.height, p.net, layer, 0, 0.0, False, 0.0, False
            )
    # Wall the start pad in on F.Cu (net-0 static obstacle ring) so its only
    # way out is a via -- which would sit in its own pad.
    gx0, gy0 = grid.world_to_grid(start.x, start.y)
    for dx in range(-8, 9):
        for dy in range(-8, 9):
            if max(abs(dx), abs(dy)) in (7, 8):
                cpp_grid._impl.mark_blocked(gx0 + dx, gy0 + dy, 0, 0, True, False)

    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    pathfinder.route(start, end, negotiated_mode=False)
    info = pathfinder.get_last_failure_info()
    if info is None:
        return  # no failure diagnostic at all -- certainly not a self-block
    assert not (
        info["failure_reason"] == router_cpp.FAILURE_VIA_VIA_BLOCKED
        and info["blocking_via_net"] == start.net
    ), f"own pad reported as a via-via blocker: {info}"


# ---------------------------------------------------------------------------
# AC 3: classifier criterion
# ---------------------------------------------------------------------------


def _sparse_decide(search_failure):
    from kicad_tools.router.stuck_classifier import _decide

    return _decide(
        net_name="PHASE_C",
        net_number=30,
        pad_names=["U2.9"],
        blockers=[],
        worst_open_arc=12,
        local_congestion=0,
        nearest_blocker_mm=None,
        congestion_threshold=6,
        dense_cluster_threshold=12,
        search_failure=search_failure,
    )


def test_classifier_geometry_only_still_budget_starved() -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    assert _sparse_decide(None).classification is StuckClass.BUDGET_STARVED


@pytest.mark.parametrize("reason", ["no_path", "via_via_blocked", "pairwise_blocked"])
def test_classifier_geometric_search_failure_is_not_budget_starved(reason: str) -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    diag = _sparse_decide(reason)
    assert diag.classification is not StuckClass.BUDGET_STARVED
    assert diag.classification is StuckClass.ESCAPE_BLOCKED
    assert reason in diag.evidence


@pytest.mark.parametrize("reason", ["timeout", "iteration_limit", "deadline-exceeded"])
def test_classifier_budget_search_failure_stays_budget_starved(reason: str) -> None:
    from kicad_tools.router.stuck_classifier import StuckClass

    assert _sparse_decide(reason).classification is StuckClass.BUDGET_STARVED


def test_normalize_search_failure_maps_cpp_codes() -> None:
    from kicad_tools.router.stuck_classifier import (
        is_budget_search_failure,
        normalize_search_failure,
    )

    assert normalize_search_failure(None) is None
    assert normalize_search_failure(0) is None
    assert normalize_search_failure(1) == "no_path"
    assert normalize_search_failure(3) == "timeout"
    assert normalize_search_failure(5) == "via_via_blocked"
    assert is_budget_search_failure("iteration_limit")
    assert not is_budget_search_failure("no_path")


# ---------------------------------------------------------------------------
# Safety rails on the exact pad test (judge request on PR #6370)
#
# The bypass only ever RELAXES the disc on cells whose sole blocker is pad
# copper + pad clearance.  Each test below pins one rule that keeps it from
# being looser than the disc it replaces.
# ---------------------------------------------------------------------------


def _attached_grid(width: float = 6.0, height: float = 4.0, resolution: float = 0.05):
    """A ``RoutingGrid`` with its native mirror attached BEFORE any pad is
    added, so ``add_pad`` takes the incremental pad sync (the production
    ``Autorouter.add_component`` order)."""
    from kicad_tools.router.cpp_backend import CppGrid

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=resolution,
    )
    grid = RoutingGrid(width=width, height=height, rules=rules, layer_stack=LayerStack.two_layer())
    cpp = CppGrid.from_routing_grid(grid)
    return grid, cpp, rules


def _synced_cells(grid: RoutingGrid, pad: Pad, layer: int = 0):
    """Native cells inside ``pad``'s marking envelope that the sync blocked."""
    cpp = grid._cpp_grid
    reach = max(pad.width, pad.height) / 2 + 0.4
    gx1, gy1 = grid.world_to_grid(pad.x - reach, pad.y - reach)
    gx2, gy2 = grid.world_to_grid(pad.x + reach, pad.y + reach)
    cells = []
    for gy in range(max(gy1, 0), min(gy2, grid.rows - 1) + 1):
        for gx in range(max(gx1, 0), min(gx2, grid.cols - 1) + 1):
            if grid._blocked[layer, gy, gx]:
                cells.append(cpp._impl.at(gx, gy, layer))
    return cells


@requires_cpp
class TestDrilledPadsAreNeverPadSource:
    """(a) A PTH / NPTH pad also carries hole (and NPTH-slot edge) clearances
    the copper-only pad test does not model, so its cells keep the disc."""

    def test_smd_pad_cells_are_pad_source(self) -> None:
        grid, _cpp, _ = _attached_grid()
        pad = Pad(x=3.0, y=2.0, width=1.0, height=1.0, net=1, net_name="N", layer=Layer.F_CU)
        grid.add_pad(pad)
        cells = _synced_cells(grid, pad)
        assert cells and all(c.pad_cell for c in cells)

    @pytest.mark.parametrize("net, name", [(1, "N"), (0, "")], ids=["pth", "npth"])
    def test_drilled_pad_cells_are_not_pad_source(self, net: int, name: str) -> None:
        grid, _cpp, _ = _attached_grid()
        pad = Pad(
            x=3.0,
            y=2.0,
            width=1.6,
            height=1.6,
            net=net,
            net_name=name,
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pad)
        for layer in range(grid.num_layers):
            cells = _synced_cells(grid, pad, layer)
            assert cells, f"layer {layer}: the drilled pad blocked nothing"
            assert not any(c.pad_cell for c in cells), f"layer {layer}: drilled pad is pad_cell"


@requires_cpp
class TestPadCellProvenanceIsSticky:
    """(b) Once anything other than the pad sync has marked a cell, it is
    never pad-only again -- not even after a later pad sync."""

    def _pad_cell(self):
        grid, cpp, _ = _attached_grid()
        impl = cpp._impl
        impl.mark_blocked(10, 10, 0, 1, True, False, True, True)
        cell = impl.at(10, 10, 0)
        assert cell.pad_cell and cell.shared_pad_halo
        return impl, cell

    def _resync_as_pad(self, impl) -> None:
        impl.mark_blocked(10, 10, 0, 1, True, False, True, True)

    def test_set_cell_static_blocked_clears_and_stays_cleared(self) -> None:
        impl, cell = self._pad_cell()
        impl.set_cell_static_blocked(10, 10, 0, True)
        assert not cell.pad_cell and not cell.shared_pad_halo
        self._resync_as_pad(impl)
        assert not cell.pad_cell and not cell.shared_pad_halo

    def test_set_cells_static_blocked_clears_and_stays_cleared(self) -> None:
        impl, cell = self._pad_cell()
        impl.set_cells_static_blocked([10], [10], [0], True)
        assert not cell.pad_cell and not cell.shared_pad_halo
        self._resync_as_pad(impl)
        assert not cell.pad_cell and not cell.shared_pad_halo

    def test_non_pad_mark_blocked_clears_and_stays_cleared(self) -> None:
        impl, cell = self._pad_cell()
        impl.mark_blocked(10, 10, 0, 0, True, False)  # keepout / edge / copper
        assert not cell.pad_cell and not cell.shared_pad_halo
        self._resync_as_pad(impl)
        assert not cell.pad_cell and not cell.shared_pad_halo

    def test_non_pad_mark_before_the_pad_wins(self) -> None:
        _grid, cpp, _ = _attached_grid()
        impl = cpp._impl
        impl.mark_blocked(10, 10, 0, 0, True, False)
        self._resync_as_pad(impl)
        assert not impl.at(10, 10, 0).pad_cell


@requires_cpp
class TestBoardEdgeIsNeverPadOnly:
    """Judge question on PR #6370: ``pad_step_clear`` does not measure the
    board edge, so an edge-keepout cell must never be a ``pad_cell``,
    whichever of the edge keepout and the pad is marked first."""

    EDGE = [((0.0, 0.0), (6.0, 0.0))]  # bottom edge of the 6 x 4 mm grid

    def _edge_pad(self) -> Pad:
        # Its clearance envelope overlaps the 0.3 mm edge keepout.
        return Pad(x=3.0, y=0.6, width=1.0, height=0.6, net=1, net_name="N", layer=Layer.F_CU)

    def _edge_cells(self, grid: RoutingGrid):
        raster = grid._raster_only_blocked
        assert raster is not None
        cpp = grid._cpp_grid
        ys, xs = raster[0].nonzero()
        cells = [cpp._impl.at(int(x), int(y), 0) for y, x in zip(ys, xs, strict=True)]
        assert cells
        return cells

    def test_edge_then_pad(self) -> None:
        grid, _cpp, _ = _attached_grid()
        grid.add_edge_keepout(self.EDGE, 0.3)
        grid.add_pad(self._edge_pad())
        assert not any(c.pad_cell for c in self._edge_cells(grid))

    def test_pad_then_edge(self) -> None:
        grid, _cpp, _ = _attached_grid()
        grid.add_pad(self._edge_pad())
        grid.add_edge_keepout(self.EDGE, 0.3)
        assert not any(c.pad_cell for c in self._edge_cells(grid))

    def test_python_only_edge_carried_by_the_pad_sync(self) -> None:
        """The edge keepout reached only the Python grid (no native mirror
        attached yet), so the pad sync is the first to push those cells to
        C++ -- with ``pad_source``.  The #5662 raster-only plane must veto it."""
        grid, cpp, _ = _attached_grid()
        grid._cpp_grid = None
        grid.add_edge_keepout(self.EDGE, 0.3)
        grid._cpp_grid = cpp
        pad = self._edge_pad()
        grid.add_pad(pad)
        cells = self._edge_cells(grid)
        synced = [c for c in cells if c.blocked]
        assert synced, "the pad sync should have pushed some edge cells"
        assert not any(c.pad_cell for c in synced)
        # Cells of the same pad away from the edge keep the provenance.
        assert any(c.pad_cell for c in _synced_cells(grid, pad))


# Corridor between two foreign pads: a 0.2 mm trace of net N must pass a
# gap whose clearance cells belong to the pads' halos.  At ``CORRIDOR_GAP``
# the trace clears each pad by (0.55 - 0.2) / 2 = 0.175 mm: legal at the
# net's 0.15 mm, illegal at 0.2 mm.


CORRIDOR_GAP = 0.55


def _corridor(net_class_clearance: float | None = None, hole=None):
    """Route net N straight through the gap; returns (route_found, gap cell)."""
    from kicad_tools.router.cpp_backend import CppPathfinder

    grid, cpp, rules = _attached_grid()
    h = (4.0 - CORRIDOR_GAP) / 2
    for net, name, y in ((2, "A", h / 2), (3, "B", 4.0 - h / 2)):
        grid.add_pad(Pad(x=3.0, y=y, width=0.6, height=h, net=net, net_name=name, layer=Layer.F_CU))
    start = Pad(x=1.0, y=2.0, width=0.4, height=0.4, net=1, net_name="N", layer=Layer.F_CU)
    end = Pad(x=5.0, y=2.0, width=0.4, height=0.4, net=1, net_name="N", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    if hole is not None:
        cpp._impl.add_component_hole(hole[0], hole[1], hole[0], hole[1], hole[2])
    ncm = {}
    if net_class_clearance is not None:
        ncm["B"] = NetClassRouting(name="Wide", trace_width=0.2, clearance=net_class_clearance)
    pf = CppPathfinder(cpp, rules, diagonal_routing=True, net_class_map=ncm)
    pf.set_routable_layers([0])
    # Judge the native verdict alone: the Python fallback A* is a different
    # engine and must not paper over (or mask) the C++ decision.
    pf._try_python_fallback = lambda *a, **k: None
    gx, gy = grid.world_to_grid(3.0, 2.0)
    gap_cell = cpp._impl.at(gx, gy, 0)
    route = pf.route(start, end, negotiated_mode=False)
    return route is not None and bool(route.segments), gap_cell


@requires_cpp
class TestExactPadStepRules:
    def test_corridor_routes_only_through_the_exact_test(self) -> None:
        """Baseline: the gap centre is a foreign pad-only cell, and the net
        still routes -- the exact test resolved it."""
        routed, gap_cell = _corridor()
        assert gap_cell.blocked and gap_cell.pad_cell
        assert routed

    def test_net_class_floor_rejects_a_step_legal_only_at_the_net_clearance(self) -> None:
        """(c) Another net class demands 0.2 mm.  The pair rule is
        max(own, pad's class) and the pad's class is unknown natively, so the
        board-wide floor (0.2 mm) applies: 0.175 mm is rejected."""
        assert _corridor(net_class_clearance=0.15)[0]
        assert not _corridor(net_class_clearance=0.2)[0]

    def test_step_within_the_hole_guard_is_not_resolved(self) -> None:
        """(e) A component hole (an NPTH slot) the disc never sees sits
        0.5 mm from the corridor: inside the 0.75 mm guard, so the exact test
        declines and the disc decides (blocked)."""
        assert not _corridor(hole=(2.3, 1.3, 0.1))[0]

    def test_hole_beyond_the_guard_does_not_matter(self) -> None:
        assert _corridor(hole=(0.4, 0.4, 0.1))[0]


@requires_cpp
def test_kelvin_isolation_voids_and_restores_pad_cell() -> None:
    """(d) Kelvin branch isolation repaints a same-net terminal's cells as a
    net-0 obstacle for one search.  The exact pad test cannot see that
    reason, so ``pad_cell`` / ``shared_pad_halo`` must be voided for the
    search and restored afterwards."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.kelvin_obstacles import isolate_kelvin_branch

    router = Autorouter(5, 5, force_python=False, physics_enabled=False)
    if router.backend_info.get("active") != "cpp":
        pytest.skip("Autorouter did not select the C++ backend")
    for ref, x, y in [("R1", 1, 1), ("Q1", 3, 1), ("U1", 3, 3)]:
        router.add_component(
            ref, [{"number": "1", "x": x, "y": y, "net": 1, "net_name": "ISENSE_TEST"}]
        )
    pads = list(router.pads.values())
    grid = router.grid
    cpp = grid._cpp_grid
    x, y = grid.world_to_grid(3, 1)  # Q1: the non-terminal, isolated pad
    cell = cpp._impl.at(x, y, 0)
    cell.shared_pad_halo = True  # exercise both flags
    assert cell.pad_cell, "precondition: Q1's metal is a pad-only cell"
    with isolate_kelvin_branch(grid, pads, pads[0], pads[2]):
        assert not cell.pad_cell
        assert not cell.shared_pad_halo
    assert cell.pad_cell
    assert cell.shared_pad_halo

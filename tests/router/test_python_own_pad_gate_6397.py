"""Issue #6397: the #6375 own-pad step gate in the pure-Python A*.

#6375 holds every planar step of the C++ A* that touches the routing net's own
static pad copper / halo to the validator's segment-vs-pad verdict.  #6398
ported the gate to the one-way Python loop (``Router._route_impl``); #6397
ported it to the bidirectional loop, which #6527 later removed (no
production caller; board 05 never engaged it).  What stays here is the gate's
inputs, its step verdicts against the C++ gate and the validator, and the
end-to-end Python search on the #6375 fixture.

Also here: the Python half of Issue #6473 -- the within-path via hole-to-hole
check the C++ A* got in #6464 (``Pathfinder::own_path_vias_clear``).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import AStarNode, Router
from kicad_tools.router.primitives import Pad, Segment, Via
from kicad_tools.router.rules import DesignRules
from tests.router.test_pad_exact_validator_parity_6375 import (
    CASES,
    CLEARANCE,
    PITCH,
    TRACE,
    _pad_gap,
    _rules,
    _steps_near_the_signal_tip,
    _u2_pads,
)

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

NET = 1
TARGET = (7.5, 7.5)


class _Twin:
    """The #6375 U2 row plus a target pad, on a Python grid and its C++ mirror."""

    def __init__(
        self, rotated: bool, *, nc_rotation: float = 0.0, rules: DesignRules | None = None
    ):
        self.rules = rules or _rules()
        self.grid = RoutingGrid(
            width=12.0, height=12.0, rules=self.rules, layer_stack=LayerStack.two_layer()
        )
        # Created before the pads, as in the #6375 fixture: ``add_pad`` syncs
        # each pad into the attached native grid.
        self.cpp_grid = CppGrid.from_routing_grid(self.grid) if is_cpp_available() else None
        self.pads: list[Pad] = []
        for spec in _u2_pads(rotated):
            self._add(spec, "U2", rotation=0.0 if spec["net"] else nc_rotation, pitch=PITCH)
        self._add(
            {
                "number": "1",
                "x": TARGET[0],
                "y": TARGET[1],
                "width": 1.0,
                "height": 1.0,
                "net": NET,
                "net_name": "SIG",
            },
            "J2",
        )
        self.start = next(p for p in self.pads if p.ref == "U2" and p.net == NET)
        self.end = next(p for p in self.pads if p.ref == "J2")
        self.router = Router(self.grid, self.rules)
        self.steps = _steps_near_the_signal_tip(self.grid, rotated)

    def _add(self, spec: dict, ref: str, rotation: float = 0.0, pitch: float | None = None) -> None:
        pad = Pad(
            x=spec["x"],
            y=spec["y"],
            width=spec["width"],
            height=spec["height"],
            net=spec["net"],
            net_name=spec["net_name"],
            layer=Layer.F_CU,
            ref=ref,
            pin=spec["number"],
            rotation=rotation,
        )
        if pitch is None:
            self.grid.add_pad(pad)
        else:
            self.grid.add_pad(pad, pin_pitch=pitch)
        self.pads.append(pad)

    def cpp_pathfinder(self) -> CppPathfinder:
        assert self.cpp_grid is not None
        return CppPathfinder(self.cpp_grid, self.rules, diagonal_routing=True)

    def validator(self, step: tuple[int, int, int, int], width: float, refs) -> bool:
        """``validate_segment_clearance`` on one grid step, as reconstruction calls it."""
        gx1, gy1, gx2, gy2 = step
        ax, ay = self.grid.grid_to_world(gx1, gy1)
        bx, by = self.grid.grid_to_world(gx2, gy2)
        seg = Segment(ax, ay, bx, by, width, Layer.F_CU, NET, "SIG")
        return self.grid.validate_segment_clearance(
            seg,
            exclude_net=NET,
            component_pitches=self.router.component_pitches,
            exclude_refs=refs,
        )[0]


# ---------------------------------------------------------------------------
# The per-search inputs
# ---------------------------------------------------------------------------


def test_gate_inputs_match_the_validators_carveout_refs_and_narrowest_width() -> None:
    twin = _Twin(False)
    refs, width = twin.router._own_pad_gate_inputs(twin.start, twin.end)
    assert refs == {"U2", "J2"}
    assert width == pytest.approx(TRACE)

    # With a neck-down floor the gate measures at the narrowest emitted width.
    necked = DesignRules(
        trace_width=TRACE,
        trace_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=CLEARANCE,
        grid_resolution=0.05,
        min_trace_width=0.1,
    )
    twin = _Twin(False, rules=necked)
    assert twin.router.component_pitches["U2"] == pytest.approx(PITCH)
    assert necked.should_apply_neck_down("U2", PITCH)
    _refs, width = twin.router._own_pad_gate_inputs(twin.start, twin.end)
    assert width == pytest.approx(necked.get_neck_down_width(0.0, PITCH, base_width=TRACE))
    assert width < TRACE


@requires_cpp
def test_gate_inputs_match_the_native_gate_inputs() -> None:
    """Same carve-out refs and the same width the C++ search is handed."""
    from kicad_tools.router.cpp_backend import router_cpp

    twin = _Twin(False)
    pathfinder = twin.cpp_pathfinder()
    refs, width = twin.router._own_pad_gate_inputs(twin.start, twin.end)
    assert width == pytest.approx(pathfinder._narrowest_emit_width(twin.start, twin.end, TRACE))
    # A relaxed corridor (#2452) puts U2 in the native skip set; the Python
    # validator derives the same mode from the refs the gate hands it.
    twin.grid._relaxed_clearance_refs.add("U2")
    exclude, clamp = pathfinder._carveout_ref_hashes(twin.grid, twin.start, twin.end)
    assert exclude == [router_cpp.fnv1a_hash("U2")]
    assert clamp == []
    assert "U2" in (refs or set())


# ---------------------------------------------------------------------------
# C++ / Python parity
# ---------------------------------------------------------------------------


@requires_cpp
@pytest.mark.parametrize("rotated", [False, True], ids=["vertical", "rotated"])
@pytest.mark.parametrize("relaxed", [False, True], ids=["full-clearance", "corridor-relaxed"])
@pytest.mark.parametrize("width", [TRACE, 0.05], ids=["base-width", "neck-width"])
def test_step_verdict_matches_cpp_and_the_validator(rotated, relaxed, width) -> None:
    """Three verdicts on every step near the tip: Python gate, C++ gate, validator.

    The Python gate equals the Python validator and the C++ gate on every
    step, including the ones that land on the rule to float noise (#6509).
    """
    twin = _Twin(rotated)
    if relaxed:
        twin.grid._relaxed_clearance_refs.add("U2")
    pathfinder = twin.cpp_pathfinder()
    exclude, clamp = pathfinder._carveout_ref_hashes(twin.grid, twin.start, twin.end)
    pathfinder._impl.set_search_own_pad_gate(exclude, clamp, width)
    refs, _width = twin.router._own_pad_gate_inputs(twin.start, twin.end)
    memo: dict = {}
    disagreements = []
    rejected = 0
    for step in twin.steps:
        gx1, gy1, gx2, gy2 = step
        py = twin.router._own_pad_step_clear(gx1, gy1, gx2, gy2, 0, NET, width, refs, memo)
        cpp = bool(pathfinder._impl.own_pad_step_clear(gx1, gy1, gx2, gy2, 0, NET, width))
        validator = twin.validator(step, width, refs)
        rejected += not py
        if not py == cpp == validator:
            disagreements.append((step, py, cpp, validator))
    assert not disagreements, disagreements[:5]
    if relaxed:
        assert rejected == 0, "the same-component skip carve-out exempts every U2 pad"
    else:
        assert rejected, "the window must contain steps the gate rejects"


def test_gate_is_off_for_legacy_callers_and_relief_probes(monkeypatch) -> None:
    """``own_gate=None`` (relief probes: the validator's own skip, #3438) asks nothing."""
    twin = _Twin(False)
    calls: list[tuple] = []
    original = twin.router._own_pad_step_clear

    def _spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(twin.router, "_own_pad_step_clear", _spy)
    twin.router.set_relief_mode(True)
    twin.router.route(twin.start, twin.end)
    assert calls == []
    twin.router.set_relief_mode(False)
    twin.router.route(twin.start, twin.end)
    assert calls


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _python_autorouter(rotated: bool, target: tuple[float, float]) -> Autorouter:
    ar = Autorouter(width=12.0, height=12.0, rules=_rules(), force_python=True)
    ar.add_component("U2", _u2_pads(rotated))
    ar.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": target[0],
                "y": target[1],
                "width": 1.0,
                "height": 1.0,
                "net": NET,
                "net_name": "SIG",
                "layer": Layer.F_CU,
            }
        ],
    )
    return ar


def _route_with_spy(ar: Autorouter, monkeypatch):
    router = ar.router
    verdicts: list[bool] = []
    original = router._validate_route_clearance

    def _spy(*args, **kwargs):
        ok = original(*args, **kwargs)
        verdicts.append(ok)
        return ok

    monkeypatch.setattr(router, "_validate_route_clearance", _spy)
    start = next(p for p in ar.pads.values() if p.ref == "U2" and p.net == NET)
    end = next(p for p in ar.pads.values() if p.ref == "J2")
    return router.route(start, end), verdicts


@pytest.mark.parametrize(("rotated", "target"), CASES)
def test_python_search_returns_no_validator_rejected_candidate(
    rotated, target, monkeypatch
) -> None:
    """A start tail that crowds the NC neighbour never reaches the validator."""
    ar = _python_autorouter(rotated, target)
    route, verdicts = _route_with_spy(ar, monkeypatch)
    assert route is not None, "SIG did not route"
    assert verdicts == [True], "a candidate was built and then rejected"
    foreign = [p for p in ar.pads.values() if p.net != NET]
    for seg in route.segments:
        for pad in foreign:
            assert _pad_gap(pad, seg) >= CLEARANCE - 1e-3


# ---------------------------------------------------------------------------
# Issue #6473 (Python half): a path's own vias keep the hole-to-hole floor
# ---------------------------------------------------------------------------


def _walled_router() -> tuple[Router, list[Pad], DesignRules]:
    """The #6464 fixture on the Python grid: an F.Cu wall across a 2-layer
    board, so the route must hop to B.Cu and back.  The cheapest hop puts
    the two vias 0.4 mm drill-to-drill; the floor demands 0.5 mm."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
    )
    grid = RoutingGrid(width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.two_layer())
    pads = [
        Pad(
            x=x,
            y=1.5,
            width=0.2,
            height=0.2,
            net=NET,
            net_name="SIG",
            layer=Layer.F_CU,
            ref=ref,
            pin="1",
        )
        for x, ref in ((1.0, "A"), (5.0, "B"))
    ]
    for pad in pads:
        grid.add_pad(pad)
    wall_gx, _ = grid.world_to_grid(3.0, 0.0)
    for gy in range(grid.rows):
        grid._blocked[0, gy, wall_gx] = True
        grid._net[0, gy, wall_gx] = 2
    return Router(grid, rules), pads, rules


def test_python_search_spaces_its_own_vias(monkeypatch) -> None:
    router, pads, rules = _walled_router()
    verdicts: list[bool] = []
    original = router._validate_route_clearance

    def _spy(*args, **kwargs):
        ok = original(*args, **kwargs)
        verdicts.append(ok)
        return ok

    monkeypatch.setattr(router, "_validate_route_clearance", _spy)
    route = router.route(pads[0], pads[1])
    assert route is not None
    assert verdicts == [True], "a candidate was built and then rejected"
    assert len(route.vias) == 2
    a, b = route.vias
    gap = math.hypot(a.x - b.x, a.y - b.y) - (a.drill + b.drill) / 2
    assert gap >= 0.5 - 1e-4, f"same-route vias {gap:.3f} mm drill-to-drill (< 0.5)"


def test_unchecked_python_search_builds_a_rejected_via_pair(monkeypatch) -> None:
    """Without the check the first candidate has its vias 0.4 mm apart."""
    router, pads, _rules = _walled_router()
    monkeypatch.setattr(router, "_own_path_vias_clear", lambda current: True)
    verdicts: list[bool] = []
    original = router._validate_route_clearance

    def _spy(*args, **kwargs):
        ok = original(*args, **kwargs)
        verdicts.append(ok)
        return ok

    monkeypatch.setattr(router, "_validate_route_clearance", _spy)
    router.route(pads[0], pads[1])
    assert verdicts and verdicts[0] is False


def _child_via_anc(parent: AStarNode) -> AStarNode | None:
    """``via_anc`` for a child of ``parent``, as both Python A* loops set it (#6526)."""
    return parent if parent.via_from_parent else parent.via_anc


def _via_path(*via_columns: int, end: int, row: int = 10) -> AStarNode:
    """A straight path along ``row`` dropping a via at each of ``via_columns``."""
    node = AStarNode(0.0, 0.0, 0, row, 0)
    layer = 0
    for gx in range(1, end + 1):
        node = AStarNode(0.0, 0.0, gx, row, layer, node, False, via_anc=_child_via_anc(node))
        if gx in via_columns:
            layer = 1 - layer
            node = AStarNode(0.0, 0.0, gx, row, layer, node, True, via_anc=_child_via_anc(node))
    return node


# Issue #6528: every start column, not just the one that happens to round
# the on-floor gap (7 cells = 0.5 mm) up rather than to 0.4999999999999998.
@pytest.mark.parametrize("start", range(1, 40))
def test_own_path_vias_clear_walks_every_earlier_via(start: int) -> None:
    router, _pads, rules = _walled_router()
    floor_cells = math.ceil((0.5 + rules.via_drill) / rules.grid_resolution)  # 7 cells = 0.7 mm

    assert router._own_path_vias_clear(_via_path(end=start + 10))  # no via on the path yet
    assert not router._own_path_vias_clear(_via_path(start, end=start + floor_cells - 1))
    assert router._own_path_vias_clear(_via_path(start, end=start + floor_cells))
    # The nearest via is far enough, an EARLIER one is not: the walk must not
    # stop at the first ancestor.  (Path doubles back in x.)
    far = _via_path(start, start + 20, end=start + 20)
    back = far
    for gx in range(start + 19, start + 3, -1):
        back = AStarNode(0.0, 0.0, gx, 10, back.layer, back, False, via_anc=_child_via_anc(back))
    assert not router._own_path_vias_clear(back)
    # A via stack at one cell is one drill.
    stacked = _via_path(start, end=start)
    assert stacked.via_from_parent
    assert router._own_path_vias_clear(stacked)


@pytest.mark.parametrize("cells", [6, 7, 8])
def test_own_path_vias_clear_agrees_with_drill_spacing_validator(cells: int) -> None:
    """Issue #6528: the search and the tolerant validator agree on every pair.

    Via pairs 6, 7 and 8 cells apart (0.4, 0.5 and 0.6 mm drill-to-drill on
    the #6464 rules: drill 0.2 mm, floor 0.5 mm, 0.1 mm grid) along one row,
    for 50 start columns, measured at the world points ``grid_to_world``
    gives -- the same points ``_convert_path_to_route`` emits vias at.
    """
    router, _pads, rules = _walled_router()
    grid = router.grid
    disagreements = []
    for start in range(1, 51):
        node = _via_path(start, start + cells, end=start + cells)
        assert node.via_from_parent
        vias = [
            Via(
                x=wx,
                y=wy,
                drill=rules.via_drill,
                diameter=rules.via_diameter,
                layers=(Layer.F_CU, Layer.B_CU),
                net=NET,
            )
            for wx, wy in (grid.grid_to_world(gx, 10) for gx in (start, start + cells))
        ]
        validator_ok, _clearance, _loc = grid.validate_same_net_drill_spacing(
            vias[1], NET, route_vias=vias
        )
        search_ok = router._own_path_vias_clear(node)
        if search_ok != validator_ok:
            disagreements.append((start, search_ok, validator_ok))
        # 6 cells is under the floor for every column; 7 and 8 are legal.
        assert search_ok is (cells >= 7), (start, cells)
    assert disagreements == []


# --- Issue #6526: ``via_anc`` must never point at its own node ----------------


def _walk_vias_by_parent(node: AStarNode | None) -> list[AStarNode]:
    """Every via node on ``node``'s path, nearest first, found by walking ``parent``."""
    found = []
    while node is not None:
        if node.via_from_parent:
            found.append(node)
        node = node.parent
    return found


def _walk_vias_by_anc(node: AStarNode) -> list[AStarNode]:
    """The node sequence ``Router._own_path_vias_clear`` visits from ``node``."""
    found = []
    cur = node if node.via_from_parent else node.via_anc
    while cur is not None:
        found.append(cur)
        cur = cur.via_anc
    return found


def test_via_anc_walk_visits_every_via_on_the_path() -> None:
    """The ``via_anc`` chain is exactly the set of via nodes on the parent chain.

    Covers two separated vias, a via stack at one cell (F -> In1 -> B), and a
    node whose own edge is the path's first via.
    """
    node = AStarNode(0.0, 0.0, 0, 10, 0)
    nodes = [node]
    # (gx, via?) steps: planar, via, planar..., via, via (stack), planar
    steps = [(1, False), (1, True), (2, False), (3, False), (3, True), (3, True), (4, False)]
    layer = 0
    for gx, is_via in steps:
        if is_via:
            layer += 1
        node = AStarNode(0.0, 0.0, gx, 10, layer, node, is_via, via_anc=_child_via_anc(node))
        nodes.append(node)
    for n in nodes:
        assert n.via_anc is not n
        walked = _walk_vias_by_anc(n)
        assert [id(v) for v in walked] == [id(v) for v in _walk_vias_by_parent(n)]
    first_via = nodes[2]
    assert first_via.via_from_parent and first_via.via_anc is None
    assert _walk_vias_by_anc(first_via) == [first_via]


def test_python_search_leaves_no_cyclic_node_garbage() -> None:
    """With the cyclic GC off, no ``AStarNode`` outlives the search (Issue #6526).

    The search on this fixture places two vias.  When via nodes referenced
    themselves through ``via_anc`` their whole node tree survived the return
    (1135 / 2181 nodes) until the next full collection.
    """
    import gc

    router, pads, _rules = _walled_router()
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        route = router.route(pads[0], pads[1])
        assert route is not None and len(route.vias) == 2
        del route
        alive = sum(1 for obj in gc.get_objects() if isinstance(obj, AStarNode))
    finally:
        if was_enabled:
            gc.enable()
    assert alive == 0, f"{alive} AStarNode objects left for the cyclic GC"

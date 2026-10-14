"""Issue #6397: the #6375 own-pad step gate in the pure-Python BIDIRECTIONAL A*.

#6375 holds every planar step of the C++ A* that touches the routing net's own
static pad copper / halo to the validator's segment-vs-pad verdict.  #6398
ported the gate to the one-way Python loop (``Router._route_impl``).  The
second Python loop, ``Router.route_bidirectional`` /
``_expand_bidirectional_neighbors``, still admitted those steps unchecked: on
the #6375 fixture every meeting point it found was built on a pad exit that
crowds the NC neighbour, ``_reconstruct_bidirectional_route`` rejected it, and
the search returned no route at all.

Also here: the Python half of Issue #6473 -- the within-path via hole-to-hole
check the C++ A* got in #6464 (``Pathfinder::own_path_vias_clear``).
"""

from __future__ import annotations

import itertools
import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.heuristics import HeuristicContext
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import AStarNode, Router
from kicad_tools.router.primitives import Pad, Segment
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

    def expand(self, cell: tuple[int, int], *, backward: bool, own_gate) -> set[tuple[int, int]]:
        """Planar neighbours ``_expand_bidirectional_neighbors`` pushes from ``cell``."""
        router = self.router
        source, target = (self.end, self.start) if backward else (self.start, self.end)
        goal_gx, goal_gy = self.grid.world_to_grid(target.x, target.y)
        context = HeuristicContext(
            goal_x=goal_gx,
            goal_y=goal_gy,
            goal_layer=0,
            rules=self.rules,
            cost_multiplier=1.0,
            diagonal_routing=True,
            get_congestion=self.grid.get_congestion,
            get_congestion_cost=router._get_congestion_cost,
        )
        nodes: dict[tuple[int, int, int], AStarNode] = {}
        router._expand_bidirectional_neighbors(
            AStarNode(0.0, 0.0, cell[0], cell[1], 0),
            [],
            set(),
            {},
            nodes,
            context,
            source,
            [0],
            [0],
            router._get_pad_metal_bounds(source, TRACE),
            router._get_pad_metal_bounds(target, TRACE),
            False,
            1.0,
            1.0,
            seq_counter=itertools.count(),
            own_gate=own_gate,
        )
        return {(x, y) for (x, y, layer) in nodes if layer == 0}

    def gate_rejections(self, own_gate, *, backward: bool) -> list[tuple[int, int, int, int]]:
        """Steps the expansion admits with the gate off and refuses with it on."""
        rejected = []
        for cell in sorted({(gx1, gy1) for gx1, gy1, _gx2, _gy2 in self.steps}):
            off = self.expand(cell, backward=backward, own_gate=None)
            on = self.expand(cell, backward=backward, own_gate=own_gate)
            assert on <= off, "the gate may only remove neighbours"
            rejected.extend((cell[0], cell[1], nx, ny) for nx, ny in sorted(off - on))
        return rejected


def _in_box(box: tuple[int, int, int, int], gx: int, gy: int) -> bool:
    return box[0] <= gx <= box[2] and box[1] <= gy <= box[3]


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


@requires_cpp
@pytest.mark.parametrize("rotated", [False, True], ids=["vertical", "rotated"])
@pytest.mark.parametrize("backward", [False, True], ids=["forward", "backward"])
def test_bidirectional_expansion_matches_the_cpp_gate(rotated, backward) -> None:
    """The steps the bidirectional expansion drops are exactly the C++ gate's.

    For every planar step the ungated expansion admits: it is refused with
    the gate on if and only if it is outside both metal boxes, touches an
    own-pad cell on either end, and ``Pathfinder::own_pad_step_clear`` says
    the swept segment is too close to a pad.  Steps on the rule to float
    noise get the same verdict on both sides (#6509).
    """
    twin = _Twin(rotated)
    router = twin.router
    pathfinder = twin.cpp_pathfinder()
    exclude, clamp = pathfinder._carveout_ref_hashes(twin.grid, twin.start, twin.end)
    refs, width = router._own_pad_gate_inputs(twin.start, twin.end)
    pathfinder._impl.set_search_own_pad_gate(exclude, clamp, width)
    boxes = [router._get_pad_metal_bounds(p, TRACE) for p in (twin.start, twin.end)]

    expected = []
    for cell in sorted({(gx1, gy1) for gx1, gy1, _gx2, _gy2 in twin.steps}):
        for nx, ny in sorted(twin.expand(cell, backward=backward, own_gate=None)):
            if any(_in_box(box, nx, ny) for box in boxes):
                continue
            if not (router._own_pad_cell(nx, ny, 0, NET) or router._own_pad_cell(*cell, 0, NET)):
                continue
            if not pathfinder._impl.own_pad_step_clear(*cell, nx, ny, 0, NET, width):
                expected.append((*cell, nx, ny))

    actual = twin.gate_rejections((refs, width, {}), backward=backward)
    assert expected, "the window must contain steps the C++ gate rejects"
    assert set(actual) == set(expected), sorted(set(actual) ^ set(expected))[:5]


# ---------------------------------------------------------------------------
# Never stricter than the validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rotated", "nc_rotation"),
    [
        pytest.param(False, 0.0, id="vertical"),
        pytest.param(True, 0.0, id="rotated-row"),
        pytest.param(False, 20.0, id="skewed-nc-pads"),
    ],
)
@pytest.mark.parametrize("backward", [False, True], ids=["forward", "backward"])
def test_gate_never_rejects_a_step_the_validator_accepts(rotated, nc_rotation, backward) -> None:
    """Every step the bidirectional gate drops fails ``validate_segment_clearance``.

    Checked at the gate's (narrowest) width, so it also holds for the wider
    widths reconstruction emits away from a neck-down.
    """
    twin = _Twin(rotated, nc_rotation=nc_rotation)
    refs, width = twin.router._own_pad_gate_inputs(twin.start, twin.end)
    rejected = twin.gate_rejections((refs, width, {}), backward=backward)
    assert rejected, "the window must contain steps the gate rejects"
    stricter = [step for step in rejected if twin.validator(step, width, refs)]
    assert not stricter, stricter[:5]


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
    twin.router.route_bidirectional(twin.start, twin.end)
    assert calls == []
    twin.router.set_relief_mode(False)
    twin.router.route_bidirectional(twin.start, twin.end)
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


def _route_with_spy(ar: Autorouter, monkeypatch, method: str):
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
    return getattr(router, method)(start, end), verdicts


@pytest.mark.parametrize("method", ["route_bidirectional", "route"])
@pytest.mark.parametrize(("rotated", "target"), CASES)
def test_python_search_returns_no_validator_rejected_candidate(
    rotated, target, method, monkeypatch
) -> None:
    """A start tail that crowds the NC neighbour never reaches the validator."""
    ar = _python_autorouter(rotated, target)
    route, verdicts = _route_with_spy(ar, monkeypatch, method)
    assert route is not None, "SIG did not route"
    assert verdicts == [True], "a candidate was built and then rejected"
    foreign = [p for p in ar.pads.values() if p.net != NET]
    for seg in route.segments:
        for pad in foreign:
            assert _pad_gap(pad, seg) >= CLEARANCE - 1e-3


@pytest.mark.parametrize(("rotated", "target"), CASES)
def test_ungated_bidirectional_search_loses_the_net(rotated, target, monkeypatch) -> None:
    """Keeps the fixture honest: with the gate neutralised the only meeting
    point is rejected by the validator and the bidirectional search has no
    route (it cannot resume past a rejected goal the way ``route`` does)."""
    ar = _python_autorouter(rotated, target)
    monkeypatch.setattr(ar.router, "_own_pad_step_clear", lambda *a, **k: True)
    route, verdicts = _route_with_spy(ar, monkeypatch, "route_bidirectional")
    assert route is None
    assert verdicts == [False]


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


@pytest.mark.parametrize("method", ["route", "route_bidirectional"])
def test_python_search_spaces_its_own_vias(method, monkeypatch) -> None:
    router, pads, rules = _walled_router()
    verdicts: list[bool] = []
    original = router._validate_route_clearance

    def _spy(*args, **kwargs):
        ok = original(*args, **kwargs)
        verdicts.append(ok)
        return ok

    monkeypatch.setattr(router, "_validate_route_clearance", _spy)
    route = getattr(router, method)(pads[0], pads[1])
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


def test_own_path_vias_clear_walks_every_earlier_via() -> None:
    router, _pads, rules = _walled_router()
    floor_cells = math.ceil((0.5 + rules.via_drill) / rules.grid_resolution)  # 7 cells = 0.7 mm

    def _path(*via_columns: int, end: int) -> AStarNode:
        """A straight row-10 path dropping a via at each of ``via_columns``."""
        node = AStarNode(0.0, 0.0, 0, 10, 0)
        layer = 0
        for gx in range(1, end + 1):
            node = AStarNode(0.0, 0.0, gx, 10, layer, node, False, via_anc=_child_via_anc(node))
            if gx in via_columns:
                layer = 1 - layer
                node = AStarNode(0.0, 0.0, gx, 10, layer, node, True, via_anc=_child_via_anc(node))
        return node

    assert router._own_path_vias_clear(_path(end=20))  # no via on the path yet
    assert not router._own_path_vias_clear(_path(10, end=10 + floor_cells - 1))
    assert router._own_path_vias_clear(_path(10, end=10 + floor_cells))
    # The nearest via is far enough, an EARLIER one is not: the walk must not
    # stop at the first ancestor.  (Path doubles back in x.)
    far = _path(10, 30, end=30)
    back = far
    for gx in range(29, 13, -1):
        back = AStarNode(0.0, 0.0, gx, 10, back.layer, back, False, via_anc=_child_via_anc(back))
    assert not router._own_path_vias_clear(back)
    # A via stack at one cell is one drill.
    stacked = _path(10, end=10)
    assert stacked.via_from_parent
    assert router._own_path_vias_clear(stacked)


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


@pytest.mark.parametrize("method", ["route", "route_bidirectional"])
def test_python_search_leaves_no_cyclic_node_garbage(method) -> None:
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
        route = getattr(router, method)(pads[0], pads[1])
        assert route is not None and len(route.vias) == 2
        del route
        alive = sum(1 for obj in gc.get_objects() if isinstance(obj, AStarNode))
    finally:
        if was_enabled:
            gc.enable()
    assert alive == 0, f"{alive} AStarNode objects left for the cyclic GC"

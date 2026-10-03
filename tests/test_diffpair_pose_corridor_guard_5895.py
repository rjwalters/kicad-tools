"""Pose-trunk corridor guard (Issue #5895, Epic #5784).

A pose-centerline trunk (#5786) is committed by the diff-pair pre-phase,
before any single-ended net, and the main strategy cannot rip it.  On board 06
(seed 42) the MIPI_D0 trunk, together with the MIPI_CLK legs the main pass
routes next, left J4.RST (MIPI_RST) with no legal exit.  The board only got
back to 21/21 through the #4463 corridor yield, which lifts the trunk again
and costs a second full negotiated pass.

``DiffPairRouter._pose_corridor_guard`` asks before the commit: it probes the
unrouted nets next to the pair's end pads jointly, with and without the trunk,
re-runs the pose search once with a sealed net's probe path reserved, and
declines the trunk if it still seals one.  These tests pin:

1. which nets the guard looks at (``_pose_corridor_neighbours``);
2. that the joint probe lands nets on top of each other and leaves no trace
   (``_probe_nets_jointly``);
3. the guard's three outcomes -- keep, re-route, decline -- and that none of
   them leaves copper or claimed cells behind.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPair, DifferentialSignal
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


def _seg(x1: float, y1: float, x2: float, y2: float, layer: Layer, net: int) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net)


def _pad(ref: str, x: float, y: float, net: int, name: str) -> tuple[str, dict]:
    return ref, {
        "number": "1",
        "x": x,
        "y": y,
        "width": 0.6,
        "height": 0.6,
        "net": net,
        "net_name": name,
    }


def _router() -> Autorouter:
    """``SIG`` (net 3) crosses the board; ``FAR`` (net 6) sits far from the pair.

    The pair (nets 1/2) ends next to ``SIG``'s pads, so a trunk spanning the
    board on every layer strands ``SIG`` the way MIPI_D0 strands MIPI_RST.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
    router = Autorouter(width=20.0, height=8.0, rules=rules)
    for ref, pad in (
        _pad("U1", 2.0, 1.0, 3, "SIG"),
        _pad("U2", 18.0, 7.0, 3, "SIG"),
        _pad("U3", 10.0, 1.0, 6, "FAR"),
        _pad("U4", 10.0, 7.0, 6, "FAR"),
    ):
        router.add_component(ref, [pad])
    return router


def _pair() -> DifferentialPair:
    return DifferentialPair(
        name="PAIR",
        positive=DifferentialSignal("PAIR+", 1, "PAIR", "P", "plus_minus"),
        negative=DifferentialSignal("PAIR-", 2, "PAIR", "N", "plus_minus"),
    )


def _spec() -> SimpleNamespace:
    """End pads of the pair: next to SIG's pads, > 2.5 mm from FAR's."""
    at = SimpleNamespace
    return SimpleNamespace(
        p_start=at(x=3.0, y=1.0),
        n_start=at(x=3.5, y=1.0),
        p_end=at(x=17.0, y=7.0),
        n_end=at(x=16.5, y=7.0),
    )


def _wall(router: Autorouter) -> tuple[Route, Route]:
    """A "trunk" spanning the full width on every routable layer."""
    p = Route(net=1, net_name="PAIR+")
    n = Route(net=2, net_name="PAIR-")
    for idx in router.grid.get_routable_indices():
        layer = Layer(router.grid.index_to_layer(idx))
        p.segments.append(_seg(0.0, 4.0, 20.0, 4.0, layer, net=1))
        n.segments.append(_seg(0.0, 4.6, 20.0, 4.6, layer, net=2))
    return p, n


def _short_trunk() -> tuple[Route, Route]:
    """A trunk that leaves the channel open."""
    return (
        Route(net=1, net_name="PAIR+", segments=[_seg(9.0, 4.0, 11.0, 4.0, Layer.F_CU, 1)]),
        Route(net=2, net_name="PAIR-", segments=[_seg(9.0, 4.6, 11.0, 4.6, Layer.F_CU, 2)]),
    )


def _claimed_cells(router: Autorouter, net: int) -> int:
    grid = router.grid
    return sum(
        1
        for idx in grid.get_routable_indices()
        for y in range(grid.rows)
        for x in range(grid.cols)
        if grid.grid[idx][y][x].net == net and not grid.grid[idx][y][x].is_obstacle
    )


def _pathfinder() -> SimpleNamespace:
    return SimpleNamespace(last_pose_report={"applied": True}, _cpp_coupled_impl=object())


@pytest.fixture
def no_copper_gate(monkeypatch):
    """The exact pad/intra gates are #5786's concern, not the guard's."""

    def _install(router: Autorouter) -> None:
        monkeypatch.setattr(router._diffpair, "_pose_copper_rejection", lambda *a, **k: None)

    return _install


# ---------------------------------------------------------------------------
# 1. which nets the guard looks at
# ---------------------------------------------------------------------------


def test_neighbours_are_unrouted_nets_near_the_pair_end_pads():
    router = _router()
    dp = router._diffpair
    spec = _spec()
    anchors = [spec.p_start, spec.p_end, spec.n_start, spec.n_end]

    assert dp._pose_corridor_neighbours(_pair(), anchors) == [3]


def test_neighbours_skip_nets_that_already_have_copper():
    router = _router()
    dp = router._diffpair
    spec = _spec()
    anchors = [spec.p_start, spec.p_end, spec.n_start, spec.n_end]
    router.routes.append(
        Route(net=3, net_name="SIG", segments=[_seg(2.0, 1.0, 2.0, 2.0, Layer.F_CU, 3)])
    )

    assert dp._pose_corridor_neighbours(_pair(), anchors) == []


# ---------------------------------------------------------------------------
# 2. the joint probe
# ---------------------------------------------------------------------------


def test_joint_probe_keeps_earlier_nets_on_the_board_and_leaves_no_trace(monkeypatch):
    router = _router()
    dp = router._diffpair
    real_route_net = router.route_net
    seen: list[set[int]] = []

    def _spy(net_id: int, **kwargs):
        seen.append({r.net for r in router.routes})
        return real_route_net(net_id, **kwargs)

    monkeypatch.setattr(router, "route_net", _spy)
    routes_before = list(router.routes)

    results = dp._probe_nets_jointly([3, 6], deadline=float("inf"))

    assert results[3] and results[6], results
    assert seen == [set(), {3}], "the second probe must see the first net's copper"
    assert router.routes == routes_before
    assert _claimed_cells(router, 3) == 0
    assert _claimed_cells(router, 6) == 0


def test_joint_probe_reports_a_sealed_net():
    router = _router()
    dp = router._diffpair
    with dp._temporarily_marked(list(_wall(router))):
        results = dp._probe_nets_jointly([3], deadline=float("inf"))
    assert results == {3: None}
    assert _claimed_cells(router, 1) == 0, "the temporary trunk must be unmarked"


# ---------------------------------------------------------------------------
# 3. the guard
# ---------------------------------------------------------------------------


def test_guard_keeps_a_trunk_that_seals_nothing(monkeypatch, no_copper_gate):
    import kicad_tools.router.diffpair_pose as pose

    router = _router()
    dp = router._diffpair
    no_copper_gate(router)

    def _no_retry(*args, **kwargs):
        raise AssertionError("a trunk that seals nothing must not be re-routed")

    monkeypatch.setattr(pose, "route_centerline_pose", _no_retry)
    trunk = _short_trunk()
    pf = _pathfinder()

    assert dp._pose_corridor_guard(pf, _spec(), _pair(), trunk, 0.2, None) is trunk
    assert router.routes == []
    assert _claimed_cells(router, 1) == 0


def test_guard_declines_a_trunk_that_still_seals_after_the_retry(monkeypatch, no_copper_gate):
    import kicad_tools.router.diffpair_pose as pose

    router = _router()
    dp = router._diffpair
    no_copper_gate(router)
    calls: list[int] = []

    def _same_wall(pf, *args, **kwargs):
        calls.append(_claimed_cells(router, 3))
        return _wall(router)

    monkeypatch.setattr(pose, "route_centerline_pose", _same_wall)
    pf = _pathfinder()

    assert dp._pose_corridor_guard(pf, _spec(), _pair(), _wall(router), 0.2, None) is None
    assert pf.last_pose_report["reason"] == "seals-corridor"
    assert pf.last_pose_report["sealed"] == "SIG"
    assert len(calls) == 1 and calls[0] > 0, "the retry must see SIG's corridor reserved"
    assert router.routes == []
    for net in (1, 2, 3):
        assert _claimed_cells(router, net) == 0, f"net {net} left claimed cells"


def test_guard_commits_a_rerouted_trunk_that_keeps_the_corridor(monkeypatch, no_copper_gate):
    import kicad_tools.router.diffpair_pose as pose

    router = _router()
    dp = router._diffpair
    no_copper_gate(router)
    rerouted = _short_trunk()

    def _reroute(pf, *args, **kwargs):
        pf.last_pose_report = {"applied": True, "centerline_poses": 20}
        return rerouted

    monkeypatch.setattr(pose, "route_centerline_pose", _reroute)
    pf = _pathfinder()

    assert dp._pose_corridor_guard(pf, _spec(), _pair(), _wall(router), 0.2, None) is rerouted
    assert pf.last_pose_report["corridor_rerouted"] == "SIG"
    assert pf._cpp_coupled_impl is None, "the reserved-grid snapshot must not be reused"
    assert router.routes == []
    for net in (1, 2, 3):
        assert _claimed_cells(router, net) == 0


def test_guard_is_inert_without_neighbours(monkeypatch):
    router = _router()
    dp = router._diffpair
    far = SimpleNamespace(x=10.0, y=4.0)
    spec = SimpleNamespace(p_start=far, n_start=far, p_end=far, n_end=far)

    def _boom(*args, **kwargs):
        raise AssertionError("no neighbour -> no probe")

    monkeypatch.setattr(dp, "_probe_nets_jointly", _boom)
    trunk = _wall(router)
    assert dp._pose_corridor_guard(_pathfinder(), spec, _pair(), trunk, 0.2, None) is trunk

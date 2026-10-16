"""One engine owns a diff pair's copper under ``--route-engine lattice`` (Issue #6535).

``kct route --route-engine lattice --differential-pairs`` runs
``route_all_with_diffpairs``.  Its grid coupled pre-pass used to commit a P/N
trunk per pair, then the lattice negotiated the same pair nets again without
seeing that copper.  Both copper sets reached ``router.routes``, and the
post-route short check demoted whole nets (board 06b: every LVDSn_N net).
The pre-pass now leaves lattice-routed pairs to the lattice.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.rules import DesignRules, NetClassRouting

pytestmark = pytest.mark.timeout(300)

_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)


def _router(strategy: str) -> Autorouter:
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=12.0,
        rules=rules,
        net_class_map={"LVDS1_P": _HS, "LVDS1_N": _HS},
        strategy=strategy,
    )
    for ref, x in (("U1", 3.0), ("U2", 27.0)):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": 5.5,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "LVDS1_P",
                },
                {
                    "number": "2",
                    "x": x,
                    "y": 6.5,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 2,
                    "net_name": "LVDS1_N",
                },
            ],
        )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 3.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "SIG",
            },
            {
                "number": "2",
                "x": 27.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 3,
                "net_name": "SIG",
            },
        ],
    )
    return router


@pytest.mark.parametrize("engine", ["lattice", "mesh"])
def test_lattice_pair_nets_carry_only_lattice_copper(capsys, engine: str) -> None:
    """One engine owns pair copper under lattice AND mesh (#6535, #6551)."""
    if engine == "mesh":
        pytest.importorskip("kicad_tools.router.router_cpp")
    router = _router(engine)
    # The CLI's phase-2 strategy: route_all over every net.
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    out = capsys.readouterr().out
    if engine == "lattice":
        assert "grid coupled pre-pass skipped (issue #6535)" in out
    else:
        assert "routed UNCOUPLED by the mesh engine" in out
        assert "grid coupled pre-pass skipped (issue #6551)" in out
    assert "[coupled-pose]" not in out and "Routing differential pair LVDS1" not in out
    assert "routed by both the lattice and an earlier pass" not in out
    if engine == "lattice":
        assert router._lattice_pair_outcomes == {"LVDS1": "coupled"}
        engine_routes = router._lattice_net_routes
    else:
        engine_routes = router._mesh_net_routes
    for net in (1, 2):
        in_routes = [id(r) for r in router.routes if r.net == net]
        owned = [id(r) for r in engine_routes[net] if r.segments]
        assert in_routes, f"net {net} has no copper"
        # Every route on a pair net is the engine's: no second copper set.
        assert sorted(in_routes) == sorted(owned), f"net {net} routed by two engines"


def _seg_gap(a, b) -> float:
    """Edge-to-edge gap between two trace segments (negative when overlapping)."""
    import math

    def point_dist(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
        return math.hypot(px - x1 - t * dx, py - y1 - t * dy)

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    a1, a2, b1, b2 = (a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2)
    if orient(a1, a2, b1) * orient(a1, a2, b2) < 0 and orient(b1, b2, a1) * orient(b1, b2, a2) < 0:
        centre = 0.0
    else:
        centre = min(
            point_dist(*a1, *b1, *b2),
            point_dist(*a2, *b1, *b2),
            point_dist(*b1, *a1, *a2),
            point_dist(*b2, *a1, *a2),
        )
    return centre - (a.width + b.width) / 2


def test_mesh_foreign_net_does_not_cross_pair_copper(capsys) -> None:
    """Issue #6551 (B): on the ``--strategy basic`` path the mesh negotiated a
    foreign net without seeing the grid pre-pass trunk, so the net crossed
    it.  With the pre-pass skipped there is one copper owner and no short."""
    pytest.importorskip("kicad_tools.router.router_cpp")
    router = _router("mesh")
    # XSIG runs top-to-bottom straight through the pair corridor (y 5.5-6.5).
    router.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": 15.0,
                "y": 1.0,
                "width": 0.5,
                "height": 0.5,
                "net": 4,
                "net_name": "XSIG",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 11.5,
                "width": 0.5,
                "height": 0.5,
                "net": 4,
                "net_name": "XSIG",
            },
        ],
    )
    router.route_all_with_diffpairs(DifferentialPairConfig(enabled=True), timeout=120)
    out = capsys.readouterr().out
    routed_nets = {r.net for r in router.routes if r.segments}
    assert {1, 2, 4} <= routed_nets, f"pair or crossing net unrouted: {routed_nets}"
    pair_segs = [s for r in router.routes if r.net in (1, 2) for s in r.segments]
    other_segs = [(r.net, s) for r in router.routes if r.net not in (1, 2) for s in r.segments]
    assert pair_segs
    violations = [
        (net, pair_seg.net, round(_seg_gap(pair_seg, seg), 3))
        for pair_seg in pair_segs
        for net, seg in other_segs
        if seg.layer == pair_seg.layer and _seg_gap(pair_seg, seg) < 0.15 - 1e-6
    ]
    assert violations == [], f"foreign copper within clearance of pair copper: {violations}"
    assert "[coupled-pose]" not in out and "Routing differential pair LVDS1" not in out


def test_lattice_warns_on_pair_net_already_carrying_copper(capsys) -> None:
    """The tripwire: pair-net copper committed before the lattice runs is named."""
    from kicad_tools.router.primitives import Route, Segment

    router = _router("lattice")
    stale = Route(net=2, net_name="LVDS1_N")
    stale.segments.append(
        Segment(
            x1=3.0,
            y1=6.5,
            x2=27.0,
            y2=6.5,
            width=0.2,
            layer=router.pads[("U1", "2")].layer,
            net=2,
            net_name="LVDS1_N",
        )
    )
    router.routes.append(stale)
    router.route_net(1)
    out = capsys.readouterr().out
    assert (
        "WARNING: diff-pair net(s) routed by both the lattice and an earlier pass: LVDS1_N" in out
    )


def test_grid_engine_still_runs_the_coupled_pre_pass(capsys) -> None:
    """The skip is lattice/mesh-only: the grid engine keeps its coupled pre-pass."""
    router = _router("grid")
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    out = capsys.readouterr().out
    assert "grid coupled pre-pass skipped" not in out
    assert "Routing differential pair LVDS1" in out

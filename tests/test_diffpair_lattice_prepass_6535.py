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


def test_lattice_pair_nets_carry_only_lattice_copper(capsys) -> None:
    router = _router("lattice")
    # The CLI's lattice phase-2 strategy: route_all over every net.
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    out = capsys.readouterr().out
    assert "grid coupled pre-pass skipped (issue #6535)" in out
    assert "[coupled-pose]" not in out and "Routing differential pair LVDS1" not in out
    assert "routed by both the lattice and an earlier pass" not in out
    assert router._lattice_pair_outcomes == {"LVDS1": "coupled"}
    for net in (1, 2):
        in_routes = [id(r) for r in router.routes if r.net == net]
        lattice = [id(r) for r in router._lattice_net_routes[net] if r.segments]
        assert in_routes, f"net {net} has no copper"
        # Every route on a pair net is the lattice's: no second copper set.
        assert sorted(in_routes) == sorted(lattice), f"net {net} routed by two engines"


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
    """The skip is lattice-only: the grid engine keeps its coupled pre-pass."""
    router = _router("grid")
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    out = capsys.readouterr().out
    assert "grid coupled pre-pass skipped" not in out
    assert "Routing differential pair LVDS1" in out

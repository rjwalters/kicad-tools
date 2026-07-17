"""Engine dispatch + dormant-path tests for ``--route-engine lattice`` (#4278).

Acceptance 3: ``--route-engine grid`` AND ``--route-engine mesh`` stay
byte-identical to pre-phase -- neither dormant path may construct or even
import the lattice engine; the mesh regression suite (``tests/router/mesh``)
proves the mesh engine itself is untouched.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _add_pad(router: Autorouter, pad: Pad) -> None:
    key = (pad.ref, pad.pin)
    router.pads[key] = pad
    router.nets.setdefault(pad.net, []).append(key)


def _two_pad_router(strategy: str) -> Autorouter:
    router = Autorouter(50, 40, strategy=strategy)
    _add_pad(
        router,
        Pad(
            x=10, y=10, width=1, height=1, net=1, net_name="N1", layer=Layer.F_CU, ref="R1", pin="1"
        ),
    )
    _add_pad(
        router,
        Pad(
            x=30, y=25, width=1, height=1, net=1, net_name="N1", layer=Layer.F_CU, ref="R2", pin="1"
        ),
    )
    return router


# ---------------------------------------------------------------------------
# The lattice engine routes through the standard dispatch seam.
# ---------------------------------------------------------------------------


def test_route_net_lattice_routes_and_commits() -> None:
    router = _two_pad_router("lattice")
    # No Edge.Cuts -> grid-derived bbox fallback (same guard as the mesh).
    assert router._board_bbox is None

    routes = router.route_net(1)

    assert router._lattice_pathfinder is not None
    assert len(routes) == 1
    assert routes[0].segments
    assert router.routes, "committed routes must land in router.routes"
    # Negotiation stats recorded; the lattice was built exactly once.
    assert router._lattice_negotiation_stats is not None
    assert router._lattice_negotiation_stats.lattice_builds == 1
    # Serving the same net again must not duplicate committed copper.
    n_committed = len(router.routes)
    router.route_net(1)
    assert len(router.routes) == n_committed


def test_lattice_serves_cached_netset_per_net() -> None:
    router = _two_pad_router("lattice")
    _add_pad(
        router,
        Pad(
            x=12, y=30, width=1, height=1, net=2, net_name="N2", layer=Layer.F_CU, ref="R3", pin="1"
        ),
    )
    _add_pad(
        router,
        Pad(
            x=38, y=12, width=1, height=1, net=2, net_name="N2", layer=Layer.F_CU, ref="R4", pin="1"
        ),
    )
    routes1 = router.route_net(1)
    pf = router._lattice_pathfinder
    routes2 = router.route_net(2)
    # Second net is served from the SAME negotiated cache and pathfinder --
    # no rebuild, no re-negotiation.
    assert router._lattice_pathfinder is pf
    assert pf.lattice_builds == 1
    assert routes1 and routes2


# ---------------------------------------------------------------------------
# Dormant paths: grid and mesh never touch the lattice engine.
# ---------------------------------------------------------------------------


def test_grid_strategy_never_constructs_lattice_state() -> None:
    router = _two_pad_router("grid")
    router.route_net(1)
    assert router._lattice_pathfinder is None
    assert router._lattice_net_routes is None
    assert router._lattice_served == set()
    # The mesh engine is equally dormant on the grid path.
    assert router._mesh_pathfinder is None


def test_mesh_strategy_never_constructs_lattice_state() -> None:
    pytest.importorskip("kicad_tools.router.router_cpp")
    router = _two_pad_router("mesh")
    router.route_net(1)
    assert router._mesh_pathfinder is not None
    assert router._lattice_pathfinder is None
    assert router._lattice_net_routes is None


# ---------------------------------------------------------------------------
# CLI flag shape: third engine value, default unchanged.
# ---------------------------------------------------------------------------


def test_route_engine_flag_accepts_lattice_and_defaults_to_grid() -> None:
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["route", "board.kicad_pcb"])
    assert args.route_engine == "grid"
    for engine in ("grid", "mesh", "lattice"):
        args = parser.parse_args(["route", "board.kicad_pcb", "--route-engine", engine])
        assert args.route_engine == engine


def test_route_cmd_mirror_parser_accepts_lattice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The duplicate ``--route-engine`` definition in ``route_cmd.main`` is
    built inside the function; capture the parser at parse time to check it."""
    import argparse

    from kicad_tools.cli import route_cmd

    captured: dict[str, argparse.ArgumentParser] = {}

    class _Captured(Exception):
        pass

    def fake_parse_args(self: argparse.ArgumentParser, *a: object, **k: object) -> None:
        captured["parser"] = self
        raise _Captured

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)
    with pytest.raises(_Captured):
        route_cmd.main(["board.kicad_pcb"])
    monkeypatch.undo()

    parser = captured["parser"]
    args = parser.parse_args(["board.kicad_pcb", "--route-engine", "lattice"])
    assert args.route_engine == "lattice"
    args = parser.parse_args(["board.kicad_pcb"])
    assert args.route_engine == "grid"

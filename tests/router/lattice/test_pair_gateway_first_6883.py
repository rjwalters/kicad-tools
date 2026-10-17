"""Gateway-first ordering for pairs preferring a non-pad layer (issue #6883).

A soft ``preferred_layers`` cost bias cannot beat a successful planar run, so
a coupled pair whose class prefers a layer none of its pads share (and that is
legal for the class) gets its gateway attempt BEFORE the planar pad-layer run
commits.  A declined gateway falls back to the planar run; classes without a
non-pad preference are untouched.
"""

from __future__ import annotations

import dataclasses

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import NetClassRouting

from .test_pair_gateway_6444 import _HS, _OUTLINE, _board, _signature


def _prefer(pc: CoupledConnection, layers: list[int] | None) -> CoupledConnection:
    cls: NetClassRouting = dataclasses.replace(_HS, preferred_layers=layers)
    return dataclasses.replace(pc, net_class=cls)


def _run(pf, pc):
    return pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)


def test_pair_preferring_non_pad_layer_takes_the_gateway_over_a_clean_planar_run() -> None:
    pf, pc = _board()
    pc = _prefer(pc, [1])  # B.Cu: no pad owns it
    routes, _ = _run(pf, pc)
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    assert "B.Cu" in pf.pair_gateways[pc.key]
    body = [s for s in routes[(pc.key, "P")].segments if s.layer == Layer.B_CU]
    assert body, "common run must be on the preferred layer"


def test_declined_gateway_falls_back_to_the_planar_run() -> None:
    no_vias = LatticeKeepoutMask(
        [
            KeepoutArea(
                polygon=tuple(_OUTLINE),
                layers=frozenset({0, 1}),
                blocks_tracks=False,
                blocks_vias=True,
            )
        ]
    )
    pf, pc = _board()
    pf.set_keepouts(no_vias)
    pc = _prefer(pc, [1])
    routes, _ = _run(pf, pc)
    assert pf.pair_outcomes[pc.key] == "coupled"
    assert pf.pair_gateways == {}
    assert all(not r.vias for r in routes.values())
    assert {s.layer for r in routes.values() for s in r.segments} == {Layer.F_CU}


def test_pad_layer_or_absent_preference_is_unchanged() -> None:
    base_pf, base_pc = _board()
    base_routes, _ = _run(base_pf, base_pc)
    for pref in (None, [0]):
        pf, pc = _board()
        routes, _ = _run(pf, _prefer(pc, pref))
        assert pf.pair_gateways == {}
        assert _signature(routes) == _signature(base_routes)

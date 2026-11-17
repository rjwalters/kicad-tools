"""A gateway-first decline still earns the one-ended hybrid retry (issue #7858).

#6883 (gateway-first for a class preferring a non-pad layer) and #6446 (the
hybrid retry) merged in parallel.  The hybrid was wired only behind the
deferred gateway *retry*, which a gateway-first pair never takes, so a
gateway-first segment whose planar run declined went straight to the plain
uncoupled fallback: board 03's ``USB_D[0]`` (USB_D prefers In2.Cu) lost its
hybrid this way.  The ring fixture of ``test_pair_hybrid_6446`` with a B.Cu
preference reproduces it.
"""

from __future__ import annotations

import dataclasses

from kicad_tools.router.lattice.coupled import CoupledConnection

from .test_pair_hybrid_6446 import _HS, _board, _coupled_trunk_mm, _route


def _prefer_bcu(pc: CoupledConnection) -> CoupledConnection:
    return dataclasses.replace(pc, net_class=dataclasses.replace(_HS, preferred_layers=[1]))


def test_gateway_first_decline_is_offered_the_hybrid() -> None:
    pf, pc, _balls = _board()
    pc = _prefer_bcu(pc)
    routes, stats = _route(pf, pc)
    # The gateway-first attempt ran (confined to B.Cu) and declined ...
    assert "B.Cu" in pf.pair_gateway_log[pc.key]
    assert pc.key not in pf.pair_gateways
    # ... and the segment was still offered the hybrid, which couples it.
    assert pc.key in pf.pair_hybrid_log, pf.pair_gateway_log[pc.key]
    assert pf.pair_outcomes[pc.key] == "hybrid:U1.1", pf.pair_hybrid_log[pc.key]
    assert stats.routed == stats.total == 2
    assert _coupled_trunk_mm(routes, pc.key) > 0.0


def test_gateway_first_hybrid_matches_the_retry_path_hybrid() -> None:
    """Same verdict, same hybrid: the gateway-first entry adds no new search."""
    base_pf, base_pc, _ = _board()
    _route(base_pf, base_pc)
    pf, pc, _ = _board()
    _route(pf, _prefer_bcu(pc))
    assert pf.pair_outcomes[pc.key] == base_pf.pair_outcomes[base_pc.key]
    assert pf.pair_hybrid_log[pc.key] == base_pf.pair_hybrid_log[base_pc.key]


def test_no_hybrid_without_the_uncoupled_fallback() -> None:
    pf, pc, _balls = _board()
    pc = _prefer_bcu(pc)
    pf.route_netset([], coupled=[pc], uncoupled_fallback=False, pair_gateways=True)
    assert pc.key not in pf.pair_hybrid_log
    assert not pf.pair_outcomes.get(pc.key, "").startswith("hybrid:")

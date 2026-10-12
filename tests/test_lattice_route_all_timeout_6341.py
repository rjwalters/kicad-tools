"""Whole-netset routes survive ``route_all``'s outer timeout (issue #6341).

The lattice and mesh engines negotiate the WHOLE net set on the first
``route_net`` call and cache every net's routes; later ``route_net`` calls
only serve that cache.  On board 05 the lattice negotiation found 56/57
connections but took longer than ``route_all``'s outer ``timeout``, so the
between-nets check broke out "after 1/33 nets" and the 32 cached nets were
never committed -- the output board had no copper.

These tests make the negotiation consume the outer budget and assert every
cached net still reaches ``router.routes`` and ``route_all``'s return value,
and that the grid engine's timeout path is unchanged.
"""

from __future__ import annotations

import time

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad

# Both whole-netset engines route through the compiled C++ backend.
pytest.importorskip("kicad_tools.router.router_cpp")

# Three independent two-pad nets, well separated so each routes trivially.
_NETS = {
    1: ((10.0, 10.0), (40.0, 10.0)),
    2: ((10.0, 20.0), (40.0, 20.0)),
    3: ((10.0, 30.0), (40.0, 30.0)),
}

_TIMEOUT = 0.05


def _router(strategy: str) -> Autorouter:
    router = Autorouter(50, 40, strategy=strategy)
    for net, ends in _NETS.items():
        for idx, (x, y) in enumerate(ends):
            pad = Pad(
                x=x,
                y=y,
                width=1.0,
                height=1.0,
                net=net,
                net_name=f"N{net}",
                layer=Layer.F_CU,
                ref=f"R{net}{idx}",
                pin="1",
            )
            key = (pad.ref, pad.pin)
            router.pads[key] = pad
            router.nets.setdefault(net, []).append(key)
            router.net_names[net] = pad.net_name
    return router


def _slow_negotiation(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """Wrap the real negotiation so it overruns ``route_all``'s budget."""
    original = getattr(Autorouter, method)

    def slow(self: Autorouter):
        result = original(self)
        time.sleep(_TIMEOUT * 4)
        return result

    monkeypatch.setattr(Autorouter, method, slow)


@pytest.mark.parametrize(
    ("strategy", "method", "cache_attr"),
    [
        ("lattice", "_negotiate_lattice_netset", "_lattice_net_routes"),
        ("mesh", "_negotiate_mesh_netset", "_mesh_net_routes"),
    ],
)
def test_negotiated_routes_survive_outer_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    strategy: str,
    method: str,
    cache_attr: str,
) -> None:
    _slow_negotiation(monkeypatch, method)
    router = _router(strategy)

    returned = router.route_all(timeout=_TIMEOUT)

    out = capsys.readouterr().out
    # The outer budget really did fire after the first (negotiating) net.
    assert "outer timeout reached" in out
    assert "after 1/3 nets" in out
    assert "cached" in out

    cache = getattr(router, cache_attr)
    assert cache is not None
    cached_nets = {net for net, routes in cache.items() if any(r.segments for r in routes)}
    # Precondition: the negotiation routed every net, so the drain has work.
    assert cached_nets == set(_NETS)

    committed_nets = {r.net for r in router.routes if r.segments}
    returned_nets = {r.net for r in returned if r.segments}
    assert committed_nets == cached_nets
    assert returned_nets == cached_nets
    # Served exactly once: no duplicate commits of the first net.
    assert len(router.routes) == sum(len(cache[n]) for n in cached_nets)


def test_lattice_without_timeout_matches_drained_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drained routes are exactly what a budget-unlimited run serves."""
    unlimited = _router("lattice")
    unlimited.route_all(timeout=3600.0)

    _slow_negotiation(monkeypatch, "_negotiate_lattice_netset")
    drained = _router("lattice")
    drained.route_all(timeout=_TIMEOUT)

    def _shape(router: Autorouter) -> list[tuple[int, int, int]]:
        return sorted((r.net, len(r.segments), len(r.vias)) for r in router.routes)

    assert _shape(drained) == _shape(unlimited)


def test_grid_timeout_path_does_not_drain(capsys: pytest.CaptureFixture[str]) -> None:
    """The grid engine has no netset cache: the timeout break is unchanged."""
    router = _router("grid")
    assert router._drain_netset_cache([1, 2, 3]) is None

    router.route_all(timeout=0.0)

    out = capsys.readouterr().out
    assert "outer timeout reached" in out
    assert "cached" not in out
    # Only the first net (always given one full try) was routed.
    assert len({r.net for r in router.routes}) <= 1

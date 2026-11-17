"""Kelvin-rooted star topology in the lattice/mesh drivers (issue #4476).

``kct route --complete`` -- and therefore
:func:`kicad_tools.router.partial_rescue.complete_unfinished_nets`, the batch
rescue path board-05 uses -- drives the **lattice** engine, not the negotiated
grid ``MSTRouter``.  Phase 3 (#4473/#4499) taught ``build_rsmt``/``MSTRouter``
to route a Kelvin current-sense net as a star rooted at the shunt pad, but
:meth:`Autorouter._negotiate_lattice_netset` and
:meth:`Autorouter._negotiate_mesh_netset` still built a naive
``anchor = pads[0]`` star, so a completion pass silently re-introduced the
exact defect Phase 3 fixed.

These tests pin the fix:

1. :func:`_star_topology_pads` roots a recognised Kelvin net at the shunt pad
   and orders the terminals shortest-first.
2. Ordinary nets keep ``(pads[0], pads[1:])`` byte-identically.
3. The lattice driver's per-connection endpoints all anchor at the shunt.
4. The mesh driver does the same.
5. The diff-pair ``reserved`` (coupled) branch is untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kicad_tools.router.core import Autorouter, _star_topology_pads
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _pad(
    x: float,
    y: float,
    net: int,
    *,
    ref: str,
    pin: str = "1",
    net_name: str = "N1",
    layer: Layer = Layer.F_CU,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=net_name,
        layer=layer,
        ref=ref,
        pin=pin,
    )


def _kelvin_pads(net: int = 1, net_name: str = "ISENSE_A+") -> list[Pad]:
    """A 4-terminal ISENSE net whose shunt (R10.1) is NOT first in pad order.

    Distances from the shunt at (20, 20): U1.5 -> 5 mm, Q1.2 -> 10 mm,
    J1.3 -> 20 mm, so the Kelvin star's shortest-first order is
    ``[U1.5, Q1.2, J1.3]``.
    """
    return [
        _pad(25.0, 20.0, net, ref="U1", pin="5", net_name=net_name),
        _pad(20.0, 20.0, net, ref="R10", pin="1", net_name=net_name),
        _pad(20.0, 30.0, net, ref="Q1", pin="2", net_name=net_name),
        _pad(40.0, 20.0, net, ref="J1", pin="3", net_name=net_name),
    ]


def _add_pad(router: Autorouter, pad: Pad) -> None:
    key = (pad.ref, pad.pin)
    router.pads[key] = pad
    router.nets.setdefault(pad.net, []).append(key)
    if pad.net_name:
        router.net_names[pad.net] = pad.net_name


class _StubPathfinder:
    """Records the connections handed to ``route_netset`` and routes nothing."""

    def __init__(self) -> None:
        self.connections: list[tuple[object, Pad, Pad, object]] = []
        self.failure_reasons: dict[object, str] = {}

    def set_pairwise(self, pairwise: Any) -> None:
        """No-op stand-in for ``LatticePathfinder.set_pairwise`` (#4602)."""

    def route_netset(self, connections: list[Any], **_kwargs: Any) -> tuple[dict, Any]:
        self.connections = list(connections)
        return {}, SimpleNamespace(
            routed=0,
            total=len(connections),
            iterations=0,
            converged=True,
            lattice_builds=1,
        )


# ---------------------------------------------------------------------------
# 1-2. The shared star-topology helper.
# ---------------------------------------------------------------------------


def test_star_topology_roots_kelvin_net_at_shunt_pad() -> None:
    pads = _kelvin_pads()
    anchor, terminals = _star_topology_pads(pads)

    assert (anchor.ref, anchor.pin) == ("R10", "1")
    # Shortest-first ordering, matching build_kelvin_star_edges.
    assert [(p.ref, p.pin) for p in terminals] == [("U1", "5"), ("Q1", "2"), ("J1", "3")]
    # Every non-root terminal appears exactly once (a star, not a chain).
    assert len(terminals) == len(pads) - 1
    assert anchor not in terminals


def test_star_topology_leaves_ordinary_net_unchanged() -> None:
    """A non-sense net keeps the pre-#4476 ``(pads[0], pads[1:])`` shape."""
    pads = [
        _pad(25.0, 20.0, 2, ref="U1", pin="5", net_name="PWM_AH"),
        _pad(20.0, 20.0, 2, ref="R20", pin="1", net_name="PWM_AH"),
        _pad(20.0, 30.0, 2, ref="Q2", pin="2", net_name="PWM_AH"),
    ]
    anchor, terminals = _star_topology_pads(pads)

    assert anchor is pads[0]
    assert terminals == pads[1:]


def test_star_topology_sense_net_without_shunt_is_unchanged() -> None:
    """Name gate alone is not enough -- no resistor pad means no Kelvin root."""
    pads = [
        _pad(25.0, 20.0, 3, ref="U1", pin="5", net_name="ISENSE_B+"),
        _pad(20.0, 20.0, 3, ref="U2", pin="1", net_name="ISENSE_B+"),
        _pad(20.0, 30.0, 3, ref="Q3", pin="2", net_name="ISENSE_B+"),
    ]
    anchor, terminals = _star_topology_pads(pads)

    assert anchor is pads[0]
    assert terminals == pads[1:]


def test_star_topology_two_pad_net_is_unchanged() -> None:
    """A 2-pin net has no topology choice; detection is skipped entirely."""
    pads = [
        _pad(25.0, 20.0, 4, ref="U1", pin="5", net_name="ISENSE_C-"),
        _pad(20.0, 20.0, 4, ref="R12", pin="1", net_name="ISENSE_C-"),
    ]
    anchor, terminals = _star_topology_pads(pads)

    assert anchor is pads[0]
    assert terminals == pads[1:]


# ---------------------------------------------------------------------------
# 3. The lattice driver.
# ---------------------------------------------------------------------------


def _lattice_connections(pads: list[Pad]) -> list[tuple[object, Pad, Pad, object]]:
    router = Autorouter(60, 50, strategy="lattice")
    for pad in pads:
        _add_pad(router, pad)
    stub = _StubPathfinder()
    router._lattice_pathfinder = stub
    router._negotiate_lattice_netset()
    return stub.connections


def test_lattice_netset_anchors_kelvin_net_at_shunt() -> None:
    connections = _lattice_connections(_kelvin_pads())

    assert len(connections) == 3
    assert all((anchor.ref, anchor.pin) == ("R10", "1") for _k, anchor, _o, _c in connections)
    assert [(other.ref, other.pin) for _k, _a, other, _c in connections] == [
        ("U1", "5"),
        ("Q1", "2"),
        ("J1", "3"),
    ]


def test_lattice_connection_endpoints_report_the_kelvin_root() -> None:
    """The #4477 unroutable-link report names the shunt as every link's start."""
    router = Autorouter(60, 50, strategy="lattice")
    for pad in _kelvin_pads():
        _add_pad(router, pad)
    router._lattice_pathfinder = _StubPathfinder()
    router._negotiate_lattice_netset()

    endpoints = router._lattice_connection_endpoints
    assert endpoints
    assert all(start == "R10.1" for start, _end in endpoints.values())


def test_lattice_netset_ordinary_net_keeps_first_pad_anchor() -> None:
    pads = [
        _pad(25.0, 20.0, 2, ref="U1", pin="5", net_name="PWM_AH"),
        _pad(20.0, 20.0, 2, ref="R20", pin="1", net_name="PWM_AH"),
        _pad(20.0, 30.0, 2, ref="Q2", pin="2", net_name="PWM_AH"),
    ]
    connections = _lattice_connections(pads)

    assert len(connections) == 2
    assert all((anchor.ref, anchor.pin) == ("U1", "5") for _k, anchor, _o, _c in connections)
    assert [(other.ref, other.pin) for _k, _a, other, _c in connections] == [
        ("R20", "1"),
        ("Q2", "2"),
    ]


def test_lattice_netset_leaves_coupled_reserved_branch_alone() -> None:
    """The diff-pair ``reserved`` branch keeps its nearest-endpoint anchor.

    A coupled pair's extra pads attach to whichever RESERVED endpoint is
    nearest -- Kelvin root selection must not leak into that branch even when
    the pair net's name would trip the sense-name pattern.
    """
    router = Autorouter(60, 50, strategy="lattice")
    pads = [
        _pad(10.0, 10.0, 5, ref="U9", pin="1", net_name="ISENSE_D+"),
        _pad(50.0, 10.0, 5, ref="J9", pin="1", net_name="ISENSE_D+"),
        _pad(48.0, 12.0, 5, ref="R99", pin="1", net_name="ISENSE_D+"),
    ]
    for pad in pads:
        _add_pad(router, pad)
    stub = _StubPathfinder()
    router._lattice_pathfinder = stub
    # Reserve the two main-run endpoints exactly as an engaged pair would.
    reserved = {5: [("U9", "1"), ("J9", "1")]}
    router._lattice_coupled_connections = lambda: ([], reserved)  # type: ignore[method-assign]
    router._negotiate_lattice_netset()

    assert len(stub.connections) == 1
    _key, anchor, other, _cls = stub.connections[0]
    # Nearest reserved endpoint to R99 is J9 -- NOT the Kelvin root (R99).
    assert (anchor.ref, anchor.pin) == ("J9", "1")
    assert (other.ref, other.pin) == ("R99", "1")


# ---------------------------------------------------------------------------
# 4. The mesh driver.
# ---------------------------------------------------------------------------


def _mesh_connections(pads: list[Pad]) -> list[tuple[object, Pad, Pad, object]]:
    router = Autorouter(60, 50, strategy="mesh")
    for pad in pads:
        _add_pad(router, pad)
    stub = _StubPathfinder()
    router._mesh_pathfinder = stub
    router._negotiate_mesh_netset()
    return stub.connections


def test_mesh_netset_anchors_kelvin_net_at_shunt() -> None:
    connections = _mesh_connections(_kelvin_pads())

    assert len(connections) == 3
    assert all((anchor.ref, anchor.pin) == ("R10", "1") for _k, anchor, _o, _c in connections)
    assert [(other.ref, other.pin) for _k, _a, other, _c in connections] == [
        ("U1", "5"),
        ("Q1", "2"),
        ("J1", "3"),
    ]


def test_mesh_netset_ordinary_net_keeps_first_pad_anchor() -> None:
    pads = [
        _pad(25.0, 20.0, 2, ref="U1", pin="5", net_name="PWM_AH"),
        _pad(20.0, 20.0, 2, ref="R20", pin="1", net_name="PWM_AH"),
        _pad(20.0, 30.0, 2, ref="Q2", pin="2", net_name="PWM_AH"),
    ]
    connections = _mesh_connections(pads)

    assert len(connections) == 2
    assert all((anchor.ref, anchor.pin) == ("U1", "5") for _k, anchor, _o, _c in connections)

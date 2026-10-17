"""Keep the post-route trace optimizer from merging Kelvin branches (#5398).

The router builds a recognised Kelvin sense net as a star: every terminal
gets its own branch from the shunt pad, and the branches share copper only
on that pad (see :mod:`kicad_tools.router.kelvin` and
:mod:`kicad_tools.router.kelvin_obstacles`).  The trace optimizer runs after
routing and rewrites each route on its own.  Its collision checker only
guards *other* nets, so a chamfered corner or a compressed staircase on one
branch can run straight over a sibling branch of the same net.  KiCad sees
nothing wrong (same net, no clearance rule), but the sense tap has moved off
the shunt pad onto another branch.

:class:`KelvinBranchGate` is an ``accept`` hook for
:func:`~kicad_tools.router.optimizer.trace.apply_route_transform_grid_synced`.
For a route on a recognised Kelvin net it lists the same-net copper the route
touches outside the shunt pad: sibling routes and the net's other terminal
pads.  A rewrite that touches anything the original route did not touch is
rejected and the original geometry is kept.  Contact that the router already
committed is inherited, not introduced, so it never causes a veto.  Routes on
other nets pass through without any geometry work.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from ..kelvin import detect_kelvin_topology
from ..kelvin_obstacles import _pad_outline

if TYPE_CHECKING:
    from ..primitives import Pad, Route


class KelvinBranchGate:
    """Reject optimizer rewrites that join two branches of a Kelvin star."""

    def __init__(self, grid: Any, kelvin: dict[int, tuple[Pad, list[Pad]]]):
        self.grid = grid
        # net id -> (shunt root pad, every terminal pad of the net)
        self.kelvin = kelvin
        self.vetoes = 0
        self.vetoed_nets: set[str] = set()

    @classmethod
    def from_router(cls, router: object) -> KelvinBranchGate | None:
        """Build the gate, or ``None`` when the board has no Kelvin net."""
        grid = getattr(router, "grid", None)
        nets = getattr(router, "nets", None)
        pads = getattr(router, "pads", None)
        if grid is None or not nets or not pads:
            return None
        routed = {route.net for route in getattr(router, "routes", ())}
        # Escape overrides move a terminal's routing endpoint; judge contact
        # against the physical pad metal, as the branch isolation does.
        physical = {pad.key: pad for pad in getattr(grid, "_pads", ()) if pad.key[0] and pad.pin}
        kelvin: dict[int, tuple[Pad, list[Pad]]] = {}
        for net in routed:
            keys = nets.get(net) or []
            net_pads = [physical.get(key, pads[key]) for key in keys if key in pads]
            if len(net_pads) < 3:
                continue
            topology = detect_kelvin_topology(net_pads)
            if topology is None:
                continue
            kelvin[net] = (net_pads[topology.root_index], net_pads)
        if not kelvin:
            return None
        return cls(grid, kelvin)

    def _layers(self, pad: Pad) -> list[int]:
        if pad.through_hole:
            return list(range(self.grid.num_layers))
        return [self.grid.layer_to_index(pad.layer.value)]

    def _copper(self, route: Route) -> dict[int, Any]:
        """Full-width copper of ``route`` per copper layer index."""
        shapes: dict[int, list[Any]] = {}
        for segment in route.segments:
            layer = self.grid.layer_to_index(segment.layer.value)
            line = LineString([segment.start, segment.end])
            shapes.setdefault(layer, []).append(line.buffer(segment.width / 2))
        for via in route.vias:
            first, last = sorted(
                self.grid.layer_to_index(layer.value)
                for layer in getattr(via, "drilled_layers", via.layers)
            )
            disc = Point(via.x, via.y).buffer(via.diameter / 2)
            for layer in range(first, last + 1):
                shapes.setdefault(layer, []).append(disc)
        return {layer: unary_union(parts) for layer, parts in shapes.items()}

    def _contacts(self, route: Route, siblings: list[Route]) -> set[tuple[str, Any]]:
        """Same-net copper ``route`` touches outside the shunt pad."""
        root, net_pads = self.kelvin[route.net]
        copper = self._copper(route)
        root_metal = _pad_outline(root)
        for layer in self._layers(root):
            if layer in copper:
                copper[layer] = copper[layer].difference(root_metal)
        touched: set[tuple[str, Any]] = set()
        for pad in net_pads:
            if pad.key == root.key:
                continue
            metal = _pad_outline(pad)
            if any(
                layer in copper and copper[layer].intersects(metal) for layer in self._layers(pad)
            ):
                touched.add(("pad", pad.key))
        for sibling in siblings:
            other = self._copper(sibling)
            if any(
                layer in other and shape.intersects(other[layer]) for layer, shape in copper.items()
            ):
                touched.add(("route", id(sibling)))
        return touched

    def __call__(self, original: Route, optimized: Route) -> bool:
        """True to accept ``optimized``; False to keep ``original``."""
        if original.net not in self.kelvin:
            return True
        # ``grid.routes`` is the live view: post-optimize copper for routes
        # already processed and pre-optimize copper for the rest.
        siblings = [
            route
            for route in getattr(self.grid, "routes", ())
            if route.net == original.net and route is not original
        ]
        if self._contacts(optimized, siblings) <= self._contacts(original, siblings):
            return True
        self.vetoes += 1
        self.vetoed_nets.add(original.net_name or str(original.net))
        return False

    def advisory(self) -> str | None:
        """One-line summary of what the gate cost, or ``None`` if nothing."""
        if not self.vetoes:
            return None
        nets = ", ".join(sorted(self.vetoed_nets))
        return (
            f"  Kelvin branch isolation: {self.vetoes} route(s) kept pre-optimization "
            f"geometry so sibling branches stay apart outside the shunt ({nets})"
        )

    def report(self) -> None:
        """Print the advisory to stderr when any rewrite was rejected."""
        line = self.advisory()
        if line is not None:
            print(line, file=sys.stderr)


def kelvin_branch_gate(router: object) -> KelvinBranchGate | None:
    """Build the #5398 Kelvin accept gate for ``router``, or ``None``."""
    return KelvinBranchGate.from_router(router)

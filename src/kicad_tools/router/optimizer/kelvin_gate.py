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
rejected and the original geometry is kept.  Routes on other nets pass
through without any geometry work.

Contact that the router already committed is inherited, not introduced, so
it is not by itself a veto.  But where that contact sits still matters
(#6889): the sense tap reads the force path's IR drop between the shunt and
the furthest point the two branches share, so a rewrite may keep, shrink or
pull that contact toward the shunt, never push it further out.  The rule
applies to sibling *routes* only.  A route always touches its own terminal
pad, and a rewrite near that pad reshapes the contact without moving any
current, so pad contact is judged by identity alone.

:class:`KelvinBoardGate` applies the same rules to a routed board file, for
``kct optimize-traces`` (#6889).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import shapely  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from ..kelvin import detect_kelvin_topology, is_sense_net_name
from ..kelvin_obstacles import _pad_outline
from ..primitives import Route

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..primitives import Pad, Segment

# How far (mm) a rewrite may push an inherited sibling contact away from the
# shunt before it is vetoed.  Covers buffering round-off only.
_CONTACT_DRIFT_MM = 0.01


def _reach(contact: Any, root_metal: Any) -> float:
    """Distance from the shunt pad to the furthest point of ``contact``.

    A pad outline is convex, so the distance to it is a convex function and
    its maximum over a polygon is reached at a vertex: checking the
    coordinates is exact, not a sample.
    """
    coords = shapely.get_coordinates(contact)
    if not len(coords):
        return 0.0
    return float(shapely.distance(root_metal, shapely.points(coords)).max())


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

    def _outside_root(self, net: int, copper: dict[int, Any]) -> dict[int, Any]:
        """``copper`` with the shunt pad's metal cut away."""
        root = self.kelvin[net][0]
        root_metal = _pad_outline(root)
        for layer in self._layers(root):
            if layer in copper:
                copper[layer] = copper[layer].difference(root_metal)
        return copper

    def _contacts(self, route: Route, siblings: list[Route]) -> dict[tuple[str, Any], Any]:
        """Same-net copper ``route`` touches outside the shunt pad.

        Maps each contact to the shared copper (for sibling routes) or to
        ``None`` (for terminal pads, which are judged by identity only).
        """
        root, net_pads = self.kelvin[route.net]
        copper = self._outside_root(route.net, self._copper(route))
        touched: dict[tuple[str, Any], Any] = {}
        for pad in net_pads:
            if pad.key == root.key:
                continue
            metal = _pad_outline(pad)
            if any(
                layer in copper and copper[layer].intersects(metal) for layer in self._layers(pad)
            ):
                touched[("pad", pad.key)] = None
        for sibling in siblings:
            other = self._copper(sibling)
            shared = [
                shape.intersection(other[layer])
                for layer, shape in copper.items()
                if layer in other and shape.intersects(other[layer])
            ]
            if shared:
                touched[("route", id(sibling))] = unary_union(shared)
        return touched

    def accepts(self, original: Route, optimized: Route, siblings: list[Route]) -> bool:
        """True to accept ``optimized`` given the net's other branches."""
        if original.net not in self.kelvin:
            return True
        before = self._contacts(original, siblings)
        after = self._contacts(optimized, siblings)
        root_metal = _pad_outline(self.kelvin[original.net][0])
        if after.keys() <= before.keys() and all(
            _reach(shared, root_metal) <= _reach(before[key], root_metal) + _CONTACT_DRIFT_MM
            for key, shared in after.items()
            if key[0] == "route"
        ):
            return True
        self.vetoes += 1
        self.vetoed_nets.add(original.net_name or str(original.net))
        return False

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
        return self.accepts(original, optimized, siblings)

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


class _BoardLayers:
    """Stand-in for the routing grid's layer map on a board file."""

    def __init__(self, layer_values: list[int]):
        self._index = {value: i for i, value in enumerate(layer_values)}

    @property
    def num_layers(self) -> int:
        return len(self._index)

    def layer_to_index(self, layer_value: int) -> int:
        # A layer the stack does not declare still gets its own index, so
        # its copper is never mistaken for another layer's.
        return self._index.setdefault(layer_value, len(self._index))


class KelvinBoardGate:
    """The Kelvin branch gate for a routed board file (#6889).

    ``kct optimize-traces`` rewrites each net's segments as one list, with no
    routes to tell the branches apart.  For a Kelvin net this splits the
    net's copper into branches (the pieces left once the shunt pad is cut
    away), optimizes each branch on its own and keeps any rewrite the
    :class:`KelvinBranchGate` rejects at its original geometry.  Branches
    the input already joins outside the shunt form one piece; their joint is
    inherited, exactly as in ``kct route``.
    """

    def __init__(self, gate: KelvinBranchGate, net_ids: dict[str, int], vias: dict[str, list]):
        self.gate = gate
        self.net_ids = net_ids
        self.vias = vias

    @classmethod
    def from_pcb_text(cls, pcb_text: str) -> KelvinBoardGate | None:
        """Build the gate from the board's pads, or ``None`` with no Kelvin net."""
        from .pcb import parse_net_names, parse_vias

        names = parse_net_names(pcb_text)
        # Cheap pre-check: an ordinary board never parses its pads here.
        if not any(is_sense_net_name(name) for name in names.values()):
            return None
        from ..io import detect_layer_stack, load_pads_for_analysis

        net_ids = {name: net for net, name in names.items()}
        pads_by_net: dict[str, list[Pad]] = {}
        for pad in load_pads_for_analysis(pcb_text):
            if pad.net_name in net_ids:
                pads_by_net.setdefault(pad.net_name, []).append(pad)
        kelvin: dict[int, tuple[Pad, list[Pad]]] = {}
        for name, net_pads in pads_by_net.items():
            topology = detect_kelvin_topology(net_pads)
            if topology is not None:
                kelvin[net_ids[name]] = (net_pads[topology.root_index], net_pads)
        if not kelvin:
            return None
        stack = sorted(detect_layer_stack(pcb_text).layers, key=lambda layer: layer.index)
        grid = _BoardLayers([layer.layer_enum.value for layer in stack])
        return cls(KelvinBranchGate(grid, kelvin), net_ids, parse_vias(pcb_text))

    def _branches(self, net: int, net_name: str, segments: list[Segment]) -> list[Route]:
        """Split a net's copper into the pieces left outside the shunt pad."""
        items: list[Route] = [Route(net=net, net_name=net_name, segments=[s]) for s in segments]
        items += [
            Route(net=net, net_name=net_name, vias=[via]) for via in self.vias.get(net_name, ())
        ]
        copper = [self.gate._outside_root(net, self.gate._copper(item)) for item in items]
        parent = list(range(len(items)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if any(
                    layer in copper[j] and shape.intersects(copper[j][layer])
                    for layer, shape in copper[i].items()
                ):
                    parent[find(j)] = find(i)
        branches: dict[int, Route] = {}
        for i, item in enumerate(items):
            branch = branches.setdefault(find(i), Route(net=net, net_name=net_name))
            branch.segments.extend(item.segments)
            branch.vias.extend(item.vias)
        return list(branches.values())

    def optimize_net(
        self,
        net_name: str,
        segments: list[Segment],
        optimize_fn: Callable[[list[Segment]], list[Segment]],
    ) -> list[Segment] | None:
        """Gated optimization of a Kelvin net, or ``None`` for any other net."""
        net = self.net_ids.get(net_name)
        if net is None or net not in self.gate.kelvin:
            return None
        current = self._branches(net, net_name, segments)
        for i, branch in enumerate(list(current)):
            if not branch.segments:
                continue
            optimized = Route(
                net=net,
                net_name=net_name,
                segments=optimize_fn(list(branch.segments)),
                vias=list(branch.vias),
            )
            siblings = [other for j, other in enumerate(current) if j != i]
            if self.gate.accepts(branch, optimized, siblings):
                current[i] = optimized
        return [segment for branch in current for segment in branch.segments]

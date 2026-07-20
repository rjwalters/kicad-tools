"""Generate copper-pour keepout (void) rule areas around HV nets (issue #4372).

KiCad's zone filler keeps a pour clear of foreign-net copper by the *board's*
netclass / DRU clearance (typically 0.2--0.3 mm), NOT by kicad-tools' routing
net-class-map.  So even when a designer declares an HV ``clearance: 1.6mm`` in
the kct sidecar, the inner GND / power planes still fill to within ~0.4 mm of a
mains net crossing overhead -- simultaneously an IEC 60664-1 / 62368-1
creepage/clearance violation and a capacitive-coupling hazard.

This module carves that clearance geometrically (Approach A from the issue):

1. Resolve the HV net set with the SAME plumbing ``kct creepage`` uses
   (:func:`kicad_tools.creepage.engine.resolve_hv_nets`) so the two commands
   agree on "which nets are HV".
2. Union each HV net's true copper across the source copper layers, reusing the
   tested shapely primitives in
   :func:`kicad_tools.creepage.engine._net_union_on_layer`.
3. Buffer that union by the required clearance to obtain the void region.
4. Emit one persistent ``keepout`` rule area
   (:func:`kicad_tools.sexp.builders.keepout_node`, ``copperpour not_allowed``)
   per void region on the target plane layers, so the inner pours void around
   the HV nets and the voids survive future ``kicad-cli`` refills.

Approach B (a custom ``.kicad_dru`` / netclass clearance rule that KiCad's own
filler honors) is intentionally NOT built here -- there is no in-repo DRU/rule
writer yet, and the geometric approach is self-verifiable against
``kct creepage``.  The per-net |ΔV| distance source (#4371) can later feed
per-net clearances through the ``clearance_mm`` seam; v1 uses one flat/derived
distance.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.sexp.builders import keepout_node

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import SExp


@dataclass
class HvVoid:
    """One buffered void region carved on a set of plane layers.

    ``points`` is the exterior ring of the void polygon in **sheet-absolute**
    millimetres (the frame the PCB S-expression tree stores), ready to hand to
    :func:`keepout_node`.
    """

    points: list[tuple[float, float]]
    layers: list[str]


@dataclass
class HvKeepoutPlan:
    """The planned set of HV pour-keepout voids for a board.

    Pure data + geometry -- constructing a plan touches no files and needs no
    ``kicad-cli``, which keeps the geometry unit-testable in isolation.
    """

    hv_nets: dict[int, str]
    clearance_mm: float
    plane_layers: list[str]
    excluded_layers: list[str] = field(default_factory=list)
    voids: list[HvVoid] = field(default_factory=list)

    @property
    def keepout_count(self) -> int:
        return len(self.voids)

    def zone_nodes(self) -> list[SExp]:
        """Materialise the plan as KiCad keepout ``(zone ...)`` S-expressions.

        ``no_pour=True`` carves the pour; ``no_tracks=False, no_vias=False``
        keeps the rule area from forbidding the HV net's own routing / vias --
        the intent is only to exclude *pour* copper (issue #4372).
        """
        nodes: list[SExp] = []
        for void in self.voids:
            nodes.append(
                keepout_node(
                    void.points,
                    void.layers,
                    no_tracks=False,
                    no_vias=False,
                    no_pour=True,
                    uuid_str=str(uuid_module.uuid4()),
                )
            )
        return nodes


def copper_layer_names(pcb: PCB) -> list[str]:
    """Names of every copper (signal/power) layer, in stack order."""
    return [layer.name for layer in pcb.copper_layers]


def _zone_net_number(pcb: PCB, zone: Any, name_to_number: dict[str, int]) -> int:
    """Resolve a zone's net number, falling back to its net name."""
    net_number = zone.net_number
    if net_number == 0 and zone.net_name:
        net_number = name_to_number.get(zone.net_name, 0)
    return int(net_number)


def _zone_fill_layers(zone: Any) -> set[str]:
    """The copper layers a zone actually fills (falls back to its own layer)."""
    layers = {zone.filled_polygon_layer(i) for i in range(len(zone.filled_polygons))}
    if not layers:
        layers.add(zone.layer)
    return layers


def hv_pour_layers(pcb: PCB, hv_net_numbers: set[int]) -> set[str]:
    """Layers on which an HV net has its OWN pour.

    Edge case (b) from the issue: an HV net that is itself poured must not have
    its own pour voided, so its pour layers are excluded from the target set.
    """
    name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}
    layers: set[str] = set()
    for zone in pcb.zones:
        if _zone_net_number(pcb, zone, name_to_number) in hv_net_numbers:
            layers |= _zone_fill_layers(zone)
    return layers


def default_plane_layers(pcb: PCB) -> list[str]:
    """Copper layers carrying at least one net-bound pour (the void targets).

    Keepout / rule-area zones (net 0, empty name) are ignored -- only real
    plane pours are candidates for voiding.  Order follows first appearance.
    """
    name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}
    ordered: list[str] = []
    seen: set[str] = set()
    for zone in pcb.zones:
        if _zone_net_number(pcb, zone, name_to_number) == 0:
            continue  # keepout / unbound zone -- not a plane pour
        for layer in _zone_fill_layers(zone):
            if layer not in seen:
                seen.add(layer)
                ordered.append(layer)
    return ordered


def _iter_polygons(geom: Any):
    """Yield each constituent shapely Polygon of a (possibly Multi) geometry."""
    geoms = getattr(geom, "geoms", None)
    if geoms is not None:
        for part in geoms:
            yield from _iter_polygons(part)
    elif getattr(geom, "geom_type", "") == "Polygon" and not geom.is_empty:
        yield geom


def build_hv_keepout_plan(
    pcb: PCB,
    hv_nets: dict[int, str],
    clearance_mm: float,
    plane_layers: list[str] | None = None,
    source_layers: list[str] | None = None,
) -> HvKeepoutPlan:
    """Plan the pour-keepout voids around ``hv_nets`` at ``clearance_mm``.

    Args:
        pcb: Parsed board.
        hv_nets: ``{net_number: net_name}`` for the HV set (from
            :func:`kicad_tools.creepage.engine.resolve_hv_nets`).
        clearance_mm: Buffer distance -- how far the plane pours must void
            around the HV copper.
        plane_layers: Copper layers whose pours must void.  ``None`` defaults to
            every layer carrying a net-bound pour (:func:`default_plane_layers`).
        source_layers: Copper layers whose HV copper seeds the void.  ``None``
            defaults to every copper layer (an HV trace on an outer layer voids
            the inner plane crossing beneath it).

    Returns:
        An :class:`HvKeepoutPlan`.  The plan is empty (no voids) when there are
        no HV nets, no eligible target layers, a non-positive clearance, or the
        HV nets carry no copper yet -- all clean no-ops, never a crash.
    """
    from kicad_tools._shapely import require_shapely

    require_shapely("HV plane pour-keepout generation")
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from kicad_tools.creepage.engine import _net_union_on_layer

    hv_numbers = set(hv_nets)

    target_layers = default_plane_layers(pcb) if plane_layers is None else list(plane_layers)

    # Edge case (b): never void an HV net's own pour layer.
    hv_own = hv_pour_layers(pcb, hv_numbers)
    excluded = [layer for layer in target_layers if layer in hv_own]
    effective_layers = [layer for layer in target_layers if layer not in hv_own]

    plan = HvKeepoutPlan(
        hv_nets=dict(hv_nets),
        clearance_mm=clearance_mm,
        plane_layers=effective_layers,
        excluded_layers=excluded,
    )

    if not hv_numbers or not effective_layers or clearance_mm <= 0:
        return plan

    if source_layers is None:
        source_layers = copper_layer_names(pcb)

    # Union the HV nets' true copper across the source layers.
    hv_parts: list[Any] = []
    for layer in source_layers:
        for net_number, geom in _net_union_on_layer(pcb, layer).items():
            if net_number in hv_numbers and geom is not None and not geom.is_empty:
                hv_parts.append(geom)

    if not hv_parts:
        return plan  # edge case (d): HV nets have no copper yet

    hv_union = unary_union(hv_parts)
    if hv_union.is_empty:
        return plan

    void = hv_union.buffer(clearance_mm)
    if void.is_empty:
        return plan

    # Emitted keepout polygons are sheet-absolute; the shapely geometry is
    # board-relative (PCB._detect_board_origin normalises copper on load), so
    # add the board origin back.
    ox, oy = pcb.board_origin

    for poly in _iter_polygons(void):
        ring = list(poly.exterior.coords)
        # Shapely closes rings (first == last); KiCad closes implicitly.
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring = ring[:-1]
        pts = [(x + ox, y + oy) for x, y in ring]
        if len(pts) >= 3:
            plan.voids.append(HvVoid(points=pts, layers=list(effective_layers)))

    return plan

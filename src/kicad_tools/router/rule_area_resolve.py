"""One shared parse of board-file keepout rule areas (Issues #5575, #6059).

KiCad rule areas -- ``(zone ... (keepout (tracks not_allowed) (vias
not_allowed) ...))`` -- are read by several routing paths:

* ``kct route`` (:class:`~kicad_tools.router.core.Autorouter`):
  :meth:`Autorouter._keepout_rule_area_polygons` feeds the lattice mask
  (#4605), the grid-engine rasterisation (#6008, :mod:`.rule_area_grid`) and
  the routing-plan capacity model (#5575);
* ``kct route-auto`` (:class:`~kicad_tools.router.orchestrator.RoutingOrchestrator`,
  #6059): the orchestrator's output check and its hierarchical strategy's
  per-stack :class:`~kicad_tools.router.adaptive.AdaptiveAutorouter`.

The two CLIs are independent code paths, and a fix shipped to one has missed
the other before.  This module holds the parse and the layer-spec resolution
they both use, so they cannot drift apart:

1. :func:`keepout_rule_area_specs` keeps only the areas that block tracks
   and/or vias (a ``copperpour not_allowed``-only area -- ``kct zones
   hv-keepout`` output -- is the zone filler's business and never affects
   routing) and optionally shifts their polygons by an origin offset.  Layer
   specs stay as KiCad names.
2. :func:`resolve_layer_spec` resolves ``*.Cu`` / ``F&B.Cu`` / ``*.In.Cu`` /
   explicit names against a concrete :class:`~.layers.LayerStack`.  It is
   separate because the hierarchical strategy tries 2-, 4- and 6-layer stacks
   in turn and must resolve the same names against each.
3. :func:`resolve_keepout_rule_areas` does both, giving the engine-neutral
   :class:`~kicad_tools.router.core.KeepoutRuleArea` list.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import KeepoutRuleArea
    from .lattice.obstacles import KeepoutArea
    from .layers import LayerStack

__all__ = [
    "RuleAreaSpec",
    "all_rule_area_zones",
    "keepout_areas_for_stack",
    "keepout_rule_area_specs",
    "resolve_keepout_rule_areas",
    "resolve_layer_spec",
]


@dataclass(frozen=True)
class RuleAreaSpec:
    """A track/via-blocking rule area with its layer spec still unresolved.

    Attributes:
        polygon: Vertices in mm, in the caller's frame (see
            :func:`keepout_rule_area_specs`'s ``offset``).
        layer_names: The zone's KiCad layer spec, e.g. ``("F.Cu", "B.Cu")``
            or ``("*.Cu",)``.
        blocks_tracks: The area forbids tracks.
        blocks_vias: The area forbids vias.
        name: The zone's name ("" when unnamed).
    """

    polygon: tuple[tuple[float, float], ...]
    layer_names: tuple[str, ...]
    blocks_tracks: bool
    blocks_vias: bool
    name: str = ""


def all_rule_area_zones(pcb: Any) -> list[Any]:
    """Every keepout rule-area zone of ``pcb``: board-level and footprint-owned.

    Footprint-embedded keepouts (an RF module's antenna keepout, Issue #6087)
    constrain routing exactly like board-level ones -- KiCad's parent-footprint
    exemption covers the footprint's own pads, never the tracks and vias the
    router places.  Both ``kct route`` and ``kct route-auto`` read them through
    here.  ``footprint_rule_areas`` is read with ``getattr`` so duck-typed
    stand-ins that only expose ``rule_areas`` keep working.
    """
    return list(pcb.rule_areas) + list(getattr(pcb, "footprint_rule_areas", None) or [])


def track_via_blocking_zones(pcb: Any) -> list[Any]:
    """The rule-area zones of ``pcb`` that block tracks and/or vias.

    ``pcb`` is a :class:`kicad_tools.schema.pcb.PCB`.  Includes
    footprint-embedded keepouts (Issue #6087, :func:`all_rule_area_zones`).
    Areas with fewer than three vertices are dropped.
    """
    return [
        zone
        for zone in all_rule_area_zones(pcb)
        if zone.keepout is not None
        and (not zone.keepout.tracks_allowed or not zone.keepout.vias_allowed)
        and len(zone.polygon) >= 3
    ]


def keepout_rule_area_specs(
    pcb: Any, *, offset: tuple[float, float] = (0.0, 0.0)
) -> list[RuleAreaSpec]:
    """Track/via-blocking rule areas of a schema ``pcb``, layers unresolved.

    Args:
        pcb: A loaded :class:`kicad_tools.schema.pcb.PCB`.  Its rule-area
            polygons are board-relative, like every other zone polygon.
        offset: Added to every vertex.  ``kct route`` passes
            ``pcb.board_origin`` because its router works in sheet-absolute
            coordinates (#4416/#4603).  ``kct route-auto`` passes ``(0, 0)``
            because the orchestrator routes in the same board-relative frame
            as the schema ``PCB``'s footprints.
    """
    ox, oy = float(offset[0]), float(offset[1])
    specs: list[RuleAreaSpec] = []
    for zone in track_via_blocking_zones(pcb):
        assert zone.keepout is not None  # filtered above
        layer_names = tuple(zone.layers or ([zone.layer] if zone.layer else []))
        specs.append(
            RuleAreaSpec(
                polygon=tuple((float(x) + ox, float(y) + oy) for x, y in zone.polygon),
                layer_names=layer_names,
                blocks_tracks=not zone.keepout.tracks_allowed,
                blocks_vias=not zone.keepout.vias_allowed,
                name=zone.name or "",
            )
        )
    return specs


def resolve_layer_spec(names: Iterable[str], stack: LayerStack) -> frozenset[int]:
    """Resolve a KiCad layer spec to the stack indices it covers.

    ``*.Cu`` is every layer, ``F&B.Cu`` the two outer layers and ``*.In.Cu``
    every inner layer (#4672).  On a 2-layer stack ``*.In.Cu`` resolves to the
    empty set, which is correct: there are no inner layers.  Names the stack
    does not have are dropped.
    """
    all_indices = frozenset(layer.index for layer in stack.layers)
    indices: set[int] = set()
    for name in names:
        if name == "*.Cu":
            indices.update(all_indices)
        elif name == "F&B.Cu":
            indices.update({0, stack.num_layers - 1})
        elif name == "*.In.Cu":
            indices.update(i for i in all_indices if 0 < i < stack.num_layers - 1)
        else:
            layer_def = stack.get_layer_by_name(name)
            if layer_def is not None:
                indices.add(layer_def.index)
    return frozenset(indices)


def resolve_keepout_rule_areas(
    specs: Sequence[RuleAreaSpec], stack: LayerStack
) -> list[KeepoutRuleArea]:
    """Resolve ``specs`` against ``stack``.

    Areas on no layer of ``stack`` are dropped (a rule area on no routable
    copper layer constrains nothing).
    """
    from .core import KeepoutRuleArea

    resolved: list[KeepoutRuleArea] = []
    for spec in specs:
        layers = resolve_layer_spec(spec.layer_names, stack)
        if not layers:
            continue
        resolved.append(
            KeepoutRuleArea(
                polygon=spec.polygon,
                layers=layers,
                blocks_tracks=spec.blocks_tracks,
                blocks_vias=spec.blocks_vias,
                name=spec.name,
            )
        )
    return resolved


def keepout_areas_for_stack(specs: Sequence[RuleAreaSpec], stack: LayerStack) -> list[KeepoutArea]:
    """``specs`` resolved against ``stack`` as all-nets lattice ``KeepoutArea``s.

    This is the shape :func:`.rule_area_grid.install_rule_area_keepouts`, the
    :class:`~.lattice.obstacles.LatticeKeepoutMask` and an Autorouter's
    ``_rule_area_keepouts_payload`` take.  No ``spatial_keepouts`` per-class
    filter is applied: route-auto has no such sidecar, so every area governs
    every net (KiCad's default).
    """
    from .lattice.obstacles import KeepoutArea

    return [
        KeepoutArea(
            polygon=area.polygon,
            layers=area.layers,
            blocks_tracks=area.blocks_tracks,
            blocks_vias=area.blocks_vias,
            name=area.name,
        )
        for area in resolve_keepout_rule_areas(specs, stack)
    ]

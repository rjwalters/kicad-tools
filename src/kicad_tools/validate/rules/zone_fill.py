"""Zone fill DRC rules.

This module checks that copper zones have been filled (i.e., they contain
actual copper geometry). Zones defined in the PCB but never filled by
KiCad's zone filler will break power/ground connectivity.

It also hosts :class:`IsolatedCopperRule` (Issue #4680): native detection
of KiCad's ``isolated_copper`` DRC class -- zone-fill islands connected to
nothing -- which was previously invisible to a kct-only workflow (199 of
the 268 missed findings on the reporting board).

Since #4729 the rule implements KiCad's actual predicate -- *an island
is isolated iff its copper connectivity cluster contains no pad*, where
the cluster is grown transitively through tracks, vias and touching
fills (:func:`~kicad_tools.validate.rules.dangling_copper.cluster_copper_kinds`).
Verified against kicad-cli 10.0.5 on the committed fills (no
``--refill-zones``) using ``tests/fixtures/drc/orphan_island*.kicad_pcb``
-- two islands, one carrying a same-net track **and** via:

===========================================  =========  ===
Fixture variant                              kicad-cli  kct
===========================================  =========  ===
track + via on island A (no pad anywhere)    2          2
+ same-net pad directly on island A          1          1
+ same-net pad off-island, reached by track  1          1
===========================================  =========  ===

Known divergences from kicad-cli (``isolated_copper``)
------------------------------------------------------

* KiCad's zone island-removal settings (``island_removal_mode``,
  ``island_area_min``) are not modeled: every committed island is judged,
  regardless of the zone's configured island policy.
* Track arcs participate as *anchors* (chord-approximated, as in
  ``dangling_copper``); they are otherwise unmodeled copper primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Zone


def _zone_bounding_box(zone: Zone) -> str:
    """Return a human-readable bounding box string for the zone polygon."""
    if not zone.polygon:
        return "no boundary"
    xs = [p[0] for p in zone.polygon]
    ys = [p[1] for p in zone.polygon]
    return f"({min(xs):.2f}, {min(ys):.2f}) to ({max(xs):.2f}, {max(ys):.2f}) mm"


def _zone_center(zone: Zone) -> tuple[float, float] | None:
    """Return the centroid of the zone boundary polygon."""
    if not zone.polygon:
        return None
    xs = [p[0] for p in zone.polygon]
    ys = [p[1] for p in zone.polygon]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


class ZoneFillRule(DRCRule):
    """Check that copper zones have been filled.

    Detects two conditions:

    1. **Unfilled zone** (``zone_unfilled``): A zone with ``is_filled=True``
       (the designer intends copper fill) but zero ``filled_polygons``
       (KiCad's zone filler was never run, so no actual copper exists).

    2. **Fill disabled** (``zone_fill_disabled``): A zone with
       ``is_filled=False``, meaning the fill flag is explicitly off.

    Both are reported as warnings because they indicate a design intent
    mismatch rather than a hard manufacturing error.

    Keepout zones (rule areas) are already excluded by the PCB parser --
    only ``(zone ...)`` S-expression nodes are parsed into ``pcb.zones``.

    Zones with ``net_number=0`` and an empty ``net_name`` are flagged as
    having an unassigned net, which indicates an incomplete zone definition.
    """

    rule_id = "zone_fill"
    name = "Zone Fill"
    description = "Check that copper zones contain filled polygon data"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all zones for fill status.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile
                (not used by this rule but required by the interface)

        Returns:
            DRCResults containing zone fill violations
        """
        results = DRCResults()
        results.rules_checked = 1

        for zone in pcb.zones:
            net_label = zone.net_name if zone.net_name else "unassigned"
            layer = zone.layer or "unknown"
            bbox = _zone_bounding_box(zone)
            center = _zone_center(zone)

            if not zone.is_filled:
                # Fill flag is explicitly off
                results.add(
                    DRCViolation(
                        rule_id="zone_fill_disabled",
                        severity="warning",
                        message=(f"Zone fill disabled for net '{net_label}' on {layer} [{bbox}]"),
                        location=center,
                        layer=layer,
                        items=(f"net:{net_label}",),
                    )
                )
            elif len(zone.filled_polygons) == 0:
                # Fill intended but no copper geometry present
                results.add(
                    DRCViolation(
                        rule_id="zone_unfilled",
                        severity="warning",
                        message=(
                            f"Zone for net '{net_label}' on {layer} "
                            f"has fill enabled but no filled polygons [{bbox}]"
                        ),
                        location=center,
                        layer=layer,
                        items=(f"net:{net_label}",),
                    )
                )

            # Additionally flag zones with no net assignment
            if zone.net_number == 0 and not zone.net_name:
                results.add(
                    DRCViolation(
                        rule_id="zone_no_net",
                        severity="warning",
                        message=(f"Zone on {layer} has no net assigned [{bbox}]"),
                        location=center,
                        layer=layer,
                    )
                )

        return results


@dataclass
class _FillIsland:
    """One committed ``filled_polygon`` of a zone, as an isolation subject."""

    zone: Zone
    layer: str
    net_number: int
    net_name: str
    polygon: Any  # shapely (Multi)Polygon


class IsolatedCopperRule(DRCRule):
    """Flag zone-fill islands whose cluster reaches no pad (Issue #4680).

    Implements KiCad's ``isolated_copper`` predicate (pad-in-cluster
    semantics landed in #4729): each committed ``filled_polygon`` of a
    zone is one fill *island*; an island is isolated when its transitive
    same-net copper connectivity cluster -- grown through touching
    fills, track segments, arcs and via barrels -- contains **no pad**.
    Orphaned pour islands are floating copper on ground/supply planes:
    EMC antennas and misleading "this plane is connected" visuals.

    Detection model
    ---------------

    * **Subjects** come from the *committed* fill data
      (``zone.filled_polygons`` + ``filled_polygon_layer(i)`` -- this
      repo's copper source of truth, issues #3482/#3523/#3527), repaired
      with ``make_valid`` so no copper lobe is dropped (#3560).
    * **Conductors** are the same per-layer copper indexes the
      ``dangling_copper`` pass consults
      (:func:`~kicad_tools.validate.rules.dangling_copper.build_copper_layer_indexes`)
      with fills *excluded* -- fill copper enters the graph as subjects,
      so indexing it again would duplicate every island as a node.
      Every edge requires a net match (resolved number first, name
      fallback for the KiCad-10 name-only dialect): a foreign-net track
      crossing an island is a clearance violation, not a connection.
    * **Transitivity** is handled by
      :func:`~kicad_tools.validate.rules.dangling_copper.cluster_copper_kinds`:
      same-layer geometric touch within ``DRC_TOLERANCE`` between *any*
      two nodes (island-island, island-track, track-track, ...), plus a
      cross-layer union of the per-layer instances of one via barrel or
      through-hole pad.  So a pad reached only through a multi-hop chain
      of tracks, or only across a via on another layer, still clears the
      island -- and a cluster of pad-less copper does not.
    * One warning per isolated island, matching kicad-cli's per-island
      reporting (``Zone [GNDD] on In1.Cu, priority 0``) so counts agree.

    **Degrade-silently guard**: detection is only as good as the
    committed fill polygons.  When a zone was never filled there are no
    subjects, so the rule reports nothing -- it must never present
    "0 isolated" as a clean bill for unfilled zones.  ``ZoneFillRule``
    separately warns about the unfilled zones themselves.

    Severity is **warning** (KiCad default parity -- see
    ``manufacturers/project_generator.py``, which pins
    ``isolated_copper: "warning"``); the finding is classified
    advisory-quality for reporting and must never turn a previously
    passing fab gate red.
    """

    rule_id = "isolated_copper"
    name = "Isolated Copper"
    description = (
        "Detects zone-fill islands (committed filled_polygon copper) "
        "whose transitive same-net copper cluster contains no pad "
        "(KiCad isolated_copper parity)."
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check every committed zone-fill island for isolation.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile.
                Kept for the common rule interface; isolation is purely
                topological and profile-independent.

        Returns:
            DRCResults containing one ``isolated_copper`` warning per
            orphaned fill island (empty when no zone carries committed
            fill polygons -- the degrade-silently guard).
        """
        # shapely is a core dependency (issue #3824); this is a
        # correctness path with no valid pure-Python fallback.
        require_shapely("isolated copper detection")

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule["isolated_copper"] = 1

        islands = self._collect_islands(pcb)
        if not islands:
            # Degrade-silently guard: no committed fill copper means
            # nothing can be judged -- report nothing rather than a
            # false "0 isolated" clean bill (ZoneFillRule owns the
            # unfilled-zone warning).
            return results

        from .dangling_copper import build_copper_layer_indexes

        copper_layers = [layer.name for layer in pcb.copper_layers] or ["F.Cu", "B.Cu"]
        name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}

        def resolve_net(number: int, name: str) -> int:
            if number == 0 and name:
                return name_to_number.get(name, 0)
            return number

        conductors = build_copper_layer_indexes(
            pcb, copper_layers, resolve_net, include_fills=False
        )

        connected = self._connected_flags(islands, conductors)

        for island, is_connected in zip(islands, connected, strict=True):
            if is_connected:
                continue
            point = island.polygon.representative_point()
            priority = getattr(island.zone, "priority", 0)
            results.add(
                DRCViolation(
                    rule_id="isolated_copper",
                    severity="warning",
                    message=(
                        f"Isolated copper fill: Zone [{island.net_name}] on "
                        f"{island.layer}, priority {priority} "
                        f"(island area {island.polygon.area:.4f} mm2)"
                    ),
                    location=(point.x, point.y),
                    layer=island.layer,
                    actual_value=island.polygon.area,
                    items=(f"net:{island.net_name}",),
                    nets=(island.net_name,),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Subjects
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_islands(pcb: PCB) -> list[_FillIsland]:
        """Every committed fill polygon as an isolation subject.

        Mirrors the net/layer resolution and ``make_valid`` repair of
        ``clearance._collect_zone_fills`` but keeps per-zone identity
        (priority for the kicad-cli-parity message).  Zones whose net
        cannot be resolved to a nonzero number are skipped -- a no-net
        pour has no connectivity contract to violate (``zone_no_net``
        owns that finding), and rule areas carry no fills at all.
        """
        from shapely.geometry import Polygon  # type: ignore[import-untyped]

        from .clearance import _repair_fill_polygon

        name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}
        number_to_name = {net.number: net.name for net in pcb.nets.values()}

        islands: list[_FillIsland] = []
        for zone in pcb.zones:
            net_number = zone.net_number
            if net_number == 0 and zone.net_name:
                net_number = name_to_number.get(zone.net_name, 0)
            if net_number == 0:
                continue
            net_name = zone.net_name or number_to_name.get(net_number, "")
            for i, points in enumerate(zone.filled_polygons):
                if len(points) < 3:
                    continue
                poly = Polygon(points)
                if not poly.is_valid:
                    poly = _repair_fill_polygon(poly)
                if poly.is_empty:
                    continue
                layer = zone.filled_polygon_layer(i)
                if not layer:
                    continue
                islands.append(
                    _FillIsland(
                        zone=zone,
                        layer=layer,
                        net_number=net_number,
                        net_name=net_name,
                        polygon=poly,
                    )
                )
        return islands

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    @staticmethod
    def _connected_flags(islands: list[_FillIsland], conductors: dict) -> list[bool]:
        """Per-island connected verdicts: KiCad's pad-in-cluster test.

        Delegates the transitive copper clustering to
        :func:`~kicad_tools.validate.rules.dangling_copper.cluster_copper_kinds`
        (each island is one cluster seed) and reports an island as
        connected iff a **pad** is present in its cluster.  Tracks, arcs
        and via barrels are conductors that extend the cluster, never
        terminals that satisfy it on their own (#4729).
        """
        from .dangling_copper import ClusterSeed, cluster_copper_kinds

        seeds = [
            ClusterSeed(
                layer=island.layer,
                geom=island.polygon,
                net_number=island.net_number,
                net_name=island.net_name,
            )
            for island in islands
        ]
        return ["pad" in kinds for kinds in cluster_copper_kinds(conductors, seeds)]

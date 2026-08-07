"""Zone fill DRC rules.

This module checks that copper zones have been filled (i.e., they contain
actual copper geometry). Zones defined in the PCB but never filled by
KiCad's zone filler will break power/ground connectivity.

It also hosts :class:`IsolatedCopperRule` (Issue #4680): native detection
of KiCad's ``isolated_copper`` DRC class -- zone-fill islands connected to
nothing -- which was previously invisible to a kct-only workflow (199 of
the 268 missed findings on the reporting board).

Known divergences from kicad-cli (``isolated_copper``)
------------------------------------------------------

* **Pad-in-cluster semantics are not modeled** (the substantive one).
  KiCad's predicate is *an island is isolated iff its copper connectivity
  cluster contains no pad*, reached transitively through tracks, vias and
  touching fills.  This rule's predicate is narrower: *an island is
  isolated iff no same-net copper item (pad, via, segment, arc) touches
  its cluster*.  A pad-less track or via therefore clears kct's test but
  not KiCad's.

  Measured on KiCad 10.0.5 with the committed fills (no ``--refill-zones``)
  using the fixture embedded as ``_FILLED_BOARD`` in
  ``tests/test_validate_isolated_copper.py`` -- two islands, one carrying
  a same-net track **and** via but no pad:

  ===========================================  =========  ===
  Fixture variant                              kicad-cli  kct
  ===========================================  =========  ===
  track + via on island A (no pad anywhere)    2          1
  + same-net pad directly on island A          1          1
  + same-net pad off-island, reached by track  1          1
  ===========================================  =========  ===

  Consequence: **kct's findings are a strict subset of kicad-cli's** --
  the rule never produces a false positive, but it under-reports islands
  whose only copper contacts are themselves pad-less (a floating stub, an
  orphaned via, or a dead pour arm).  Real-board impact is nil so far:
  0/0 across all 8 repo boards.

  This blind spot **compounds with the #4680 first slice**: ``track_dangling``
  treats same-net committed fills as terminators, so the pad-less stub in
  that cluster is not dangling either -- the whole pathological cluster can
  yield *zero* kct findings while kicad-cli reports ``isolated_copper``.
  (The barrel of a via bonding only one layer is still caught by
  ``via_dangling``, so the fixture above is not entirely silent.)

  Implementing KiCad's semantics needs a transitive copper-connectivity
  clustering pass that treats fills as conductors and pads as the only
  connectivity seeds -- a scope step up that likely belongs with the
  ``validate/connectivity.py`` tracer rather than this rule.  Tracked as
  #4729; the divergence is pinned by
  ``TestKnownDivergencePadCluster`` so a future change to the predicate is
  a deliberate, test-visible decision.
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
from .base import DRC_TOLERANCE, DRCRule

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
    """Flag zone-fill islands connected to no other copper (Issue #4680).

    Detects a **strict subset** of KiCad's ``isolated_copper`` DRC class:
    each committed ``filled_polygon`` of a zone is one fill *island*; an
    island is isolated when its same-net connectivity cluster contains no
    anchor copper -- no pad, via, track segment, or track arc of the
    zone's net touches it (directly, or transitively through another
    same-net island on the same layer).  Orphaned pour islands are
    floating copper on ground/supply planes: EMC antennas and misleading
    "this plane is connected" visuals.

    KiCad's own predicate is stricter -- it requires a **pad** in the
    island's cluster, so an island held only by pad-less copper is
    isolated to kicad-cli but connected here.  That divergence (no false
    positives, some under-reporting) is measured and pinned; see the
    module docstring's *Known divergences* section.

    Detection model
    ---------------

    * **Subjects** come from the *committed* fill data
      (``zone.filled_polygons`` + ``filled_polygon_layer(i)`` -- this
      repo's copper source of truth, issues #3482/#3523/#3527), repaired
      with ``make_valid`` so no copper lobe is dropped (#3560).
    * **Anchors** are the same per-layer copper indexes the
      ``dangling_copper`` pass consults
      (:func:`~kicad_tools.validate.rules.dangling_copper.build_copper_layer_indexes`)
      with fills *excluded*: an isolated island must never anchor
      another isolated island.  Anchors must match the island's net
      (resolved number first, name fallback for the KiCad-10 name-only
      dialect) -- a foreign-net track touching an island is a clearance
      violation, not a connection.
    * **Transitivity**: same-net islands on the same layer that touch
      each other form one cluster (union-find); the cluster is connected
      iff any member island touches an anchor.  Cross-layer clusters
      need a via to bridge, and that via is itself an anchor for every
      island it touches, so no cross-layer union pass is required.
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
        "touched by no pad, via, or track of their net (a subset of "
        "KiCad's isolated_copper class)."
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

        anchors = build_copper_layer_indexes(pcb, copper_layers, resolve_net, include_fills=False)

        connected = self._connected_flags(islands, anchors)

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
    def _connected_flags(islands: list[_FillIsland], anchors: dict) -> list[bool]:
        """Per-island connected verdicts with same-layer transitivity.

        Union-find over touching same-net islands per layer, then a
        cluster is connected iff any member touches a same-net anchor.
        """
        from shapely import box  # type: ignore[import-untyped]
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        def probe(geom: Any) -> Any:
            # Tolerance-expanded bbox probe: STRtree queries are pure
            # bbox tests, so copper within DRC_TOLERANCE of the island
            # but with a disjoint bbox would otherwise be missed (the
            # dangling_copper rule expands its probes identically).
            minx, miny, maxx, maxy = geom.bounds
            return box(
                minx - DRC_TOLERANCE,
                miny - DRC_TOLERANCE,
                maxx + DRC_TOLERANCE,
                maxy + DRC_TOLERANCE,
            )

        parent = list(range(len(islands)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        by_layer: dict[str, list[int]] = {}
        for idx, island in enumerate(islands):
            by_layer.setdefault(island.layer, []).append(idx)

        anchored: list[bool] = [False] * len(islands)

        for layer, indices in by_layer.items():
            # Same-layer, same-net island-island transitivity.
            tree = STRtree([islands[i].polygon for i in indices])
            for idx in indices:
                island = islands[idx]
                for other_pos in tree.query(probe(island.polygon)):
                    other_idx = indices[int(other_pos)]
                    if other_idx <= idx:
                        continue
                    other = islands[other_idx]
                    if island.net_number != other.net_number:
                        continue
                    if island.polygon.distance(other.polygon) <= DRC_TOLERANCE:
                        union(idx, other_idx)
            # Direct anchoring by same-net pad / via / segment / arc.
            index = anchors.get(layer)
            if index is None:
                continue
            for idx in indices:
                island = islands[idx]
                for item in index.candidates(probe(island.polygon)):
                    if not _anchor_net_matches(island, item):
                        continue
                    if item.geom.distance(island.polygon) <= DRC_TOLERANCE:
                        anchored[idx] = True
                        break

        cluster_connected: dict[int, bool] = {}
        for idx in range(len(islands)):
            root = find(idx)
            cluster_connected[root] = cluster_connected.get(root, False) or anchored[idx]
        return [cluster_connected[find(idx)] for idx in range(len(islands))]


def _anchor_net_matches(island: _FillIsland, item: Any) -> bool:
    """Same-net test between a fill island and an anchor copper item.

    Prefer resolved net numbers; fall back to name equality for the
    KiCad-10 name-only dialect.  Islands always carry a nonzero resolved
    net number (no-net zones are skipped as subjects), so an anchor with
    no net identity at all never connects.
    """
    if island.net_number and item.net_number:
        return bool(island.net_number == item.net_number)
    if island.net_name and item.net_name:
        return bool(island.net_name == item.net_name)
    return False

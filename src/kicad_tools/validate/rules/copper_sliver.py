"""Thin copper sliver detection using KiCad-compatible tip geometry.

KiCad's native DRC (``kicad-cli pcb drc``) emits a ``copper_sliver``
violation for every thin filament of copper whose width drops below the
fab's minimum reproducible feature width.  Slivers arise where a pour
necks down around pads/vias/clearance carves: the etch process can
under-etch (leaving a hairline short) or over-etch (lifting a fragment
that lands elsewhere).  ``kicad-cli`` flags 16 ``copper_sliver`` defects
on the softstart board; before this rule ``kct check`` reported zero
because no rule inspected the *internal width* of a single copper
region -- every existing copper rule measures the gap *between two
distinct features*, never the thickness of one region.

KiCad's native check identifies acute copper tips: an interior angle below
20 degrees whose opposite edge spans the width tolerance.  An earlier
morphological-open approximation classified long, legitimate ribbons as
slivers and then used an area floor to discard numerical corner specks.
That over-reported on the repository's routed boards.  This implementation
uses the same angle-and-width semantics as KiCad instead.

**Threshold:** there is no ``min_copper_width_mm`` / ``min_sliver_mm``
field on :class:`~kicad_tools.manufacturers.DesignRules`.  KiCad's
"minimum copper width" / sliver threshold is the same physical quantity
as the manufacturer's minimum reproducible trace width, so this rule
gates against ``design_rules.min_trace_width_mm`` (Issue #3843
deliberately does NOT add a new DesignRules field -- that would be scope
creep across all six manufacturer YAMLs).  If a future issue wants a
distinct, looser sliver threshold, it can add the field then.

**Severity:** ``kicad-cli`` classifies ``copper_sliver`` as a *warning*
(fab-process advisory, not a guaranteed short).  This rule emits
``severity="warning"`` to match and to avoid turning a soft fab note
into a hard CI gate.

**Performance:** ``buffer(+/-r)`` cost scales with vertex count, and a
full ground pour can have thousands of vertices after clearance carving.
This rule unions all copper on a layer *once*, then runs a *single*
``buffer(-r).buffer(r)`` with ``join_style="mitre"`` (round joins
tessellate arcs and explode vertex count; mitre keeps vertex count near
the input and is correct for a straight-line width test).  Empty layers
and ``min_trace_width_mm <= 0`` short-circuit.  The check has its own
CLI category so it can be skipped on very large pours via
``--skip copper_sliver``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Iterator

from kicad_tools._shapely import require_shapely
from kicad_tools.core.layers import via_spans_layer as _via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule
from .clearance import _collect_zone_fills, _pad_on_layer, _pad_polygon

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class CopperSliverRule(DRCRule):
    """Flag acute copper sliver tips on each copper layer.

    For each copper layer, union filled zones, tracks, pads, vias, and
    filled footprint copper into one geometry.  Report acute convex tips
    whose angle is below KiCad's 20-degree threshold and whose opposite
    chord exceeds ``min_trace_width_mm``.

    Reuses :func:`_collect_zone_fills` /
    :func:`_repair_fill_polygon` from
    :mod:`kicad_tools.validate.rules.clearance` so the sliver detector
    consumes the *same* committed ``filled_polygon`` copper (this repo's
    source of truth, issues #3482/#3523/#3527) and the same
    ``make_valid`` invalid-ring repair (never ``buffer(0)``, which
    silently drops bowtie lobes -- #3560).
    """

    rule_id = "copper_sliver"
    name = "Copper Sliver"
    description = (
        "Detects thin copper slivers (regions narrower than the minimum "
        "reproducible copper width) via a per-layer morphological open."
    )

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Check every copper layer for thin slivers.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile.
                ``min_trace_width_mm`` is used as the sliver threshold.

        Returns:
            DRCResults containing ``copper_sliver`` warnings, one per
            surviving residual sliver region.
        """
        # shapely is a core dependency (issue #3824).  This is a
        # correctness path with no valid pure-Python fallback, so fail
        # loud rather than silently returning zero violations.
        require_shapely("copper sliver detection")

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[self.rule_id] = 1

        min_width = design_rules.min_trace_width_mm
        if min_width <= 0:
            # No reproducible-width spec to gate against -- nothing to do.
            return results

        # Build per-layer copper geometry: union of all filled-zone copper
        # (the source of softstart's 16 slivers) plus track/pad/via copper.
        copper_by_layer = self._collect_copper_by_layer(pcb)

        for layer_name, geoms in copper_by_layer.items():
            self._check_layer(layer_name, geoms, min_width, results)

        return results

    def _collect_copper_by_layer(self, pcb: PCB) -> dict[str, list[Any]]:
        """Group all copper geometry by copper-layer name.

        Slivers are a single-net geometric property, so this unions *all*
        copper on a layer regardless of net (no per-net split).  The
        union must include **every** copper feature on the layer -- zone
        fills, tracks, pads and via barrels -- so the morphological open
        sees the same connected copper KiCad does.  Omitting pad/via
        copper is a false-positive source: a fill that necks down to a
        pad reads as a sliver when the pad's own copper (which makes the
        neck wide enough) is missing from the union (the pad/via copper
        is intentionally round/rectangular and rarely a sliver itself,
        but it must be present so it does not *create* phantom slivers at
        fill-to-pad junctions).
        """
        from shapely import Point  # type: ignore[import-untyped]
        from shapely.geometry import LineString  # type: ignore[import-untyped]

        copper_by_layer: dict[str, list[Any]] = {}
        layer_names = [layer.name for layer in pcb.copper_layers]
        for layer_name in layer_names:
            copper_by_layer.setdefault(layer_name, [])

        # Zone fills -- the primary sliver source.  Reuses the shared
        # collector (net resolution + make_valid repair).  ``_collect_zone_fills``
        # already skips empty/degenerate rings, so every collected polygon
        # is real copper.
        fills_by_layer = _collect_zone_fills(pcb)
        for layer, fills in fills_by_layer.items():
            bucket = copper_by_layer.setdefault(layer, [])
            for fill in fills:
                bucket.append(fill.polygon)

        # Track copper -- each segment as a width-buffered line.
        for layer_name in layer_names:
            bucket = copper_by_layer[layer_name]
            for seg in pcb.segments_on_layer(layer_name):
                if seg.width <= 0:
                    continue
                # mitre join keeps the buffered segment's vertex count low.
                line = LineString([seg.start, seg.end])
                bucket.append(line.buffer(seg.width / 2.0, join_style="mitre"))

        # Pad copper -- true outline honoring pad.shape (issue #3826).
        for fp in pcb.footprints:
            for pad in fp.pads:
                poly = _pad_polygon(pad, fp)
                if poly is None or poly.is_empty:
                    continue
                for layer_name in layer_names:
                    if _pad_on_layer(pad, layer_name):
                        copper_by_layer[layer_name].append(poly)

            # Filled footprint polygons on copper are basic copper items in
            # KiCad's native sliver pass.  Include them so positive-control
            # acute tips are visible to both engines.
            for graphic in fp.graphics:
                if (
                    graphic.graphic_type != "poly"
                    or graphic.layer not in copper_by_layer
                    or len(graphic.points) < 3
                ):
                    continue
                from shapely import affinity  # type: ignore[import-untyped]
                from shapely.geometry import Polygon  # type: ignore[import-untyped]

                poly = Polygon(graphic.points)
                if fp.rotation:
                    poly = affinity.rotate(poly, fp.rotation, origin=(0, 0))
                poly = affinity.translate(poly, fp.position[0], fp.position[1])
                if not poly.is_empty:
                    copper_by_layer[graphic.layer].append(poly)

        # Via barrel copper -- physical copper on every layer the barrel
        # spans.  Model as a disc; vias are large/round and rarely a
        # sliver, but (like pads) must be present so they do not create
        # phantom slivers at fill-to-via junctions.
        for via in pcb.vias:
            if via.size <= 0:
                continue
            disc = Point(via.position).buffer(via.size / 2.0)
            for layer_name in layer_names:
                if _via_spans_layer(via.layers, layer_name):
                    copper_by_layer[layer_name].append(disc)

        return copper_by_layer

    def _check_layer(
        self,
        layer_name: str,
        geoms: list[Any],
        min_width: float,
        results: DRCResults,
    ) -> None:
        """Union one layer's copper and emit KiCad-style acute sliver tips."""
        import shapely
        from shapely.geometry import (
            GeometryCollection,
            MultiPolygon,
            Polygon,
        )

        if not geoms:
            return

        # Union once per layer (NOT per-feature) so the expensive buffer
        # runs a single time over the merged geometry.
        geom = shapely.unary_union(geoms)
        if geom.is_empty:
            return

        # Defensive simplify: drop collinear vertices from the fill
        # rasterization without moving any edge beyond fab precision.
        # Keep the tolerance well below DRC_TOLERANCE so it cannot move an
        # edge enough to create or erase a sliver near the threshold.
        geom = geom.simplify(0.5 * DRC_TOLERANCE, preserve_topology=True)
        if geom.is_empty:
            return

        for component in self._iter_polygons(geom, Polygon, MultiPolygon, GeometryCollection):
            for tip, angle, opposite_width in self._iter_sliver_tips(component, min_width):
                results.add(
                    DRCViolation(
                        rule_id=self.rule_id,
                        severity="warning",
                        message=(
                            f"Copper sliver on {layer_name}: region narrower than "
                            f"minimum copper width {min_width:.3f}mm "
                            f"(tip angle {angle:.1f}°, "
                            f"opposite width {opposite_width:.3f}mm)"
                        ),
                        location=(round(tip[0], 3), round(tip[1], 3)),
                        layer=layer_name,
                        actual_value=round(opposite_width, 4),
                        required_value=min_width,
                    )
                )

    @staticmethod
    def _iter_sliver_tips(
        component: Any, min_width: float
    ) -> Iterator[tuple[tuple[float, float], float, float]]:
        """Yield acute convex vertices using KiCad's angle/width semantics."""
        angle_limit = 20.0
        rings = [component.exterior, *component.interiors]
        for ring in rings:
            coords = list(ring.coords)[:-1]
            if len(coords) < 3:
                continue
            for index, current in enumerate(coords):
                previous = coords[index - 1]
                following = coords[(index + 1) % len(coords)]
                if not CopperSliverRule._chord_is_locally_inside(coords, index):
                    continue
                arm1 = math.hypot(previous[0] - current[0], previous[1] - current[1])
                arm2 = math.hypot(following[0] - current[0], following[1] - current[1])
                if arm1 <= DRC_TOLERANCE or arm2 <= DRC_TOLERANCE:
                    continue
                dot = (previous[0] - current[0]) * (following[0] - current[0]) + (
                    previous[1] - current[1]
                ) * (following[1] - current[1])
                cosine = max(-1.0, min(1.0, dot / (arm1 * arm2)))
                angle = math.degrees(math.acos(cosine))
                opposite_width = math.hypot(following[0] - previous[0], following[1] - previous[1])
                if angle < angle_limit and opposite_width > min_width:
                    yield (current, angle, opposite_width)

    @staticmethod
    def _chord_is_locally_inside(coords: list[tuple[float, float]], tip_index: int) -> bool:
        """Match KiCad's local-inside guard for the chord across a tip."""

        def area(p, q, r):
            return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

        count = len(coords)
        a = (tip_index - 1) % count
        b = (tip_index + 1) % count
        previous = (a - 1) % count
        following = (a + 1) % count
        if area(coords[previous], coords[a], coords[following]) < 0:
            return (
                area(coords[a], coords[b], coords[following]) >= 0
                and area(coords[a], coords[previous], coords[b]) >= 0
            )
        return (
            area(coords[a], coords[b], coords[previous]) < 0
            or area(coords[a], coords[following], coords[b]) < 0
        )

    @staticmethod
    def _iter_polygons(geom, Polygon, MultiPolygon, GeometryCollection):
        """Yield the polygonal components of a residual geometry."""
        if isinstance(geom, Polygon):
            yield geom
        elif isinstance(geom, MultiPolygon):
            yield from geom.geoms
        elif isinstance(geom, GeometryCollection):
            for sub in geom.geoms:
                if isinstance(sub, Polygon):
                    yield sub
                elif isinstance(sub, MultiPolygon):
                    yield from sub.geoms
        # Pure linework/points are zero-area -- nothing to yield.

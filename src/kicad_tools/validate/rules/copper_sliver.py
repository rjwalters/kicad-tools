"""Thin copper sliver detection via morphological open (Issue #3843).

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

A sliver is an *intra-region* property: a single connected copper
polygon that is locally thinner than the minimum reproducible copper
width.  The clean detector is a **morphological open**: erode the
copper by ``r = min_width / 2``, then dilate by the same ``r``.  Any
sub-region narrower than ``2r = min_width`` is fully consumed by the
erosion and never returns under the dilation, so::

    opened = geom.buffer(-r).buffer(r)   # r = min_width / 2
    slivers = geom.difference(opened)     # residual = sliver regions

Shapely implements erode/dilate as negative/positive ``buffer``.  This
rule is the one #3830 child that requires true polygon morphology rather
than pairwise distance math, which is why shapely was graduated to a core
dependency in #3824.

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

from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.layers import via_spans_layer as _via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule
from .clearance import _collect_zone_fills, _pad_on_layer, _pad_polygon

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class CopperSliverRule(DRCRule):
    """Flag thin copper slivers via a per-layer morphological open.

    For each copper layer, union all filled-zone copper (and track
    copper) into a single geometry, run a morphological open
    (``buffer(-r).buffer(r)`` with ``r = min_trace_width_mm / 2``), and
    report the residual ``original - opened`` regions as slivers
    narrower than ``min_trace_width_mm``.

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

        # Erode by slightly *less* than ``min_width / 2`` so the
        # morphological open is forgiving by one fab tolerance: a feature
        # exactly ``min_width`` wide (or within ``DRC_TOLERANCE`` of it)
        # survives the open and is NOT flagged -- only copper clearly
        # narrower than the spec (by more than a tolerance) is consumed by
        # the erosion and reported.  This also absorbs the flat-endcap
        # residual a single straight track exactly at ``min_width`` would
        # otherwise leave behind, and the float-precision rounding at
        # mitre joints.  Threshold = ``min_width - DRC_TOLERANCE``.
        effective_width = max(min_width - DRC_TOLERANCE, 0.0)
        r = effective_width / 2.0
        if r <= 0:
            return results

        # Build per-layer copper geometry: union of all filled-zone copper
        # (the source of softstart's 16 slivers) plus track/pad/via copper.
        copper_by_layer = self._collect_copper_by_layer(pcb)

        for layer_name, geoms in copper_by_layer.items():
            self._check_layer(layer_name, geoms, r, min_width, results)

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
        r: float,
        min_width: float,
        results: DRCResults,
    ) -> None:
        """Union one layer's copper, morph-open it, and emit residuals."""
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

        # Morphological open: erode by r, dilate by r.  Mitre joins keep
        # vertex count near the input (round joins tessellate arcs and
        # explode it) and are correct for a straight-line width test.
        opened = geom.buffer(-r, join_style="mitre").buffer(r, join_style="mitre")
        if opened.is_empty:
            # The entire copper region is narrower than min_width.
            residual = geom
        else:
            residual = geom.difference(opened)
        if residual.is_empty:
            return

        # Numerically-trivial residuals must not be flagged.  The
        # ``difference`` of a pour against its own morphological open
        # leaves two kinds of region: (a) genuine slivers -- ribbons of
        # copper narrower than ``min_width`` that span a meaningful length
        # -- and (b) a long tail of tiny corner triangles where mitre
        # joints at acute pour corners do not re-fill exactly under the
        # open.  A genuine sliver at least ``min_width`` long and up to
        # ``min_width`` wide has area on the order of ``min_width**2``;
        # the corner specks are an order of magnitude smaller.  An area
        # floor of ``min_width**2`` cleanly separates the two (on the
        # softstart routed board the residual areas split at ~0.01 mm**2:
        # ~12 genuine slivers above, ~230 sub-0.007 mm**2 specks below --
        # matching the order of magnitude of the 16 ``copper_sliver``
        # defects kicad-cli reports).  The floor scales with the spec, so
        # a finer fab process (smaller ``min_width``) flags
        # proportionally finer slivers.
        area_floor = min_width * min_width

        for component in self._iter_polygons(residual, Polygon, MultiPolygon, GeometryCollection):
            if component.is_empty or component.area < area_floor:
                continue
            centroid = component.representative_point()
            # Approximate the sliver width from area/perimeter: a long thin
            # ribbon of width w and length L has area ~= w*L and perimeter
            # ~= 2L, so width ~= 2*area/perimeter.
            perimeter = component.length
            approx_width = (2.0 * component.area / perimeter) if perimeter > 0 else 0.0
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="warning",
                    message=(
                        f"Copper sliver on {layer_name}: region narrower than "
                        f"minimum copper width {min_width:.3f}mm "
                        f"(approx width {approx_width:.3f}mm, "
                        f"area {component.area:.4f}mm^2)"
                    ),
                    location=(round(centroid.x, 3), round(centroid.y, 3)),
                    layer=layer_name,
                    actual_value=round(approx_width, 4),
                    required_value=min_width,
                )
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

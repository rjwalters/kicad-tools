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

**Threshold:** KiCad's sliver-tip discriminator is independent of the
manufacturer's minimum trace width.  Stock KiCad uses an advanced-config
``DRCSliverWidthTolerance`` default of 0.08 mm, a 0.0008 mm minimum adjacent
edge length, and considers only outlines with more than five vertices.  This
rule mirrors those native defaults directly; substituting a fab profile's
``min_trace_width_mm`` creates false negatives in the 0.08--fab-width band.

**Minimum length is a traversal, not a drop.**  Native KiCad does not
discard a candidate tip whose immediate neighbour is closer than
0.0008 mm -- it *walks past* such micro-vertices until it finds usable
prior/next arms, using a component-wise smallness test
(``abs(dx) < min_len and abs(dy) < min_len``, not Euclidean distance).
Dropping instead of traversing lets a single numerical kink beside an
acute tip hide a sliver that ``kicad-cli`` reports.  See
:meth:`CopperSliverRule._resolve_arm_index`.

**Severity:** ``kicad-cli`` classifies ``copper_sliver`` as a *warning*
(fab-process advisory, not a guaranteed short).  This rule emits
``severity="warning"`` to match and to avoid turning a soft fab note
into a hard CI gate.

**Performance:** the rule unions all copper on a layer once, simplifies
collinear fill vertices below DRC precision, then walks each polygon ring.
Empty layers short-circuit.  The check has its own CLI category so it can be
skipped on very large pours via ``--skip copper_sliver``.
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


KICAD_SLIVER_WIDTH_TOLERANCE_MM = 0.08
KICAD_SLIVER_MINIMUM_LENGTH_MM = 0.0008
KICAD_SLIVER_MINIMUM_VERTEX_COUNT = 6
KICAD_SLIVER_ANGLE_TOLERANCE_DEG = 20.0


class CopperSliverRule(DRCRule):
    """Flag acute copper sliver tips on each copper layer.

    For each copper layer, union filled zones, tracks, pads, vias, and
    filled footprint copper into one geometry.  Report acute convex tips
    whose angle is below KiCad's 20-degree threshold and whose opposite
    chord exceeds KiCad's independent 0.08 mm sliver-width tolerance.

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
        "Detects thin copper slivers using KiCad-compatible acute-tip "
        "angle, chord-width, minimum-edge, and vertex-count thresholds."
    )

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Check every copper layer for thin slivers.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile. Kept in
                the common rule interface; native sliver thresholds are
                independent of the profile's minimum trace width.

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

        # Build per-layer copper geometry: union of all filled-zone copper
        # (the source of softstart's 16 slivers) plus track/pad/via copper.
        copper_by_layer = self._collect_copper_by_layer(pcb)

        for layer_name, geoms in copper_by_layer.items():
            self._check_layer(
                layer_name,
                geoms,
                KICAD_SLIVER_WIDTH_TOLERANCE_MM,
                results,
            )

        return results

    def _collect_copper_by_layer(self, pcb: PCB) -> dict[str, list[Any]]:
        """Group all copper geometry by copper-layer name.

        Slivers are a single-net geometric property, so this unions *all*
        copper on a layer regardless of net (no per-net split).  The
        union must include **every** copper feature on the layer -- zone
        fills, tracks, pads and via barrels -- so the acute-tip pass sees
        the same connected copper KiCad does.  Omitting pad/via
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
                from shapely import affinity
                from shapely.geometry import Polygon

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
        sliver_width_tolerance: float,
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
            for tip, angle, opposite_width in self._iter_sliver_tips(
                component, sliver_width_tolerance
            ):
                results.add(
                    DRCViolation(
                        rule_id=self.rule_id,
                        severity="warning",
                        message=(
                            f"Copper sliver on {layer_name}: acute tip exceeds "
                            f"native chord tolerance {sliver_width_tolerance:.3f}mm "
                            f"(tip angle {angle:.1f}°, "
                            f"opposite width {opposite_width:.3f}mm)"
                        ),
                        location=(round(tip[0], 3), round(tip[1], 3)),
                        layer=layer_name,
                        actual_value=round(opposite_width, 4),
                        required_value=sliver_width_tolerance,
                    )
                )

    @staticmethod
    def _resolve_arm_index(coords: list[tuple[float, float]], index: int, step: int) -> int | None:
        """Walk past adjacent *tiny* vertices to the first usable arm vertex.

        Mirrors KiCad's native ``do { pt = CPoint( --prevIdx ); } while (…)``
        traversal in its sliver test provider.  Two details of the native
        semantics are load-bearing and are reproduced exactly:

        * **The smallness predicate is component-wise**
          (``abs(dx) < min_len and abs(dy) < min_len``), *not* Euclidean.
          A vertex offset by ``(0.0007, 0.0007)`` mm is 0.00099 mm away --
          above the 0.0008 mm floor by hypot, yet native KiCad still treats
          it as a numerical micro-vertex and walks past it.
        * **A tiny neighbour is skipped, never used to drop the candidate.**
          Dropping the tip (the previous behaviour here) lets a single
          numerical micro-vertex adjacent to an acute tip *hide* a real
          sliver that native KiCad reports -- a false negative.

        Args:
            coords: Ring vertices without the closing duplicate.
            index: Index of the tip whose arm is being resolved.
            step: ``-1`` to walk backwards (prior arm), ``+1`` forwards.

        Returns:
            The index of the first vertex that is not component-wise tiny
            relative to ``coords[index]``, or ``None`` when the whole ring
            collapses inside the minimum length (a degenerate speck).
        """
        count = len(coords)
        x, y = coords[index]
        for offset in range(1, count):
            candidate = (index + step * offset) % count
            cx, cy = coords[candidate]
            if (
                abs(cx - x) >= KICAD_SLIVER_MINIMUM_LENGTH_MM
                or abs(cy - y) >= KICAD_SLIVER_MINIMUM_LENGTH_MM
            ):
                return candidate
        return None

    @staticmethod
    def _iter_sliver_tips(
        component: Any, sliver_width_tolerance: float
    ) -> Iterator[tuple[tuple[float, float], float, float]]:
        """Yield acute convex vertices using KiCad's angle/width semantics."""
        angle_limit = KICAD_SLIVER_ANGLE_TOLERANCE_DEG
        resolve = CopperSliverRule._resolve_arm_index
        rings = [component.exterior, *component.interiors]

        # Keyhole/pinch guard (issue #4521): ``make_valid()`` /
        # ``unary_union`` can leave a hole ring touching the exterior ring
        # (or another hole) at exactly one shared coordinate -- a
        # topologically degenerate "keyhole" pinch.  ``_iter_sliver_tips``
        # walks ``[exterior, *interiors]`` independently, so it evaluates
        # the interior ring's local neighbourhood on its own and finds a
        # spurious acute tip from the hole's own shape rather than from real
        # copper thinness at that location.  Native KiCad's outline set does
        # not walk such a pinch as a tip.  Any coordinate shared by two
        # rings of this component is a topological join, not a copper tip --
        # collect those coordinates so the walk below can skip them.
        ring_membership: dict[tuple[float, float], int] = {}
        for ring in rings:
            for pt in set(list(ring.coords)[:-1]):
                ring_membership[pt] = ring_membership.get(pt, 0) + 1
        pinch_coords = {pt for pt, count in ring_membership.items() if count > 1}

        for ring in rings:
            coords = list(ring.coords)[:-1]
            if len(coords) < KICAD_SLIVER_MINIMUM_VERTEX_COUNT:
                continue
            # One marker per acute corner: every vertex of a micro-vertex
            # chain at the same tip resolves to the *same* pair of usable
            # arms, and native KiCad emits a single ``copper_sliver`` for
            # that corner (verified against kicad-cli).  Keying on the
            # resolved arm pair collapses the chain without merging two
            # genuinely distinct tips, which resolve to different arms.
            seen_arms: set[tuple[int, int]] = set()
            for index, current in enumerate(coords):
                prior_index = resolve(coords, index, -1)
                next_index = resolve(coords, index, 1)
                if prior_index is None or next_index is None:
                    continue
                if (prior_index, next_index) in seen_arms:
                    continue
                if current in pinch_coords:
                    # Keyhole/pinch join shared with another ring -- not a
                    # real copper tip (see ``pinch_coords`` above, #4521).
                    continue
                previous = coords[prior_index]
                following = coords[next_index]
                if not CopperSliverRule._chord_is_locally_inside(
                    coords, prior_index, index, next_index
                ):
                    continue
                arm1 = math.hypot(previous[0] - current[0], previous[1] - current[1])
                arm2 = math.hypot(following[0] - current[0], following[1] - current[1])
                if arm1 <= 0.0 or arm2 <= 0.0:
                    continue
                dot = (previous[0] - current[0]) * (following[0] - current[0]) + (
                    previous[1] - current[1]
                ) * (following[1] - current[1])
                cosine = max(-1.0, min(1.0, dot / (arm1 * arm2)))
                angle = math.degrees(math.acos(cosine))
                opposite_width = math.hypot(following[0] - previous[0], following[1] - previous[1])
                if angle < angle_limit and opposite_width > sliver_width_tolerance:
                    # Union-seam guard (issue #4521): a genuine acute sliver
                    # protrudes above its opposite chord by at least the
                    # sliver width tolerance.  Where a same-net track buffer
                    # fuses into a zone fill, ``unary_union`` leaves an acute
                    # seam whose two arms are wildly mismatched in length and
                    # nearly collinear with the chord -- its perpendicular
                    # height above the chord is ~0, so it is not real copper
                    # thinness.  Symmetric acute tips that pass the width test
                    # always have height >> tolerance (height >= chord /
                    # (2*tan(angle/2)) > 2.8*chord for angle < 20 deg), so
                    # this drops the seam without hiding a real sliver.
                    cross = abs(
                        (following[0] - previous[0]) * (current[1] - previous[1])
                        - (following[1] - previous[1]) * (current[0] - previous[0])
                    )
                    tip_height = cross / opposite_width
                    if tip_height <= sliver_width_tolerance:
                        continue
                    seen_arms.add((prior_index, next_index))
                    yield (current, angle, opposite_width)

    @staticmethod
    def _chord_is_locally_inside(
        coords: list[tuple[float, float]],
        prior_index: int,
        tip_index: int,
        next_index: int,
    ) -> bool:
        """Match KiCad's local-inside guard for the chord across a tip.

        ``prior_index`` / ``next_index`` are the *resolved* arm vertices
        (see :meth:`_resolve_arm_index`), so the guard is evaluated on the
        same neighbourhood the angle test uses instead of on raw index
        neighbours that may be numerical micro-vertices.
        """

        def area(
            p: tuple[float, float],
            q: tuple[float, float],
            r: tuple[float, float],
        ) -> float:
            return float((q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1]))

        a = prior_index
        b = next_index
        # The vertex preceding the prior arm, resolved with the same
        # tiny-vertex traversal; ``following`` is the tip itself, exactly
        # as in the native index arithmetic (a + 1 == tip when no micro
        # vertices intervene).
        previous = CopperSliverRule._resolve_arm_index(coords, a, -1)
        if previous is None:
            return False
        following = tip_index
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

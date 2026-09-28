"""Pin-1 / polarity silkscreen-marker DRC rule.

A board can pass every electrical and fabrication gate and still be
assembled or reworked backwards: nothing checks that an IC, diode, LED,
polarized capacitor or connector carries a *visible* pin-1 / polarity mark
on the silkscreen.  Markers go missing when a footprint is hand-edited, when
a custom footprint only draws the mark on ``F.Fab``, or when the mark sits
under the package body (hidden once the part is placed) or on a pad (clipped
away by the fab's silk-over-mask removal).

For each selected footprint this rule looks for at least one silkscreen
element that qualifies as a pin-1 indicator:

1. It is on the silkscreen layer of the footprint's side -- the footprint's
   own ``fp_line`` / ``fp_rect`` / ``fp_circle`` / ``fp_arc`` / ``fp_poly``
   / user text, or a board-level ``gr_*`` graphic.
2. Its stroked geometry comes within ``search_radius_mm`` of pad 1's copper.
3. It *points at* pad 1: it is strictly closer to pad 1 than to any other
   pad (``require_asymmetry``, default on).  A body outline that runs past
   pad 1 and pad 2 alike does not identify either, so it is not counted --
   this is what makes a bare two-line LED/diode outline fail while the
   cathode bar next to it passes.  Tie-break for multi-segment marks such
   as the L-shaped corner on KiCad's stock crystal footprints, whose legs
   each tie between pad 1 and a neighbour: touching silk is grouped into
   connected components, and a *bent* component (L, U, box; never a single
   straight stroke) also counts when its centroid is strictly closer to
   pad 1 than to any other pad.
4. Some of it is actually visible: the part left after removing the package
   body outline (Fab, courtyard fallback, via
   :func:`kicad_tools.geometry.package_body.package_body_polygon`) and the
   pad copper is non-empty.

Outcomes (both ``warning`` severity by default, advisory category):

* ``pin1_marker_missing`` -- no silkscreen element satisfies 1-3.
* ``pin1_marker_obscured`` -- elements satisfy 1-3, but every one of them
  lies entirely under the package body or on pad copper.

Footprint selection (see :class:`Pin1MarkerRule`): footprints with at least
``min_pads`` copper pads (default 3) including a pad ``1`` (or ``A1``), plus
footprints whose id matches :data:`DEFAULT_POLARIZED_PATTERN` (two-pin
diodes, LEDs and polarized capacitors), minus
:data:`DEFAULT_EXCLUDE_PATTERN` (mounting holes, test points, jumpers,
switches, passives, keyed USB-C / coax connectors ...), with explicit ``include_references`` /
``exclude_references`` overrides.

Out of scope: the rule does not judge whether a mark is *unambiguous*
(e.g. a SOIC outline's corner segments are closer to pin 1 than pin 8 and
so count), and it does not generate markers -- adding polarity marks is a
listed follow-up of :mod:`kicad_tools.silkscreen.generator`.  Board-level
``gr_poly`` silk is not modeled (not parsed by the PCB schema).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.geometry.courtyard import _fp_transform
from kicad_tools.geometry.package_body import footprint_side, package_body_polygon

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .silkscreen import _silk_side, _text_bbox_geometry

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, BoardGraphic, Footprint, FootprintGraphic, Pad

PIN1_MARKER_MISSING_RULE_ID = "pin1_marker_missing"
PIN1_MARKER_OBSCURED_RULE_ID = "pin1_marker_obscured"

# Two-pin (or otherwise pad-count-exempt) orientation-sensitive parts,
# matched with ``re.search`` against the footprint id (``Lib:Name``): KiCad's
# diode / LED libraries and polarized-capacitor footprints (``CP_*``).
DEFAULT_POLARIZED_PATTERN = r"^(Diode_|LED_)|:(D|LED|CP)_|Capacitor_Tantalum"

# Footprints never checked by default: no orientation, orientation that does
# not matter for assembly, or orientation fixed by the mechanical body
# (reversible USB-C, coax).  Matched with ``re.search``.
DEFAULT_EXCLUDE_PATTERN = (
    r"MountingHole|TestPoint|Fiducial|NetTie|Jumper|Button_Switch|"
    r"^Resistor_|^Inductor_|^Fuse|:R_Array|:R_Pack|:C_|"
    r"USB_C|Connector_Coaxial|BNC|SMA_|U\.FL"
)

# Pad numbers treated as "pin 1" (``A1`` for ball-grid arrays).
PIN1_PAD_NUMBERS = ("1", "A1")

# Default search radius (mm) from pad-1 copper for a marker.  Large enough
# for the "+" on a radial electrolytic; the asymmetry test keeps it from
# picking up marks that belong to neighboring pins.
DEFAULT_SEARCH_RADIUS_MM = 2.5

# A marker must be closer to pad 1 than to any other pad by at least this
# margin (mm), so floating-point ties on symmetric outlines do not count.
_ASYMMETRY_MARGIN_MM = 1e-3

# Minimum visible area (mm^2) for a marker to count as visible.
_MIN_VISIBLE_AREA_MM2 = 1e-4

_ARC_SEGMENTS = 8

_Transform = Any


def _has_copper(pad: Pad) -> bool:
    return any(layer.endswith(".Cu") or layer == "*.Cu" for layer in pad.layers)


def _arc_points(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float]]:
    """Sample a three-point arc; falls back to the chord points if collinear."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, mid, end]
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / d
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def ccw(a: float, b: float) -> float:
        return (b - a) % (2.0 * math.pi)

    # Sweep through ``mid``: go CCW if mid lies on the CCW path to end.
    sweep = ccw(a1, a3)
    if ccw(a1, a2) > sweep:
        sweep -= 2.0 * math.pi
    return [
        (
            ux + r * math.cos(a1 + sweep * i / _ARC_SEGMENTS),
            uy + r * math.sin(a1 + sweep * i / _ARC_SEGMENTS),
        )
        for i in range(_ARC_SEGMENTS + 1)
    ]


def _graphic_geometry(
    graphic: FootprintGraphic | BoardGraphic, transform: _Transform | None
) -> Any | None:
    """Build a shapely geometry for a silk graphic (stroke-buffered)."""
    from shapely.geometry import LineString, Point, Polygon  # type: ignore[import-untyped]

    def xf(p: tuple[float, float]) -> tuple[float, float]:
        return transform(p) if transform is not None else p

    half = max(graphic.stroke_width, 0.0) / 2.0
    kind = graphic.graphic_type
    geom: Any | None = None
    if kind == "line":
        if graphic.start == graphic.end:
            return Point(xf(graphic.start)).buffer(max(half, 0.05))
        geom = LineString([xf(graphic.start), xf(graphic.end)])
    elif kind == "rect":
        sx, sy = graphic.start
        ex, ey = graphic.end
        geom = LineString([xf(p) for p in [(sx, sy), (ex, sy), (ex, ey), (sx, ey), (sx, sy)]])
    elif kind == "circle" and graphic.center is not None:
        radius = getattr(graphic, "radius", None) or math.dist(graphic.center, graphic.end)
        # Pin-1 dots are usually filled; model the disc so a dot counts
        # whether or not ``(fill ...)`` was parsed.
        return Point(xf(graphic.center)).buffer(radius + half)
    elif kind == "arc" and graphic.mid is not None:
        geom = LineString([xf(p) for p in _arc_points(graphic.start, graphic.mid, graphic.end)])
    elif kind == "poly":
        points = getattr(graphic, "points", None) or []
        if len(points) >= 3:
            polygon = Polygon([xf(p) for p in points])
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            return polygon.buffer(half) if half > 0 else polygon
        return None
    if geom is None:
        return None
    return geom.buffer(max(half, 1e-3))


class Pin1MarkerRule(DRCRule):
    """Check that orientation-sensitive footprints carry a visible pin-1 mark.

    Rule IDs generated:
        - pin1_marker_missing: no silkscreen element near pad 1 identifies it.
        - pin1_marker_obscured: the only candidates are hidden under the
          package body or on pad copper.
    """

    rule_id = PIN1_MARKER_MISSING_RULE_ID
    name = "Pin-1 Marker"
    description = (
        "Checks that ICs, diodes, LEDs, polarized capacitors and connectors "
        "have a visible silkscreen pin-1 / polarity marker next to pad 1"
    )

    def __init__(
        self,
        *,
        min_pads: int | None = 3,
        polarized_pattern: str | re.Pattern[str] | None = DEFAULT_POLARIZED_PATTERN,
        exclude_pattern: str | re.Pattern[str] | None = DEFAULT_EXCLUDE_PATTERN,
        include_references: Iterable[str] = (),
        exclude_references: Iterable[str] = (),
        search_radius_mm: float = DEFAULT_SEARCH_RADIUS_MM,
        require_asymmetry: bool = True,
        include_board_silk: bool = True,
        severity: str = "warning",
    ) -> None:
        """Configure the rule.

        Args:
            min_pads: Footprints with at least this many copper pads,
                including a pin-1 pad, are checked.  ``None`` disables the
                pad-count heuristic.
            polarized_pattern: Regex (``re.search`` on ``Lib:Name``)
                selecting footprints regardless of pad count.
            exclude_pattern: Regex excluding footprints the heuristics
                would otherwise select.
            include_references: References always checked.
            exclude_references: References never checked.
            search_radius_mm: Maximum distance from pad-1 copper to a marker.
            require_asymmetry: Require a marker to be closer to pad 1 than
                to every other pad.
            include_board_silk: Also accept board-level ``gr_*`` silk.
            severity: Severity of emitted violations.
        """
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        if not math.isfinite(search_radius_mm) or search_radius_mm <= 0:
            raise ValueError("search_radius_mm must be finite and positive")
        self.min_pads = min_pads
        self.polarized_pattern = (
            re.compile(polarized_pattern)
            if isinstance(polarized_pattern, str)
            else polarized_pattern
        )
        self.exclude_pattern = (
            re.compile(exclude_pattern) if isinstance(exclude_pattern, str) else exclude_pattern
        )
        self.include_references = frozenset(include_references)
        self.exclude_references = frozenset(exclude_references)
        self.search_radius_mm = search_radius_mm
        self.require_asymmetry = require_asymmetry
        self.include_board_silk = include_board_silk
        self.severity = severity

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    @staticmethod
    def pin1_pads(footprint: Footprint) -> list[Pad]:
        """Return the footprint's pin-1 copper pads (``1``, else ``A1``)."""
        for number in PIN1_PAD_NUMBERS:
            pads = [p for p in footprint.pads if p.number == number and _has_copper(p)]
            if pads:
                return pads
        return []

    def selects(self, footprint: Footprint) -> bool:
        """Return True if ``footprint`` is subject to this rule."""
        ref = footprint.reference
        if ref in self.exclude_references:
            return False
        if not self.pin1_pads(footprint):
            return False
        if ref in self.include_references:
            return True
        name = footprint.name
        if self.exclude_pattern is not None and self.exclude_pattern.search(name):
            return False
        if self.polarized_pattern is not None and self.polarized_pattern.search(name):
            return True
        if self.min_pads is None:
            return False
        copper_pads = [p for p in footprint.pads if p.number and _has_copper(p)]
        return len(copper_pads) >= self.min_pads

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check every selected footprint for a visible pin-1 marker.

        Args:
            pcb: The PCB to check.
            design_rules: Unused; the rule is geometric only.

        Returns:
            DRCResults with at most one violation per footprint.
        """
        del design_rules
        results = DRCResults()
        results.rules_checked = 1

        selected = [fp for fp in pcb.footprints if self.selects(fp)]
        if not selected:
            return results
        require_shapely("pin-1 marker check")

        board_silk: dict[str, list[Any]] = {"F": [], "B": []}
        if self.include_board_silk:
            for graphic in pcb.graphics:
                side = _silk_side(graphic.layer)
                if side is None:
                    continue
                geom = _graphic_geometry(graphic, None)
                if geom is not None and not geom.is_empty:
                    board_silk[side].append(geom)

        for footprint in selected:
            violation = self._check_footprint(footprint, board_silk[footprint_side(footprint)])
            if violation is not None:
                results.add(violation)
        return results

    def _check_footprint(self, footprint: Footprint, board_silk: list[Any]) -> DRCViolation | None:
        from shapely.ops import unary_union  # type: ignore[import-untyped]

        from .clearance import _pad_polygon

        pin1_numbers = {p.number for p in self.pin1_pads(footprint)}
        pin1_geoms: list[Any] = []
        other_geoms: list[Any] = []
        for pad in footprint.pads:
            if not _has_copper(pad):
                continue
            polygon = _pad_polygon(pad, footprint)
            if polygon is None:
                continue
            if pad.number in pin1_numbers:
                pin1_geoms.append(polygon)
            elif pad.number:
                other_geoms.append(polygon)
        if not pin1_geoms:
            return None
        pin1 = unary_union(pin1_geoms)
        others = unary_union(other_geoms) if other_geoms else None
        all_pads = unary_union(pin1_geoms + other_geoms)
        body, _source = package_body_polygon(footprint)

        own_silk = list(self._silk_geometries(footprint, []))
        near_board_silk = [g for g in board_silk if g.distance(pin1) <= self.search_radius_mm]

        candidates = 0
        for geom in own_silk + near_board_silk:
            d1 = geom.distance(pin1)
            if d1 > self.search_radius_mm:
                continue
            if (
                self.require_asymmetry
                and others is not None
                and geom.distance(others) <= d1 + _ASYMMETRY_MARGIN_MM
            ):
                continue
            candidates += 1
            if self._is_visible(geom, all_pads, body):
                return None

        # Tie-break for multi-segment marks (#5737 review): KiCad's stock
        # crystal footprints mark pin 1 with an L-shaped corner drawn as two
        # separate ``fp_line`` legs.  Each leg is exactly as close to a
        # neighbouring pad as to pad 1, so the per-element test above ties
        # and rejects both.  Group touching silk into connected components
        # and, for *bent* components only (an L, U or box -- never a single
        # straight stroke, so a bare LED/diode outline line still fails),
        # accept the component when its centroid is strictly closer to pad 1
        # than to every other pad.  A closed box or a U centred on the part
        # keeps tying and is still rejected; an L at another pad's corner
        # points at that pad instead.
        if self.require_asymmetry and others is not None:
            for component in self._bent_components(own_silk + near_board_silk):
                if component.distance(pin1) > self.search_radius_mm:
                    continue
                centroid = component.centroid
                if centroid.distance(others) <= centroid.distance(pin1) + _ASYMMETRY_MARGIN_MM:
                    continue
                candidates += 1
                if self._is_visible(component, all_pads, body):
                    return None

        return self._make_violation(footprint, pin1, obscured=candidates > 0)

    @staticmethod
    def _is_visible(geom: Any, all_pads: Any, body: Any | None) -> bool:
        visible = geom.difference(all_pads)
        if body is not None:
            visible = visible.difference(body)
        return bool(visible.area > _MIN_VISIBLE_AREA_MM2)

    @staticmethod
    def _bent_components(geoms: list[Any]) -> Iterator[Any]:
        """Yield connected silk components that are not a single straight stroke.

        A component counts as *bent* when it fills less than half of its
        convex hull -- true for an L, U or closed
        outline, false for a lone (stroke-buffered) straight line, dot or
        filled triangle (which the per-element test already handles).
        """
        from shapely.ops import unary_union

        if len(geoms) < 2:
            return
        merged = unary_union(geoms)
        parts = getattr(merged, "geoms", [merged])
        for part in parts:
            if part.is_empty or part.area <= 0:
                continue
            if part.area < 0.5 * part.convex_hull.area:
                yield part

    def _silk_geometries(self, footprint: Footprint, board_silk: list[Any]) -> Iterator[Any]:
        side = footprint_side(footprint)
        transform = _fp_transform(footprint)
        for graphic in footprint.graphics:
            if _silk_side(graphic.layer) != side:
                continue
            geom = _graphic_geometry(graphic, transform)
            if geom is not None and not geom.is_empty:
                yield geom
        for text in footprint.texts:
            if text.text_type != "user" or text.hidden or not text.text.strip():
                continue
            if _silk_side(text.layer) != side:
                continue
            geom = _text_bbox_geometry(
                text.text, text.font_size, text.font_thickness, transform(text.position)
            )
            if geom is not None:
                yield geom
        yield from board_silk

    def _make_violation(self, footprint: Footprint, pin1: Any, *, obscured: bool) -> DRCViolation:
        centroid = pin1.centroid
        ref = footprint.reference
        if obscured:
            rule_id = PIN1_MARKER_OBSCURED_RULE_ID
            detail = (
                "has pin-1 silkscreen marks only under the package body or on pad "
                "copper, so none is visible after assembly"
            )
        else:
            rule_id = PIN1_MARKER_MISSING_RULE_ID
            detail = (
                f"has no silkscreen pin-1 / polarity marker within "
                f"{self.search_radius_mm:g}mm of pad 1"
            )
        return DRCViolation(
            rule_id=rule_id,
            severity=self.severity,
            message=(
                f"{ref} ({footprint.name}) {detail} -- add a dot, triangle or bar "
                f"next to pad 1 outside the body, or waive via .kct_waivers.json"
            ),
            location=(round(centroid.x, 3), round(centroid.y, 3)),
            layer=footprint.layer,
            required_value=self.search_radius_mm,
            items=(ref,),
        )

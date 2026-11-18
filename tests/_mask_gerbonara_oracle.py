"""Independent Gerbonara reconstruction for native mask-export comparisons.

This helper deliberately does not import the production Gerber reader or its
geometry constructors. Gerbonara expands apertures/macros and approximates arcs;
Shapely only combines the resulting polygons. Coordinates are returned in the
board's millimetre, y-down frame.
"""

import math
from pathlib import Path


def read_native_gerber(path: Path, *, chord_error_mm: float = 0.000001):
    from gerbonara.graphic_primitives import ArcPoly, Line, Rectangle
    from gerbonara.rs274x import GerberFile
    from gerbonara.utils import MM, polygon_area
    from shapely import make_valid
    from shapely.affinity import scale
    from shapely.geometry import GeometryCollection, Polygon
    from shapely.ops import unary_union

    layer = GeometryCollection()
    for obj in GerberFile.open(path).objects:
        # A clear primitive inside an aperture is a transparent hole in that
        # aperture, not an instruction to erase earlier objects on the layer.
        aperture = GeometryCollection()
        for primitive in obj.to_primitives(unit=MM):
            # Skip only primitives that really have zero area. Gerbonara's
            # is_zero_size() compares a Line's endpoints and ignores its
            # width, but KiCad 10.0.6 plots round pads -- an obround with
            # equal width and height -- as a zero-length line with a width,
            # which renders as a disk of that diameter (issue #9232). A Line
            # has zero area exactly when its width is zero.
            #
            # ArcPoly gets the mirrored treatment: its is_zero_size() tests
            # the polygon area of the *linear* outline, so a full circle
            # drawn as two semicircular arcs -- a two-point outline around a
            # real disk -- reports zero size (measured on gerbonara 1.6.3:
            # Line(0,0,0,0,1.7).to_arc_poly() encloses 2.27 mm^2, issue
            # #9288). Such regions do occur in KiCad output: a graphic
            # gr_poly with (arc ...) corners is plotted as a G36..G37 region
            # with G02/G03 segments (measured on KiCad 10.0.6, issue #9288),
            # while zone fills are polygonized at the board's arc
            # approximation error before plotting -- every committed region
            # block and fresh antipad/keepout-void/island plots use G01
            # segments only. ArcPoly's other sources are polygon apertures
            # (from_regular_polygon: genuine multi-point outline) and
            # aperture-macro Outline primitives (arc centers all None, so
            # always arc-free with a genuine multi-point outline). Testing
            # the arc-approximated outline's area keeps genuinely degenerate
            # regions skipped while retaining real copper.
            if isinstance(primitive, Line):
                if primitive.width <= 0:
                    continue
            elif isinstance(primitive, ArcPoly):
                if math.isclose(
                    polygon_area(primitive.approximate_arcs(max_error=chord_error_mm).outline),
                    0,
                ):
                    continue
            elif primitive.is_zero_size():
                continue
            if isinstance(primitive, Rectangle):
                # Gerbonara 1.6.3 Rectangle.to_arc_poly returns an axis-aligned
                # box even for rotated rectangles. Use its parsed dimensions
                # and angle directly, as its SVG renderer does.
                c, s = math.cos(primitive.rotation), math.sin(primitive.rotation)
                vertices = [
                    (primitive.x + c * x - s * y, primitive.y + s * x + c * y)
                    for x, y in (
                        (-primitive.w / 2, -primitive.h / 2),
                        (primitive.w / 2, -primitive.h / 2),
                        (primitive.w / 2, primitive.h / 2),
                        (-primitive.w / 2, primitive.h / 2),
                    )
                ]
            else:
                vertices = (
                    primitive.to_arc_poly().approximate_arcs(max_error=chord_error_mm).outline
                )
            shape = Polygon(vertices)
            if not shape.is_valid:
                # Mirror the production reader's repair for self-touching
                # fill contours (issue #8947): keep the polygonal parts of
                # make_valid and require the shoelace area to survive the
                # repair, so the oracle stays an independent witness on the
                # same inputs without rejecting legal KiCad output.
                repaired = make_valid(shape)
                parts = [
                    part
                    for part in getattr(repaired, "geoms", [repaired])
                    if part.geom_type in {"Polygon", "MultiPolygon"}
                ]
                if not parts:
                    raise ValueError(f"Invalid independent oracle polygon in {path}")
                repaired = unary_union(parts)
                if abs(repaired.area - shape.area) > 1e-9 * max(shape.area, 1.0):
                    raise ValueError(f"Invalid independent oracle polygon in {path}")
                shape = repaired
            if primitive.polarity_dark == obj.polarity_dark:
                aperture = aperture.union(shape)
            else:
                aperture = aperture.difference(shape)
        if obj.polarity_dark:
            layer = layer.union(aperture)
        else:
            layer = layer.difference(aperture)
    return scale(layer, xfact=1, yfact=-1, origin=(0, 0))

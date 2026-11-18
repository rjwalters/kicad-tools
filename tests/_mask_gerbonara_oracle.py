"""Independent Gerbonara reconstruction for native mask-export comparisons.

This helper deliberately does not import the production Gerber reader or its
geometry constructors. Gerbonara expands apertures/macros and approximates arcs;
Shapely only combines the resulting polygons. Coordinates are returned in the
board's millimetre, y-down frame.
"""

import math
from pathlib import Path


def read_native_gerber(path: Path, *, chord_error_mm: float = 0.000001):
    from gerbonara.graphic_primitives import Line, Rectangle
    from gerbonara.rs274x import GerberFile
    from gerbonara.utils import MM
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
            # For the remaining primitive types is_zero_size() is not a
            # zero-area test either, but the one bad case cannot arise from
            # KiCad output: a region drawn as a full circle from exactly two
            # semicircular arcs has a two-point outline and polygon area 0
            # even though the disk it encloses is real (measured on
            # gerbonara 1.6.3: Line(0,0,0,0,1.7).to_arc_poly(), issue
            # #9288). ArcPoly reaches this arm only from G36..G37 regions
            # and polygon apertures, and KiCad polygonizes zone fills at the
            # board's arc approximation error before plotting, so no KiCad
            # region carries G02/G03 segments: all 49 committed region
            # blocks in this repo's bundles and fresh KiCad 10.0.6 plots of
            # antipad, keepout-void and isolated-copper-island boards use
            # G01 segments only (issue #9288). Polygon apertures likewise
            # always carry a genuine multi-point outline, so every ArcPoly
            # built from KiCad output with nonzero area is retained here.
            if isinstance(primitive, Line):
                if primitive.width <= 0:
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

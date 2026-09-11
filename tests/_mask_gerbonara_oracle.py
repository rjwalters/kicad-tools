"""Independent Gerbonara reconstruction for native mask-export comparisons.

This helper deliberately does not import the production Gerber reader or its
geometry constructors. Gerbonara expands apertures/macros and approximates arcs;
Shapely only combines the resulting polygons. Coordinates are returned in the
board's millimetre, y-down frame.
"""

import math
from pathlib import Path


def read_native_gerber(path: Path, *, chord_error_mm: float = 0.000001):
    from gerbonara.graphic_primitives import Rectangle
    from gerbonara.rs274x import GerberFile
    from gerbonara.utils import MM
    from shapely.affinity import scale
    from shapely.geometry import GeometryCollection, Polygon

    layer = GeometryCollection()
    for obj in GerberFile.open(path).objects:
        # A clear primitive inside an aperture is a transparent hole in that
        # aperture, not an instruction to erase earlier objects on the layer.
        aperture = GeometryCollection()
        for primitive in obj.to_primitives(unit=MM):
            if primitive.is_zero_size():
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
                raise ValueError(f"Invalid independent oracle polygon in {path}")
            if primitive.polarity_dark == obj.polarity_dark:
                aperture = aperture.union(shape)
            else:
                aperture = aperture.difference(shape)
        if obj.polarity_dark:
            layer = layer.union(aperture)
        else:
            layer = layer.difference(aperture)
    return scale(layer, xfact=1, yfact=-1, origin=(0, 0))

"""Conservative, deterministic broad-phase candidates for exact copper checks."""

from collections.abc import Iterator, Sequence
from itertools import combinations

from kicad_tools._shapely import has_shapely

Bounds = tuple[float, float, float, float]


def candidate_pairs(bounds: Sequence[Bounds], margin: float) -> Iterator[tuple[int, int]]:
    """Yield overlapping expanded bounds once, in exhaustive input-pair order.

    Callers supply bounds enclosing *every* shape used by their exact predicate.
    Inclusive bounding-box queries retain touches and tolerance boundaries; this
    broad phase never decides whether copper actually violates or connects.
    Full segment extents also retain interior contacts for chain predicates.
    """
    # Preserve the connectivity module's legacy fallback without Shapely.
    if not has_shapely():
        yield from combinations(range(len(bounds)), 2)
        return
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import box  # type: ignore[import-untyped]

    tree = STRtree([box(*bound) for bound in bounds])
    for i, (xmin, ymin, xmax, ymax) in enumerate(bounds):
        query = box(xmin - margin, ymin - margin, xmax + margin, ymax + margin)
        for j in sorted(int(index) for index in tree.query(query) if index > i):
            yield i, j

"""Conservative, deterministic broad-phase candidates for exact copper checks."""

import math
from collections.abc import Iterator, Sequence
from itertools import combinations

from kicad_tools._shapely import has_shapely

Bounds = tuple[float, float, float, float]

# Query envelopes are built and probed in blocks rather than one geometry at a
# time: the vectorized Shapely entry points do the per-box construction and the
# tree traversal in C instead of per element in Python.  The block size bounds
# peak temporary memory (and keeps the generator incremental for callers that
# stop early, e.g. the coupling predicate's cumulative-length short circuit)
# while still being large enough for the vectorized call to amortize.
_QUERY_BLOCK = 2048


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
    count = len(bounds)
    if count < 2:
        # A single box (or none) can never produce an ordered pair, and the
        # vectorized reshape below needs a populated 2-D envelope array.
        return
    import numpy as np
    import shapely  # type: ignore[import-untyped]
    from shapely import STRtree

    envelopes = np.asarray(bounds, dtype=float).reshape(count, 4)
    if not math.isfinite(margin) or not bool(np.isfinite(envelopes).all()):
        # Malformed input: keep the original per-element behavior exactly,
        # including the GEOS error a fully non-finite envelope raises.  A
        # vectorized query would instead report "no candidates" for such an
        # envelope, silently skipping the exact check the caller asked for.
        yield from _scalar_candidate_pairs(bounds, margin)
        return
    # ``shapely.box`` broadcasts over coordinate arrays, producing exactly the
    # rectangles the per-element ``shapely.geometry.box`` calls produced.
    tree = STRtree(shapely.box(*envelopes.T))
    queries = shapely.box(
        envelopes[:, 0] - margin,
        envelopes[:, 1] - margin,
        envelopes[:, 2] + margin,
        envelopes[:, 3] + margin,
    )
    for start in range(0, count, _QUERY_BLOCK):
        # ``query`` on an array of geometries returns (input index, tree index)
        # rows with the same inclusive bounding-box semantics as the scalar
        # query, so the candidate SET is unchanged; ``lexsort`` restores the
        # ascending (i, j) emission ORDER, and ``j > i`` yields each pair once.
        sources, targets = tree.query(queries[start : start + _QUERY_BLOCK])
        sources = sources + start
        retained = targets > sources
        sources, targets = sources[retained], targets[retained]
        for position in np.lexsort((targets, sources)):
            yield int(sources[position]), int(targets[position])


def _scalar_candidate_pairs(bounds: Sequence[Bounds], margin: float) -> Iterator[tuple[int, int]]:
    """Per-element broad phase retained for non-finite envelopes/margins."""
    from shapely import STRtree
    from shapely.geometry import box  # type: ignore[import-untyped]

    tree = STRtree([box(*bound) for bound in bounds])
    for i, (xmin, ymin, xmax, ymax) in enumerate(bounds):
        query = box(xmin - margin, ymin - margin, xmax + margin, ymax + margin)
        for j in sorted(int(index) for index in tree.query(query) if index > i):
            yield i, j

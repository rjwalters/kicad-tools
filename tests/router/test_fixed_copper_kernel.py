"""The fixed-copper predicate is the clearance kernel, indexed (Epic #5509, 3f).

``FixedFillObstacles.segment_clear`` / ``via_clear`` and their native mirror
``Grid3D::fixed_fill_clear`` are consumer group 6.  Phase 3f moved both onto the
Phase 1b kernel, but neither hands the kernel a whole ``KZonePoly``: a pour has
thousands of boundary edges and the predicate sits in the A* step loop, so both
sides walk a 1 mm spatial index and ask the kernel per edge
(:mod:`kicad_tools.router.fixed_copper_kernel`).

That optimisation is only legitimate if it cannot move a verdict, which is what
this module asserts, three ways:

* :func:`test_indexed_walk_matches_the_unindexed_kernel` -- the indexed verdict
  equals the one ``clearance_kernel.copper_gap`` gives for the *whole* pour, on
  random pours (holes and multi-lobe copper included) and random probes.  This
  is the load-bearing one: it says the index selects, and the kernel judges.
* :func:`test_python_and_native_predicates_agree` -- the two implementations of
  group 6 agree with each other, the property
  ``tests/conformance/adapters/fixed_copper.py`` relies on when it drives both
  and flags a pair when either refuses.
* :func:`test_probe_shapes_are_the_kernel_s_own` and the containment tests --
  the projection itself: a via is the degenerate ``a == b`` query, a hole is
  outside the copper, a layer is not shared.

Seeded ``random`` (no hypothesis), matching
``tests/router/test_clearance_kernel_parity.py``.
"""

from __future__ import annotations

import math
import random

import pytest
from shapely.geometry import Point, Polygon

from kicad_tools.router.clearance_kernel import (
    CLEARANCE_EPSILON_MM,
    KSegment,
    KZonePoly,
    copper_gap,
)
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
from kicad_tools.router.fixed_copper_kernel import (
    TOUCH_EPSILON_MM,
    kernel_fill,
    polygon_rings,
)

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(), reason="C++ router backend not built in this worktree"
)

#: Layer every probe in this module uses; group 6 is a per-layer predicate and
#: the layer gate has its own test.
LAYER = 0


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _pour(rng: random.Random) -> Polygon:
    """A blobby pour, sometimes with a hole punched in it.

    Deliberately not convex and not axis-aligned: an axis-aligned rectangle
    would put every boundary edge in its own bin row and hide any disagreement
    about which edges an index selects.
    """
    cx, cy = rng.uniform(20.0, 80.0), rng.uniform(20.0, 80.0)
    lobes = rng.randint(6, 40)
    points = []
    for i in range(lobes):
        angle = 2.0 * math.pi * i / lobes
        radius = rng.uniform(2.0, 9.0)
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if rng.random() < 0.4 and not polygon.is_empty:
        polygon = polygon.difference(Point(cx, cy).buffer(rng.uniform(0.5, 2.0), quad_segs=8))
    return polygon


def _usable(polygon) -> bool:
    return not polygon.is_empty and polygon.geom_type in ("Polygon", "MultiPolygon")


def _obstacles(polygon, clearance: float) -> FixedFillObstacles:
    return FixedFillObstacles(
        (
            FixedFill(
                source_net="FIXED",
                source_net_id=1,
                layer=LAYER,
                clearance=clearance,
                geometry=polygon,
            ),
        )
    )


def _unindexed_clear(polygon, a, b, half: float, reach: float, clearance: float) -> bool:
    """The same verdict, from the kernel with no index at all.

    One ``copper_gap(KSegment, KZonePoly)`` call per lobe -- the whole ring
    set, walked end to end -- with the comparison
    ``fixed_copper_kernel.fixed_fill_clear`` applies.  This is the reference the
    indexed walk must reproduce.
    """
    required = max(reach, half + clearance)
    probe = KSegment(a[0], a[1], b[0], b[1], 2.0 * half, LAYER)
    for rings in polygon_rings(polygon):
        zone = KZonePoly(rings=tuple(tuple(ring) for ring in rings), layer=LAYER)
        gap = copper_gap(probe, zone)
        if gap + half <= TOUCH_EPSILON_MM or gap < (required - half) - CLEARANCE_EPSILON_MM:
            return False
    return True


# ---------------------------------------------------------------------------
# The index selects; the kernel judges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_indexed_walk_matches_the_unindexed_kernel(seed: int) -> None:
    """Indexing a pour's edges cannot move a clearance verdict.

    The claim ``fixed_copper_kernel`` makes in prose -- an edge outside the
    query box expanded by ``required`` cannot be the nearest one, and the row
    index holds every edge the containment ray can cross -- measured instead of
    asserted.  Probes are drawn across the pour, so the sample covers copper
    the query is inside, copper it merely approaches, and copper far enough
    away that the bbox reject fires.
    """
    rng = random.Random(seed)
    polygon = _pour(rng)
    if not _usable(polygon):
        pytest.skip(f"seed {seed} degenerated to empty copper")
    obstacles = _obstacles(polygon, rng.choice([0.0, 0.1, 0.2]))
    fill = obstacles.fills[0]

    centre = polygon.centroid
    disagreements = []
    for _ in range(300):
        ax = centre.x + rng.uniform(-12.0, 12.0)
        ay = centre.y + rng.uniform(-12.0, 12.0)
        b = (ax + rng.uniform(-3.0, 3.0), ay + rng.uniform(-3.0, 3.0))
        half = rng.choice([0.05, 0.1, 0.2])
        clearance = rng.choice([0.1, 0.15, 0.2])

        indexed = obstacles.segment_clear((ax, ay), b, LAYER, half, clearance)
        whole = _unindexed_clear(polygon, (ax, ay), b, half, half + clearance, fill.clearance)
        if indexed != whole:
            disagreements.append(((ax, ay), b, half, clearance, indexed, whole))

    assert not disagreements, (
        "the 1 mm index changed a verdict the whole-pour kernel walk gives "
        f"(seed {seed}): {disagreements[:5]}"
    )


def test_a_query_deep_inside_the_copper_is_refused() -> None:
    """The case the distance walk alone cannot see.

    A probe far from every boundary edge selects no bin at all, so without the
    even-odd containment step it would read as clear -- while sitting in the
    middle of the pour.  This is why ``fixed_fill_clear`` keeps a containment
    test beside the distance walk, exactly as ``Grid3D::fixed_fill_clear`` does.
    """
    obstacles = _obstacles(Polygon([(0, 0), (40, 0), (40, 40), (0, 40)]), 0.2)
    assert not obstacles.segment_clear((20.0, 20.0), (20.5, 20.5), LAYER, 0.1, 0.2)
    assert not obstacles.via_clear((20.0, 20.0), (LAYER,), 0.3, 0.2)


def test_a_query_inside_a_hole_is_clear() -> None:
    """Even-odd containment across every ring: a hole is not copper.

    The hole is 20 mm across, so an interior probe is also far from every
    boundary edge -- it can only come out clear if the parity walk really
    counts crossings from both rings.
    """
    polygon = Polygon(
        [(0, 0), (40, 0), (40, 40), (0, 40)],
        holes=[[(10, 10), (30, 10), (30, 30), (10, 30)]],
    )
    obstacles = _obstacles(polygon, 0.2)
    assert obstacles.segment_clear((20.0, 20.0), (20.5, 20.5), LAYER, 0.1, 0.2)
    assert not obstacles.segment_clear((10.2, 20.0), (10.2, 21.0), LAYER, 0.1, 0.2)


def test_the_layer_gate_still_holds() -> None:
    """A fill is copper on its own layer and nowhere else."""
    obstacles = _obstacles(Polygon([(0, 0), (40, 0), (40, 40), (0, 40)]), 0.2)
    assert not obstacles.segment_clear((20.0, 20.0), (20.5, 20.5), LAYER, 0.1, 0.2)
    assert obstacles.segment_clear((20.0, 20.0), (20.5, 20.5), LAYER + 1, 0.1, 0.2)


def test_probe_shapes_are_the_kernel_s_own() -> None:
    """A via is the degenerate ``a == b`` query, on every layer it spans.

    ``via_clear`` has always expressed a via as a point query per layer; this
    pins that projection so a future refactor cannot quietly turn it into a
    zero-length segment on one layer only.
    """
    obstacles = _obstacles(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 0.2)
    assert obstacles.via_clear((20.0, 20.0), (LAYER, LAYER + 1), 0.3, 0.2)
    assert not obstacles.via_clear((10.2, 5.0), (LAYER, LAYER + 1), 0.3, 0.2)
    assert obstacles.via_clear((10.2, 5.0), (LAYER + 1,), 0.3, 0.2)
    assert obstacles.segment_clear((10.2, 5.0), (10.2, 5.0), LAYER + 1, 0.3, 0.2) is True


def test_the_fills_own_clearance_can_only_raise_the_requirement() -> None:
    """``max(reach, half + fill.clearance)``, unchanged by the migration."""
    obstacles = _obstacles(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 0.5)
    # 0.25 mm of copper-to-copper gap: clear at the caller's 0.2, refused at
    # the fill's own 0.5.
    assert obstacles.segment_clear((10.35, 5.0), (10.35, 6.0), LAYER, 0.1, 0.2) is False
    loose = _obstacles(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 0.0)
    assert loose.segment_clear((10.35, 5.0), (10.35, 6.0), LAYER, 0.1, 0.2) is True


def test_an_empty_obstacle_set_builds_no_index() -> None:
    """The common case stays free: no fills, no ring walk, no kernel call."""
    assert FixedFillObstacles().segment_clear((0, 0), (1, 1), LAYER, 0.1, 0.2)
    assert FixedFillObstacles().kernel_fills == ()


def test_the_index_is_built_once_per_obstacle_set() -> None:
    """``kernel_fills`` is cached: the A* step loop must not rebuild it.

    A pour's index costs work proportional to its vertex count, and
    ``pathfinder._fixed_step_clear`` asks this predicate once per neighbour
    expansion.  Rebuilding per query would be a routing-time regression no
    verdict test would catch.
    """
    obstacles = _obstacles(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 0.2)
    assert obstacles.kernel_fills is obstacles.kernel_fills


def test_kernel_fill_refuses_empty_rings() -> None:
    """``add_fixed_fill``'s early return, ported: no rings, no fill."""
    assert kernel_fill(LAYER, 0.2, []) is None
    assert kernel_fill(LAYER, 0.2, [[]]) is None


# ---------------------------------------------------------------------------
# The two implementations of group 6 agree
# ---------------------------------------------------------------------------


@requires_cpp
@pytest.mark.parametrize("seed", range(6))
def test_python_and_native_predicates_agree(seed: int) -> None:
    """``FixedFillObstacles`` and ``Grid3D::fixed_fill_clear``, same copper.

    Both halves are now the kernel behind the same index, so this is a parity
    test in the sense ``test_clearance_kernel_parity`` uses the word: a
    divergence is a bug on whichever side disagrees with kicad-cli, never
    something to loosen.  The native side is installed through the production
    path (``add_fixed_fill`` over ``native_polygons()``), which is also what
    keeps the two ring sets identical.
    """
    from kicad_tools.router import router_cpp

    rng = random.Random(1000 + seed)
    polygon = _pour(rng)
    if not _usable(polygon):
        pytest.skip(f"seed {seed} degenerated to empty copper")
    clearance = rng.choice([0.0, 0.1, 0.2])
    obstacles = _obstacles(polygon, clearance)

    grid = router_cpp.Grid3D(1200, 1200, 2, 0.1, 0.0, 0.0)
    for layer, fill_clearance, rings in obstacles.native_polygons():
        grid.add_fixed_fill(layer, fill_clearance, rings)

    centre = polygon.centroid
    disagreements = []
    for _ in range(300):
        ax = centre.x + rng.uniform(-12.0, 12.0)
        ay = centre.y + rng.uniform(-12.0, 12.0)
        bx, by = ax + rng.uniform(-3.0, 3.0), ay + rng.uniform(-3.0, 3.0)
        half = rng.choice([0.05, 0.1, 0.2])
        reach = half + rng.choice([0.1, 0.15, 0.2])

        native = bool(grid.fixed_fill_clear(ax, ay, bx, by, LAYER, half, reach))
        python = obstacles.segment_clear((ax, ay), (bx, by), LAYER, half, reach - half)
        if native != python:
            disagreements.append(((ax, ay), (bx, by), half, reach, native, python))

    assert not disagreements, (
        f"group 6's two implementations disagree (seed {seed}): {disagreements[:5]}"
    )


@requires_cpp
def test_the_native_index_and_the_python_index_hold_the_same_edges() -> None:
    """``kernel_fill`` is a port of ``add_fixed_fill``, not an approximation.

    Both index the same rings into 1 mm cells with the same ``floor`` walk, so
    the Python side's edge list must be the ring set's edges in ring order --
    the property that lets the two predicates above select the same candidates.
    """
    polygon = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    obstacles = _obstacles(polygon, 0.2)
    (indexed,) = obstacles.kernel_fills
    (_, _, rings) = next(iter(obstacles.native_polygons()))

    expected = [
        (ring[i - 1][0], ring[i - 1][1], ring[i][0], ring[i][1])
        for ring in rings
        for i in range(1, len(ring))
    ]
    assert list(indexed.edges) == expected
    assert (indexed.minx, indexed.miny, indexed.maxx, indexed.maxy) == (0.0, 0.0, 10.0, 10.0)
    # Every edge is reachable from both indices.
    assert set().union(*indexed.rows.values()) == set(range(len(expected)))
    assert set().union(*indexed.bins.values()) == set(range(len(expected)))

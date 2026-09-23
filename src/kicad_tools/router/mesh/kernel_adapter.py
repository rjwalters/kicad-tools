"""Mesh obstacles as kernel shapes (Epic #5509, Phase 3e).

:mod:`kicad_tools.router.mesh.obstacles` is consumer group 10 of Epic #5509's
implementation inventory: the predicate the octilinear 45-fit checks every
dogleg leg against before it will emit a corridor.  Until Phase 3e it carried
its own arithmetic -- a ray-cast containment test, an AABB straddle test and a
segment/polygon crossing test, all in :mod:`.geometry`.  This module is the one
place where that model now meets the Phase 1b exact-geometry kernel
(:mod:`kicad_tools.router.clearance_kernel`), the way
``router/lattice/kernel_adapter.py`` is for the lattice engine (Phase 3d) and
``router/fixed_copper_kernel.py`` is for the fixed-copper predicate (Phase 3f).

What the adapter owns
---------------------
* **Shape projection.**  A keep-out rectangle and a pour outline are both
  *regions* -- :func:`region_of_rect` / :func:`region_of_poly` close them into
  the kernel's ``KZonePoly`` ring convention.  A foreign pad is a
  ``KPad`` built by the kernel's own ``make_pad`` (:func:`pad_of`), so its
  copper is measured exactly rather than bounded by a box.  The board outline
  is a closed ``KEdge`` polyline (:func:`outline_of`).  A candidate leg is a
  ``KSegment``: :func:`leg` for the zero-width form the pre-inflated obstacles
  want, :func:`copper_leg` for the real-width form the measured ones want.
* **The broad phase.**  :func:`region_box` / :func:`pad_reach_box` give each
  obstacle the bounding box the caller rejects it by before paying for a kernel
  query.  The boxes only *select* candidates, they never judge one -- an
  obstacle whose box the leg's own box cannot reach provably clears the leg --
  so the broad phase cannot move a verdict.
* **Nothing else.**  Every number in a verdict comes from the kernel: the gaps
  from :func:`~kicad_tools.router.clearance_kernel.copper_gap`, the
  containment parity from
  :func:`~kicad_tools.router.clearance_kernel.ring_edge_crosses_ray`, the
  comparison slack from
  :data:`~kicad_tools.router.clearance_kernel.CLEARANCE_EPSILON_MM`.  No rule
  value, no net semantics, no heuristic (Epic #5509 scope guards #1/#2).

Two obstacle kinds, two leg widths
----------------------------------
:class:`~.obstacles.ObstacleModel` carries obstacles of two different
provenances and the distinction is load-bearing:

* **Pre-inflated** ones -- ``keepouts`` (rectangles a caller already grew) and
  ``pours`` (pour outlines and the committed-trace capsules
  ``MeshPathfinder._route_obstacles`` inflates by a full
  ``trace_width + clearance``).  Their inflation has *already* absorbed the
  candidate's half width, so they are queried with the **zero-width** leg and a
  zero requirement: "does the centreline touch this region?", which is exactly
  what ``segment_intersects_rect`` / ``segment_intersects_polygon`` answered
  before.
* **Measured** ones -- ``pads`` and the board outline, which arrive
  un-inflated.  They are queried with the **copper** leg (the real trace width)
  against the real requirement, which is what makes the answer exact.

Passing the copper leg to a pre-inflated obstacle would count the half width
twice; passing the zero-width leg to a measured one would drop it.  Keeping
both forms in one module is what makes that pairing checkable in one place.

Why the pour branch keeps a touch test
--------------------------------------
``leg_touches`` is a kernel measurement compared against zero, not a relaxed
one: ``copper_gap(region, zero-width leg) <= 0`` is true exactly when the leg
meets or enters the region, which is the predicate
``segment_intersects_polygon`` implemented by hand.  Phase 3e swaps *how the
distance is computed*, not *what is required* -- the requirement stays the
consumer's own, per the epic's scope guard #1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..clearance_kernel import (
    ALL_LAYERS,
    CLEARANCE_EPSILON_MM,
    KEdge,
    KPad,
    KSegment,
    KZonePoly,
    copper_gap,
    make_pad,
    ring_edge_crosses_ray,
)
from .geometry import Pt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ..primitives import Pad

__all__ = [
    "MESH_CLEARANCE_EPSILON_MM",
    "MeshOutline",
    "MeshPad",
    "MeshRegion",
    "copper_leg",
    "leg",
    "leg_clears",
    "leg_touches",
    "outline_of",
    "pad_of",
    "pad_reach_box",
    "region_box",
    "region_of_poly",
    "region_of_rect",
]

MESH_CLEARANCE_EPSILON_MM = CLEARANCE_EPSILON_MM
"""The slack a "gap meets requirement" comparison carries, from the kernel.

Unlike the lattice (whose own ``1e-9`` predates the kernel and was preserved
verbatim in Phase 3d), the mesh predicate had **no** numeric slack at all
before this phase: it was a set of boolean straddle tests.  There is therefore
no incumbent epsilon to preserve, and adopting the kernel's is the choice that
introduces no second model.
"""

MeshPad = KPad
"""A foreign pad's real copper -- the kernel's exact Minkowski pad."""

MeshRegion = KZonePoly
"""A pre-inflated keep-out rectangle or a pour outline, as a kernel region."""

Box = tuple[float, float, float, float]
"""A broad-phase bounding box: ``(xmin, ymin, xmax, ymax)``."""


# ---------------------------------------------------------------------------
# Candidate projection -- the leg under test
# ---------------------------------------------------------------------------


def leg(a: Pt, b: Pt) -> KSegment:
    """The candidate leg as a **centreline**, carrying no copper width.

    The form the pre-inflated obstacles are queried with: their inflation has
    already absorbed the trace's half width, so the question they answer is
    "does the centreline enter this region?".
    """
    return KSegment(a[0], a[1], b[0], b[1], 0.0, ALL_LAYERS)


def copper_leg(a: Pt, b: Pt, half: float) -> KSegment:
    """The candidate leg as **copper**: centreline ``a-b``, width ``2 * half``.

    The form the measured obstacles (pads, the board outline) are queried
    with, so the kernel returns an edge-to-edge gap rather than a centreline
    distance.
    """
    return KSegment(a[0], a[1], b[0], b[1], 2.0 * half, ALL_LAYERS)


# ---------------------------------------------------------------------------
# Obstacle projection
# ---------------------------------------------------------------------------


def region_of_rect(xmin: float, ymin: float, xmax: float, ymax: float) -> MeshRegion:
    """One pre-inflated keep-out rectangle, as a closed kernel region."""
    return KZonePoly(
        rings=(
            (
                (xmin, ymin),
                (xmax, ymin),
                (xmax, ymax),
                (xmin, ymax),
                (xmin, ymin),
            ),
        ),
        layer=ALL_LAYERS,
    )


def region_of_poly(poly: Sequence[Pt]) -> MeshRegion | None:
    """One pour outline or committed-trace capsule, as a closed kernel region.

    Args:
        poly: A ring WITHOUT a repeated final vertex -- the mesh engine's own
            convention (:func:`~.geometry.segment_intersects_polygon`).

    Returns:
        The region, or ``None`` for a degenerate ring of fewer than three
        vertices.  ``segment_intersects_polygon`` answered ``False`` for those,
        and a two-vertex "region" would otherwise become a bare line the kernel
        measures a real distance to.
    """
    if len(poly) < 3:
        return None
    ring = tuple((float(x), float(y)) for x, y in poly)
    return KZonePoly(rings=(ring + (ring[0],),), layer=ALL_LAYERS)


def pad_of(pad: Pad) -> MeshPad | None:
    """One foreign pad's copper, as the kernel's exact Minkowski pad.

    ``Pad.rotation`` is the *residual* board-space angle (issue #4910): the
    ``io.py`` parsers already swap ``width``/``height`` for a pad within a
    degree of 90/270 and leave only what that swap does not absorb.  Passing
    the pair straight to
    :func:`~kicad_tools.router.clearance_kernel.make_pad` is therefore correct
    for every pad, cardinal or not -- the same contract
    :func:`~kicad_tools.router.primitives.pad_half_extents` consumes.

    ``layer`` is deliberately left at
    :data:`~kicad_tools.router.clearance_kernel.ALL_LAYERS`: the mesh engine
    filters pads by layer *before* they reach the model
    (``MeshPathfinder._foreign_pads_layer``), so a second filter here would be
    a second model of the same rule.

    Returns:
        The pad, or ``None`` when it has no positive copper extent.
    """
    if pad.width <= 0.0 or pad.height <= 0.0:
        return None
    return make_pad(
        pad.shape,
        pad.width,
        pad.height,
        rotation_deg=pad.rotation,
        cx=pad.x,
        cy=pad.y,
        layer=ALL_LAYERS,
        drill=0.0,
    )


class MeshOutline:
    """The board outline: a containment test plus a copper-to-edge measurement.

    Holds the outline twice, because ``is_clear`` asks two different questions
    of it and the kernel answers them with two different primitives:

    * **Inside the board?** -- even-odd parity over the closed ring, walked
      with the kernel's own
      :func:`~kicad_tools.router.clearance_kernel.ring_edge_crosses_ray`.  This
      is the same predicate :func:`~.geometry.point_in_polygon` implemented by
      hand, and the same walk
      :func:`kicad_tools.router.clearance_kernel._point_in_rings` performs for
      a region.
    * **Far enough from the edge?** -- ``copper_gap`` against a
      :class:`~kicad_tools.router.clearance_kernel.KEdge` polyline, the shape
      the kernel models ``Edge.Cuts`` with.

    Attributes:
        ring: The outline closed (first vertex repeated), for the parity walk.
        edge: The same outline as the kernel's board-edge polyline.
    """

    __slots__ = ("edge", "ring")

    def __init__(self, ring: tuple[Pt, ...], edge: KEdge) -> None:
        self.ring = ring
        self.edge = edge

    def contains(self, p: Pt) -> bool:
        """True when ``p`` is inside the outline (boundary counts as inside).

        Boundary inclusion is what :func:`~.geometry.point_in_polygon`
        guaranteed with its explicit on-edge branch; here it falls out of the
        distance reading instead: a point within
        :data:`MESH_CLEARANCE_EPSILON_MM` of the outline is on it, and the
        parity walk's own answer is only consulted away from the boundary,
        where it is unambiguous.
        """
        px, py = p
        inside = False
        ring = self.ring
        for i in range(1, len(ring)):
            ax, ay = ring[i - 1]
            bx, by = ring[i]
            if ring_edge_crosses_ray(px, py, ax, ay, bx, by):
                inside = not inside
        if inside:
            return True
        return copper_gap(KSegment(px, py, px, py, 0.0, ALL_LAYERS), self.edge) <= (
            MESH_CLEARANCE_EPSILON_MM
        )

    def clears_edge(self, copper: KSegment, required_mm: float) -> bool:
        """True when ``copper`` keeps ``required_mm`` from the board outline.

        A non-positive requirement means the caller has no copper-to-edge rule
        to apply, and the branch short-circuits to the containment test alone
        -- the behaviour every pre-Phase-3e call site had.
        """
        if required_mm <= 0.0:
            return True
        return copper_gap(copper, self.edge) >= required_mm - MESH_CLEARANCE_EPSILON_MM


def outline_of(outline: Sequence[Pt]) -> MeshOutline | None:
    """The board outline as a :class:`MeshOutline`, or ``None`` if degenerate.

    Args:
        outline: The outline ring WITHOUT a repeated final vertex -- the mesh
            engine's own convention.
    """
    if len(outline) < 3:
        return None
    ring = tuple((float(x), float(y)) for x, y in outline)
    closed = ring + (ring[0],)
    return MeshOutline(closed, KEdge(closed))


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def leg_touches(candidate: KSegment, region: MeshRegion) -> bool:
    """True when the centreline ``candidate`` meets or enters ``region``.

    The kernel reading of :func:`~.geometry.segment_intersects_rect` /
    :func:`~.geometry.segment_intersects_polygon`: a gap of zero or less is
    contact, and the region already carries whatever inflation its producer
    applied, so the requirement is zero by construction.
    """
    return copper_gap(region, candidate) <= 0.0


def leg_clears(copper: KSegment, pad: MeshPad, required_mm: float) -> bool:
    """True when the leg's copper keeps ``required_mm`` from the pad's copper."""
    return copper_gap(copper, pad) >= required_mm - MESH_CLEARANCE_EPSILON_MM


# ---------------------------------------------------------------------------
# Broad phase
# ---------------------------------------------------------------------------


def region_box(region: MeshRegion) -> Box:
    """The region's bounding box.

    A leg whose own bounding box misses this one cannot touch the region, so
    the caller may skip the kernel query.  Touch is a zero-requirement test,
    so the box needs no margin.
    """
    xs: list[float] = []
    ys: list[float] = []
    for ring in region.rings:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    if not xs:
        return (0.0, 0.0, -1.0, -1.0)  # empty: misses every query box
    return (min(xs), min(ys), max(xs), max(ys))


def pad_reach_box(pad: MeshPad, reach: float) -> Box:
    """The pad's copper box, grown by ``reach``.

    ``reach`` is the querying leg's own half width plus the clearance it must
    keep, so a leg whose centreline bounding box misses this box is provably
    further than the requirement from the pad and needs no kernel query.  The
    box is built from the pad's Minkowski core plus its corner radius, which
    bounds its copper exactly.
    """
    if not pad.core:
        return (0.0, 0.0, -1.0, -1.0)  # empty: misses every query box
    xs = [x for x, _ in pad.core]
    ys = [y for _, y in pad.core]
    grow = pad.corner_radius + reach
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)

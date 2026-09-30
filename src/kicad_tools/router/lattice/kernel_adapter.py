"""Id-space adapter onto the exact-geometry clearance kernel (Epic #5509, 3d).

The lattice engine is deliberately **geometry-only in an integer net-id
space**: it never sees a net name or a net class (the #4597 discipline, and
the reason :mod:`.pairwise` exists at all).  The Phase 1b kernel
(:mod:`kicad_tools.router.clearance_kernel`) is the opposite kind of module --
it knows nothing about nets *or* rules, only about copper shapes and the
distance between them.  Neither can call the other directly, so this module is
the one place where the two meet, and the only place in the lattice package
that imports the kernel.

What the adapter owns
---------------------
* **Shape projection.**  A lattice trace is a centreline plus a half-width on
  one layer (:func:`trace`); a lattice node probe and a committed via body are
  both discs (:func:`site`); the board outline is a polyline (:func:`outline_edge`).
  Every lattice clearance predicate is one of the pairs those three shapes
  admit, so the kernel's five shape classes reduce to three here.
* **Net-id semantics.**  Same-net copper is not an obstacle to itself, and the
  pair-level HV requirement (#4602) is keyed by an *id* pair.  The kernel has
  no notion of either, so :class:`LatticeProbe` carries the querying net id
  beside the shape and the predicate stays a property of the pair.
* **The comparison tolerance.**  :data:`LATTICE_CLEARANCE_EPSILON_MM` is the
  lattice's own long-standing ``1e-9`` slack, preserved verbatim.  It is
  deliberately **not** the kernel's ``CLEARANCE_EPSILON_MM`` (``1e-4``): this
  phase swaps *geometry*, and adopting a 0.1 um looser verdict tolerance at
  the same time would be a behaviour change wearing a refactor's clothes.
  Collapsing the epsilon copies is Phase 4's job, once every consumer is on
  the kernel.

Why the gaps are edge-to-edge now
---------------------------------
The lattice predicates used to compare a **centreline** distance against a
composed gap (``own_half + stored_half + clearance``).  The kernel answers the
edge-to-edge question directly, so each call site now compares
``copper_gap(...)`` against the *clearance* alone.  The two forms are
algebraically identical -- ``d - own_half - stored_half >= clr`` is
``d >= own_half + stored_half + clr`` -- so no verdict moves except by
floating-point noise far below the ``1e-9`` slack.

The kernel's ``segment_to_segment_distance`` also differs from
``lattice.geometry.seg_seg_dist`` in one degenerate case: where an endpoint
lies exactly on the other segment's supporting line, the lattice's
``(o > 0) != (o > 0)`` straddle test reports a crossing while the kernel's
strict test does not.  That divergence cannot move a verdict -- whenever it
fires, the point in question provably lies *on* the other segment, so both
models return a zero distance anyway (see the analysis on #5663).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clearance_kernel import ALL_LAYERS, KEdge, KSegment, KShape, KVia, copper_gap
from .geometry import Pt

__all__ = [
    "LATTICE_CLEARANCE_EPSILON_MM",
    "LatticeProbe",
    "gap",
    "outline_edge",
    "satisfies",
    "site",
    "trace",
]

LATTICE_CLEARANCE_EPSILON_MM = 1e-9
"""The lattice's own "is this gap at least the requirement" slack, unchanged.

Every predicate in :mod:`.obstacles`, :mod:`.coupled` and
:mod:`.escape_boundary` compared against ``requirement - 1e-9`` before this
phase and compares against ``requirement - 1e-9`` after it.
"""


# ---------------------------------------------------------------------------
# Shape projection -- lattice copper as kernel shapes
# ---------------------------------------------------------------------------


def trace(a: Pt, b: Pt, half_width: float, layer: int = ALL_LAYERS) -> KSegment:
    """A routed trace: centreline ``a-b``, copper ``2 * half_width``, one layer.

    Args:
        a: Centreline start.
        b: Centreline end.
        half_width: Copper half-width in mm (the lattice's own unit).
        layer: Lattice layer index, or :data:`ALL_LAYERS` when the caller has
            already restricted the query to one layer's copper.

    Returns:
        The kernel segment.
    """
    return KSegment(a[0], a[1], b[0], b[1], 2.0 * half_width, layer)


def site(point: Pt, radius: float) -> KVia:
    """A lattice node probe or a committed via body: a disc of ``radius``.

    Modelled as a :class:`~kicad_tools.router.clearance_kernel.KVia` rather
    than a zero-length segment for two reasons.  A committed lattice via
    really is a through-via -- copper on every layer, which is what ``KVia``
    means -- and a node probe is only ever asked about copper the caller has
    already filtered to one layer, so the all-layers reading cannot widen it.
    The disc form also routes through the kernel's cheaper point-to-segment
    branch instead of its segment-to-segment one.

    Args:
        point: Centre.
        radius: Copper radius in mm (a via's body radius, or the querying
            connection's half-width for a node site).

    Returns:
        The kernel via.  No drill is carried: the lattice's hole-to-hole
        floor arrives pre-composed as ``same_net_via_gap`` and never as a
        drill diameter, so there is nothing for :func:`hole_gap` to read.
    """
    return KVia(point[0], point[1], 2.0 * radius)


def outline_edge(c: Pt, d: Pt) -> KEdge:
    """One board-outline segment, as the kernel's open-polyline edge shape.

    :class:`~kicad_tools.router.clearance_kernel.KEdge` carries a polyline,
    but :class:`~.escape_boundary.EscapeBoundary` holds its outline as a set
    of *unordered* edges (they need not be consecutive), so each becomes a
    two-vertex edge of its own.  Copper-to-outline clearance is a minimum over
    the edges either way.
    """
    return KEdge(((c[0], c[1]), (d[0], d[1])))


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def gap(own: KShape, other: KShape) -> float:
    """Edge-to-edge copper gap in mm; negative when the copper overlaps.

    A thin, named delegation to
    :func:`kicad_tools.router.clearance_kernel.copper_gap` so every lattice
    call site reads as "ask the kernel" rather than as an import detail.
    """
    return copper_gap(own, other)


def satisfies(gap_mm: float, required_mm: float) -> bool:
    """True when an already-measured ``gap_mm`` meets ``required_mm``.

    Split from :meth:`LatticeProbe.clears` because the #4602 pairwise term
    tests the *same* gap against a second, wider requirement: measuring once
    and comparing twice is both faster and provably consistent between the
    two verdicts.
    """
    return gap_mm >= required_mm - LATTICE_CLEARANCE_EPSILON_MM


@dataclass(frozen=True)
class LatticeProbe:
    """The querying copper: a kernel shape plus the net id that owns it.

    Built once per predicate call and reused across the whole obstacle scan,
    so the per-item cost stays one shape construction and one kernel query.

    Attributes:
        shape: The candidate's geometry, in the kernel's shape vocabulary.
        net: The candidate's integer net id -- the lattice's own id space.
    """

    shape: KShape
    net: int

    def gap(self, other: KShape) -> float:
        """Edge-to-edge gap in mm between this probe and ``other``."""
        return copper_gap(self.shape, other)

    def clears(self, other: KShape, required_mm: float) -> bool:
        """True when this probe is at least ``required_mm`` from ``other``."""
        return copper_gap(self.shape, other) >= required_mm - LATTICE_CLEARANCE_EPSILON_MM

    def foreign(self, other_net: int) -> bool:
        """True when ``other_net`` is a different net than the probe's own.

        The one net-id rule the kernel cannot express: same-net copper is
        never a clearance obstacle to itself.  Callers that need the *other*
        same-net rules (the #4318 body-crossing check, the same-net via drill
        floor) branch on this too, which is why it is a predicate rather than
        an early return inside :meth:`clears`.
        """
        return other_net != self.net

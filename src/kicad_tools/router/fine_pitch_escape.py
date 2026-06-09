"""
Fine-pitch escape predicates, detector, and manufacturer-aware clearance defaults.

Issue #3371 (P_FP1 + P_FP2) -- the foundation layer for the fine-pitch
escape ladder.  This module exposes the *pure-logic* primitives that
downstream phases build on:

- :func:`geometry_needs_fine_pitch_escape` (P_FP1) -- the Q_FP1
  recipe-relative predicate.  Returns ``True`` when the trace-plus-clearance
  corridor cannot fit between adjacent pads of a fine-pitch package at the
  current routing parameters.

- :func:`get_default_escape_clearance` (P_FP1) -- the Q_FP2
  manufacturer-aware safe-default helper.  Returns a clearance value that
  is strictly above the manufacturer's minimum capability so the
  per-net-class ``escape_clearance`` overrides cannot accidentally violate
  fab limits.

- :class:`FinePitchRegion` (P_FP2) -- frozen dataclass describing a single
  fine-pitch escape region as a circular halo of fixed radius centred on a
  detected fine-pitch package.  Carries the metadata downstream consumers
  (validator, grid halo, C++ pad-segment check) need to apply the
  per-net-class escape clearance once threaded.

- :func:`detect_fine_pitch_regions` (P_FP2) -- iterates the router-level
  pad list and returns one :class:`FinePitchRegion` per qualifying
  component (pitch <= ``FINE_PITCH_THRESHOLD_MM`` AND geometry-needs the
  escape per the Q_FP1 predicate).  The detector is the trigger feeding the
  P_FP3 per-net clearance application.

- :func:`resolve_clearance_with_escape_region` (P_FP2) -- single-threading-
  point helper for the validator / grid-halo / C++ pad-segment consumers.
  Returns the escape clearance for a (pad, net_class, regions) triple when
  the gates fire, falling back to the standard
  :meth:`DesignRules.get_clearance_for_component` otherwise.  Enforces the
  impedance-controlled-net guard from PR #3273 so the escape rule cannot
  shrink clearance on impedance-controlled segments.

No router behaviour is changed by this module.  P_FP2 wires the trigger
predicate into a region detector + the clearance-resolution helper; the
threading points at :meth:`cpp_backend.from_routing_grid` line 619 and at
the pathfinder boundary opt in via an *empty* default regions list, so
the change is zero-impact until P_FP3 wires the regions through.  P_FP3
applies the per-net-class clearance at the consumer side; P_FP4 composes
the ladder with auto-layers and auto-pcb-size; P_FP5 adds the consumer
test on softstart rev B.

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mfr_limits import MfrLimits
    from .primitives import Pad
    from .rules import DesignRules, NetClassRouting

__all__ = [
    "ESCAPE_CLEARANCE_SAFETY_MARGIN_MM",
    "FinePitchRegion",
    "detect_fine_pitch_regions",
    "geometry_needs_fine_pitch_escape",
    "get_default_escape_clearance",
    "resolve_clearance_with_escape_region",
]


# Q_FP2 safety margin (Issue #3371): the per-net-class escape clearance
# default is set to ``mfr.min_clearance + ESCAPE_CLEARANCE_SAFETY_MARGIN_MM``
# so callers never sit exactly on the manufacturer floor.  0.013mm
# (~0.5 mil) is large enough to absorb the rounding error introduced by
# the 0.1mm routing grid quantisation and the C++ pad-segment validator's
# ``epsilon`` tolerance, while small enough that it does not consume the
# headroom buyers pay for at JLCPCB tier-1 (0.127mm + 0.013mm = 0.14mm,
# which is still well under the recipe-side 0.20mm clearance the
# fine-pitch escape replaces in the corridor).
#
# Hardcoded for now.  If a future manufacturer surfaces a tighter epsilon
# tolerance and the 0.013mm margin starts to bite, promote this to an
# ``MfrLimits`` field rather than tuning the constant globally.
ESCAPE_CLEARANCE_SAFETY_MARGIN_MM: float = 0.013


def geometry_needs_fine_pitch_escape(
    trace_width: float,
    clearance: float,
    pin_pitch: float,
    pad_size: float,
) -> bool:
    """Return True when the trace corridor cannot fit between adjacent pads.

    Implements the Q_FP1 *recipe-relative* trigger from Issue #3371: a
    fine-pitch package needs special escape handling iff a 0-degree trace
    (with full per-side clearance) cannot fit through the gap between two
    neighbouring same-row pads at the current routing parameters.

    Geometry (pin-to-pin centre-to-centre = ``pin_pitch``):

        gap_between_pads = pin_pitch - pad_size              # edge-to-edge
        required_corridor = 2 * (trace_width + clearance)    # trace + clearances

        needs_escape  iff  required_corridor > gap_between_pads

    The predicate is *recipe-relative* because the trigger flips with
    routing parameters: the same UCC27211 SOIC-8 footprint (1.27mm
    pitch, 0.30mm pad) does NOT need escape at 0.20mm clearance + 0.15mm
    trace (corridor 0.70mm <= gap 0.97mm) but DOES need escape at
    0.20mm + 0.30mm (corridor 1.00mm > gap 0.97mm).  This is the right
    semantic: the router only pays the escape-pipeline cost when the
    geometry forces it.

    Args:
        trace_width: Trace width in mm at the escape stub.
        clearance: Per-side clearance in mm between trace edge and pad
            edge.  This is the *backbone* / default clearance, not the
            shrunk fine-pitch clearance (which is what the caller will
            apply *if* this predicate fires).
        pin_pitch: Centre-to-centre pin pitch in mm.
        pad_size: Pad dimension along the pitch axis in mm (typically
            the pad width for a horizontal SOIC row; the *short* axis
            of an elongated leaded pad).

    Returns:
        ``True`` when the corridor is infeasible at these parameters
        (escape pipeline should engage); ``False`` when a trace fits
        through cleanly.

    Example -- UCC27211 SOIC-8 at JLCPCB tier-1 recipe (0.30mm trace,
    0.20mm clearance, 1.27mm pitch, 0.30mm pad):

        >>> geometry_needs_fine_pitch_escape(0.30, 0.20, 1.27, 0.30)
        True

    Example -- same SOIC-8 at relaxed clearance (0.30 trace + 0.15
    clearance fits in the 0.97mm gap):

        >>> geometry_needs_fine_pitch_escape(0.30, 0.15, 1.27, 0.30)
        False

    Example -- 2.54mm-pitch header (DIP-style) is never corridor
    constrained for typical recipes:

        >>> geometry_needs_fine_pitch_escape(0.30, 0.20, 2.54, 0.50)
        False
    """
    gap = pin_pitch - pad_size
    required = 2.0 * (trace_width + clearance)
    return required > gap


def get_default_escape_clearance(mfr_limits: MfrLimits) -> float:
    """Return the manufacturer-aware safe default for escape clearance.

    Implements the Q_FP2 decision from Issue #3371: the default
    per-net-class escape clearance is the manufacturer's minimum
    clearance plus :data:`ESCAPE_CLEARANCE_SAFETY_MARGIN_MM` so callers
    never sit exactly on the fab floor.  Per-net-class overrides via
    :attr:`kicad_tools.router.rules.NetClassRouting.escape_clearance`
    are still allowed; this helper just supplies the *fallback* value
    when the recipe does not specify one.

    Concrete values (from :mod:`kicad_tools.router.mfr_limits`):

    - jlcpcb / jlcpcb-tier1 / pcbway: ``0.127 + 0.013 = 0.140`` mm
    - oshpark:                        ``0.152 + 0.013 = 0.165`` mm

    Args:
        mfr_limits: Manufacturer capability profile.  The only field
            consumed is :attr:`MfrLimits.min_clearance`.

    Returns:
        Safe default escape clearance in mm.

    Note:
        The safety margin is intentionally small.  Callers that want a
        more generous floor (e.g. for sub-tier-1 manufacturers with
        wider lot-to-lot variation) should set
        :attr:`NetClassRouting.escape_clearance` explicitly rather than
        tuning the global constant -- the constant is calibrated for
        tier-1 fabs where 0.013mm covers grid quantisation + validator
        epsilon.
    """
    return mfr_limits.min_clearance + ESCAPE_CLEARANCE_SAFETY_MARGIN_MM


# ============================================================================
# P_FP2 -- region detector
# ============================================================================
#
# The detector returns one :class:`FinePitchRegion` per qualifying component
# (i.e. one per fine-pitch SOIC/SSOP/TSSOP/etc. that the pathfinder will
# encounter on this board).  Regions are circular halos centred on the
# component's pad-cluster centroid, with a configurable radius (default
# 5mm -- architect Q2 recommendation in Issue #3371's open-question table).
#
# The choice of *circular halo* (vs per-package bounding box or per-row
# corridor strip) is the simplest of the architect's three options and is
# the most robust for the dispatcher: a circular ``contains_point`` check
# is one ``hypot`` call per A* edge expansion, no axis-aligned-vs-rotated
# package handling, no per-row dispatch.  Future phases can widen the
# region shape if a per-row strip turns out to be necessary, but the P_FP1
# builder's decision #1 was "circular halo per pad cluster centroid";
# P_FP2 honours that.
#
# The detector is a *pure function* of (pads, rules, mfr_limits).  Routing
# state (the negotiator's current iteration count, the auto-layer stack,
# the auto-pcb-size escalation tier, etc.) is intentionally NOT consumed
# -- the same set of regions is valid for the lifetime of a board's design
# rules, so the detector can run once at route-start and the result is
# cached at the consumer side.


# Default escape-region radius (mm).  Q_FP2 architect recommendation in
# Issue #3371: a single fixed radius covering "near the fine-pitch
# package" is sufficient for the vast majority of boards (one or two
# fine-pitch parts per board, neighbouring traffic blocked by the
# package's own pad cluster well before 5mm).  Tighter radii (e.g. 3mm)
# under-cover the escape corridor for SOIC-8 inboard pins; wider radii
# (e.g. 10mm) start to consume the inter-package backbone routing where
# the relaxed default clearance is the right answer.
#
# Recipes that want per-package tuning should set
# :attr:`DesignRules.escape_region_radius` (when that field lands in
# P_FP3 / P_FP4) or pass an explicit ``radius_mm`` to
# :func:`detect_fine_pitch_regions`.
DEFAULT_ESCAPE_REGION_RADIUS_MM: float = 5.0


@dataclass(frozen=True)
class FinePitchRegion:
    """A single fine-pitch escape region.

    Issue #3371 / P_FP2 -- describes a circular halo around a detected
    fine-pitch package within which the per-net-class
    :attr:`NetClassRouting.escape_clearance` is applied (for non-
    impedance-controlled nets) instead of the global
    :attr:`DesignRules.trace_clearance`.

    Frozen dataclass for two reasons:

    1. The detector's output is a pure function of the board's design
       rules + footprints; recomputing it cheaply is fine and accidental
       mutation downstream would silently desync the validator from the
       grid halo.
    2. The :meth:`contains_point` check is on the per-edge A*-expansion
       hot path; immutable instances are safely hashable / cacheable by
       reference if a future consumer wants to memoise the dispatch.

    Attributes:
        package_ref: Component reference of the fine-pitch package
            (e.g. ``"U5"``).  Carried for diagnostics and for callers
            that want to log the per-region escape decision.
        package_origin: World-coordinate ``(x, y)`` centre of the pad
            cluster, in mm.  Used as the circular-halo centre by
            :meth:`contains_point`.
        radius_mm: Halo radius in mm.  The escape clearance applies to
            cells / pads strictly within this radius of
            :attr:`package_origin`.
        pin_pitch: Detected centre-to-centre pin pitch of the
            fine-pitch package, in mm.  Stored for diagnostics and so
            P_FP3+ consumers can re-validate the corridor math without
            re-deriving the pitch.
        pad_size_along_pitch: Pad dimension along the pitch axis, in
            mm.  For a horizontal SOIC row this is the pad *width*; for
            a vertical row it is the pad *height*.  Used by P_FP3+ to
            apply the Q_FP1 corridor predicate at edge-expansion time.
        escape_clearance: Default escape clearance for this region, in
            mm, computed at detection time from
            :func:`get_default_escape_clearance` and the configured
            manufacturer.  Per-net-class overrides
            (:attr:`NetClassRouting.escape_clearance`) take precedence
            at the consumer side (see
            :func:`resolve_clearance_with_escape_region`).
        pad_refs: Set of ``(ref, pin)`` tuples identifying the pads
            that belong to this fine-pitch package.  Used by
            :meth:`applies_to_pad` for the strict "is this pad inside
            *this* fine-pitch package" check (which is more precise
            than the circular-halo geometry test for a foreign pad
            that just happens to sit inside the halo).
    """

    package_ref: str
    package_origin: tuple[float, float]
    radius_mm: float
    pin_pitch: float
    pad_size_along_pitch: float
    escape_clearance: float
    pad_refs: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def contains_point(self, x: float, y: float) -> bool:
        """Return True when the world-coordinate ``(x, y)`` is inside the halo.

        Circular containment check; strictly less than the radius (a
        point exactly on the radius boundary is considered *outside*
        the region so the standard ``trace_clearance`` applies).  The
        ``hypot`` form is the canonical Python idiom for 2D
        Euclidean distance and avoids the sqrt-of-sum-of-squares
        rounding error you can hit at small distances.

        Args:
            x: World x coordinate in mm.
            y: World y coordinate in mm.

        Returns:
            ``True`` when the point is strictly inside the circular
            halo of radius :attr:`radius_mm` centred at
            :attr:`package_origin`.
        """
        dx = x - self.package_origin[0]
        dy = y - self.package_origin[1]
        return math.hypot(dx, dy) < self.radius_mm

    def applies_to_pad(self, pad: Pad) -> bool:
        """Return True when this region applies to ``pad``.

        Two paths:

        1. **Identity match** -- when the pad's ``(ref, pin)`` is in
           :attr:`pad_refs`, this region *owns* the pad (it is one of
           the fine-pitch package's own pads) and the escape clearance
           applies unconditionally.
        2. **Geometric containment** -- when the pad's centre lies
           strictly inside the circular halo (per
           :meth:`contains_point`), the pad sits in the escape region
           even though it belongs to a foreign component.  This case
           covers neighbouring decoupling caps and signal-trace
           start/end pads inside the halo radius.

        Strict identity match comes first so a fine-pitch package's
        own pad is *always* considered an escape-region pad even when
        the package geometry is large enough that its outermost pads
        sit on or past the halo boundary.

        Args:
            pad: Router-level pad to test.

        Returns:
            ``True`` when the per-net-class escape clearance should be
            considered for this pad.
        """
        if (pad.ref, pad.pin) in self.pad_refs:
            return True
        return self.contains_point(pad.x, pad.y)


def _calculate_min_pitch(pads: list[Pad]) -> float:
    """Return the minimum centre-to-centre pin pitch in ``pads``, or 0.

    Replicates :func:`kicad_tools.router.escape._calculate_min_pitch`
    locally to avoid a circular import (the escape module already
    depends on this module for :data:`FINE_PITCH_THRESHOLD_MM`).
    Coincident pads (within 0.01mm) are ignored.
    """
    if len(pads) < 2:
        return 0.0
    min_dist = math.inf
    for i, p1 in enumerate(pads):
        for p2 in pads[i + 1 :]:
            dist = math.hypot(p2.x - p1.x, p2.y - p1.y)
            if dist > 0.01:
                if dist < min_dist:
                    min_dist = dist
    return min_dist if min_dist is not math.inf else 0.0


def _infer_pad_size_along_pitch(pads: list[Pad]) -> float:
    """Return the pad dimension along the pitch axis for ``pads``.

    Q_FP2 builder decision #2: rather than tagging each pad with an
    explicit "pitch axis = X|Y" annotation, the detector infers the
    pitch direction from the cluster geometry.  For a dual-row SOIC
    arrangement with two distinct Y values, the pitch axis is X and the
    relevant pad dimension is :attr:`Pad.width`; for a vertical row
    (two distinct X values), the pitch axis is Y and the dimension is
    :attr:`Pad.height`.  When the cluster does not look like a clear
    row layout (e.g. a quad QFN), we fall back to the *shorter* of the
    two pad dimensions, which is the conservative choice for the
    Q_FP1 predicate (the corridor predicate fires more readily on
    smaller pad sizes -> we are biased toward applying the escape
    rule, which is the right safe direction for P_FP2).

    Args:
        pads: All pads belonging to a single component.

    Returns:
        Pad size along the pitch axis in mm.  Falls back to the
        ``min(width, height)`` of the median pad for non-row layouts.
        Returns ``0.0`` when ``pads`` is empty.
    """
    if not pads:
        return 0.0

    ys = sorted({round(p.y, 2) for p in pads})
    xs = sorted({round(p.x, 2) for p in pads})

    # Horizontal row layout: 2 distinct Y values, many X values -> pitch
    # axis is X, relevant pad dimension is width.
    if len(ys) == 2 and len(xs) >= len(pads) // 2 - 1 and len(xs) >= 2:
        widths = [p.width for p in pads if p.width > 0]
        if widths:
            return min(widths)  # use smallest -- conservative for predicate
        return 0.0

    # Vertical row layout: 2 distinct X values, many Y values -> pitch
    # axis is Y, relevant pad dimension is height.
    if len(xs) == 2 and len(ys) >= len(pads) // 2 - 1 and len(ys) >= 2:
        heights = [p.height for p in pads if p.height > 0]
        if heights:
            return min(heights)
        return 0.0

    # Non-row layout (BGA-like, quad QFN, etc.) -- conservative
    # fallback: use the shortest of width/height across the cluster so
    # the Q_FP1 predicate fires more readily.  These packages are not
    # the primary target of P_FP2 (BGA escape is a different ladder per
    # the issue's "Out of scope" list), but the detector should not
    # silently skip them either.
    sizes: list[float] = []
    for p in pads:
        if p.width > 0:
            sizes.append(p.width)
        if p.height > 0:
            sizes.append(p.height)
    return min(sizes) if sizes else 0.0


def detect_fine_pitch_regions(
    pads: list[Pad],
    rules: DesignRules,
    mfr_limits: MfrLimits | None = None,
    radius_mm: float = DEFAULT_ESCAPE_REGION_RADIUS_MM,
) -> list[FinePitchRegion]:
    """Detect per-component fine-pitch escape regions on a board.

    Issue #3371 / P_FP2 -- groups ``pads`` by component reference
    (``pad.ref``), computes per-cluster pin pitch / pad size, applies
    the :data:`FINE_PITCH_THRESHOLD_MM` pitch ceiling AND the Q_FP1
    recipe-relative geometry predicate
    (:func:`geometry_needs_fine_pitch_escape`), and returns one
    :class:`FinePitchRegion` per qualifying component.

    Trigger conditions (BOTH must hold):

    1. The cluster's minimum pin pitch is ``<= FINE_PITCH_THRESHOLD_MM``
       (1.5mm by default; raised from the historical 0.8mm in P_FP1
       so 1.27mm SOIC qualifies).
    2. The recipe-relative corridor predicate fires at the current
       (rules.trace_width, rules.trace_clearance, pin_pitch,
       pad_size_along_pitch) parameters.  This filters out 1.27mm-pitch
       packages that already route fine at the active clearance (the
       same UCC27211 routes without escape at 0.15mm + 0.30mm).

    The default escape clearance for each region is computed once via
    :func:`get_default_escape_clearance` using ``mfr_limits``.  When
    ``mfr_limits`` is ``None`` the function attempts to look up the
    manufacturer via ``rules.manufacturer``; if that is also unset the
    region's :attr:`FinePitchRegion.escape_clearance` is left at
    ``rules.trace_clearance`` (i.e. no shrink) so consumers see a
    no-op region rather than an undefined value.

    Args:
        pads: All router-level pads on the board, across all
            components.  The detector groups by ``pad.ref`` internally.
            Pads with empty ``ref`` (e.g. board-edge fiducials) are
            skipped.
        rules: Design rules.  ``rules.trace_width`` and
            ``rules.trace_clearance`` are the recipe parameters fed
            into the Q_FP1 predicate.  ``rules.manufacturer`` is used
            as a fallback to look up the escape-clearance default
            when ``mfr_limits`` is not supplied.
        mfr_limits: Optional explicit manufacturer profile.  When
            supplied, takes precedence over ``rules.manufacturer`` for
            the escape-clearance default.
        radius_mm: Halo radius in mm.  Defaults to
            :data:`DEFAULT_ESCAPE_REGION_RADIUS_MM` (5mm).  Callers can
            shrink this for board-specific tuning (e.g. dense boards
            where two fine-pitch packages sit within 10mm of each
            other and overlapping halos would over-shrink the inter-
            package backbone).

    Returns:
        List of :class:`FinePitchRegion` objects, one per qualifying
        component.  Empty list when no fine-pitch packages need the
        escape shrink at the current recipe parameters.  Order is
        insertion order over the input ``pads`` (deterministic for
        callers that care about stable region lists).

    Example -- a board with one UCC27211 SOIC-8 at JLCPCB tier-1
    recipe:

        >>> from kicad_tools.router.mfr_limits import MFR_JLCPCB
        >>> from kicad_tools.router.rules import DesignRules
        >>> from kicad_tools.router.primitives import Pad
        >>> rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        >>> # 8 pads of UCC27211: 4 per side, 1.27mm pitch, 0.30mm wide
        >>> pads = []
        >>> for i in range(4):
        ...     pads.append(Pad(x=i*1.27, y=0.0, width=0.30, height=1.55,
        ...                     net=1, net_name="N1", ref="U5",
        ...                     pin=str(i+1)))
        ...     pads.append(Pad(x=i*1.27, y=4.0, width=0.30, height=1.55,
        ...                     net=1, net_name="N1", ref="U5",
        ...                     pin=str(i+5)))
        >>> regions = detect_fine_pitch_regions(pads, rules, MFR_JLCPCB)
        >>> len(regions)
        1
        >>> regions[0].package_ref
        'U5'
    """
    # Resolve the per-region escape clearance default.  When neither
    # the explicit ``mfr_limits`` argument nor ``rules.manufacturer``
    # is set, fall back to ``rules.trace_clearance`` (a no-shrink
    # default that keeps the region from accidentally tightening
    # clearance on boards without a configured manufacturer).
    resolved_mfr_limits: MfrLimits | None = mfr_limits
    if resolved_mfr_limits is None and getattr(rules, "manufacturer", None):
        try:
            from .mfr_limits import get_mfr_limits

            resolved_mfr_limits = get_mfr_limits(rules.manufacturer)
        except (ValueError, ImportError):
            resolved_mfr_limits = None

    if resolved_mfr_limits is not None:
        default_escape_clearance = get_default_escape_clearance(resolved_mfr_limits)
    else:
        default_escape_clearance = rules.trace_clearance

    # Group pads by component reference, preserving insertion order
    # for deterministic region output.
    pads_by_ref: dict[str, list[Pad]] = defaultdict(list)
    ref_order: list[str] = []
    for pad in pads:
        if not pad.ref:
            continue
        if pad.ref not in pads_by_ref:
            ref_order.append(pad.ref)
        pads_by_ref[pad.ref].append(pad)

    regions: list[FinePitchRegion] = []
    for ref in ref_order:
        cluster = pads_by_ref[ref]
        # Require at least 4 pads to qualify as a fine-pitch escape
        # region (Issue #3371 / P_FP3 follow-up).  Two-pad clusters
        # (R / C / L / D passives) and three-pad clusters (SOT-23
        # ICs) have a trivially-routable corridor "between own pads"
        # in the sense the Q_FP1 predicate measures, but the
        # corridor is never threaded because external traces route
        # around the package.  Without this guard the detector
        # fires on every 0402 passive at strict clearance (their
        # 0.96mm pitch + 0.56mm pads trips the geometry predicate
        # at 0.20mm clearance + 0.30mm trace -- corridor = 0.40mm,
        # required = 0.70mm), causing the C++ pad-segment validator
        # to relax to ``escape_clearance`` for every passive on the
        # board.  That makes the pathfinder commit to traces too
        # close to passive pads, regressing reach on boards 03/05/07.
        # Real fine-pitch escape candidates (SOIC-8, SSOP, TSSOP,
        # LQFP, QFN, etc.) all have >= 4 pads.
        if len(cluster) < 4:
            continue

        pin_pitch = _calculate_min_pitch(cluster)
        if pin_pitch <= 0.0:
            continue

        # Pitch ceiling -- excludes 2.54mm DIP-style headers and the
        # like.  Uses the module-level :data:`FINE_PITCH_THRESHOLD_MM`
        # so the cap stays in sync with
        # :data:`kicad_tools.router.escape.FINE_PITCH_THRESHOLD_MM`
        # (drift-prevention test in
        # ``tests/test_fine_pitch_escape.py``).
        if pin_pitch > FINE_PITCH_THRESHOLD_MM:
            continue

        pad_size = _infer_pad_size_along_pitch(cluster)
        if pad_size <= 0.0:
            continue

        # Q_FP1 recipe-relative trigger -- a fine-pitch package only
        # qualifies for the escape region when the corridor is
        # actually infeasible at the active recipe.
        if not geometry_needs_fine_pitch_escape(
            trace_width=rules.trace_width,
            clearance=rules.trace_clearance,
            pin_pitch=pin_pitch,
            pad_size=pad_size,
        ):
            continue

        # Centroid of the cluster's pad cluster.  Use the bounding-box
        # centre rather than a per-pad mean: clusters with asymmetric
        # pad fill (e.g. a 14-pin SOIC with one pad depopulated) come
        # out with a centre that better tracks the package's physical
        # outline.
        xs = [p.x for p in cluster]
        ys = [p.y for p in cluster]
        origin = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

        pad_refs = frozenset((p.ref, p.pin) for p in cluster)

        regions.append(
            FinePitchRegion(
                package_ref=ref,
                package_origin=origin,
                radius_mm=radius_mm,
                pin_pitch=pin_pitch,
                pad_size_along_pitch=pad_size,
                escape_clearance=default_escape_clearance,
                pad_refs=pad_refs,
            )
        )

    return regions


# Module-level :data:`FINE_PITCH_THRESHOLD_MM` import is deliberately
# placed at the bottom of the module to avoid a circular import with
# :mod:`kicad_tools.router.escape` (which imports
# :func:`geometry_needs_fine_pitch_escape` from this module for the
# P_FP3+ dispatcher work).  The constant is defined in escape.py for
# historical reasons (the predicate ``is_fine_pitch_ssop`` consumes it)
# and we re-export it via this lazy reference so detector callers do
# not have to know about the escape module.  P_FP1 builder decision #4
# acknowledged the manual drift risk; the drift-prevention test guards
# both literals match.
from .escape import FINE_PITCH_THRESHOLD_MM  # noqa: E402

# ============================================================================
# P_FP2 -- single-threading-point clearance resolver
# ============================================================================


def resolve_clearance_with_escape_region(
    rules: DesignRules,
    pad: Pad,
    net_class: NetClassRouting | None,
    regions: list[FinePitchRegion] | None,
    pin_pitch: float | None = None,
) -> float:
    """Resolve the clearance to use for ``pad`` under the active ``net_class``.

    Issue #3371 / P_FP2 -- the single-threading-point helper that
    consumers (validator clearance source, grid halo, C++ pad-segment
    check) call to fold the fine-pitch escape-region logic into their
    existing :meth:`DesignRules.get_clearance_for_component` lookup.

    Precedence (high to low):

    1. **Per-component explicit override** -- if ``pad.ref`` is in
       ``rules.component_clearances``, that value wins
       unconditionally.  Preserves the Issue #1016 contract.
    2. **Impedance-controlled-net guard (PR #3273)** -- if
       ``net_class`` has either ``target_diff_impedance`` or
       ``target_single_impedance`` set, the escape rule is bypassed
       entirely and we fall through to the standard
       :meth:`DesignRules.get_clearance_for_component` lookup so the
       impedance budget is preserved.  This is the load-bearing
       carve-out: shrinking clearance on a 50 Ω single-ended escape
       segment would break the trace's characteristic impedance,
       which is exactly the trap PR #3273 closed.
    3. **Per-net-class escape clearance in a fine-pitch region** --
       when ``net_class.escape_clearance`` is set AND at least one
       :class:`FinePitchRegion` in ``regions`` applies to ``pad`` (per
       :meth:`FinePitchRegion.applies_to_pad`), return that escape
       clearance.
    4. **Region default escape clearance** -- when ``net_class`` does
       NOT supply its own override but a region applies, return the
       region's :attr:`FinePitchRegion.escape_clearance` (the
       manufacturer-aware safe default from
       :func:`get_default_escape_clearance` computed at detection
       time).  This lets boards opt into the escape shrink without
       having to set per-class overrides explicitly.
    5. **Fall through** -- standard
       :meth:`DesignRules.get_clearance_for_component` lookup, which
       handles the global ``fine_pitch_clearance`` shrink (with the
       Issue #2867 narrow-channel guard) and the per-net-class
       ``escape_clearance`` override when no region matches.

    Backward-compat: when ``regions`` is ``None`` or empty (the
    pre-#3371 default), this function returns the same value as
    ``rules.get_clearance_for_component(pad.ref, pin_pitch, net_class)``
    byte-for-byte.  P_FP2 leaves all call sites passing ``None`` /
    ``[]`` for ``regions`` so router behaviour is unchanged.  P_FP3
    wires the actual region list at the cpp_backend ``from_routing_grid``
    bulk-copy and at the pathfinder per-net A* boundary.

    Args:
        rules: Active design rules.
        pad: Router-level pad whose clearance is being resolved.
            ``pad.ref`` drives the per-component override lookup;
            ``(pad.x, pad.y)`` and ``(pad.ref, pad.pin)`` drive the
            region applicability check.
        net_class: Active net class for the route consuming this pad's
            clearance.  ``None`` falls through to global defaults.
        regions: Optional list of fine-pitch escape regions on this
            board.  ``None`` and empty list both mean "no escape regions
            configured" -- the resolver behaves exactly like
            :meth:`DesignRules.get_clearance_for_component`.
        pin_pitch: Optional pin pitch for the component, in mm.  When
            supplied, threaded into the fall-through
            :meth:`DesignRules.get_clearance_for_component` call so the
            global ``fine_pitch_clearance`` shrink can fire on the
            component-pad-pitch path.  ``None`` (the default) preserves
            the pre-#3371 call shape.

    Returns:
        Clearance in mm to apply for ``pad`` under ``net_class``.

    Note:
        This function is the *only* P_FP2-supplied seam between the
        detector and the validator / grid halo / C++ clearance source.
        Future phases that add more escape-region behaviour (per-
        package radius override, escape-direction-aware clearance,
        etc.) should land here so the threading footprint stays a
        single function.
    """
    # Layer 1: per-component explicit override.  Wins unconditionally so
    # designers can opt-out of the escape rule for a specific component
    # by pinning ``component_clearances[ref]`` to the relaxed value.
    if pad.ref and pad.ref in rules.component_clearances:
        return rules.component_clearances[pad.ref]

    # Layer 2: impedance-controlled-net guard (PR #3273).
    #
    # When the active net class declares any impedance target -- either
    # differential (``target_diff_impedance``) or single-ended
    # (``target_single_impedance``) -- the escape clearance rule is
    # bypassed entirely.  Shrinking clearance on an impedance-controlled
    # segment changes the trace's effective characteristic impedance
    # (the C between trace and ground / between coupled traces depends
    # on clearance), which is precisely the trap that PR #3273's
    # validation closed.  Re-opening the trap on fine-pitch escape
    # would be a silent regression.  Architect Q7 + P_FP1 builder
    # decision #5: gate on both impedance fields being ``None``.
    if net_class is not None and (
        net_class.target_diff_impedance is not None
        or net_class.target_single_impedance is not None
    ):
        # Fall through to the standard lookup -- trace_clearance (or
        # the global ``fine_pitch_clearance`` shrink, which has its
        # own #2867 narrow-channel guard).
        return rules.get_clearance_for_component(pad.ref, pin_pitch, net_class=None)

    # Layers 3 & 4: region applicability check.
    if regions:
        for region in regions:
            if region.applies_to_pad(pad):
                # Resolve the candidate escape clearance: per-net-class
                # override wins over the region default.
                if net_class is not None and net_class.escape_clearance is not None:
                    candidate = net_class.escape_clearance
                else:
                    candidate = region.escape_clearance

                # Issue #3371 / P_FP3 narrow-channel guard.  Apply the
                # Issue #2867 corridor-feasibility check to the region
                # path so the C++ validator does not accept geometrically
                # infeasible through-channel routes.  Geometry (mirrors
                # the grid halo's ``_clearance_for_pin_pitch`` guard):
                #
                #     effective_channel = pin_pitch - 2*candidate - trace_width
                #     required_channel  = 2*candidate + trace_width
                #
                # ``effective_channel`` is the band available for a
                # trace centred between two halo edges; ``required_channel``
                # is the minimum copper-to-copper distance the candidate
                # clearance demands.  When the channel cannot fit the
                # trace at the candidate clearance, the shrink is
                # infeasible -- fall through to the standard
                # ``get_clearance_for_component`` so the validator
                # rejects through-channel routes the same way the grid
                # halo does.
                pitch_for_guard = pin_pitch
                if pitch_for_guard is None:
                    pitch_for_guard = region.pin_pitch
                if pitch_for_guard is not None:
                    effective_channel = (
                        pitch_for_guard - 2.0 * candidate - rules.trace_width
                    )
                    required_channel = 2.0 * candidate + rules.trace_width
                    if effective_channel < required_channel:
                        # Channel too narrow at candidate clearance --
                        # decline the shrink for this in-region pad.
                        return rules.get_clearance_for_component(
                            pad.ref, pin_pitch, net_class=net_class
                        )

                return candidate

    # Layer 5: standard fall-through.  ``net_class`` is forwarded so
    # the global ``fine_pitch_clearance`` / per-component-pitch logic
    # in :meth:`DesignRules.get_clearance_for_component` runs unchanged.
    return rules.get_clearance_for_component(pad.ref, pin_pitch, net_class=net_class)

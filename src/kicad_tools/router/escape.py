"""
Escape routing for dense packages (BGA, QFP, QFN).

This module provides escape routing patterns for dense packages where
standard routing fails due to pin congestion. Dense packages have pins
that can't all route outward simultaneously - inner pins get blocked
by outer pins trying to escape.

Escape routing strategies:
- Ring-based escape (BGA): Route outer pins first, inner pins via down
- Alternating direction (QFP/QFN): Alternate escape directions per pin
- Staggered via fanout: Place vias in staggered pattern for via-in-pad

Example::

    from kicad_tools.router.escape import EscapeRouter

    # Create escape router
    escape = EscapeRouter(grid, rules)

    # Detect dense packages and generate escape routes
    for pad in pads:
        if escape.needs_escape_routing(pad, all_pads):
            routes = escape.generate_escape_routes(pad, all_pads)
            for route in routes:
                grid.reserve_escape_path(route)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules, NetClassRouting

from kicad_tools.core.geometry import point_to_segment_distance

from .layers import Layer, LayerType
from .primitives import Pad, Route, Segment, Via
from .via_clearance import point_clear_of_copper, segment_clears_foreign_via

logger = logging.getLogger(__name__)


# Issue #3371 (P_FP1): default cap for "fine-pitch" classification.
#
# Raised from the historical 0.75mm to 1.5mm so 1.27mm-pitch SOIC packages
# (UCC27211 SOIC-8, LM393, MCP6001-SOIC, and every other SOIC-8 family
# part) qualify for the fine-pitch escape path.  The previous 0.75mm value
# capped detection at SSOP/TSSOP and silently excluded SOIC, which left
# the router falling through to the generic SOP path for 1.27mm pitches
# even when the recipe-relative corridor math (``2 * (trace_width +
# clearance) > pin_pitch - pad_size``) said the corridor was infeasible.
#
# The 1.5mm value is intentionally generous: it covers 1.27mm SOIC plus
# every tighter standard pitch (SOP/SSOP/TSSOP/QFN at 0.65mm or 0.5mm)
# under one threshold so callers do not have to enumerate pitches.  This
# constant is the SAME value as :attr:`DesignRules.fine_pitch_threshold`
# default and the two are intentionally kept in sync -- when one
# changes, the other should be revisited.  Centralising it here gives
# the escape-router-side code a single reference rather than re-quoting
# the literal.
FINE_PITCH_THRESHOLD_MM: float = 1.5

# Issue #3398: locality radius for the consumer-aware SOP rescue deferral.
#
# When a rescue-only-band SOP pad's net has its nearest off-package pad
# within this distance, the P_FP6 in-pad rescue is DEFERRED and the pad
# emits no escape geometry at all.  Empirical basis (softstart rev B,
# Jun 2026): the UCC27211 gate-driver pins with local consumers sit
# 2-12 mm from their targets (bootstrap caps, TVS clamps, gate
# resistors, Kelvin-source stitches), while the genuinely-far driver
# input nets are 20-54 mm away (MCU).  Rescuing the local-consumer pads
# placed 19 in-pad vias whose B.Cu via field blocked the FET bus +
# snubber routing and DROPPED reach 18 -> 8/30; the short local hops
# route better with no canned escape at all.  15 mm splits the two
# observed clusters with margin on both sides.
SOP_LOCAL_CONSUMER_RADIUS_MM: float = 15.0

# Issue #3398: maximum number of in-pad rescues granted per SOP row on a
# rescue-only-band package, awarded to the candidate(s) with the FARTHEST
# off-package consumer.  Override with the
# ``KICAD_TOOLS_SOP_RESCUE_ROW_CAP`` environment variable (integer) for
# reach experiments.
#
# DEFAULT 0 (= defer every rescue; rescue-only-band packages enter the
# dense list but emit NO escape geometry, reproducing the pre-#3398
# not-dense routing bit-for-bit).  This is an EMPIRICAL decision, from
# four same-machine paired A/B measurements on softstart rev B
# (Jun 9 2026, PYTHONHASHSEED=0, C++ backend):
#
#   | Config                  | L=2 (2400 s)   | L=4 (480 s floor test) |
#   | ----------------------- | -------------- | ---------------------- |
#   | main (no rescue)        | 17/30          | 22/30 (all attempted)  |
#   | cap=1/row + target stub | 15/30          | --                     |
#   | cap=1/row + perp stub   | 16/30          | 20/30 (timeout @28/30) |
#
# Mechanisms observed:
#
# - Full-row rescue (pre-cap): the U5/U6 pin 7+8 via pairs walled the
#   rows' shared back-layer launch corridor; the pin-7 nets that route
#   in < 3 s from their surface pads went to ``blocked_path`` after
#   ~45 s of search each (18 -> 8/30 in the #3395 measurement).
# - cap=1 (pin 8 only): the pin-7 nets recover, but GATE_POS_A -- the
#   one net the rescue targets -- stays ``blocked_path`` WITH its own
#   in-pad via, at both L=2 and L=4.  The via field does not unblock
#   it; the binding constraint is corridor capacity near the MCU, not
#   the driver-side launch.
# - Wall-clock: the rescues enlarge the explorable B.Cu space around
#   the FET pairs, slowing failed searches; at L=4 the 480 s production
#   budget then truncates 2 nets that unmodified main has time to
#   route (22/30 -> 20/30 with an identical blocked-set minus SWDIO).
# - A static back-layer congestion gate cannot discriminate: softstart
#   is all-SMD, so B.Cu is 0% occupied at escape time everywhere; the
#   congestion that kills these nets is created dynamically during
#   routing.
#
# Net: until the detailed router can exploit (rather than merely
# tolerate) a pre-placed in-pad via field on this geometry class, the
# correct production setting is "admit to dense list, emit nothing".
# Keeping the band admission + this machinery means the dispatcher
# plumbing is in place and a one-variable experiment
# (KICAD_TOOLS_SOP_RESCUE_ROW_CAP=1) re-enables the rescue.
SOP_RESCUE_MAX_PER_ROW: int = 0


def _sop_rescue_row_cap() -> int:
    """Resolve the per-row rescue cap (env override, Issue #3398)."""
    import os

    raw = os.environ.get("KICAD_TOOLS_SOP_RESCUE_ROW_CAP")
    if raw is None:
        return SOP_RESCUE_MAX_PER_ROW
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Ignoring invalid KICAD_TOOLS_SOP_RESCUE_ROW_CAP=%r "
            "(expected integer); using default %d.",
            raw,
            SOP_RESCUE_MAX_PER_ROW,
        )
        return SOP_RESCUE_MAX_PER_ROW


@dataclass(frozen=True)
class _SegmentAdapter:
    """Adapter that exposes a :class:`Segment` with the ``start_x/start_y/end_x/end_y``
    attribute names expected by :func:`point_clear_of_copper`.

    Issue #2944: The shared clearance helper consumes duck-typed track
    segments via the ``TrackSegmentLike`` protocol (start_x, start_y, end_x,
    end_y, width).  The router's :class:`Segment` uses ``x1, y1, x2, y2``.
    Rather than rename the latter (which is a wide-blast-radius change),
    we adapt at the boundary.
    """

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float


class PackageType(Enum):
    """Package type classification for escape routing."""

    UNKNOWN = auto()
    BGA = auto()  # Ball Grid Array
    QFP = auto()  # Quad Flat Package
    QFN = auto()  # Quad Flat No-lead
    TQFP = auto()  # Thin Quad Flat Package
    SOP = auto()  # Small Outline Package
    SSOP = auto()  # Shrink Small Outline Package (0.65mm pitch)
    TSSOP = auto()  # Thin Shrink Small Outline Package (0.65mm pitch)
    SOT = auto()  # Small Outline Transistor
    DIP = auto()  # Dual In-line Package
    MULTI_ROW_CONNECTOR = auto()  # Multi-row through-hole connector (2xN, 3xN, 4xN, >= 20 pins)
    USB_C_CONNECTOR = auto()  # Fine-pitch (<= 0.6mm) 2-row SMT connector with mounting tabs
    THROUGH_HOLE = auto()  # Generic through-hole


class EscapeDirection(Enum):
    """Direction for pin escape routing."""

    NORTH = auto()
    SOUTH = auto()
    EAST = auto()
    WEST = auto()
    NORTHEAST = auto()
    NORTHWEST = auto()
    SOUTHEAST = auto()
    SOUTHWEST = auto()
    VIA_DOWN = auto()  # Escape via layer change


@dataclass
class EscapeRoute:
    """An escape route from a pin to open routing space.

    Attributes:
        pad: The pad being escaped
        direction: Primary escape direction
        via_pos: Position for via if layer change needed (None if surface escape)
        escape_layer: Layer to route on after escape
        escape_point: Point where escape route ends (open for further routing)
        segments: Trace segments for the escape
        via: Via object if layer change needed
        ring_index: For BGA, which ring this pad is in (0=outer)
    """

    pad: Pad
    direction: EscapeDirection
    escape_point: tuple[float, float]
    escape_layer: Layer
    via_pos: tuple[float, float] | None = None
    segments: list[Segment] = field(default_factory=list)
    via: Via | None = None
    ring_index: int = 0


@dataclass
class PackageInfo:
    """Information about a detected package.

    Attributes:
        ref: Component reference (e.g., "U1")
        package_type: Detected package type
        center: Package center position (x, y)
        pads: List of pads belonging to this package
        pin_count: Number of pins
        pin_pitch: Estimated pin pitch in mm
        bounding_box: (min_x, min_y, max_x, max_y)
        is_dense: Whether this qualifies as a dense package
        rows: Number of rows (for grid packages like BGA)
        cols: Number of columns (for grid packages like BGA)
    """

    ref: str
    package_type: PackageType
    center: tuple[float, float]
    pads: list[Pad]
    pin_count: int
    pin_pitch: float
    bounding_box: tuple[float, float, float, float]
    is_dense: bool
    rows: int = 0
    cols: int = 0


def is_dense_package(
    pads: list[Pad],
    pin_pitch_threshold: float = 0.5,
    pin_count_threshold: int = 48,
    trace_width: float | None = None,
    clearance: float | None = None,
) -> bool:
    """Detect if a set of pads represents a dense package.

    A package is considered dense if:
    - Pin pitch is too small for traces to pass between pins, OR
    - Pin pitch < 0.5mm (when no clearance info provided), OR
    - Pin count > 48
    - Fine-pitch SSOP/TSSOP (0.65mm pitch or less) - always dense
    - TQFP-32-class quad packages: >= 32 pins on a quad arrangement with
      pitch <= 0.8 mm are always dense.  At common board-house defaults
      (trace=0.2 mm, clearance=0.15 mm) the dynamic threshold of
      2*(0.2+0.15) = 0.7 mm is JUST below the 0.8 mm pitch, so without
      this rule TQFP-32 packages are not flagged as dense and the inner
      pins of nets that route to them get blocked by the surrounding
      perimeter routing.  See issue #2513.

    When trace_width and clearance are provided, the threshold is calculated
    dynamically: a package is dense if there's insufficient space between
    adjacent pins to route a trace. This accounts for the fact that packages
    like TQFP-32 with 0.8mm pitch may need escape routing when clearance
    requirements are strict.

    Args:
        pads: List of pads from a single component
        pin_pitch_threshold: Maximum pin pitch to be considered dense (mm).
            This is overridden by dynamic calculation when trace_width and
            clearance are provided.
        pin_count_threshold: Minimum pin count to be considered dense
        trace_width: Trace width in mm. When provided with clearance,
            calculates dynamic threshold.
        clearance: Trace-to-pad clearance in mm. When provided with
            trace_width, calculates dynamic threshold.

    Returns:
        True if the package is dense and needs escape routing
    """
    if len(pads) < 2:
        return False

    # Pin count check
    if len(pads) > pin_count_threshold:
        return True

    # Multi-row through-hole connectors (>= 20 pins) are dense because
    # inner-row pads are blocked by outer-row escape paths
    if len(pads) >= 20 and _is_multi_row(pads):
        return True

    # Issue #2919: USB-C-class fine-pitch SMT connectors with mounting tabs
    # are always dense -- adjacent USB_D+ / USB_D- pads at 0.5mm pitch
    # cannot host a between-pin trace at jlcpcb tier-1 clearance.
    if is_usb_c_class_connector(pads):
        return True

    # Calculate minimum pin pitch
    min_pitch = _calculate_min_pitch(pads)
    if min_pitch <= 0:
        return False

    # Fine-pitch SSOP/TSSOP check (0.75mm or less is always dense)
    # These packages need escape routing regardless of design rules
    if min_pitch <= 0.75 and _is_dual_row(pads):
        return True

    # Issue #3398: SOIC-8-class dual-row SMD band ((0.75, 1.5] mm pitch).
    # UCC27211 / LM393 SOIC-8 at 1.27 mm pitch sit ABOVE the dynamic
    # threshold at common power recipes (2 * (0.30 + 0.20) = 1.0 mm),
    # so the escape pre-pass previously skipped them and the P_FP6 SOP
    # in-pad rescue was unreachable end-to-end (Issue #3395).  The band
    # matches :data:`FINE_PITCH_THRESHOLD_MM` (the same cap the
    # fine-pitch classifier and ``_sop_in_pad_rescue_eligible`` use)
    # and is deliberately narrow:
    #
    # - ``len(pads) >= 8``: SOIC-8 and up; SOT-23-5/6 class parts keep
    #   their pre-#3398 classification (dynamic threshold only).
    # - all-SMD: dual-row connectors with through-hole anchor tabs
    #   (e.g. board 07's J1/J2 1.0 mm receptacles) are NOT SOPs and
    #   must not enter the escape pre-pass -- their TH tabs fail this
    #   guard.  A real SOP/SOIC body is all-SMD.
    # - ``_is_dual_row``: quad/grid parts are handled by their own
    #   rules above.
    #
    # The consumer-aware SOP rescue deferral (Issue #3398, see
    # ``_create_staggered_row_escapes``) ensures entering the dense
    # list does not regress nets whose consumers are local: those pads
    # emit NO escape geometry and route exactly as before.
    if (
        len(pads) >= 8
        and min_pitch <= FINE_PITCH_THRESHOLD_MM + 1e-9
        and not any(getattr(p, "through_hole", False) for p in pads)
        and _is_dual_row(pads)
    ):
        return True

    # TQFP-32-class quad packages (issue #2513).
    # A quad arrangement with >= 32 pins at <= 0.8 mm pitch is dense
    # regardless of trace/clearance.  At common JLCPCB-style defaults
    # (trace=0.2, clearance=0.15) the dynamic threshold below works out
    # to 0.70 mm which is just under the 0.8 mm pitch of a TQFP-32, so
    # the dynamic check would otherwise miss this class of MCU.  This
    # is intentionally conservative: it requires both a quad layout AND
    # >= 32 pins, so leaded SOIC-32 (dual row) and small QFP/QFN parts
    # at 32 pins (e.g. QFN-32 at 0.5mm pitch) are unaffected -- the
    # SOIC case fails the quad arrangement check and the small-pitch
    # case is already covered by the TSSOP/dynamic threshold rules.
    if len(pads) >= 32 and min_pitch <= 0.8 + 1e-3 and _looks_like_quad_layout(pads):
        return True

    # Issue #3343: SOT-23-class small packages (3-7 pads forming exactly
    # two short rows/columns) are NOT dense regardless of the dynamic
    # threshold.  Escape fan-out exists for pin FIELDS where perimeter
    # routing blocks inner pads; a SOT-23-5/6 has at most 3 perimeter
    # pads per side and its perpendicular stub direction is exactly the
    # first move A* makes anyway -- the staggered stub + via geometry
    # only consumes the pocket around the package.  Empirically on
    # softstart rev B (L=4 @ jlcpcb-tier1, 0.30/0.20), classifying the
    # two SOT-23-5s (U3 INA180A3, U8 MCP6001) as dense cost 3 nets of
    # reach (19/26 with stubs vs 22/26 without), and before the column-
    # orientation fix above their broken stubs made two nets statically
    # unroutable.  Plain A* routes these pads natively.
    if 2 < len(pads) < 8 and not any(getattr(p, "through_hole", False) for p in pads):
        xs = sorted(p.x for p in pads)
        ys = sorted(p.y for p in pads)

        def _axis_clusters(vals: list[float], tol: float = 0.2) -> int:
            count = 0
            prev: float | None = None
            for v in vals:
                if prev is None or v - prev > tol:
                    count += 1
                prev = v
            return count

        if _axis_clusters(xs) == 2 or _axis_clusters(ys) == 2:
            return False

    # Dynamic threshold based on design rules
    # A trace needs: trace_width + clearance on each side from adjacent pins
    # So minimum pitch to route between pins is: 2 * (trace_width/2 + clearance) + trace_width
    # Simplified: 2 * trace_width + 2 * clearance = 2 * (trace_width + clearance)
    if trace_width is not None and clearance is not None:
        # Calculate the minimum pitch needed to fit a trace between pins
        # Each pin needs clearance + half the trace width on the routing side
        # So for two adjacent pins: 2 * (clearance + trace_width/2) + trace_width
        # This equals: 2*clearance + 2*trace_width = 2*(clearance + trace_width)
        dynamic_threshold = 2 * (trace_width + clearance)
        if min_pitch < dynamic_threshold:
            return True
    elif min_pitch < pin_pitch_threshold:
        # Fall back to static threshold when no design rules provided
        return True

    return False


def _looks_like_quad_layout(pads: list[Pad]) -> bool:
    """Convenience wrapper around _is_quad_arrangement using the pads' bbox.

    Used by is_dense_package() so the TQFP-32 rule does not need to
    duplicate bbox-and-center math at the call site.

    Args:
        pads: List of pads from a single component

    Returns:
        True if pads form a QFP/QFN-style quad arrangement
    """
    if len(pads) < 8:
        return False
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width <= 0 or height <= 0:
        return False
    center_x = (max(xs) + min(xs)) / 2
    center_y = (max(ys) + min(ys)) / 2
    return _is_quad_arrangement(pads, center_x, center_y, width, height)


def is_fine_pitch_ssop(pads: list[Pad], pitch_threshold: float = FINE_PITCH_THRESHOLD_MM) -> bool:
    """Check if pads represent a fine-pitch dual-row package.

    Fine-pitch dual-row packages (SOIC-8 at 1.27mm; SSOP at 0.65mm; TSSOP
    at 0.5mm) have adjacent pins close enough together that escaping
    inboard pins between two outboard pins is corridor-constrained at
    JLCPCB tier-1 clearance (0.20mm + 0.30mm trace).  They benefit from
    the alternating-layer escape path (``_escape_fine_pitch_dual_row``)
    that this predicate gates.

    Args:
        pads: List of pads from a single component
        pitch_threshold: Maximum pitch to be considered fine-pitch (mm).
            Default :data:`FINE_PITCH_THRESHOLD_MM` (1.5mm) catches
            1.27mm-pitch SOIC (UCC27211, LM393, MCP6001-SOIC, etc.)
            *and* every tighter standard pitch (SSOP at 0.65mm, TSSOP at
            0.5mm).  Issue #3371 (P_FP1): the historical default was
            0.75mm, which silently excluded SOIC and forced 1.27mm-pitch
            traffic through the generic SOP path that does not know
            about corridor infeasibility under tight clearance.

    Returns:
        True if the package is a fine-pitch dual-row package needing
        special routing.

    Note:
        Callers that want the *old* pre-#3371 SSOP/TSSOP-only behavior
        (where SOIC-8 falls through to the generic SOP path) can pass
        ``pitch_threshold=0.75`` explicitly.  This is the right
        behaviour for callers that pair this predicate with a SOP-
        specific path; the default broadens the dual-row class.
    """
    if len(pads) < 4:  # Need at least 4 pads for SSOP
        return False

    # Check for dual-row arrangement (SSOP/TSSOP characteristic)
    if not _is_dual_row(pads):
        return False

    # Check pin pitch
    min_pitch = _calculate_min_pitch(pads)
    return 0 < min_pitch <= pitch_threshold


def is_usb_c_class_connector(pads: list[Pad]) -> bool:
    """Detect a USB-C-class fine-pitch SMT connector with mounting tabs.

    Issue #2919: USB-C receptacles (e.g., GCT_USB4105) have a 2-row SMT signal
    pad cluster at 0.5mm pitch plus 2 through-hole shield/mounting tabs.  The
    mounting tabs introduce a third Y coordinate, so ``_is_dual_row()`` (which
    requires exactly two unique Y values) returns False and the package falls
    through ``detect_package_type`` to ``UNKNOWN``.  The ``UNKNOWN`` dispatcher
    invokes ``_escape_radial`` which cannot resolve the 0.123mm channel between
    adjacent USB_D+/USB_D- pads at jlcpcb tier-1 clearance (0.127mm).

    A package qualifies as USB-C-class when:

    - It has at least 8 SMT (non-through-hole) pads (USB-C has 14-16 SMT
      signals in a fully-populated footprint; conservative lower bound covers
      reduced pinouts).
    - It has at least one through-hole pad (mounting tab).  Pure SMT dual-row
      packages with no PTH tabs are TSSOP/SSOP and already handled by
      ``_is_dual_row``.
    - The SMT pads form a dual-row arrangement (``_is_dual_row`` returns True
      when applied to the SMT subset).
    - The SMT row pitch is fine (<= 0.6mm).  This excludes 2.54mm headers
      (which already route via the multi-row connector or radial paths).

    Args:
        pads: All pads from a single component (SMT + through-hole).

    Returns:
        True when the pads form a USB-C-class fine-pitch 2-row SMT connector.
    """
    if len(pads) < 4:
        return False

    smt_pads = [p for p in pads if not p.through_hole]
    pth_pads = [p for p in pads if p.through_hole]

    # Need both SMT signal pads and through-hole tabs (the latter is what
    # distinguishes USB-C-class from plain TSSOP/SSOP).
    if not pth_pads or len(smt_pads) < 8:
        return False

    # The SMT subset must look like a dual-row package.
    if not _is_dual_row(smt_pads):
        return False

    # Fine pitch check (USB-C is 0.5mm; allow slack up to 0.6mm to cover
    # near-USB-C connectors like some 0.5mm-pitch FFC/FPC headers).
    smt_pitch = _calculate_min_pitch(smt_pads)
    if smt_pitch <= 0 or smt_pitch > 0.6:
        return False

    return True


def _is_column_aligned_connector(smt_pads: list[Pad], tol: float = 0.05) -> bool:
    """Return True when every pad column of a 2-row connector is single-net.

    Issue #3410: a 2-row connector whose SMT tail columns each carry ONE
    net (e.g. board 03's re-spun GCT USB4105 with USB_D+ on A6/B6 both
    at x-0.25 and USB_D- on A7/B7 both at x+0.25) needs no escape
    pre-pass: same-net pads in a column tie together with a vertical
    surface stub (the main router's intra-IC consolidation) and each
    signal exits through its outer-row pad into open board area.
    Legacy tongue-mirrored footprints place DIFFERENT nets in the same
    column (B7 = USB_D- directly under A6 = USB_D+), forcing the
    diagonal X-crossover that the #2919 alternating-layer escape
    exists to untangle -- those return False and keep the legacy
    behaviour.

    Args:
        smt_pads: The connector's SMT signal pads (through-hole shield
            tabs already filtered out by the caller).
        tol: Column-coordinate tolerance in mm.  USB-C pitch is 0.5mm,
            so 0.05mm cleanly separates "same column" from "adjacent
            column" while absorbing float noise.

    Returns:
        True when every column's netted pads (net > 0) share one net and
        at least one column actually pairs two same-net pads (otherwise
        there is nothing the column-tie geometry buys and the legacy
        escape treatment is kept).  Vertical (rotated) connectors are
        handled symmetrically by grouping on y instead when the pads
        span more in y than in x.
    """
    xs = [p.x for p in smt_pads]
    ys = [p.y for p in smt_pads]
    horizontal = (max(xs) - min(xs)) >= (max(ys) - min(ys))

    # Group netted pads into columns along the row axis.  Each column
    # records the set of nets and the pad count it carries.
    columns: dict[int, tuple[set[int], int]] = {}
    for p in smt_pads:
        if not p.net or p.net <= 0:
            continue
        coord = p.x if horizontal else p.y
        key = round(coord / tol) if tol > 0 else 0
        # Absorb boundary rounding: merge into a neighbouring key when
        # one exists within a single tolerance step.
        for k in (key, key - 1, key + 1):
            if k in columns:
                key = k
                break
        nets, count = columns.get(key, (set(), 0))
        nets.add(p.net)
        columns[key] = (nets, count + 1)

    if not columns:
        return False

    # Any column carrying two different nets forces the X-crossover
    # geometry -- keep the legacy alternating-layer escape.
    if any(len(nets) > 1 for nets, _count in columns.values()):
        return False

    # Require at least one true same-net column pair so the defer only
    # fires for re-spun (tie-friendly) connectors.
    return any(count >= 2 for _nets, count in columns.values())


def detect_package_type(pads: list[Pad]) -> PackageType:
    """Detect the package type from pad arrangement.

    Uses pad positions and characteristics to classify the package.

    Args:
        pads: List of pads from a single component

    Returns:
        Detected PackageType
    """
    if len(pads) < 2:
        return PackageType.UNKNOWN

    # Check for through-hole pads
    through_hole_count = sum(1 for p in pads if p.through_hole)
    if through_hole_count > len(pads) * 0.8:
        if len(pads) <= 3:
            return PackageType.SOT
        # Multi-row through-hole connectors (2xN, 3xN, 4xN with >= 20 pins)
        # need BGA-style fanout escape with row-aware layer assignment
        if _is_multi_row(pads) and len(pads) >= 20:
            return PackageType.MULTI_ROW_CONNECTOR
        if _is_dual_row(pads):
            return PackageType.DIP
        return PackageType.THROUGH_HOLE

    # Issue #2919: USB-C-class connectors mix SMT signal rows with through-hole
    # mounting tabs.  The mounting tabs prevent ``_is_dual_row`` from firing on
    # the SMT subset when run over the full pad list, so we test the SMT subset
    # explicitly before falling through to the SMT-only dispatchers below.
    if is_usb_c_class_connector(pads):
        return PackageType.USB_C_CONNECTOR

    # Calculate bounding box and center
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    center_x = (max(xs) + min(xs)) / 2
    center_y = (max(ys) + min(ys)) / 2

    # IMPORTANT: Check detection order matters!
    # 1. Dual-row packages (SOP/SSOP/TSSOP) - only 2 rows of pads
    # 2. Quad packages (QFP/QFN) - pads on 4 edges, empty interior
    # 3. Grid packages (BGA) - filled grid throughout

    # Check for dual-row first (SOP/SSOP/TSSOP) - most specific
    if _is_dual_row(pads):
        # Distinguish between SOP, SSOP, TSSOP based on pin pitch
        min_pitch = _calculate_min_pitch(pads)
        if min_pitch < 0.55:
            # TSSOP: 0.5mm pitch (thin shrink)
            return PackageType.TSSOP
        elif min_pitch < 0.75:
            # SSOP: 0.65mm pitch (shrink)
            return PackageType.SSOP
        else:
            # Standard SOP/SOIC: 1.27mm pitch
            return PackageType.SOP

    # Check for quad arrangement (QFP/QFN/TQFP) before BGA
    # QFP/QFN have pads only on edges, not in interior
    if _is_quad_arrangement(pads, center_x, center_y, width, height):
        # Only classify as quad if there are no interior pads
        if not _has_interior_pads(pads, center_x, center_y, width, height):
            # QFN typically has an exposed thermal pad in center
            has_center_pad = any(
                abs(p.x - center_x) < 1.0 and abs(p.y - center_y) < 1.0 for p in pads
            )
            if has_center_pad and len(pads) <= 64:
                return PackageType.QFN

            # TQFP has finer pitch
            min_pitch = _calculate_min_pitch(pads)
            if min_pitch < 0.5:
                return PackageType.TQFP

            return PackageType.QFP

    # Check for grid pattern (BGA) - must have interior pads
    if _is_grid_pattern(pads, center_x, center_y):
        return PackageType.BGA

    return PackageType.UNKNOWN


def get_package_info(
    pads: list[Pad],
    trace_width: float | None = None,
    clearance: float | None = None,
) -> PackageInfo:
    """Get comprehensive information about a package.

    Args:
        pads: List of pads from a single component
        trace_width: Optional trace width for dynamic dense detection
        clearance: Optional clearance for dynamic dense detection

    Returns:
        PackageInfo with detected characteristics
    """
    if not pads:
        return PackageInfo(
            ref="",
            package_type=PackageType.UNKNOWN,
            center=(0, 0),
            pads=[],
            pin_count=0,
            pin_pitch=0,
            bounding_box=(0, 0, 0, 0),
            is_dense=False,
        )

    ref = pads[0].ref if pads else ""
    package_type = detect_package_type(pads)

    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
    bounding_box = (min(xs), min(ys), max(xs), max(ys))
    pin_pitch = _calculate_min_pitch(pads)

    # Estimate rows/cols for grid and multi-row packages
    rows, cols = 0, 0
    if package_type in (PackageType.BGA, PackageType.MULTI_ROW_CONNECTOR):
        rows, cols = _estimate_grid_dimensions(pads)

    return PackageInfo(
        ref=ref,
        package_type=package_type,
        center=center,
        pads=pads,
        pin_count=len(pads),
        pin_pitch=pin_pitch,
        bounding_box=bounding_box,
        is_dense=is_dense_package(pads, trace_width=trace_width, clearance=clearance),
        rows=rows,
        cols=cols,
    )


def _calculate_min_pitch(pads: list[Pad]) -> float:
    """Calculate minimum pin-to-pin distance."""
    if len(pads) < 2:
        return 0

    min_dist = float("inf")
    for i, p1 in enumerate(pads):
        for p2 in pads[i + 1 :]:
            dist = math.sqrt((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2)
            if dist > 0.01:  # Ignore coincident pads
                min_dist = min(min_dist, dist)

    return min_dist if min_dist != float("inf") else 0


def _is_dual_row(pads: list[Pad]) -> bool:
    """Check if pads form a dual-row arrangement."""
    if len(pads) < 4:
        return False

    ys = sorted({round(p.y, 2) for p in pads})
    xs = sorted({round(p.x, 2) for p in pads})

    # Dual row: 2 distinct Y values, many X values
    if len(ys) == 2 and len(xs) >= len(pads) // 2 - 1:
        return True

    # Or 2 distinct X values, many Y values
    if len(xs) == 2 and len(ys) >= len(pads) // 2 - 1:
        return True

    return False


def _is_multi_row(pads: list[Pad]) -> bool:
    """Check if pads form a multi-row arrangement (2, 3, or 4+ rows).

    Multi-row connectors have a small number of rows (2-6) and many columns.
    This is more general than ``_is_dual_row`` which only detects exactly
    2 rows.  A 2-row connector passes both checks, but a 3xN or 4xN header
    only passes this one.

    The heuristic: count unique coordinate values along each axis.  If one
    axis has 2-6 unique values and the other has at least as many unique
    values as the smaller axis count, it is a multi-row arrangement.

    Args:
        pads: List of pads from a single component

    Returns:
        True if the pads form a multi-row arrangement
    """
    if len(pads) < 4:
        return False

    ys = sorted({round(p.y, 2) for p in pads})
    xs = sorted({round(p.x, 2) for p in pads})

    # Check if rows are along Y axis (few Y values, many X values)
    if 2 <= len(ys) <= 6 and len(xs) >= len(ys):
        # Verify roughly equal pad counts per row
        row_counts = []
        for y_val in ys:
            count = sum(1 for p in pads if round(p.y, 2) == y_val)
            row_counts.append(count)
        # Rows should have similar pad counts (within 2x)
        if min(row_counts) > 0 and max(row_counts) / min(row_counts) <= 2.0:
            return True

    # Check if rows are along X axis (few X values, many Y values)
    if 2 <= len(xs) <= 6 and len(ys) >= len(xs):
        row_counts = []
        for x_val in xs:
            count = sum(1 for p in pads if round(p.x, 2) == x_val)
            row_counts.append(count)
        if min(row_counts) > 0 and max(row_counts) / min(row_counts) <= 2.0:
            return True

    return False


def _has_interior_pads(
    pads: list[Pad],
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> bool:
    """Check if pads exist in the interior (not just on edges).

    Used to distinguish BGA (interior pads) from QFP/QFN (edge-only pads).
    """
    if width < 0.1 or height < 0.1:
        return False

    # Define interior as 30% from each edge
    min_x = center_x - width / 2
    max_x = center_x + width / 2
    min_y = center_y - height / 2
    max_y = center_y + height / 2

    interior_margin_x = width * 0.25
    interior_margin_y = height * 0.25

    interior_pads = [
        p
        for p in pads
        if (min_x + interior_margin_x < p.x < max_x - interior_margin_x)
        and (min_y + interior_margin_y < p.y < max_y - interior_margin_y)
    ]

    # Consider interior if there are non-trivial interior pads
    # Allow 1 pad in center for QFN thermal pad
    return len(interior_pads) > 1


def _is_grid_pattern(pads: list[Pad], center_x: float, center_y: float) -> bool:
    """Check if pads form a grid pattern (BGA).

    BGA packages have pads distributed throughout the interior,
    not just on edges. This distinguishes them from QFP/QFN.

    Grid (BGA) detection requires:
    - At least 16 pads (room for at least a 4x4 grid)
    - At least 3 substantial rows AND 3 substantial cols (BGA is at least
      a 3x3 grid; this guards against 2-row connectors with mounting tabs
      that produce a tiny "third row" being misclassified as BGA -- see
      issue #2513 for USB-C with 2 SMT rows + 2 mounting tabs being
      reported as BGA-18 with 3 unique Y values).
    - Significant interior pads (not just edge pads, distinguishing BGA
      from QFP/QFN)
    - Roughly balanced quadrant distribution
    """
    if len(pads) < 16:  # Need at least 4x4 for BGA
        return False

    # Calculate bounding box
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x
    height = max_y - min_y

    if width < 0.1 or height < 0.1:
        return False

    # Issue #2513: A real BGA grid has many rows AND many cols, with each
    # row and col having a substantial number of pads.  A 2-row connector
    # (USB-C, etc.) with mounting tabs may produce a third "row" with just
    # 2 pads in it.  Filter outlier rows/cols (those whose pad count is
    # less than half the median) before counting -- only substantive rows
    # and cols qualify as BGA "axes".
    substantive_rows = _count_substantive_axis_groups(pads, axis="y")
    substantive_cols = _count_substantive_axis_groups(pads, axis="x")
    if substantive_rows < 3 or substantive_cols < 3:
        return False

    # For BGA, check that there are pads in the interior (not just on edges)
    # Define interior as 20% from each edge (relaxed to catch typical BGA grids)
    interior_margin_x = width * 0.2
    interior_margin_y = height * 0.2

    interior_pads = [
        p
        for p in pads
        if (min_x + interior_margin_x < p.x < max_x - interior_margin_x)
        and (min_y + interior_margin_y < p.y < max_y - interior_margin_y)
    ]

    # BGA should have significant interior pads (at least 10% of total)
    # For an 8x8 grid this means ~6+ interior pads
    if len(interior_pads) < len(pads) * 0.1:
        return False

    # Count pads in each quadrant relative to center.
    #
    # Issue #3419: odd-dimension grids (7x7 BGA-49, etc.) have a full row
    # AND a full column lying exactly ON the center axes.  The previous
    # strict ``if/elif/else`` cascade lumped every axis pad into the last
    # quadrant, producing a 9/9/9/22 distribution for a perfectly
    # symmetric 7x7 grid and failing the balance check below (avg=12.25,
    # upper bound 20.8 < 22).  Distribute axis pads fractionally across
    # the adjacent quadrants instead:
    #   - pad on a single axis: 0.5 to each of the two adjacent quadrants
    #   - pad at the exact center (both axes): 0.25 to all four
    #   - strictly in-quadrant pads: 1.0 as before
    # Even grids have no axis pads, so their counts are unchanged
    # (all-integer weights, identical to the pre-fix behaviour).
    # Use a small epsilon so float jitter in pad coordinates does not
    # spuriously classify near-axis pads as off-axis.
    eps = 1e-6
    quadrants = [0.0, 0.0, 0.0, 0.0]
    for p in pads:
        on_x_axis = abs(p.y - center_y) <= eps  # lies on horizontal axis
        on_y_axis = abs(p.x - center_x) <= eps  # lies on vertical axis
        if on_x_axis and on_y_axis:
            # Exact center pad: shared equally by all four quadrants.
            for i in range(4):
                quadrants[i] += 0.25
        elif on_y_axis:
            # On the vertical axis: split between the upper pair (q0/q1)
            # or the lower pair (q2/q3) depending on the y side.
            if p.y > center_y:
                quadrants[0] += 0.5
                quadrants[1] += 0.5
            else:
                quadrants[2] += 0.5
                quadrants[3] += 0.5
        elif on_x_axis:
            # On the horizontal axis: split between the right pair
            # (q0/q3) or the left pair (q1/q2) depending on the x side.
            if p.x > center_x:
                quadrants[0] += 0.5
                quadrants[3] += 0.5
            else:
                quadrants[1] += 0.5
                quadrants[2] += 0.5
        elif p.x > center_x and p.y > center_y:
            quadrants[0] += 1
        elif p.x < center_x and p.y > center_y:
            quadrants[1] += 1
        elif p.x < center_x and p.y < center_y:
            quadrants[2] += 1
        else:
            quadrants[3] += 1

    # BGA should have roughly equal distribution across quadrants
    avg = len(pads) / 4
    return all(0.3 * avg <= q <= 1.7 * avg for q in quadrants if avg > 0)


def _count_substantive_axis_groups(pads: list[Pad], axis: str) -> int:
    """Count rows or columns that hold a substantial fraction of total pads.

    Used by _is_grid_pattern (and other classifiers) to ignore outlier
    "rows" or "cols" that are really just a few off-axis pads -- e.g.
    USB-C mounting tabs or alignment posts that share neither a row nor
    a column with the main signal grid.

    A group is "substantive" if its pad count is at least 50% of the
    median group count along that axis.  Singletons and tiny groups are
    therefore filtered out.

    Args:
        pads: List of pads from a single component
        axis: Which axis to group by - "x" counts unique X (i.e. column
            count), "y" counts unique Y (row count).

    Returns:
        Number of substantive groups along that axis.  Returns 0 for
        empty input.
    """
    if not pads:
        return 0
    if axis == "y":
        coords = [round(p.y, 2) for p in pads]
    else:
        coords = [round(p.x, 2) for p in pads]
    counts: dict[float, int] = {}
    for c in coords:
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return 0
    sorted_counts = sorted(counts.values())
    n = len(sorted_counts)
    median = (
        sorted_counts[n // 2]
        if n % 2 == 1
        else (sorted_counts[n // 2 - 1] + sorted_counts[n // 2]) / 2
    )
    threshold = max(1.0, median * 0.5)
    return sum(1 for v in counts.values() if v >= threshold)


def _is_quad_arrangement(
    pads: list[Pad],
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> bool:
    """Check if pads form a quad arrangement (QFP/QFN)."""
    if len(pads) < 8:
        return False

    # Count pads on each edge (within margin of edge)
    margin = min(width, height) * 0.15
    edges = [0, 0, 0, 0]  # N, S, E, W

    min_x = center_x - width / 2
    max_x = center_x + width / 2
    min_y = center_y - height / 2
    max_y = center_y + height / 2

    for p in pads:
        if abs(p.y - max_y) < margin:
            edges[0] += 1  # North
        elif abs(p.y - min_y) < margin:
            edges[1] += 1  # South
        if abs(p.x - max_x) < margin:
            edges[2] += 1  # East
        elif abs(p.x - min_x) < margin:
            edges[3] += 1  # West

    # QFP/QFN should have pins on all 4 edges
    return all(e >= 2 for e in edges)


def _estimate_grid_dimensions(pads: list[Pad]) -> tuple[int, int]:
    """Estimate rows and columns for a grid package."""
    if len(pads) < 4:
        return (0, 0)

    # Count unique positions
    unique_x = len({round(p.x, 2) for p in pads})
    unique_y = len({round(p.y, 2) for p in pads})

    return (unique_y, unique_x)


class EscapeRouter:
    """Router for generating escape routes from dense packages.

    This class analyzes package pin arrangements and generates
    escape routing patterns that allow all pins to route outward
    without blocking each other.

    Example::

        router = EscapeRouter(grid, rules)
        package_info = router.analyze_package(component_pads)

        if package_info.is_dense:
            escapes = router.generate_escapes(package_info)
            for escape in escapes:
                router.apply_escape(escape)
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        via_spacing: float | None = None,
        escape_clearance: float | None = None,
        net_class_map: dict[str, NetClassRouting] | None = None,
        edge_clearance: float | None = None,
        board_bounds: tuple[float, float, float, float] | None = None,
        manufacturer: str | None = None,
        diff_pair_map: dict[str, str] | None = None,
        net_pad_positions: dict[str, list[tuple[float, float]]] | None = None,
        net_target_positions: dict[int, list[tuple[float, float, str]]] | None = None,
        enable_cross_package_pair_corridor: bool = False,
        net_name_to_id: dict[str, int] | None = None,
        enable_slack_corridor_widening: bool = False,
    ):
        """Initialize the escape router.

        Args:
            grid: Routing grid to work with
            rules: Design rules for dimensions
            via_spacing: Minimum via-to-via spacing (defaults to via_diameter + clearance)
            escape_clearance: Clearance from package edge (defaults to trace_clearance * 2)
            net_class_map: Optional net class map for per-net trace widths
            edge_clearance: Copper-to-board-edge clearance in mm. When set along
                with board_bounds, escape points and via positions are clamped so
                they do not violate the edge clearance zone.
            board_bounds: Board outline bounding box (min_x, min_y, max_x, max_y)
                in mm. Required together with edge_clearance for clamping.
            manufacturer: Manufacturer identifier (e.g. ``"jlcpcb"``,
                ``"jlcpcb-tier1"``).  When provided, capability flags such as
                ``via_in_pad_supported`` are looked up via
                ``mfr_limits.get_mfr_limits()`` and used to enable in-pad
                escape on fine-pitch SSOP/TSSOP packages (Issue #2605).
                Falls back to ``rules.manufacturer`` when not supplied.
            diff_pair_map: Optional bidirectional net-name to partner-net-name
                map for differential pairs (Issue #2639 / Epic #2556 Phase 2F).
                When provided and BOTH halves of a pair land on the same
                package, the escape router emits paired escape segments that
                leave the package already at the target intra-pair spacing.
                Pads whose partner is on a different package fall through to
                the standard per-package escape pattern.  Defaults to ``None``
                which preserves pre-#2639 single-ended behaviour exactly.
            net_pad_positions: Optional board-wide map of net name to ALL pad
                positions on that net (Issue #3419).  When provided, the
                paired-escape pre-pass (``_escape_diff_pair_segment``) uses
                the off-package endpoints of the pair's nets to pick a
                launch direction TOWARD the partner connector instead of
                blindly outward from the package center.  This keeps the
                tightly-coupled escape endpoints facing the destination so
                the main per-net A* does not have to fight its way around
                the package from a tight launch.  Defaults to ``None``
                which preserves the center-outward quadrant heuristic.
            net_target_positions: Optional board-wide net-to-pad-position map
                (Issue #3428): ``{net_id: [(x, y, ref), ...]}`` covering
                every pad on every net (plane nets / net 0 may be
                omitted).  When supplied, the fine-pitch QFP in-pad
                rescue (``_escape_qfp_alternating`` ->
                ``_try_in_pad_escape``) points the inner-layer escape
                stub toward the net's nearest OFF-package pad instead of
                the parity-derived along-edge direction, so the stub no
                longer blocks the adjacent pin's own via slot (board 04
                LQFP-48 OSC_OUT stub vs NRST, Issue #3411).  Defaults to
                ``None`` which preserves legacy parity-based stub
                directions exactly.  Distinct from ``net_pad_positions``
                (Issue #3419), which is keyed by net NAME and consumed by
                the diff-pair launch heuristic.
            enable_cross_package_pair_corridor: Issue #4086 (Phase 1,
                epic #4049).  Default ``False``.  When ``True`` and a
                diff pair's two halves live on DIFFERENT packages,
                ``_generate_paired_escapes`` resolves the off-package
                partner endpoint via ``net_pad_positions`` and reserves a
                SOFT (attractor-only) corridor from this leg's escape
                launch point toward the partner, so the downstream coupled
                pathfinder (``CoupledPathfinder`` / #4080 attractor) has
                real geometry to follow even when no single-ended guide
                route exists yet.  The two legs still escape independently
                at the per-pad level (this is NOT joint pin-assignment
                ILP).  With the flag ``False`` cross-package pairs fall
                through to the single-ended dispatcher exactly as before
                (byte-identical), so the intra-package Phase 2F behaviour
                and all board 00-07 fixtures are unchanged.
            net_name_to_id: Optional board-wide map of net name to net id
                (Issue #4086).  Consulted ONLY by the cross-package
                corridor path to resolve the off-package partner's net id
                for the corridor owner set (so both legs of the pair see
                the soft attractor bonus).  Defaults to ``None``; when
                absent the cross-package corridor owner set falls back to
                this leg's net id alone.
            enable_slack_corridor_widening: Issue #4085 (Phase 1,
                epic #4049 Gap 2).  Default ``False``.  When ``True``,
                ``_reserve_pair_continuation_corridor`` estimates the
                pair's expected length skew from pin geometry (via
                :func:`slack_budget.estimate_pair_skew_budget`) and, when
                that estimate exceeds the pair's net-class skew tolerance,
                WIDENS the reserved corridor's lateral half-width by the
                estimated slack budget.  This reserves room for the
                downstream serpentine tuner to meander the shorter half
                into already-protected cells rather than scavenging
                leftover space.  With the flag ``False`` the corridor
                width is byte-identical to today (the fixed
                ``intra_pair_clearance + trace_width`` padding), so all
                board 00-07 fixtures are unchanged.
        """
        self.grid = grid
        self.rules = rules
        self.via_spacing = via_spacing or (rules.via_diameter + rules.via_clearance)
        self.escape_clearance = escape_clearance or (rules.trace_clearance * 2)
        self.net_class_map = net_class_map or {}
        self.edge_clearance = edge_clearance
        self.board_bounds = board_bounds
        # Issue #2639 / Epic #2556 Phase 2F: diff-pair-aware escape coupling.
        # The map is consulted by ``generate_escapes`` to find pads that
        # belong to a detected differential pair AND whose partner pad lives
        # on the same package.  Such pads are routed via
        # ``_escape_diff_pair_segment`` instead of the per-package
        # dispatcher.  An empty / None map disables the feature.
        self.diff_pair_map: dict[str, str] = diff_pair_map or {}
        # Issue #3419: board-wide net -> pad-position map for partner-
        # connector-aware paired-escape launch direction.  Empty map
        # disables the heuristic (falls back to center-outward quadrant
        # direction).
        self.net_pad_positions: dict[str, list[tuple[float, float]]] = net_pad_positions or {}
        # Issue #3428: board-wide net-id -> [(x, y, ref), ...] pad-position
        # map used by ``_compute_target_direction`` to make the QFP in-pad
        # rescue's inner stub target-aware.  Empty map disables the
        # feature (every call site falls back to the legacy
        # parity-derived direction).
        self.net_target_positions: dict[int, list[tuple[float, float, str]]] = (
            net_target_positions or {}
        )
        # Instrumentation counter (Gate 3/4 of the #2587-style verification
        # chain): bumped every time ``_escape_diff_pair_segment`` is
        # invoked.  Tests assert this is non-zero on board 03 and zero
        # when no diff_pair_map is supplied.  This is intentionally a
        # public attribute so test code does not need to monkey-patch
        # internals to observe the call path.
        self.diff_pair_segment_calls: int = 0
        # Issue #2677: Instrumentation counter for paired continuation
        # corridor reservations.  Bumped once per
        # ``_reserve_pair_continuation_corridor`` call so tests can assert
        # the corridor reservation happened BEFORE partner-via marking.
        # The companion attribute ``pair_corridor_reserved_cells`` records
        # the total number of grid cells reserved across all calls.
        self.pair_corridor_reservations: int = 0
        self.pair_corridor_reserved_cells: int = 0
        # Issue #4086 (Phase 1): cross-package diff-pair corridor gate +
        # instrumentation.  When enabled, a diff pair whose two halves are
        # on different packages gets a SOFT continuation corridor from each
        # leg's escape launch point toward the off-package partner.  The
        # counters mirror ``pair_corridor_reservations`` /
        # ``pair_corridor_reserved_cells`` so tests can assert the
        # cross-package path fired (or did not, when the flag is off / no
        # partner is resolvable) without monkey-patching internals.
        self.enable_cross_package_pair_corridor: bool = bool(enable_cross_package_pair_corridor)
        self.cross_package_pair_corridor_reservations: int = 0
        self.cross_package_pair_corridor_reserved_cells: int = 0
        # Issue #4085 (Phase 1): slack-corridor widening gate +
        # instrumentation.  When enabled, ``_reserve_pair_continuation_corridor``
        # widens its lateral reservation by a pin-geometry-estimated slack
        # budget for pairs whose estimated skew exceeds their net class's
        # ``effective_skew_tolerance``.  ``pair_corridor_slack_widened``
        # counts how many reservations were actually widened;
        # ``pair_corridor_slack_budget_mm`` accumulates the total slack
        # (mm) applied so tests can assert the widening fired without
        # monkey-patching internals.
        self.enable_slack_corridor_widening: bool = bool(enable_slack_corridor_widening)
        self.pair_corridor_slack_widened: int = 0
        self.pair_corridor_slack_budget_mm: float = 0.0
        # Issue #4086: board-wide net-name -> net-id map used ONLY to
        # resolve the off-package partner's net id for the cross-package
        # corridor owner set.  Empty map => owner set is this leg alone.
        self.net_name_to_id: dict[str, int] = net_name_to_id or {}
        # Issue #2983: Instrumentation counters for single-ended byte-lane
        # inner-corner corridor reservations.  Mirrors the diff-pair
        # ``pair_corridor_*`` pattern but tracks calls into
        # ``_reserve_inner_corner_lane_corridor`` which generalises the
        # mechanism to mirrored byte-lane (e.g. board 07 DDR data) pads
        # at sorted positions 1 and N-2 of a co-located row.  Tests in
        # ``tests/router/test_byte_lane_corridor_reservation.py`` assert
        # both the call count (one per inner-corner net) and the cell
        # count (non-zero on a 4-layer board with an inner signal layer).
        self.byte_lane_corridor_reservations: int = 0
        self.byte_lane_corridor_reserved_cells: int = 0

        # Issue #4256 (A3): instrumentation for the discrete BundlePlan
        # allocator's per-member HARD lanes.  These are the generalisation of
        # the #2983 single-ended inner-corner corridor to one HARD
        # (foreign-net keep-out, C++-mirrored) lane per ``CoupledGroup``
        # member.  Tracked separately from ``byte_lane_corridor_*`` (which
        # counts the SOFT, Python-only single-ended default) so the flag-off
        # identity tests stay byte-identical while the new HARD path has its
        # own asserts.
        self.bundle_plan_corridor_reservations: int = 0
        self.bundle_plan_corridor_reserved_cells: int = 0

        # Issue #3900: set by ``_escape_sop_staggered`` when it takes the
        # rescue-only-band path with the rescue cap disabled
        # (``_sop_rescue_row_cap() == 0``).  ``generate_escapes`` reads it to
        # report an empty escape list as "not attempted" (INFO) rather than
        # "failed clearance validation" (WARNING).  Reset at the start of each
        # ``generate_escapes`` call.
        self._escape_not_attempted_rescue_band: bool = False

        # Issue #2605: Resolve manufacturer capability flags.  Caller-supplied
        # arg wins; otherwise fall back to ``rules.manufacturer``.  If the
        # manufacturer is unknown we silently treat it as "no via-in-pad"
        # rather than raising -- the router should never crash because of an
        # unrecognized manufacturer string.
        self.manufacturer: str | None = manufacturer or getattr(rules, "manufacturer", None)
        self._mfr_limits = None
        if self.manufacturer is not None:
            try:
                from .mfr_limits import get_mfr_limits

                self._mfr_limits = get_mfr_limits(self.manufacturer)
            except (ValueError, ImportError):
                self._mfr_limits = None
        self.via_in_pad_supported: bool = bool(
            self._mfr_limits is not None and self._mfr_limits.via_in_pad_supported
        )

        # Issue #3033 / #3062: When True, the in-pad rescue path
        # (``_try_in_pad_escape``) returns None instead of placing a
        # via that would clip a neighbouring foreign-net pad (the
        # "proceed anyway, defer DRC to the user" branch from PR #2945).
        # Defaults to False so legacy callers preserve the historical
        # behaviour exactly; opt-in callers (e.g. the QFP-alternating
        # dispatcher) flip this to True when they prefer surfacing the
        # deferral over committing a DRC violation that cascades into
        # adjacent-pin routing failures (board-04 OSC_OUT clipping
        # NRST/U2.8 was the original trigger).
        #
        # CLI knob: the route command's ``--strict-in-pad-clearance``
        # flag sets ``KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1`` in the
        # subprocess env before invoking the router; reading it here
        # threads the user opt-in through to the lazily-constructed
        # EscapeRouter without touching every call site between
        # ``route_cmd`` and ``Autorouter._escape``.
        import os as _os

        self.strict_in_pad_clearance: bool = (
            _os.environ.get("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", "0") == "1"
        )

        # Issue #3118: micro-via in-pad rescue.  When enabled, the
        # ``_try_in_pad_escape`` helper retries with micro-via dimensions
        # (default 0.3 mm OD / 0.15 mm drill) if the standard via clips a
        # neighbouring foreign-net pad on a fine-pitch QFP/SSOP.  This
        # addresses the board-04 OSC_OUT cluster (LQFP-48 0.5 mm pitch
        # where the manufacturer's 0.6 mm minimum via cannot fit between
        # adjacent foreign-net pads).  The micro-via emitted on this
        # path is tagged ``is_micro=True`` so the dimensions DRC
        # rule exempts it from the standard min-via floors (and the
        # via serialises as ``(via micro ...)`` round-trip per
        # #3124/#3126).  The flag
        # is independent of strict mode: it works on the default
        # "proceed anyway" branch too.  CLI knob: route command's
        # ``--micro-via-in-pad-fallback`` flag stamps
        # ``KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK=1`` in the subprocess
        # env so the lazily-constructed EscapeRouter inherits the
        # opt-in.  The companion dimension flags
        # (``KICAD_TOOLS_MICRO_VIA_SIZE`` /
        # ``KICAD_TOOLS_MICRO_VIA_DRILL``) tune the retry dimensions;
        # defaults match the stitch --micro-via 0.3 / 0.15 values.
        self.micro_via_in_pad_fallback: bool = (
            _os.environ.get("KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK", "0") == "1"
        )
        try:
            self.micro_via_diameter: float = float(
                _os.environ.get("KICAD_TOOLS_MICRO_VIA_SIZE", "0.3")
            )
        except (TypeError, ValueError):
            self.micro_via_diameter = 0.3
        try:
            self.micro_via_drill: float = float(
                _os.environ.get("KICAD_TOOLS_MICRO_VIA_DRILL", "0.15")
            )
        except (TypeError, ValueError):
            self.micro_via_drill = 0.15

        # Issue #3183: extended-pitch in-pad fallback.  When enabled, the
        # in-pad fallback gate (originally ``pin_pitch <= 0.55``) is raised
        # to ``pin_pitch <= 0.8`` so 0.65-0.8 mm-pitch QFP/TQFP packages
        # (e.g. board-03 U1 TQFP-32 at 0.8mm pitch) can route their inner
        # signal pins via in-pad vias when the surface escape would clip
        # an adjacent foreign-net pad's clearance.  This is opt-in to keep
        # behaviour bit-identical for any board that does not request it
        # -- the broader-blast-radius "raise the gate globally" alternative
        # would have required cross-board validation on every 0.55-0.8 mm
        # package in the repo.  Capability gating is unchanged: the
        # fallback still requires ``self.via_in_pad_supported`` (i.e. a
        # via-in-pad-capable manufacturer profile), so tier-0 jlcpcb
        # remains unaffected even when this flag is set.  CLI/board knob:
        # set ``KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK=1`` in the
        # process env before invoking the router.  Default off preserves
        # the pre-#3183 ``pin_pitch <= 0.55`` gate exactly.
        self.extended_pitch_in_pad_fallback: bool = (
            _os.environ.get("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "0") == "1"
        )

        # Issue #2881: Counter for "would-have-rescued" events -- bumped
        # every time the escape router would have invoked
        # ``_try_in_pad_escape`` for a fine-pitch QFP/SSOP pin but the
        # current manufacturer's ``via_in_pad_supported`` is False.  When
        # this counter is non-zero after a routing attempt, the
        # ``--auto-mfr-tier`` escalation loop knows that switching to a
        # via-in-pad-capable manufacturer would unblock those pins, and
        # the diagnostic surface can name the constraint that is blocking
        # progress.  Tracked per EscapeRouter instance and reset between
        # routing attempts by ``Autorouter.reset_attempt_state``.
        self.missed_via_in_pad_rescues: int = 0
        # Per-component refs whose pins would have been rescued -- used
        # for the named-constraint diagnostic line.
        self.missed_via_in_pad_components: set[str] = set()

        # Issue #3429: Counter for adjacent-pin via-in-pad CONFLICTS that
        # the dispatcher refused before committing a second in-pad via.
        # Two adjacent fine-pitch QFP pins can each be flagged for an
        # in-pad rescue, but at sub-pitch spacing their two via barrels +
        # annular rings cannot both fit (center spacing < via_r_A +
        # via_r_B + clearance).  ``_try_in_pad_escape`` validates the
        # candidate via against PADS only -- sibling escapes' vias from
        # the same pass are invisible there -- so without this guard the
        # losing escape was silently dropped at the ``apply_escape_routes``
        # commit-time cross-validation with no retry.  When the guard
        # fires, the second in-pad rescue is refused (returns None) so the
        # dispatcher falls through to the lateral / surface escape path
        # instead of emitting a pair that the commit step later drops.
        # Mirrors the ``missed_via_in_pad_rescues`` instrumentation
        # pattern; consumed by diagnostics / future tier-escalation.
        self.adjacent_in_pad_via_conflicts_refused: int = 0

        # Issue #3430: Counter for the auto-lateral-via FALLBACK that the
        # dispatcher takes after #3429 refuses an in-pad rescue because a
        # sibling fine-pitch pin's via barrel blocks it.  #3429 is the
        # DETECTION half (``_try_in_pad_escape`` returns None on a sub-pitch
        # via-via conflict); this is the RECOVERY half -- the dispatcher
        # falls through to ``_try_lateral_via_escape``, which pushes the
        # via OFF the pad along the outward escape direction so the second
        # pin still escapes instead of being silently dropped at
        # ``apply_escape_routes`` commit time.  Bumped once per pin whose
        # in-pad rescue was refused AND whose lateral fallback succeeded.
        # When this counter is non-zero a fine-pitch package that would
        # otherwise have lost a pin to the commit-time drop was rescued by
        # the offset escape.  Mirrors the instrumentation pattern above;
        # reset between attempts by ``Autorouter.reset_attempt_state``.
        self.forced_lateral_via_fallbacks: int = 0

        # Issue #3257: per-pad escape-layer overrides for SSOP/TSSOP
        # fine-pitch dual-row dispatcher.  Maps ``(ref, pin)`` to the
        # forced escape layer (``Layer.F_CU`` -> stay on surface,
        # ``Layer.B_CU`` / inner-signal -> via to inner).  Consulted by
        # ``_create_fine_pitch_row_escapes`` BEFORE the default
        # alternating-parity ``needs_via`` calculation, allowing surgical
        # per-pin overrides on packages where the alternation pattern
        # would otherwise place adjacent foreign-net traces on the same
        # post-escape layer (e.g. softstart U1 east column where pin 17
        # SWDIO odd-via to B.Cu and pin 15 STATUS_LED odd-via to B.Cu
        # produce overlapping B.Cu routes near the U1 east-side cluster
        # -- the kind of fine-pitch escape regression now guarded by the
        # rev B harness ``test_softstart_revb_fine_pitch_escape``).
        #
        # Env-var encoding (preferred for both CLI and Python callers):
        #
        #     export KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES='{"U1.15":"F.Cu"}'
        #
        # The JSON dict keys are ``"REF.PIN"`` strings (str-cast pin id);
        # values are KiCad layer names ("F.Cu", "B.Cu", "In1.Cu", ...).
        # Invalid JSON or unknown layer names are logged and treated as
        # an empty override map so the routing call never crashes on a
        # malformed env var.
        #
        # Direct dict constructor argument is the simpler integration
        # point for Python callers (e.g. board-specific ``route_pcb``
        # functions); the env-var path exists for the CLI ``kct route``
        # subprocess flow.  Both feed the same backing dict.  This
        # mirrors the existing pattern used by
        # ``strict_in_pad_clearance`` / ``micro_via_in_pad_fallback`` /
        # ``extended_pitch_in_pad_fallback`` (env-var-driven opt-in
        # flags) for stylistic consistency.
        self.escape_pad_layer_overrides: dict[tuple[str, str], Layer] = {}
        _override_json = _os.environ.get("KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES")
        if _override_json:
            try:
                import json as _json

                _raw = _json.loads(_override_json)
                if isinstance(_raw, dict):
                    for key, value in _raw.items():
                        if not isinstance(key, str) or "." not in key:
                            logger.warning(
                                "Skipping malformed escape_pad_layer_overrides "
                                "key %r (expected 'REF.PIN').",
                                key,
                            )
                            continue
                        ref, pin = key.split(".", 1)
                        try:
                            layer = Layer.from_kicad_name(value)
                        except (ValueError, KeyError):
                            logger.warning(
                                "Skipping escape_pad_layer_overrides[%s] -- "
                                "unknown layer %r (expected 'F.Cu', 'B.Cu', "
                                "'In1.Cu', ...).",
                                key,
                                value,
                            )
                            continue
                        self.escape_pad_layer_overrides[(ref, pin)] = layer
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Failed to parse KICAD_TOOLS_ESCAPE_PAD_LAYER_OVERRIDES "
                    "as JSON (%s); treating as empty.",
                    exc,
                )

    def _get_trace_width_for_net(self, net_name: str) -> float:
        """Get the trace width for a net based on its net class.

        Args:
            net_name: Name of the net

        Returns:
            Trace width in mm
        """
        if self.net_class_map and net_name in self.net_class_map:
            return self.net_class_map[net_name].trace_width
        return self.rules.trace_width

    def _clamp_to_edge_clearance(self, x: float, y: float) -> tuple[float, float]:
        """Clamp a point so it respects the board edge clearance zone.

        When both edge_clearance and board_bounds are set, ensures that the
        point stays at least edge_clearance mm inside the board outline.
        Returns the point unchanged when edge clearance is not configured.

        Args:
            x: X coordinate in mm
            y: Y coordinate in mm

        Returns:
            Clamped (x, y) tuple
        """
        if self.edge_clearance is None or self.board_bounds is None:
            return (x, y)

        min_x, min_y, max_x, max_y = self.board_bounds
        ec = self.edge_clearance
        clamped_x = max(min_x + ec, min(x, max_x - ec))
        clamped_y = max(min_y + ec, min(y, max_y - ec))
        return (clamped_x, clamped_y)

    def _escape_clearance_for_ref(self, ref: str, pads: list[Pad]) -> float:
        """Return the per-component escape clearance for the staggered SOP path.

        Issue #3371 / P_FP4 -- looks up the fine-pitch escape region (if
        any) installed on the grid that covers ``ref`` and returns its
        :attr:`FinePitchRegion.escape_clearance`.  This is the manufacturer-
        aware safe default computed at detection time (e.g. 0.140mm at
        JLCPCB tier-1).  Falls back to the standard
        :meth:`DesignRules.get_clearance_for_component` value when:

        - No fine-pitch regions are installed on the grid (back-compat).
        - No installed region matches ``ref`` or the component's pads (the
          component is outside any escape region).
        - The narrow-channel guard declines the shrink for this geometry
          (corridor infeasible at the candidate clearance).

        Args:
            ref: Component reference.
            pads: The component's pads (used for region applicability via
                ``FinePitchRegion.applies_to_pad`` -- the identity match
                covers wide-package outermost pins that sit at the halo
                boundary).

        Returns:
            Escape clearance in mm for this component.
        """
        # Standard fall-through value -- preserves pre-P_FP4 behaviour
        # for callers without an installed region.
        fallback = self.rules.get_clearance_for_component(
            ref,
            pin_pitch=None,
        )

        # Look up regions installed on the grid.  ``getattr`` defends
        # against grids that pre-date the P_FP3 ``set_fine_pitch_regions``
        # API (e.g. test fixtures that build their own RoutingGrid).
        get_regions = getattr(self.grid, "get_fine_pitch_regions", None)
        if get_regions is None:
            return fallback
        regions = get_regions()
        if not regions:
            return fallback

        for region in regions:
            # Region applies when the component ref matches the region's
            # package_ref OR any of the component's pads sit inside the
            # halo (identity match + geometric containment).
            if region.package_ref == ref:
                # Narrow-channel guard -- decline the shrink when the
                # candidate clearance would produce an infeasible
                # corridor at the active recipe.  Mirrors the guard at
                # ``resolve_clearance_with_escape_region`` and
                # ``_clearance_for_pin_pitch``.
                candidate = region.escape_clearance
                pitch = region.pin_pitch
                effective_channel = pitch - 2.0 * candidate - self.rules.trace_width
                required_channel = 2.0 * candidate + self.rules.trace_width
                if effective_channel < required_channel:
                    return fallback
                return candidate

        # No matching region -- fall through.
        return fallback

    def analyze_package(self, pads: list[Pad]) -> PackageInfo:
        """Analyze a package to determine escape routing needs.

        Args:
            pads: All pads from a single component

        Returns:
            PackageInfo with detected characteristics
        """
        return get_package_info(
            pads,
            trace_width=self.rules.trace_width,
            clearance=self.rules.trace_clearance,
        )

    def generate_escapes(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes for all pins of a package.

        Routes are generated based on package type:
        - BGA: Ring-based escape with layer alternation
        - QFP/QFN/TQFP: Alternating direction escape
        - SSOP/TSSOP: Alternating layer escape for fine-pitch
        - SOP: Staggered via fanout
        - Other: Simple radial escape

        When edge_clearance and board_bounds are configured, escape points
        and segment endpoints are clamped to stay within the edge clearance
        zone so the escape router does not produce board-edge violations.

        Issue #2639 / Epic #2556 Phase 2F: when ``self.diff_pair_map`` is
        non-empty AND any pad on this package has a partner pad on the
        SAME package, those pads are escaped first via
        ``_escape_diff_pair_segment``.  The paired escape produces two
        EscapeRoutes whose endpoints are at the target intra-pair
        spacing in the launch direction.  Remaining pads (single-ended
        or pairs whose partner is off-package) fall through to the
        existing per-package dispatcher.  The pair-aware path is only
        active for the BGA, QFP/QFN/TQFP, and MULTI_ROW_CONNECTOR
        dispatchers (the three priority dispatchers identified by the
        curator in #2639); SSOP/TSSOP / SOP / radial fall through
        single-ended for v1.

        Args:
            package: Package info from analyze_package()

        Returns:
            List of EscapeRoute objects for each pin
        """
        # Issue #3900: track whether the per-package dispatcher deliberately
        # declined to attempt an escape (rescue-only band with the rescue cap
        # disabled) versus actually attempting and failing clearance.  Reset
        # per call; set by ``_escape_sop_staggered`` when it takes the
        # rescue-only-band path with ``_sop_rescue_row_cap() == 0``.
        self._escape_not_attempted_rescue_band = False

        # ------------------------------------------------------------------
        # Phase 2F pre-pass: paired escape coupling at launch.
        # ------------------------------------------------------------------
        paired_escapes: list[EscapeRoute] = []
        paired_pad_keys: set[tuple[float, float]] = set()
        pair_aware_dispatchers = (
            PackageType.BGA,
            PackageType.QFP,
            PackageType.QFN,
            PackageType.TQFP,
            PackageType.MULTI_ROW_CONNECTOR,
        )
        if self.diff_pair_map and package.package_type in pair_aware_dispatchers:
            paired_escapes, paired_pad_keys = self._generate_paired_escapes(package)

        # Reduce the package's pad list to the un-paired pads for the
        # per-package dispatcher.  We rebuild a shallow PackageInfo with
        # the filtered pad list rather than mutating the input.
        if paired_pad_keys:
            remaining_pads = [p for p in package.pads if (p.x, p.y) not in paired_pad_keys]
            from dataclasses import replace as _replace

            remaining_package = _replace(package, pads=remaining_pads)
        else:
            remaining_package = package

        if remaining_package.pads:
            if package.package_type == PackageType.BGA:
                escapes = self._escape_bga_rings(remaining_package)
            elif package.package_type in (
                PackageType.QFP,
                PackageType.QFN,
                PackageType.TQFP,
            ):
                escapes = self._escape_qfp_alternating(remaining_package)
            elif package.package_type in (PackageType.SSOP, PackageType.TSSOP):
                # Fine-pitch SSOP/TSSOP needs alternating layer escape for adjacent pins
                escapes = self._escape_fine_pitch_dual_row(remaining_package)
            elif package.package_type == PackageType.USB_C_CONNECTOR:
                # Issue #2919: USB-C-class fine-pitch SMT connectors -- route the
                # SMT signal cluster through the alternating-layer escape so
                # adjacent USB_D+ / USB_D- (and CC1/CC2) pads land on different
                # layers.  Through-hole shield/mounting tabs are handled by the
                # main router (they're typically GND and connect to a stitched
                # plane, so no per-pin escape is required).
                escapes = self._escape_usb_c_connector(remaining_package)
            elif package.package_type == PackageType.SOP:
                escapes = self._escape_sop_staggered(remaining_package)
            elif package.package_type == PackageType.MULTI_ROW_CONNECTOR:
                escapes = self._escape_multi_row_connector(remaining_package)
            else:
                escapes = self._escape_radial(remaining_package)
        else:
            escapes = []

        # Paired escapes come first so callers (and the grid reservation
        # pass) see them adjacent in the output list -- this matches the
        # convention in `_escape_bga_rings` where outer-ring pads precede
        # inner-ring pads.
        escapes = paired_escapes + escapes

        # Apply edge clearance clamping when configured
        if self.edge_clearance is not None and self.board_bounds is not None:
            escapes = self._apply_edge_clearance(escapes)

        # Issue #2350: Warn when an entire package gets 0 escapes.
        # Silent failure makes it very hard to diagnose routing problems.
        if not escapes and package.pin_count > 0:
            if self._escape_not_attempted_rescue_band:
                # Issue #3900: a rescue-only-band SOP package with the rescue
                # cap disabled (``SOP_RESCUE_MAX_PER_ROW=0``) deliberately
                # emits no escape geometry -- no clearance check ever runs, so
                # the empty list is *correct* behaviour, not a failure.  Report
                # it as INFO with no ``fine_pitch_clearance`` suggestion (which
                # would be fabricated attribution -- see #3900).
                logger.info(
                    "Escape routing for %s (%s, %d pins, %.2fmm pitch): "
                    "escape not attempted -- rescue band disabled "
                    "(SOP_RESCUE_MAX_PER_ROW=0). To enable the in-pad rescue "
                    "experiment set KICAD_TOOLS_SOP_RESCUE_ROW_CAP=1.",
                    package.ref,
                    package.package_type.name,
                    package.pin_count,
                    package.pin_pitch,
                )
            else:
                logger.warning(
                    "Escape routing for %s (%s, %d pins, %.2fmm pitch): "
                    "0 pins escaped -- all escapes failed clearance validation. "
                    "Consider setting fine_pitch_clearance in DesignRules or "
                    "adding a component_clearances override for %s.",
                    package.ref,
                    package.package_type.name,
                    package.pin_count,
                    package.pin_pitch,
                    package.ref,
                )

        return escapes

    def _apply_edge_clearance(self, escapes: list[EscapeRoute]) -> list[EscapeRoute]:
        """Clamp escape route points to respect board edge clearance.

        Adjusts escape_point, via_pos, and segment endpoints so that no
        copper generated by the escape router falls within the edge
        clearance zone. The pad origin is never moved (the component is
        placed by the placer and is not our concern).

        Args:
            escapes: Escape routes to clamp

        Returns:
            The same list with coordinates adjusted in place
        """
        for escape in escapes:
            # Clamp escape point
            escape.escape_point = self._clamp_to_edge_clearance(*escape.escape_point)

            # Clamp via position if present
            if escape.via_pos is not None:
                escape.via_pos = self._clamp_to_edge_clearance(*escape.via_pos)
                if escape.via is not None:
                    clamped_x, clamped_y = escape.via_pos
                    escape.via = Via(
                        x=clamped_x,
                        y=clamped_y,
                        drill=escape.via.drill,
                        diameter=escape.via.diameter,
                        layers=escape.via.layers,
                        net=escape.via.net,
                        net_name=escape.via.net_name,
                        # Issue #3118: preserve in-pad and micro-via
                        # markers across edge-clearance clamp.  Without
                        # this, the in-pad escape rescue's
                        # ``is_micro=True`` marker silently disappears
                        # when the clamp pass rebuilds the Via, causing
                        # the downstream dimensions DRC exemption to
                        # miss the micro-via and the routed PCB to land
                        # with bare ``(via ...)`` 0.3/0.15 tokens
                        # instead of ``(via micro ...)``.
                        in_pad=escape.via.in_pad,
                        is_micro=escape.via.is_micro,
                    )

            # Clamp segment endpoints (skip x1/y1 of the first segment --
            # that is the pad origin which we must not move)
            for i, seg in enumerate(escape.segments):
                # For the first segment, only clamp the endpoint (x2, y2)
                if i == 0:
                    cx2, cy2 = self._clamp_to_edge_clearance(seg.x2, seg.y2)
                    escape.segments[i] = Segment(
                        x1=seg.x1,
                        y1=seg.y1,
                        x2=cx2,
                        y2=cy2,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )
                else:
                    cx1, cy1 = self._clamp_to_edge_clearance(seg.x1, seg.y1)
                    cx2, cy2 = self._clamp_to_edge_clearance(seg.x2, seg.y2)
                    escape.segments[i] = Segment(
                        x1=cx1,
                        y1=cy1,
                        x2=cx2,
                        y2=cy2,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )

        return escapes

    # ------------------------------------------------------------------
    # Diff-pair-aware escape coupling (Issue #2639 / Epic #2556 Phase 2F)
    # ------------------------------------------------------------------

    def _generate_paired_escapes(
        self,
        package: PackageInfo,
    ) -> tuple[list[EscapeRoute], set[tuple[float, float]]]:
        """Generate paired escapes for diff-pair pads on this package.

        Scans ``package.pads`` for pads whose net is listed in
        ``self.diff_pair_map`` AND whose partner pad is also on this
        package.  Each such pair is escaped via
        ``_escape_diff_pair_segment`` so the two traces leave the
        package already at the target intra-pair spacing.

        Pads whose partner is on a DIFFERENT package (cross-package
        pair coupling) fall through to the single-ended dispatcher and
        are coupled by the main pathfinder later -- their per-pad escape
        geometry is NOT hijacked here (this is not joint pin-assignment
        ILP; see Issue #4086's scope).

        Issue #4086 (Phase 1, epic #4049): when
        ``self.enable_cross_package_pair_corridor`` is True (default
        False) AND ``self.net_pad_positions`` resolves an off-package
        endpoint for the partner net, this method additionally reserves a
        SOFT continuation corridor from this leg's escape launch point
        toward the partner (via ``_reserve_cross_package_pair_corridor``)
        so the downstream coupled pathfinder has geometry to follow.  The
        pad is deliberately NOT added to ``paired_pad_keys`` in that case
        -- the leg still escapes single-ended; only the corridor
        reservation is added.  With the flag False the cross-package
        branch is a byte-identical no-op ``continue`` (all board 00-07
        fixtures, whose pairs are intra-package, are unaffected).

        Args:
            package: Package info, expected to be one of the three
                pair-aware dispatcher types (BGA / QFP-family /
                MULTI_ROW_CONNECTOR).

        Returns:
            Tuple of (paired_escapes, paired_pad_keys).
            ``paired_pad_keys`` is the set of ``(pad.x, pad.y)`` keys
            for pads that received a paired escape -- the caller uses
            this set to filter out paired pads from the per-package
            dispatcher's input so they are not double-escaped.  Pad
            coordinates are used as the key because pad equality
            depends on net assignment which we are intentionally
            cross-referencing here.
        """
        paired_escapes: list[EscapeRoute] = []
        paired_pad_keys: set[tuple[float, float]] = set()

        # Build a lookup from net_name to pad for this package only.
        # When two pads on the same package share a net (rare but
        # possible for thermal / ground pads on a QFN), the first
        # occurrence wins.  Diff-pair signal pads are by definition
        # unique-per-net so this is the correct degenerate behaviour.
        net_to_pad: dict[str, Pad] = {}
        for pad in package.pads:
            if pad.net_name and pad.net_name not in net_to_pad:
                net_to_pad[pad.net_name] = pad

        # Track already-paired net names so we don't emit two paired
        # escapes for the same (P, N) pair.
        already_paired: set[str] = set()

        # Resolve the intra-pair spacing once.  Prefer a per-net-class
        # value (``effective_intra_pair_clearance``); fall back to a
        # conservative default of ``trace_clearance``.  ``net_class_map``
        # is the same map the rest of the escape router uses.
        def _resolve_intra_pair_clearance(p_net: str) -> float:
            nc = self.net_class_map.get(p_net) if self.net_class_map else None
            if nc is not None and hasattr(nc, "effective_intra_pair_clearance"):
                try:
                    return float(nc.effective_intra_pair_clearance())
                except Exception:
                    pass
            return self.rules.trace_clearance

        for pad in package.pads:
            if pad.net_name in already_paired:
                continue
            partner_name = self.diff_pair_map.get(pad.net_name)
            if not partner_name:
                continue
            partner_pad = net_to_pad.get(partner_name)
            if partner_pad is None:
                # Partner net does not appear on this package.  The leg
                # defers to the per-package dispatcher (single-ended
                # escape); cross-package coupling is handled by the main
                # pathfinder.  Issue #4086 (Phase 1): when the gate is on
                # and the off-package partner endpoint is resolvable,
                # additionally reserve a SOFT continuation corridor from
                # this leg toward the partner so the coupled pathfinder
                # has geometry to follow.  This does NOT change the leg's
                # own escape geometry -- the pad is intentionally left out
                # of ``paired_pad_keys`` so it still escapes single-ended.
                if self.enable_cross_package_pair_corridor:
                    self._reserve_cross_package_pair_corridor(
                        pad=pad,
                        partner_name=partner_name,
                        intra_pair_clearance=_resolve_intra_pair_clearance(pad.net_name),
                        package=package,
                    )
                continue
            if partner_pad is pad:
                # Self-pair shouldn't happen but be defensive.
                continue

            intra = _resolve_intra_pair_clearance(pad.net_name)
            esc_p, esc_n = self._escape_diff_pair_segment(
                pad_p=pad,
                pad_n=partner_pad,
                package=package,
                intra_pair_clearance=intra,
            )
            paired_escapes.append(esc_p)
            paired_escapes.append(esc_n)
            paired_pad_keys.add((pad.x, pad.y))
            paired_pad_keys.add((partner_pad.x, partner_pad.y))
            already_paired.add(pad.net_name)
            already_paired.add(partner_name)
            logger.debug(
                "Phase 2F: paired escape for %s/%s on %s",
                pad.net_name,
                partner_name,
                package.ref,
            )

            # Issue #2677: reserve an inner-layer continuation corridor
            # for this pair BEFORE the per-package dispatcher places
            # partner-net through-hole vias.  Without this reservation,
            # the partner vias (which block ALL inner layers since they
            # are through-hole) can colonise the corridor the pair needs
            # to continue toward its destination -- on board 06 this is
            # the binding gap that strands USB3_TX1+/- with 0 segments.
            inner_layer = self._select_inner_escape_layer(esc_p.escape_layer)
            self._reserve_pair_continuation_corridor(
                members=[esc_p, esc_n],
                target_inner_layer=inner_layer,
                intra_pair_clearance=intra,
            )

        return paired_escapes, paired_pad_keys

    def _select_pair_launch_direction(
        self,
        pad_p: Pad,
        pad_n: Pad,
        mid_x: float,
        mid_y: float,
        package: PackageInfo,
    ) -> EscapeDirection:
        """Pick the launch direction for a paired diff-pair escape.

        Issue #3419: the original heuristic launched the pair outward
        from the package center (quadrant rule).  On board 06's BGA-49
        that strands the tightly-coupled escape endpoints on whatever
        side of the package the pads happen to sit -- when the partner
        connector is elsewhere, the per-net A* has to drag the coupled
        pair around the package perimeter from a 0.1 mm-lateral launch
        and times out.  This helper instead aims the launch TOWARD the
        pair's off-package endpoints (the partner connector), consulting
        ``self.net_pad_positions`` (board-wide net -> pad positions map).

        Falls back to the center-outward quadrant direction when:
          - ``net_pad_positions`` was not provided (default behaviour),
          - neither net has an off-package endpoint,
          - the target direction is degenerate (zero-length vector).

        Directions that would launch INTO the package interior (negative
        dot product with the outward vector from package center to the
        pair midpoint) are excluded so the escape never crosses the
        package's own pad field on the surface layer.

        Args:
            pad_p: Positive-half pad
            pad_n: Negative-half pad
            mid_x: X of the pair midpoint
            mid_y: Y of the pair midpoint
            package: Package info for center and pad field

        Returns:
            Cardinal EscapeDirection for the paired launch.
        """
        center_x, center_y = package.center
        fallback = self._get_quadrant_direction(mid_x, mid_y, center_x, center_y)
        if not self.net_pad_positions:
            return fallback

        # Collect off-package endpoints for both halves of the pair.
        # Position-keyed exclusion (not identity) because the board-wide
        # map stores plain coordinate tuples, not Pad objects.
        on_package = {(round(p.x, 3), round(p.y, 3)) for p in package.pads}
        targets: list[tuple[float, float]] = []
        for net_name in (pad_p.net_name, pad_n.net_name):
            if not net_name:
                continue
            for x, y in self.net_pad_positions.get(net_name, ()):
                if (round(x, 3), round(y, 3)) in on_package:
                    continue
                targets.append((x, y))
        if not targets:
            return fallback

        tx = sum(t[0] for t in targets) / len(targets)
        ty = sum(t[1] for t in targets) / len(targets)
        vx = tx - mid_x
        vy = ty - mid_y
        norm = math.hypot(vx, vy)
        if norm < 1e-6:
            return fallback
        vx /= norm
        vy /= norm

        # Outward vector (pair midpoint relative to package center):
        # used to veto launch directions that would cross the package
        # interior.
        ox = mid_x - center_x
        oy = mid_y - center_y

        best_dir = fallback
        best_score = -math.inf
        for cand in (
            EscapeDirection.NORTH,
            EscapeDirection.SOUTH,
            EscapeDirection.EAST,
            EscapeDirection.WEST,
        ):
            cdx, cdy = self._direction_to_vector(cand)
            if (cdx * ox + cdy * oy) < -1e-9:
                # Points back through the package interior -- never
                # launch a surface-layer pair into the pad field.
                continue
            score = cdx * vx + cdy * vy
            if score > best_score:
                best_score = score
                best_dir = cand
        return best_dir

    def _escape_diff_pair_segment(
        self,
        pad_p: Pad,
        pad_n: Pad,
        package: PackageInfo,
        intra_pair_clearance: float,
    ) -> tuple[EscapeRoute, EscapeRoute]:
        """Emit two coupled escape segments for a diff-pair pin pair.

        Both escapes leave the package in the SAME direction (chosen
        from the midpoint of the two pads using the same quadrant rule
        the single-ended escape uses).  The end-points are placed at
        ``intra_pair_clearance + trace_width`` apart in the lateral
        (cross-launch) axis so that downstream routing inherits the
        coupled spacing instead of having to re-converge.

        The launch direction is perpendicular to the pair axis when the
        pair axis is well-aligned with one of the package edges; in the
        degenerate diagonal case we fall back to whichever axis (NSEW)
        the midpoint quadrant suggests.

        Args:
            pad_p: Positive-half pad
            pad_n: Negative-half pad
            package: Package info for bounds and center
            intra_pair_clearance: Target inner-edge-to-inner-edge
                clearance between the two paired escape segments

        Returns:
            ``(escape_p, escape_n)`` -- two EscapeRoute objects, each
            with a single straight segment from its pad to its escape
            point.  Both escapes are on ``pad.layer`` (surface escape;
            via-down coupling is left to the per-package dispatcher
            since it is not the failure mode this phase targets).
        """
        # Bump the instrumentation counter (Gate 3/4 verification).
        self.diff_pair_segment_calls += 1

        # Midpoint of the two pads -- used to pick the launch direction
        # so both escapes leave together.
        mid_x = (pad_p.x + pad_n.x) / 2.0
        mid_y = (pad_p.y + pad_n.y) / 2.0

        # Issue #3419: pick the launch direction toward the partner
        # connector when board-wide net endpoints are available; fall
        # back to the original center-outward quadrant heuristic.
        direction = self._select_pair_launch_direction(pad_p, pad_n, mid_x, mid_y, package)
        dx, dy = self._direction_to_vector(direction)

        # Trace widths come from per-net config.  Use the wider of the
        # two so the coupled-spacing math leaves room for both traces.
        trace_w_p = self._get_trace_width_for_net(pad_p.net_name)
        trace_w_n = self._get_trace_width_for_net(pad_n.net_name)
        trace_w = max(trace_w_p, trace_w_n)

        # Launch distance: same heuristic the per-package alternating
        # escape uses (clearance + 2 * trace_width).  This puts the
        # escape point clearly outside the pad clearance zone.
        escape_dist = self.escape_clearance + trace_w * 2

        # Pair axis (between the two pads) -- the perpendicular to the
        # launch direction.  We project the pair vector onto the lateral
        # axis to figure out which pad is "left" of the launch direction
        # so the two escape segments don't cross.
        pair_dx = pad_n.x - pad_p.x
        pair_dy = pad_n.y - pad_p.y

        # Lateral (perpendicular-to-launch) unit vector.  For a launch
        # direction (dx, dy) the right-hand-rule perpendicular is
        # (-dy, dx).
        lat_dx, lat_dy = -dy, dx

        # Project the pad-to-pad vector onto the lateral axis: positive
        # means pad_n is "right" of pad_p along the launch direction.
        proj = pair_dx * lat_dx + pair_dy * lat_dy

        # Target half-offset: each escape point sits ``half_offset``
        # away from the pair midpoint along the lateral axis.  The
        # outer-edge-to-outer-edge spacing of the two parallel traces
        # then equals ``intra_pair_clearance + trace_w``.  We keep the
        # symmetric placement so the geometry is verifiable by tests
        # without sub-mm float jitter.
        half_offset = (intra_pair_clearance + trace_w) / 2.0

        # Sign chosen so pad_p escape ends up on the "left" side
        # (negative projection) and pad_n on the "right" (positive).
        sign_p = -1.0 if proj >= 0 else 1.0
        sign_n = +1.0 if proj >= 0 else -1.0

        # Escape points: launch from the midpoint along the launch
        # direction, then step laterally by half_offset for each pad.
        launch_x = mid_x + dx * escape_dist
        launch_y = mid_y + dy * escape_dist
        ep_p = (launch_x + sign_p * half_offset * lat_dx, launch_y + sign_p * half_offset * lat_dy)
        ep_n = (launch_x + sign_n * half_offset * lat_dx, launch_y + sign_n * half_offset * lat_dy)

        seg_p = Segment(
            x1=pad_p.x,
            y1=pad_p.y,
            x2=ep_p[0],
            y2=ep_p[1],
            width=trace_w_p,
            layer=pad_p.layer,
            net=pad_p.net,
            net_name=pad_p.net_name,
        )
        seg_n = Segment(
            x1=pad_n.x,
            y1=pad_n.y,
            x2=ep_n[0],
            y2=ep_n[1],
            width=trace_w_n,
            layer=pad_n.layer,
            net=pad_n.net,
            net_name=pad_n.net_name,
        )

        escape_p = EscapeRoute(
            pad=pad_p,
            direction=direction,
            escape_point=ep_p,
            escape_layer=pad_p.layer,
            via_pos=None,
            segments=[seg_p],
            via=None,
            ring_index=0,
        )
        escape_n = EscapeRoute(
            pad=pad_n,
            direction=direction,
            escape_point=ep_n,
            escape_layer=pad_n.layer,
            via_pos=None,
            segments=[seg_n],
            via=None,
            ring_index=0,
        )
        return escape_p, escape_n

    def _resolve_slack_budget(
        self,
        members: list[EscapeRoute],
        slack_budget_mm: float | None,
    ) -> float:
        """Resolve the pin-geometry slack budget for a paired corridor.

        Issue #4085 (Phase 1).  When ``slack_budget_mm`` is supplied it is
        returned verbatim (a caller that already computed the estimate).
        Otherwise the budget is estimated from the members' boundary pin
        geometry via :func:`slack_budget.estimate_pair_skew_budget` (2
        legs) or :func:`slack_budget.estimate_group_skew_budget` (N>2).

        The per-leg pin set prefers the board-wide ``net_pad_positions``
        map (all pads on the net, so the estimate spans the full
        source->sink extent) and falls back to the escape point + pad
        centre when the map is not populated.

        Args:
            members: The paired EscapeRoutes.
            slack_budget_mm: Optional explicit override.

        Returns:
            The slack budget in mm (``>= 0``).
        """
        if slack_budget_mm is not None:
            return max(0.0, float(slack_budget_mm))

        from .slack_budget import (
            estimate_group_skew_budget,
            estimate_pair_skew_budget,
        )

        legs_pins: list[list[tuple[float, float]]] = []
        for m in members:
            name = m.pad.net_name or ""
            pins = list(self.net_pad_positions.get(name, [])) if name else []
            if len(pins) < 2:
                # Fallback: the two endpoints we know about are the pad
                # centre and the escape point.  This still captures the
                # per-leg launch asymmetry when no board-wide map exists.
                pins = [(m.pad.x, m.pad.y), m.escape_point]
            legs_pins.append(pins)

        if len(legs_pins) == 2:
            return estimate_pair_skew_budget(legs_pins[0], legs_pins[1])
        return estimate_group_skew_budget(legs_pins)

    def _resolve_pair_skew_tolerance(self, members: list[EscapeRoute]) -> float:
        """Resolve the skew tolerance (mm) for a paired corridor.

        Issue #4085 (Phase 1).  Reads the first member's net class from
        ``net_class_map`` and returns its
        :meth:`NetClassRouting.effective_skew_tolerance`.  Falls back to
        the module-level 0.5 mm default (matching
        ``Autorouter.apply_diffpair_length_tuning``) when no net class is
        configured for the pair.

        Args:
            members: The paired EscapeRoutes.

        Returns:
            The skew tolerance in mm.
        """
        default = 0.5
        first_net = members[0].pad.net_name or ""
        nc = self.net_class_map.get(first_net) if self.net_class_map else None
        if nc is not None and hasattr(nc, "effective_skew_tolerance"):
            try:
                return float(nc.effective_skew_tolerance(default))
            except Exception:
                return default
        return default

    def _reserve_pair_continuation_corridor(
        self,
        members: list[EscapeRoute],
        target_inner_layer: Layer,
        intra_pair_clearance: float | None = None,
        slack_budget_mm: float | None = None,
    ) -> int:
        """Reserve an inner-layer continuation corridor for paired escapes.

        Issue #2677: After ``_escape_diff_pair_segment`` produces two
        surface-layer escape segments, the pair has no reserved
        downstream channel on an inner copper layer.  Partner-net escape
        vias (through-hole, generated by ``_escape_bga_rings`` and the
        other per-package dispatchers) block ALL inner layers and can
        colonise the same channel the diff pair needs to continue
        toward its destination.  This helper reserves a rectangular
        corridor on ``target_inner_layer`` extruding forward from the
        midpoint of the paired escape points along the launch direction.
        ``RoutingGrid._mark_via`` respects the reservation (see
        ``grid.reserve_corridor_cells``) so partner-net vias detour
        around the corridor.

        The API takes a generic ``members: list[EscapeRoute]`` (not a
        hard-coded pair) so Epic #2661 Phase 2E
        (``tune_match_group_v2``) can reuse it for N>=3 match groups by
        passing the full member list.  The corridor envelope and
        net-owner set scale with ``len(members)``.

        Geometry:
            * Launch direction is taken from ``members[0].direction``.
            * Origin is the centroid of the escape points.
            * Corridor extends ``length`` mm in the launch direction.
            * Corridor width spans the bounding box of the escape
              points PLUS a ``(intra_pair_clearance + trace_width)``
              padding on each lateral side (so a partner via that just
              clears the corridor edge still cannot blockade the
              continuation).

        Sized empirically for the BGA-49 USB3 case on board 06: a
        corridor ~3x the launch step long is enough to outlast the
        nearest inner-ring partner via that ``_escape_bga_rings`` would
        place (``via_offset = via_spacing`` at the next ring).

        Pathfinder integration (Issue #2911): The reservation is
        consulted by ``RoutingGrid.get_corridor_attractor_bonus`` and
        applied as a NEGATIVE step cost in the A* pathfinder, biasing
        the main routing pipeline toward dropping a via into the
        corridor and continuing on the reserved layer.  This is the
        "attractor" mechanism — without it, the protection from #2677
        was a no-blockade region but not preferentially used by the
        pathfinder.

        Envelope robustness (Issue #2911 AC6): The reservation map is
        consulted PER CELL by ``RoutingGrid._mark_via`` -- a partner via
        whose centre sits OUTSIDE the corridor but whose clearance
        envelope overlaps the corridor will have its in-corridor cells
        skipped (the centre cell itself is still blocked, but the
        envelope is harmless to the reservation).  No additional halo
        widening of ``lat_half`` is required for this -- and in fact
        widening it starves neighbouring single-ended nets of routing
        channels on dense match-group boards (e.g. board 07's DDR data
        byte).

        Args:
            members: Paired EscapeRoutes (2 for a diff pair, N for a
                match group). Must contain at least 2 members; an empty
                or single-member list is a no-op.
            target_inner_layer: Inner copper layer for the reservation
                (typically from ``_select_inner_escape_layer``).
            intra_pair_clearance: Optional override for the lateral
                padding factor; defaults to the value derived from the
                first member's net class via
                ``_resolve_intra_pair_clearance`` (same value the
                segment generator used).
            slack_budget_mm: Issue #4085 (Phase 1).  Optional pre-computed
                pin-geometry slack budget (mm).  Only consulted when
                ``self.enable_slack_corridor_widening`` is ``True`` and the
                budget exceeds the pair's net-class skew tolerance, in
                which case ``lat_half`` is widened by the budget so the
                downstream serpentine tuner has reserved cells to meander
                into.  When ``None`` (default) the estimator is invoked
                internally from the members' pad geometry; pass an explicit
                value to override (e.g. from a caller that already computed
                it).  Ignored entirely when the widening gate is off, so
                behaviour is byte-identical to today.

        Returns:
            Number of grid cells reserved.  Returns 0 if the helper is
            a no-op (e.g. fewer than 2 members, or the grid lacks the
            requested layer).
        """
        if len(members) < 2:
            return 0

        # Issue #2677: Restrict corridor reservation to genuine INNER
        # routable layers.  When the grid is 2-layer,
        # ``_select_inner_escape_layer`` falls back to ``Layer.B_CU`` --
        # reserving on B.Cu would block partner-net through-hole vias
        # from completing their footprint on B.Cu, which actively breaks
        # routing on 2-layer boards (a partner via on a 2-layer board
        # MUST be free to land both on F.Cu and B.Cu).  The fix only
        # applies when there is a true inner copper layer available.
        if self.grid.layer_stack is not None:
            target_def = self.grid.layer_stack.get_layer_by_name(target_inner_layer.kicad_name)
            if target_def is None or target_def.is_outer:
                logger.debug(
                    "Corridor reservation skipped: %s is not an inner layer "
                    "(or not in stack); 2-layer boards do not need this fix",
                    target_inner_layer.name,
                )
                return 0
        # Resolve target layer index.  If the layer isn't in the grid's
        # layer stack (defensive), bail out gracefully.
        try:
            target_idx = self.grid.layer_to_index(target_inner_layer.value)
        except Exception:
            logger.debug(
                "Corridor reservation skipped: layer %s not in grid stack",
                target_inner_layer.name,
            )
            return 0

        # Build the net-owner set so members can still place vias inside
        # their own corridor.  Defensive: skip None nets.
        owner_nets: set[int] = set()
        for m in members:
            if m.pad.net is not None:
                owner_nets.add(int(m.pad.net))
        if not owner_nets:
            return 0

        # Launch direction: take from the first member.  All paired
        # members share the same direction by construction (see
        # ``_escape_diff_pair_segment``).
        dx, dy = self._direction_to_vector(members[0].direction)
        if dx == 0 and dy == 0:
            # VIA_DOWN or unknown direction -- no meaningful corridor.
            return 0

        # Normalise the direction vector (the diagonal directions return
        # 0.707/0.707 which is already unit-length, but be defensive).
        length_norm = math.hypot(dx, dy)
        if length_norm == 0:
            return 0
        dx /= length_norm
        dy /= length_norm

        # Lateral unit vector (right-hand-rule perpendicular).
        lat_dx, lat_dy = -dy, dx

        # Origin: centroid of escape points.
        cx = sum(m.escape_point[0] for m in members) / len(members)
        cy = sum(m.escape_point[1] for m in members) / len(members)

        # Lateral half-width: span the escape points' lateral extent
        # plus a padding term equal to the intra-pair clearance + trace
        # width.  This ensures a partner via that just clears the
        # outermost member trace still cannot fit between the corridor
        # and the next routing channel.
        lat_projections = [
            (m.escape_point[0] - cx) * lat_dx + (m.escape_point[1] - cy) * lat_dy for m in members
        ]
        lat_extent = max(abs(p) for p in lat_projections)

        if intra_pair_clearance is None:
            # Resolve from the first member's net class, mirroring the
            # _generate_paired_escapes resolution path.
            first_net = members[0].pad.net_name or ""
            nc = self.net_class_map.get(first_net) if self.net_class_map else None
            if nc is not None and hasattr(nc, "effective_intra_pair_clearance"):
                try:
                    intra_pair_clearance = float(nc.effective_intra_pair_clearance())
                except Exception:
                    intra_pair_clearance = self.rules.trace_clearance
            else:
                intra_pair_clearance = self.rules.trace_clearance

        # Use the WIDEST member trace_width for the padding so a partner
        # via clears the worst-case-width member.
        max_trace_w = max(self._get_trace_width_for_net(m.pad.net_name or "") for m in members)
        lat_pad = intra_pair_clearance + max_trace_w
        lat_half = lat_extent + lat_pad

        # Issue #4085 (Phase 1): widen the corridor by a pin-geometry slack
        # budget so the downstream serpentine tuner has reserved cells to
        # meander the shorter half into, instead of scavenging leftover
        # space.  Gated OFF by default -- when the gate is off, or the
        # estimated skew is within the pair's tolerance, ``lat_half`` is
        # unchanged and the reservation is byte-identical to today.
        if self.enable_slack_corridor_widening and len(members) >= 2:
            budget = self._resolve_slack_budget(members, slack_budget_mm)
            tol = self._resolve_pair_skew_tolerance(members)
            if budget > tol:
                lat_half += budget
                self.pair_corridor_slack_widened += 1
                self.pair_corridor_slack_budget_mm += budget
                logger.debug(
                    "Issue #4085 slack widening: budget=%.4fmm > tol=%.4fmm; "
                    "lat_half widened to %.4fmm for nets %s",
                    budget,
                    tol,
                    lat_half,
                    [m.pad.net_name for m in members],
                )

        # Issue #2911 (AC6 envelope robustness): The corridor reservation
        # is consulted PER CELL by ``RoutingGrid._mark_via`` -- when a
        # partner via's envelope (radius
        # ``(via_diameter/2 + via_clearance + trace_w/2)/resolution + 1``)
        # overlaps the corridor, the CELLS inside the corridor are skipped
        # individually, even if the via centre is outside the corridor.
        # That means the existing per-cell protection already absorbs the
        # partner-via clearance halo: a partner via just outside the
        # corridor can still place its centre cell, but none of its
        # envelope cells inside the corridor will be blocked.  We
        # therefore do NOT widen ``lat_half`` to include the partner-via
        # halo -- doing so would over-reserve the inner layer and starve
        # neighbouring single-ended nets of routing channels (board 07
        # match-group regression observed during PR-2911 development).
        # The halo widening from earlier #2911 iterations is intentionally
        # NOT applied here; AC6 is satisfied by the per-cell skip in
        # ``_mark_via`` instead.

        # Corridor length: extrude forward by ~3 launch-distance steps
        # so the corridor outlasts the nearest partner via.  The launch
        # distance for the paired segments is
        # ``escape_clearance + 2 * trace_width`` (see
        # ``_escape_diff_pair_segment``); we use 3x to comfortably
        # outlast the via_spacing-offset partner via.
        launch_step = self.escape_clearance + max_trace_w * 2
        corridor_length = launch_step * 3.0

        # Enumerate grid cells covered by the rectangle.  We use a
        # parametric (t, u) walk where t is along the launch axis
        # (0 .. corridor_length) and u is the lateral coordinate
        # (-lat_half .. +lat_half).  Step by half the grid resolution
        # to avoid aliasing on diagonal launches.
        step = self.grid.resolution * 0.5
        t = 0.0
        cells: set[tuple[int, int]] = set()
        while t <= corridor_length:
            u = -lat_half
            while u <= lat_half:
                wx = cx + dx * t + lat_dx * u
                wy = cy + dy * t + lat_dy * u
                gx, gy = self.grid.world_to_grid(wx, wy)
                cells.add((gx, gy))
                u += step
            t += step

        if not cells:
            return 0

        count = self.grid.reserve_corridor_cells(
            layer_idx=target_idx,
            cells=cells,
            net_ids=owner_nets,
        )
        if count > 0:
            self.pair_corridor_reservations += 1
            self.pair_corridor_reserved_cells += count
            logger.debug(
                "Phase 2F corridor reserved: layer=%s cells=%d nets=%s members=%d direction=%s",
                target_inner_layer.name,
                count,
                sorted(owner_nets),
                len(members),
                members[0].direction.name,
            )
        return count

    def _reserve_cross_package_pair_corridor(
        self,
        pad: Pad,
        partner_name: str,
        intra_pair_clearance: float,
        package: PackageInfo,
    ) -> int:
        """Reserve a SOFT continuation corridor for a cross-package pair leg.

        Issue #4086 (Phase 1, epic #4049).  When a diff pair's two halves
        live on DIFFERENT packages, the intra-package paired-escape path
        (``_escape_diff_pair_segment`` +
        ``_reserve_pair_continuation_corridor``) does not apply -- each
        leg escapes single-ended and the only coupling downstream is
        ``CoupledPathfinder`` free-searching the joint state space.  This
        helper reserves a corridor connecting THIS leg's escape launch
        point to the off-package partner endpoint so the coupled search
        (via the #4080 attractor) has geometry to follow instead of
        discovering feasibility from scratch.

        Unlike ``_reserve_pair_continuation_corridor`` (a HARD keep-out
        that fences foreign copper out of a planar intra-package
        corridor), this reservation is SOFT (``soft=True``): it applies
        ONLY the A* attractor bonus and does NOT fence foreign vias or
        lateral traces out.  A hard fence spanning the whole board between
        two packages would carve a foreign-copper-free channel across the
        board and starve unrelated nets -- the #4087 evidence that hard
        fences short-induce in dense areas applies doubly to a long
        cross-package span.  Soft keeps the corridor attractor-visible
        (so the coupled pair is pulled onto it) while leaving every other
        net free to cross it.

        Geometry (single leg -> partner):
            * Partner endpoint is the nearest off-package position for
              ``partner_name`` in ``self.net_pad_positions``.  If none is
              resolvable the method is a no-op (returns 0) -- this is the
              fallback-safety contract mirrored on
              ``_select_pair_launch_direction``.
            * Launch direction is the unit vector from THIS pad toward the
              partner endpoint (this is where the leg wants to go, so the
              single-ended escape and the coupled continuation both head
              this way).
            * Origin is this pad's escape launch point
              (``pad + direction * escape_dist``), the same launch
              distance the single-ended and paired escapes use.
            * The corridor is a rectangle from the launch point toward the
              partner, laterally padded by ``intra_pair_clearance +
              trace_width`` so both legs of the pair fit side by side.
            * Length is clamped so the corridor never overshoots the
              partner endpoint.

        Args:
            pad: This leg's pad (on ``package``).
            partner_name: Net name of the off-package partner half.
            intra_pair_clearance: Target intra-pair clearance (resolved by
                the caller from the net class), used for lateral padding.
            package: This leg's package (for the on-package exclusion set
                when picking the partner endpoint).

        Returns:
            Number of grid cells reserved.  0 when the flag path is a
            no-op (partner endpoint unresolvable, degenerate direction,
            no inner layer, or grid cells empty).
        """
        if not self.net_pad_positions:
            return 0
        if pad.net_name is None:
            return 0

        # Resolve the partner's nearest OFF-package endpoint.  Positions
        # that coincide with a pad on THIS package are excluded (the
        # partner's on-package pads, if any, are not the cross-package
        # target).  Position-keyed exclusion mirrors
        # ``_select_pair_launch_direction``.
        on_package = {(round(p.x, 3), round(p.y, 3)) for p in package.pads}
        partner_positions = [
            (x, y)
            for (x, y) in self.net_pad_positions.get(partner_name, ())
            if (round(x, 3), round(y, 3)) not in on_package
        ]
        if not partner_positions:
            return 0

        # Nearest off-package partner endpoint to this pad.
        tx, ty = min(
            partner_positions,
            key=lambda pt: math.hypot(pt[0] - pad.x, pt[1] - pad.y),
        )

        # Launch direction: from this pad toward the partner.
        vx = tx - pad.x
        vy = ty - pad.y
        span = math.hypot(vx, vy)
        if span == 0:
            return 0
        dx = vx / span
        dy = vy / span
        # Lateral (right-hand-rule perpendicular) unit vector.
        lat_dx, lat_dy = -dy, dx

        # Inner routable layer for the corridor (same selection the
        # intra-package continuation corridor uses).
        target_inner_layer = self._select_inner_escape_layer(pad.layer)
        if self.grid.layer_stack is not None:
            target_def = self.grid.layer_stack.get_layer_by_name(target_inner_layer.kicad_name)
            if target_def is None or target_def.is_outer:
                # 2-layer boards / no inner signal layer: no corridor.
                return 0
        try:
            target_idx = self.grid.layer_to_index(target_inner_layer.value)
        except Exception:
            return 0

        # Owner set: this leg's net id plus (when resolvable) the partner
        # net id, so BOTH legs of the pair receive the soft attractor
        # bonus toward this corridor.
        owner_nets: set[int] = set()
        if pad.net is not None:
            owner_nets.add(int(pad.net))
        partner_id = self.net_name_to_id.get(partner_name)
        if partner_id is not None:
            owner_nets.add(int(partner_id))
        if not owner_nets:
            return 0

        # Trace width for the padding term (widest of the two legs when
        # the partner width is resolvable, else this leg's width).
        trace_w = self._get_trace_width_for_net(pad.net_name)
        partner_w = self._get_trace_width_for_net(partner_name)
        max_trace_w = max(trace_w, partner_w)

        # Escape launch point: step off the pad by the same launch
        # distance the single-ended / paired escapes use, so the corridor
        # begins where the leg actually leaves the package.
        escape_dist = self.escape_clearance + max_trace_w * 2
        # Do not overshoot the partner endpoint if it is closer than the
        # launch distance (degenerate very-near packages).
        launch = min(escape_dist, span * 0.5)
        cx = pad.x + dx * launch
        cy = pad.y + dy * launch

        # Corridor length: from the launch point to the partner endpoint,
        # clamped to the remaining span.  This connects the two escape
        # exits without overshooting into the partner package.
        corridor_length = max(0.0, span - launch)
        if corridor_length <= 0.0:
            return 0

        # Lateral half-width: room for both legs side by side plus the
        # intra-pair clearance.  (No via-halo widening -- the soft
        # reservation does not fence, so over-reserving would only dilute
        # the attractor field, not fence neighbours out.)
        lat_half = intra_pair_clearance + max_trace_w

        # Enumerate covered cells with a (t, u) parametric walk, stepping
        # by half the grid resolution to avoid diagonal aliasing -- same
        # sampling ``_reserve_pair_continuation_corridor`` uses.
        step = self.grid.resolution * 0.5
        if step <= 0:
            return 0
        cells: set[tuple[int, int]] = set()
        t = 0.0
        while t <= corridor_length:
            u = -lat_half
            while u <= lat_half:
                wx = cx + dx * t + lat_dx * u
                wy = cy + dy * t + lat_dy * u
                gx, gy = self.grid.world_to_grid(wx, wy)
                cells.add((gx, gy))
                u += step
            t += step
        if not cells:
            return 0

        count = self.grid.reserve_corridor_cells(
            layer_idx=target_idx,
            cells=cells,
            net_ids=owner_nets,
            soft=True,
        )
        if count > 0:
            self.cross_package_pair_corridor_reservations += 1
            self.cross_package_pair_corridor_reserved_cells += count
            logger.debug(
                "Issue #4086 cross-package corridor reserved (soft): "
                "layer=%s cells=%d nets=%s %s->%s",
                target_inner_layer.name,
                count,
                sorted(owner_nets),
                pad.net_name,
                partner_name,
            )
        return count

    def reserve_inner_corner_lane_corridor(
        self,
        pad: Pad,
        launch_dx: float,
        launch_dy: float,
        target_inner_layer: Layer | None = None,
        corridor_length: float | None = None,
        corridor_half_width: float | None = None,
        *,
        soft: bool = True,
        mirror_to_cpp: bool = False,
    ) -> int:
        """Reserve an inner-layer lateral corridor for a single-ended pad.

        Issue #2983: Generalises ``_reserve_pair_continuation_corridor``
        to the **single-ended inner-corner case** on mirrored byte-lane
        packages (e.g. board 07's DDR data byte on a mirrored QFN-48
        pair).  Pin row order on U1.25-35 is
        ``DQ0, DQ1, DQ2, DQ3, DM0, DQS_P, DQS_N, DQ4, DQ5, DQ6, DQ7``
        (mirrored on U2.1-11) — the pads at sorted positions 1 and N-2
        ("inner-corner") are squeezed by their corner neighbour's
        through-hole via placement.  Reserving a lateral corridor on
        an inner signal layer (typically In1.Cu on the JLCPCB 4-layer
        tier-1 stack-up) BEFORE any corner-net escapes prevents the
        partner via from colonising the only continuation lane.

        This is the single-ended sibling of the diff-pair corridor
        primitive: same grid mechanic
        (``RoutingGrid.reserve_corridor_cells``), same per-cell
        consultation in ``RoutingGrid._mark_via`` (non-matching nets
        skip the cell), same instrumentation pattern.  The geometry
        is simpler — one pad, one launch direction — so no centroid
        or partner projection is needed.

        Geometry:
            * Origin = pad centre.
            * Corridor extends ``corridor_length`` mm along the launch
              vector (dx, dy).
            * Corridor width = ``2 * corridor_half_width`` mm,
              centred on the pad and oriented perpendicular to launch.

        Defaults are sized for board 07's 0.8mm-pitch QFN-48: the
        launch step is ``escape_clearance + 2 * trace_width`` and the
        corridor extrudes ~3 launch steps long (matching the PR #2911
        diff-pair recipe).  The lateral half-width is one launch step
        — narrower than the diff-pair recipe because we only need to
        protect ONE net, not a pair, and a wider reservation would
        starve the second-inward neighbour (the same trade-off that
        led PR #2911 to NOT widen ``lat_half`` for the partner-via
        halo; see ``_reserve_pair_continuation_corridor`` AC6 note).

        Args:
            pad: The inner-corner pad whose lane is being protected.
                Must have a non-zero ``pad.net``.
            launch_dx: Outward x-component of launch direction.
            launch_dy: Outward y-component of launch direction.
                ``(launch_dx, launch_dy)`` will be normalised to a unit
                vector; the caller can pass component-centroid
                differences directly.
            target_inner_layer: Inner copper layer for the reservation.
                Defaults to ``_select_inner_escape_layer(pad.layer)``
                which returns the first inner signal layer (In1.Cu on
                a 4-layer board) or B.Cu on 2-layer boards.  The 2-layer
                fallback path is short-circuited inside this helper
                (same guard as the diff-pair version): reserving B.Cu
                would block the pad's own through-hole vias.
            corridor_length: Optional override for forward extent.
                Defaults to ``3 * (escape_clearance + 2 * trace_w)``.
            corridor_half_width: Optional override for lateral
                half-width.  Defaults to ``escape_clearance + trace_w``.
            soft: Issue #4256.  Keep-out STRENGTH for the reservation.
                Defaults to ``True`` (SOFT, attractor-only) — the
                byte-identical #2983 single-ended default, whose C++
                honouring #4079 measured to regress board 07's reversed DDR
                byte.  The discrete ``BundlePlan`` allocator (A3) passes
                ``soft=False`` to make a member's lane a HARD keep-out that
                foreign nets are fenced out of (``is_reserved_excluding``).
            mirror_to_cpp: Issue #4256.  Whether the reservation is mirrored
                onto the production C++ grid.  Defaults to ``False`` (the
                #2983 single-ended default stays Python-only, per #4079).
                The A3 bundle-plan lanes pass ``mirror_to_cpp=True`` so the
                HARD keep-out actually fences foreign nets out on the C++
                A* — a SOFT/unmirrored reservation cannot meet the
                "foreign net provably excluded" acceptance bar.

        Returns:
            Number of grid cells reserved.  Returns 0 if the helper is
            a no-op (e.g. zero net id, layer not in stack, 2-layer
            board, zero launch vector).

        Note:
            The two defaults (``soft=True, mirror_to_cpp=False``) are
            load-bearing: they keep the existing single-ended byte-lane call
            site (``_apply_byte_lane_inner_priority``) byte-identical to
            pre-#4256 ``main``.  Only the new HARD bundle-plan callers flip
            them — #4079 measured that honouring the single-ended default on
            C++ (hard OR soft) regresses board 07, so the default must NOT
            change.
        """
        # Skip no-op cases up front.
        net_id = int(pad.net) if pad.net else 0
        if net_id == 0:
            return 0

        length_norm = math.hypot(launch_dx, launch_dy)
        if length_norm == 0:
            return 0
        dx = launch_dx / length_norm
        dy = launch_dy / length_norm

        # Select target layer.  Default mirrors the diff-pair primitive
        # (first inner signal layer; B.Cu fallback when no inner signal
        # layers are available — e.g. board 07's 4-layer stack-up where
        # In1.Cu/In2.Cu are PLANES).  For the single-ended inner-corner
        # case the B.Cu fallback is *valid*: a pad-specific reservation
        # on B.Cu blocks OTHER nets' through-hole vias from invading
        # the corridor while still allowing the pad's own escape via
        # (which carries the same net id and matches the reservation).
        # The 2-layer fallback (where B.Cu is the ONLY alternate signal
        # layer) is still excluded because reserving cells on the only
        # alternate routable surface would starve partner-net escapes
        # — same hazard as the diff-pair primitive's 2-layer guard.
        if target_inner_layer is None:
            target_inner_layer = self._select_inner_escape_layer(pad.layer)

        # Require at least 3 routable layers in the stack-up.  On
        # 2-layer boards this fix is unnecessary (no via-blocking
        # contention to resolve) and *harmful* (would block partner-net
        # vias from completing).  On 4-layer (and deeper) stacks the
        # corridor reservation is safe regardless of whether the
        # selected layer is an inner signal layer (In1.Cu) or an outer
        # routing layer (B.Cu) — partner-net vias will detour because
        # the cells are reserved for *this* net only.
        if self.grid.layer_stack is not None:
            if self.grid.layer_stack.num_layers < 3:
                logger.debug(
                    "Inner-corner corridor skipped: 2-layer stack-up "
                    "(no contention to resolve, reservation would starve "
                    "partner escapes)"
                )
                return 0
            target_def = self.grid.layer_stack.get_layer_by_name(target_inner_layer.kicad_name)
            if target_def is None:
                logger.debug(
                    "Inner-corner corridor skipped: layer %s not in stack",
                    target_inner_layer.name,
                )
                return 0

        try:
            target_idx = self.grid.layer_to_index(target_inner_layer.value)
        except Exception:
            logger.debug(
                "Inner-corner corridor skipped: layer %s not in grid stack",
                target_inner_layer.name,
            )
            return 0

        # Resolve trace width from net class (same idiom as the diff-pair
        # primitive).
        trace_w = self._get_trace_width_for_net(pad.net_name or "")
        launch_step = self.escape_clearance + 2 * trace_w
        if corridor_length is None:
            corridor_length = launch_step * 3.0
        if corridor_half_width is None:
            # One launch step — narrower than the diff-pair half-width
            # (which spans the pair extent plus padding).  See docstring
            # for the starvation-avoidance rationale.
            corridor_half_width = launch_step

        # Lateral unit vector (right-hand-rule perpendicular).
        lat_dx, lat_dy = -dy, dx
        cx, cy = pad.x, pad.y

        step = self.grid.resolution * 0.5
        cells: set[tuple[int, int]] = set()
        t = 0.0
        while t <= corridor_length:
            u = -corridor_half_width
            while u <= corridor_half_width:
                wx = cx + dx * t + lat_dx * u
                wy = cy + dy * t + lat_dy * u
                gx, gy = self.grid.world_to_grid(wx, wy)
                cells.add((gx, gy))
                u += step
            t += step

        if not cells:
            return 0

        # Issue #4256: this method is now parameterised on ``soft`` /
        # ``mirror_to_cpp``.  The DEFAULTS below (``soft=True,
        # mirror_to_cpp=False``) keep the #2983 single-ended inner-corner
        # reservation Python-only and byte-identical, for the reason #4079
        # measured; the A3 discrete BundlePlan lanes override both to
        # ``soft=False, mirror_to_cpp=True`` (HARD, C++-mirrored) via a
        # DIFFERENT topology (coupled diff-pair bundle, not the reversed DDR
        # byte), so the regression below does not apply to them.
        #
        # Issue #4079 (the single-ended DEFAULT rationale): keeping this
        # reservation Python-only (``mirror_to_cpp=False``, PR #4078's
        # "Path B").  #4079 investigated honouring it on the C++ backend two
        # ways and MEASURED both to regress board 07's fully-reversed DDR
        # byte on the CI recipe
        # (`generate_design.py ... --step all --seed 42`, out-of-process
        # copper-LVS re-check):
        #
        #   * HARD lateral-trace keep-out (fence foreign traces out): 25/31,
        #     3 shorts (DM0<->DQ7, DM0<->DQS_N, DQ7<->DQS_N at U1.29/31/35)
        #     + 6 opens.  The byte's crossing conflict graph is COMPLETE, so
        #     a hard fence forces the mandatory crossings AROUND the corridor
        #     and they collide at the tight U1 pad fan-in.
        #   * SOFT attractor-only reservation (``soft=True``, no keep-out):
        #     IDENTICAL 25/31, 3 shorts, 6 opens.  The attractor concentrates
        #     the reversed-byte nets onto one inner-layer channel that then
        #     fans into the same congested U1 pad box and shorts -- and costs
        #     a net of reach vs Path B.
        #   * Path B (this branch, Python-only, NO C++ honouring): 26/31,
        #     ZERO shorts, exactly the 5 seed-invariant #3438 opens.  Clean.
        #
        # So honouring this single-ended byte-lane corridor on C++ AT ALL
        # (hard OR soft) is topologically over-constraining for the reversed
        # byte -- the crossing must resolve at the pad fan-in, which no
        # corridor reservation can planarise.  Fixing that needs a different
        # approach (re-shaped/relaxed byte-lane geometry, or a coupled/pad-
        # aware fan-in planner) tracked for architect re-scoping on #4079.
        # The lateral keep-out machinery + the per-reservation HARD/SOFT flag
        # #4079 built are still shipped and USED: the #2677 pair-continuation
        # corridor (board 06, PLANAR) mirrors to C++ as a HARD reservation
        # and benefits from the new lateral-trace fence.
        count = self.grid.reserve_corridor_cells(
            layer_idx=target_idx,
            cells=cells,
            net_ids={net_id},
            soft=soft,
            mirror_to_cpp=mirror_to_cpp,
        )
        if count > 0:
            # Issue #4256: the SOFT single-ended default (#2983) and the new
            # HARD bundle-plan lanes (A3) track separate counters so the
            # flag-off identity tests keep asserting exactly 2 on the SOFT
            # counter while the HARD path exposes its own instrumentation.
            if soft:
                self.byte_lane_corridor_reservations += 1
                self.byte_lane_corridor_reserved_cells += count
            else:
                self.bundle_plan_corridor_reservations += 1
                self.bundle_plan_corridor_reserved_cells += count
            logger.debug(
                "Inner-corner lane corridor reserved (%s%s): "
                "layer=%s cells=%d net=%d pad=%s.%s launch=(%.2f,%.2f) "
                "length=%.2fmm half_width=%.2fmm",
                "soft" if soft else "HARD",
                ", C++-mirrored" if mirror_to_cpp else "",
                target_inner_layer.name,
                count,
                net_id,
                pad.ref,
                pad.pin,
                dx,
                dy,
                corridor_length,
                corridor_half_width,
            )
        return count

    def reserve_bundle_river_via_hop_corridor(
        self,
        pad: Pad,
        launch_dx: float,
        launch_dy: float,
        row_axis_dx: float,
        row_axis_dy: float,
        crossing_span: float,
        target_inner_layer: Layer | None = None,
    ) -> int:
        """Reserve an inner-layer via-hop corridor for one crossing net.

        Issue #4053 (Phase 3, epic #4049): the single-strip generalisation
        of ``reserve_inner_corner_lane_corridor``.  Where that helper
        protects a single inner-corner pad's *straight* continuation lane,
        this one reserves an inner-layer corridor for the "losing" net of
        an inverted (crossing) pair in a full/partial bus reversal — the
        net that must dip to an inner layer, travel LATERALLY across the
        escape strip to pass under its partner, then return.

        A bus reversal between two facing pin columns forces every net
        whose relative row order flips to cross its partner.  On a single
        shared F.Cu strip those crossings cannot be planarised by lane
        ordering (the conflict graph of a full reversal is complete), so
        v1 gives each losing net a reserved inner-layer channel that spans
        the crossing: forward along the launch vector AND laterally along
        the row axis by ``crossing_span``, so the reserved cells cover the
        under-pass the net needs.

        Same soft mechanic as the diff-pair / inner-corner corridors
        (``RoutingGrid.reserve_corridor_cells`` + the ``_mark_via`` per-cell
        skip + the A* attractor bonus): the cells are net-OWNED, not
        universally blocked, so partner-net vias detour while the crossing
        net is *attracted* onto the reserved inner layer.  This is the
        SOFT reservation the epic scoped — deliberately NOT a hard
        pre-routed via (``--preserve-existing`` measured counterproductive
        in #3414) and NOT the coupled-search primitive (#4052/#4065 proved
        it basin-floods).

        Args:
            pad: The losing net's pad (origin of the via hop).  Must have
                a non-zero ``pad.net``.
            launch_dx, launch_dy: Outward launch direction (perpendicular
                to the row, away from the package body) — same convention
                as ``reserve_inner_corner_lane_corridor``.  Normalised
                internally.
            row_axis_dx, row_axis_dy: Unit vector ALONG the row's long
                axis.  The lateral extent of the corridor is grown along
                this axis so the reserved channel covers the net's
                cross-strip travel toward its partner's lane.
            crossing_span: How far along the row axis (mm) the corridor
                must reach — sized by the caller to the row-position gap
                between the losing net and the partner it passes under.
            target_inner_layer: Inner copper layer.  Defaults to
                ``_select_inner_escape_layer(pad.layer)`` (In1.Cu on a
                4-layer signal stack; B.Cu fallback on a plane stack).

        Returns:
            Number of grid cells reserved (0 on any no-op: zero net id,
            zero launch vector, 2-layer stack, layer not in stack).
        """
        net_id = int(pad.net) if pad.net else 0
        if net_id == 0:
            return 0

        length_norm = math.hypot(launch_dx, launch_dy)
        if length_norm == 0:
            return 0
        dx = launch_dx / length_norm
        dy = launch_dy / length_norm

        # Normalise the row axis vector (defensive; caller passes a unit
        # vector but a zero vector collapses the lateral extent to the
        # single-pad case, which is still a valid — if minimal — hop).
        row_norm = math.hypot(row_axis_dx, row_axis_dy)
        if row_norm > 0:
            rax = row_axis_dx / row_norm
            ray = row_axis_dy / row_norm
        else:
            rax = ray = 0.0

        if target_inner_layer is None:
            target_inner_layer = self._select_inner_escape_layer(pad.layer)

        # Same 2-layer / layer-presence guards as the inner-corner helper:
        # on a 2-layer board there is no inner layer to hop to, and a B.Cu
        # reservation would starve partner escapes.
        if self.grid.layer_stack is not None:
            if self.grid.layer_stack.num_layers < 3:
                logger.debug("Bundle-river via-hop corridor skipped: 2-layer stack-up")
                return 0
            target_def = self.grid.layer_stack.get_layer_by_name(target_inner_layer.kicad_name)
            if target_def is None:
                logger.debug(
                    "Bundle-river via-hop corridor skipped: layer %s not in stack",
                    target_inner_layer.name,
                )
                return 0

        try:
            target_idx = self.grid.layer_to_index(target_inner_layer.value)
        except Exception:
            logger.debug(
                "Bundle-river via-hop corridor skipped: layer %s not in grid stack",
                target_inner_layer.name,
            )
            return 0

        trace_w = self._get_trace_width_for_net(pad.net_name or "")
        launch_step = self.escape_clearance + 2 * trace_w

        # Forward extent: a couple of launch steps to clear the pad's own
        # escape via before the lateral run begins.
        corridor_length = launch_step * 2.0
        # Lateral half-width: one launch step of protection around the
        # under-pass channel, matching the inner-corner recipe's
        # starvation-avoidance sizing (narrow, per-net, not a wide halo).
        corridor_half_width = launch_step

        # Enumerate cells: a rectangle that extends ``corridor_length``
        # forward along the launch vector AND spans ``crossing_span`` along
        # the row axis (the cross-strip under-pass), padded laterally by
        # ``corridor_half_width`` on the launch-perpendicular sides.
        cx, cy = pad.x, pad.y
        # Lateral (launch-perpendicular) unit vector.
        lat_dx, lat_dy = -dy, dx

        span = max(0.0, float(crossing_span))
        step = self.grid.resolution * 0.5
        cells: set[tuple[int, int]] = set()

        # Segment 1: forward launch stub (pad -> inner layer).
        t = 0.0
        while t <= corridor_length:
            u = -corridor_half_width
            while u <= corridor_half_width:
                wx = cx + dx * t + lat_dx * u
                wy = cy + dy * t + lat_dy * u
                gx, gy = self.grid.world_to_grid(wx, wy)
                cells.add((gx, gy))
                u += step
            t += step

        # Segment 2: lateral under-pass run along the row axis, offset
        # forward by the launch stub so it sits in the escape channel, not
        # on top of the pad row.  Reserved as a band ``2*corridor_half_width``
        # wide (perpendicular to the row axis) sliding ``span`` along it.
        base_x = cx + dx * corridor_length
        base_y = cy + dy * corridor_length
        s = 0.0
        while s <= span:
            w = -corridor_half_width
            while w <= corridor_half_width:
                # Perpendicular to the row axis within the strip plane is
                # the launch direction (dx, dy).
                wx = base_x + rax * s + dx * w
                wy = base_y + ray * s + dy * w
                gx, gy = self.grid.world_to_grid(wx, wy)
                cells.add((gx, gy))
                w += step
            s += step

        if not cells:
            return 0

        # Issue #4079: this #4053 bundle-river via-hop reservation stays
        # Python-only (``mirror_to_cpp=False``, PR #4078's "Path B"), for the
        # same MEASURED reason as the #2983 inner-corner sibling above -- it
        # too sits on the fully-reversed DDR byte, whose crossings are
        # mandatory by construction.  #4079 verified on the CI recipe that
        # honouring this corridor on C++ regresses board 07 into the
        # DM0<->DQ7 short cluster whether the keep-out is HARD (fence) or
        # SOFT (attractor-only): the crossing must resolve at the tight U1
        # pad fan-in, which no corridor reservation can planarise.  Path B
        # (Python-only) is clean: 26/31, 0 shorts, exactly the 5 known opens.
        # (This planner is also OFF by default via
        # ``enable_bundle_river_planner``.)  The lateral keep-out machinery
        # and the per-reservation HARD/SOFT flag #4079 built still ship and
        # are used by the #2677 pair-continuation corridor (board 06, PLANAR)
        # which mirrors to C++ as a HARD reservation.
        count = self.grid.reserve_corridor_cells(
            layer_idx=target_idx,
            cells=cells,
            net_ids={net_id},
            soft=True,
            mirror_to_cpp=False,
        )
        if count > 0:
            self.byte_lane_corridor_reservations += 1
            self.byte_lane_corridor_reserved_cells += count
            logger.debug(
                "Bundle-river via-hop corridor reserved: layer=%s cells=%d "
                "net=%d pad=%s.%s launch=(%.2f,%.2f) span=%.2fmm",
                target_inner_layer.name,
                count,
                net_id,
                pad.ref,
                pad.pin,
                dx,
                dy,
                span,
            )
        return count

    def _escape_bga_rings(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate ring-based escape routes for BGA packages.

        Outer ring pins escape horizontally/vertically on top layer.
        Inner ring pins drop via and escape on inner layer.
        Pattern alternates layers for each ring.

        Args:
            package: BGA package info

        Returns:
            List of escape routes, outer ring first
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Issue #3419: Skip pads that belong to skipped/plane nets (net=0).
        # Plane nets (GND, VCC, etc.) are stitched via planes, not routed
        # via escapes.  Generating escapes for them wastes the BGA perimeter
        # channel that the signal nets need -- mirrors the equivalent filter
        # in ``_escape_qfp_alternating`` (Issue #2513).
        routable_pads = [p for p in package.pads if p.net != 0]

        # Group pads by ring (distance from center)
        rings = self._group_pads_by_ring(routable_pads, center_x, center_y)

        for ring_idx, ring_pads in enumerate(rings):
            # Alternate layers: even rings on F.Cu, odd on B.Cu
            escape_layer = Layer.F_CU if ring_idx % 2 == 0 else Layer.B_CU
            needs_via = ring_idx > 0  # Outer ring stays on top

            for pad in ring_pads:
                escape = self._create_ring_escape(
                    pad=pad,
                    center=(center_x, center_y),
                    ring_idx=ring_idx,
                    escape_layer=escape_layer,
                    needs_via=needs_via,
                    package=package,
                )
                escapes.append(escape)

        return escapes

    def _group_pads_by_ring(
        self,
        pads: list[Pad],
        center_x: float,
        center_y: float,
    ) -> list[list[Pad]]:
        """Group pads into concentric rings based on distance from center.

        Args:
            pads: All pads of the package
            center_x: Package center X
            center_y: Package center Y

        Returns:
            List of rings, each containing pads at that distance
        """
        if not pads:
            return []

        # Calculate distance from center for each pad
        pad_distances: list[tuple[Pad, float]] = []
        for pad in pads:
            dist = math.sqrt((pad.x - center_x) ** 2 + (pad.y - center_y) ** 2)
            pad_distances.append((pad, dist))

        # Sort by distance
        pad_distances.sort(key=lambda x: x[1], reverse=True)

        # Group into rings by distance (allow some tolerance for grid irregularity)
        rings: list[list[Pad]] = []
        pitch = self.rules.trace_width * 3  # Approximate ring separation

        current_ring: list[Pad] = []
        current_dist = pad_distances[0][1] if pad_distances else 0

        for pad, dist in pad_distances:
            if current_dist - dist > pitch:
                if current_ring:
                    rings.append(current_ring)
                current_ring = [pad]
                current_dist = dist
            else:
                current_ring.append(pad)

        if current_ring:
            rings.append(current_ring)

        return rings

    def _create_ring_escape(
        self,
        pad: Pad,
        center: tuple[float, float],
        ring_idx: int,
        escape_layer: Layer,
        needs_via: bool,
        package: PackageInfo,
    ) -> EscapeRoute:
        """Create an escape route for a pad in a ring.

        Args:
            pad: The pad to escape
            center: Package center
            ring_idx: Which ring this pad is in (0=outer)
            escape_layer: Layer to escape to
            needs_via: Whether a via is needed
            package: Package info for bounds

        Returns:
            EscapeRoute for this pad
        """
        center_x, center_y = center

        # Determine escape direction based on quadrant
        direction = self._get_quadrant_direction(pad.x, pad.y, center_x, center_y)

        # Calculate escape point (beyond package edge + clearance)
        dx, dy = self._direction_to_vector(direction)
        min_x, min_y, max_x, max_y = package.bounding_box

        # Find distance to edge in this direction
        if dx > 0:
            edge_dist = max_x - pad.x + self.escape_clearance
        elif dx < 0:
            edge_dist = pad.x - min_x + self.escape_clearance
        else:
            edge_dist = 0

        if dy > 0:
            edge_dist = max(edge_dist, max_y - pad.y + self.escape_clearance)
        elif dy < 0:
            edge_dist = max(edge_dist, pad.y - min_y + self.escape_clearance)

        escape_x = pad.x + dx * edge_dist
        escape_y = pad.y + dy * edge_dist

        # Create segments and via if needed
        segments: list[Segment] = []
        via: Via | None = None
        via_pos: tuple[float, float] | None = None

        if needs_via:
            # Place via offset from pad
            via_offset = self.via_spacing
            via_x = pad.x + dx * via_offset
            via_y = pad.y + dy * via_offset
            via_pos = (via_x, via_y)

            # Short segment from pad to via
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            # Create via
            via = Via(
                x=via_x,
                y=via_y,
                drill=self.rules.via_drill,
                diameter=self.rules.via_diameter,
                layers=(pad.layer, escape_layer),
                net=pad.net,
                net_name=pad.net_name,
            )

            # Segment from via to escape point on escape layer
            segments.append(
                Segment(
                    x1=via_x,
                    y1=via_y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=escape_layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )
        else:
            # Direct escape on same layer
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

        return EscapeRoute(
            pad=pad,
            direction=direction,
            escape_point=(escape_x, escape_y),
            escape_layer=escape_layer,
            via_pos=via_pos,
            segments=segments,
            via=via,
            ring_index=ring_idx,
        )

    def _escape_qfp_alternating(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate alternating direction escapes for QFP/QFN packages.

        Even-indexed pins escape perpendicular (outward).
        Odd-indexed pins escape parallel (along edge), alternating left/right.

        Args:
            package: QFP/QFN package info

        Returns:
            List of escape routes
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center
        min_x, min_y, max_x, max_y = package.bounding_box

        # Group pads by edge
        north_pads: list[Pad] = []
        south_pads: list[Pad] = []
        east_pads: list[Pad] = []
        west_pads: list[Pad] = []

        edge_margin = min(max_x - min_x, max_y - min_y) * 0.2

        for pad in package.pads:
            # Skip center pad (thermal pad)
            if abs(pad.x - center_x) < edge_margin and abs(pad.y - center_y) < edge_margin:
                continue

            # Issue #2513: Skip pads that belong to skipped/plane nets (net=0).
            # Plane nets (GND, VCC, etc.) are stitched via planes, not routed
            # via escapes.  Generating escapes for them wastes perimeter
            # routing space (a TQFP-32 MCU may have 19/32 pins on plane nets;
            # without this filter the escape phase blocks the perimeter for
            # the actual signal nets that need to escape).
            if pad.net == 0:
                continue

            if abs(pad.y - max_y) < edge_margin:
                north_pads.append(pad)
            elif abs(pad.y - min_y) < edge_margin:
                south_pads.append(pad)
            elif abs(pad.x - max_x) < edge_margin:
                east_pads.append(pad)
            elif abs(pad.x - min_x) < edge_margin:
                west_pads.append(pad)

        # Sort each edge by position
        north_pads.sort(key=lambda p: p.x)
        south_pads.sort(key=lambda p: p.x)
        east_pads.sort(key=lambda p: p.y)
        west_pads.sort(key=lambda p: p.y)

        # Issue #2513: For lower-density QFP/TQFP (pitch >= 0.65 mm) the
        # alternating perpendicular/parallel scheme blocks more perimeter
        # space than it saves -- a TQFP-32 at 0.8 mm pitch has plenty of
        # room between pins to fit a 0.2 mm trace with 0.15 mm clearance,
        # so every pin can escape perpendicular and the parallel arms of
        # the alternating pattern just consume routing real-estate.  Use
        # the simpler perpendicular-only escape for these packages and
        # reserve the alternating pattern for true fine-pitch QFP/QFN.
        use_perpendicular_only = package.pin_pitch >= 0.65

        # Issue #2695: For fine-pitch QFP/LQFP/TQFP at 0.5mm pitch and finer,
        # the alternating scheme still cannot fit a 0.2mm trace + 0.15mm
        # clearance between adjacent pads.  Inner pins fail surface escape
        # and have historically been deferred to the main router, where they
        # remain unrouted because the package perimeter is fully blocked.
        # When the manufacturer supports via-in-pad processing (e.g.
        # jlcpcb-tier1, pcbway), we fall back to ``_try_in_pad_escape`` --
        # the same strategy PR #2608 introduced for SSOP/TSSOP.  Plain
        # ``jlcpcb`` and unknown manufacturers continue to defer (no silent
        # surcharge for users who did not opt into via-in-pad).
        #
        # Issue #3183: when the opt-in
        # ``extended_pitch_in_pad_fallback`` flag is set, the gate is
        # raised to ``pin_pitch <= 0.8`` so 0.65-0.8 mm-pitch packages
        # (TQFP-32 at 0.8 mm, TQFP-48 at 0.5 mm, etc.) can also fall back
        # to the in-pad escape when their surface escape would clip an
        # adjacent foreign-net pad's clearance.  Without this, board-03's
        # TQFP-32 inner-row signal pins (USB_CC1, BTN1, BTN4) end up
        # routed through the 0.3 mm inter-pad channel at <0.127 mm
        # clearance -- a footprint-pitch geometric ceiling that no router
        # tuning can resolve.  Default off so all existing callers see
        # the original gate exactly.
        try_in_pad_fallback = (
            (package.pin_pitch <= 0.55)
            or (self.extended_pitch_in_pad_fallback and package.pin_pitch <= 0.8)
        ) and self.via_in_pad_supported

        # Issue #2881: Track whether this package is a "would-have-rescued"
        # candidate -- fine-pitch enough to need via-in-pad rescue, but the
        # manufacturer doesn't support it.  This flag drives the
        # ``missed_via_in_pad_rescues`` counter increment inside the per-pad
        # loop when surface escapes would have been blocked by neighbour
        # clearance.  The counter is consumed by ``--auto-mfr-tier`` to
        # decide whether escalating to a via-in-pad-capable tier would
        # help.
        #
        # Issue #3183: when the opt-in extended-pitch flag is set we widen
        # the "would-have-rescued" pitch window to match the widened gate
        # above (0.8 mm).  This keeps ``--auto-mfr-tier`` honest for boards
        # that opted into the extended fallback but landed on a tier-0
        # manufacturer profile: the counter still fires so the escalation
        # loop can surface the upgrade.
        in_pad_pitch_ceiling = 0.8 if self.extended_pitch_in_pad_fallback else 0.55
        wants_in_pad_but_unavailable = (
            package.pin_pitch <= in_pad_pitch_ceiling and not self.via_in_pad_supported
        )

        # Effective clearance and escape width for the in-pad rescue
        # fallback.  We mirror the values used inside
        # ``_create_fine_pitch_row_escapes`` so the in-pad routes are
        # geometrically consistent regardless of which dispatcher created
        # them.
        ref = package.ref
        effective_clearance = self.rules.get_clearance_for_component(
            ref,
            pin_pitch=package.pin_pitch,
        )
        escape_width = (
            self.rules.min_trace_width
            if self.rules.min_trace_width is not None
            else self._get_trace_width_for_net(package.pads[0].net_name if package.pads else "")
        )

        skipped_clearance = 0

        # Generate escapes for each edge
        for pads, primary_dir, alt_dir_cw, alt_dir_ccw in [
            (north_pads, EscapeDirection.NORTH, EscapeDirection.EAST, EscapeDirection.WEST),
            (south_pads, EscapeDirection.SOUTH, EscapeDirection.WEST, EscapeDirection.EAST),
            (east_pads, EscapeDirection.EAST, EscapeDirection.SOUTH, EscapeDirection.NORTH),
            (west_pads, EscapeDirection.WEST, EscapeDirection.NORTH, EscapeDirection.SOUTH),
        ]:
            for i, pad in enumerate(pads):
                if use_perpendicular_only or i % 2 == 0:
                    direction = primary_dir
                else:
                    direction = alt_dir_cw if (i // 2) % 2 == 0 else alt_dir_ccw

                # Issue #2756: generate the unclipped escape first so we
                # can detect the pre-#2756 violation condition and route
                # it through the in-pad fallback when supported.  The
                # in-pad fallback rescues pins that would otherwise be
                # blocked at the launch step; without this ordering, the
                # clipped escape would mask the violation from the
                # ``_segment_violates_pad_clearance`` check and the
                # in-pad rescue would never trigger (regression of
                # Issue #2695).
                unclipped_escape = self._create_alternating_escape(
                    pad=pad,
                    direction=direction,
                    package=package,
                    pad_clearance_margin=None,
                )

                # Issue #2695: For fine-pitch QFP packages on capable
                # manufacturers, replace the surface escape with an
                # in-pad via escape when the surface segment violates
                # clearance against neighbouring pads on the same edge.
                # The alternating scheme alone cannot fit traces between
                # 0.5mm-pitch pads, so without this rescue inner pins
                # never reach the main router successfully.
                #
                # Issue #2880: Additionally, force the in-pad rescue when
                # a fine-pitch signal pin is sandwiched between two
                # same-component plane-net pads on its immediate same-edge
                # neighbour positions AND its escape direction is along
                # the edge (alternating-direction odd-indexed pin).  For
                # plane-sandwiched pins escaping PERPENDICULAR to the
                # edge the surface escape is geometrically clean (it
                # exits the package immediately and does not cross any
                # same-edge pads), so the in-pad rescue is unnecessary
                # cost; we only force the rescue when the dispatcher
                # would otherwise emit an along-edge segment that would
                # have to thread between same-component plane pads.
                # The row-level violation check can miss this case when
                # the unclipped escape segment is short enough to stop
                # before reaching the next plane pad, but at 0.5 mm
                # LQFP pitch + jlcpcb-tier1 0.127 mm clearance the
                # channel between plane pads is geometrically too narrow
                # (0.2 mm available, 0.381 mm required) -- the only
                # viable along-edge escape is vertical via-in-pad.
                escape_is_along_edge = direction != primary_dir
                pin_boxed = escape_is_along_edge and self._is_pin_boxed_by_plane_neighbours(
                    pad,
                    package,
                )
                if try_in_pad_fallback and unclipped_escape.segments:
                    surface_seg = unclipped_escape.segments[0]
                    violation = self._segment_violates_pad_clearance(
                        surface_seg,
                        i,
                        pads,
                        effective_clearance,
                        # Issue #2755: Also check against pads on the OTHER
                        # edges of this QFP plus plane-net pads (net==0)
                        # that were filtered out of ``pads`` above.
                        extra_pads=self._other_footprint_pads(package, pads),
                    )

                    # Issue #3428 (POCKET-ESCAPE rescue): a clean surface
                    # escape is not always a USEFUL surface escape.  On
                    # board 04, U2.7 NRST's perpendicular WEST escape is
                    # geometrically clean -- but its routing target
                    # (J1.5) lies EAST across the package, and the only
                    # west-side exit corridor is already consumed by
                    # OSC_IN's escape via after U2.6 OSC_OUT claimed its
                    # in-pad via.  The result is a dead-end pocket and a
                    # stranded net no amount of rip-up can recover.
                    #
                    # Trigger conditions (ALL must hold; deliberately
                    # narrow so every other board keeps byte-identical
                    # escapes):
                    #   1. The standard rescue triggers (violation /
                    #      pin_boxed) did NOT fire,
                    #   2. the net is not a pour net (plane nets connect
                    #      via zone fill, never via escape stubs),
                    #   3. an immediately adjacent same-package pin
                    #      already claimed an in-pad via in this pass
                    #      (the contested-channel signal),
                    #   4. the net's snapped target direction points
                    #      AWAY from the parity-derived escape direction
                    #      (negative dot product -- the escape would
                    #      walk away from the target into the pocket).
                    # When they hold, rescue this pin too with an in-pad
                    # via whose inner stub points AT the target --
                    # under-package inner-layer space is open for SMD
                    # packages, so ``allow_into_package=True``.
                    pocket_target: EscapeDirection | None = None
                    if not violation and not pin_boxed and pad.net != 0:
                        nc = self.net_class_map.get(pad.net_name)
                        is_pour = nc is not None and getattr(nc, "is_pour_net", False)
                        if not is_pour:
                            raw_target = self._compute_target_direction(
                                pad=pad,
                                package=package,
                                primary_dir=primary_dir,
                                allow_into_package=True,
                            )
                            if raw_target is not None:
                                tvx, tvy = self._direction_to_vector(raw_target)
                                dvx, dvy = self._direction_to_vector(direction)
                                if tvx * dvx + tvy * dvy < 0 and self._neighbour_claimed_in_pad_via(
                                    pad,
                                    package,
                                    escapes,
                                ):
                                    pocket_target = raw_target

                    if violation or pin_boxed or pocket_target is not None:
                        # Issue #3033 / #3062: forward the EscapeRouter-level
                        # strict flag so the dispatcher inherits the "defer
                        # rather than commit a violating via" policy.
                        # Defaults to False on the EscapeRouter constructor,
                        # preserving legacy behaviour exactly for every
                        # existing caller.
                        #
                        # Issue #3428: point the rescue's inner stub at the
                        # net's actual routing target (nearest off-package
                        # same-net pad, snapped to a cardinal) instead of
                        # the parity-derived along-edge direction.  On
                        # board 04 the parity direction for U2.6 OSC_OUT
                        # was NORTH (+y, toward U2.7 NRST); the 0.5mm
                        # stub then blocked NRST's only escape-via slot.
                        # Returns None (-> legacy direction) when no
                        # net-position map was wired in, the net has no
                        # off-package pads, or the target points into the
                        # package body.
                        in_pad_route = self._try_in_pad_escape(
                            pad=pad,
                            direction=direction,
                            effective_clearance=effective_clearance,
                            escape_width=escape_width,
                            package=package,
                            skip_on_clearance_violation=self.strict_in_pad_clearance,
                            target_direction=(
                                pocket_target
                                if pocket_target is not None
                                else self._compute_target_direction(
                                    pad=pad,
                                    package=package,
                                    primary_dir=primary_dir,
                                )
                            ),
                            # Issue #3470: let the stub-direction picker
                            # avoid copper conflicts with the escapes
                            # generated earlier in this pass.
                            existing_escapes=escapes,
                            # Issue #3429: enforce via-barrel spacing
                            # against sibling in-pad vias for the LEGACY
                            # violation / pin_boxed rescues (which have a
                            # lateral / surface fallback when refused).
                            # The #3428 pocket-escape rescue is exempt --
                            # its divergent target-aware stubs are designed
                            # to let two adjacent in-pad vias coexist, and
                            # it has no lateral fallback.
                            enforce_adjacent_via_spacing=(pocket_target is None),
                        )
                        if in_pad_route is not None:
                            if pocket_target is not None:
                                logger.info(
                                    "POCKET-ESCAPE in-pad rescue for %s pin %s "
                                    "(net %s): clean surface escape pointed "
                                    "AWAY from the net target while an "
                                    "adjacent pin holds an in-pad via; "
                                    "rescued with target-aware stub dir=%s "
                                    "(Issue #3428).",
                                    package.ref,
                                    pad.pin,
                                    pad.net_name,
                                    pocket_target.name,
                                )
                            if pin_boxed and not violation:
                                logger.info(
                                    "In-pad rescue forced for %s pin %s "
                                    "(net %s): boxed between same-component "
                                    "plane-net neighbours on %s edge "
                                    "(Issue #2880).",
                                    package.ref,
                                    pad.pin,
                                    pad.net_name,
                                    package.package_type.name,
                                )
                            escapes.append(in_pad_route)
                            continue

                        # Issue #3063 (sub-B of #3048): when the in-pad
                        # rescue deferred (returned None because the dead-
                        # centre via would clip a foreign neighbour and the
                        # long-axis nudge cannot rescue), try the lateral
                        # re-attempt before giving up.
                        # The lateral helper probes off-pad via candidates
                        # along ``primary_dir`` (perpendicular outward from
                        # the chip body) -- NOT ``direction``, which for
                        # odd-index pins is the along-edge alt_dir_cw/ccw
                        # and would search into the next pin in the row.
                        # The outward direction is the only one with
                        # consistent room for an off-pad via because the
                        # row's pin pitch is geometrically fixed and the
                        # outward half-plane is open by construction.
                        # Issue #3080: gate removed -- the lateral helper
                        # is invoked on BOTH the strict and non-strict
                        # paths now.  Board 04's stranded U2.8 GND stitch
                        # window (#3075) needed the PR #3079 surface-stub
                        # necking to fit through the 0.2mm channel between
                        # neighbour pads, but the necking only ran when
                        # the strict-mode branch invoked the lateral
                        # helper.  Removing the gate is strictly additive:
                        # if the in-pad rescue already succeeded (route is
                        # not None), the `continue` above means we never
                        # reach this point, so non-strict callers whose
                        # in-pad rescue currently succeeds are unaffected.
                        # If the in-pad rescue returned None (the only way
                        # to reach this branch), the lateral helper is
                        # the same "off-pad via plus possibly-necked stub"
                        # the strict-mode path was already using -- still
                        # falls back to "defer to main router" when it
                        # returns None.
                        #
                        # Issue #3428: the lateral attempt is gated to the
                        # LEGACY triggers (violation / pin_boxed).  When
                        # only the pocket-escape trigger fired and the
                        # in-pad rescue could not place a via, the pin's
                        # surface escape is geometrically clean -- fall
                        # through to it unchanged rather than introducing
                        # an off-pad via that the pre-#3428 code would
                        # never have placed.
                        if violation or pin_boxed:
                            lateral_route = self._try_lateral_via_escape(
                                pad=pad,
                                direction=primary_dir,
                                effective_clearance=effective_clearance,
                                escape_width=escape_width,
                                package=package,
                                # Issue #3430: forward the escapes placed so
                                # far this pass so the off-pad via candidate
                                # is validated against sibling escape vias
                                # (in-pad OR lateral), not just footprint
                                # pads.  Without this the lateral fallback
                                # could relocate the very commit-time drop
                                # #3429's detection was meant to prevent.
                                existing_escapes=escapes,
                            )
                            if lateral_route is not None:
                                # Issue #3430: observe the auto-lateral-via
                                # FALLBACK.  ``adjacent_in_pad_via_conflicts_refused``
                                # (#3429) counts the refused in-pad rescue;
                                # this counts the successful offset escape
                                # that rescued the same pin.  When the in-pad
                                # via was refused for a barrel conflict, this
                                # is the recovery that keeps board 04 at 9/9.
                                self.forced_lateral_via_fallbacks += 1
                                logger.info(
                                    "AUTO-LATERAL-VIA fallback for %s pin %s "
                                    "(net %s): in-pad rescue refused (sibling "
                                    "via barrel conflict / strict clearance); "
                                    "escaped via off-pad lateral via instead "
                                    "of deferring the pin to the main router "
                                    "(Issue #3430, follow-on to #3429).",
                                    package.ref,
                                    pad.pin,
                                    pad.net_name,
                                )
                                escapes.append(lateral_route)
                                continue

                # Issue #2881: Missed-rescue detection.  When the package is
                # fine-pitch enough to need via-in-pad rescue but the
                # manufacturer doesn't support it, AND the unclipped surface
                # escape would have violated neighbour-pad clearance,
                # increment the missed-rescue counter so ``--auto-mfr-tier``
                # can see that switching to a via-in-pad-capable manufacturer
                # would help.  Note: we do this BEFORE the clearance-clip
                # short-segment skip below, because both the "clipped to
                # nothing" and "clipped but stub kept" cases are equally
                # rescue-able by an in-pad via.
                if wants_in_pad_but_unavailable and unclipped_escape.segments:
                    surface_seg = unclipped_escape.segments[0]
                    if self._segment_violates_pad_clearance(
                        surface_seg,
                        i,
                        pads,
                        effective_clearance,
                        extra_pads=self._other_footprint_pads(package, pads),
                    ):
                        self.missed_via_in_pad_rescues += 1
                        if package.ref:
                            self.missed_via_in_pad_components.add(package.ref)

                # Issue #2880: If the pin is boxed by same-component plane
                # neighbours AND its dispatcher direction is along-edge,
                # but via-in-pad is unavailable, no surface escape can
                # satisfy the clearance constraints at this pitch.  Emit
                # a clear error pointing at the unfixable constraint
                # rather than producing a route that DRC will later
                # reject.  (The ``pin_boxed`` flag above already gates on
                # along-edge direction.)
                #
                # Issue #2891: when ``--auto-mfr-tier`` is escalating, demote
                # the ERROR to DEBUG -- the outer wrapper recovers by walking
                # forward to a tier that supports via-in-pad, so the inner
                # message is a false alarm from the user's perspective.  The
                # wrapper is responsible for clearing the flag before the
                # FINAL tier attempt so a fully-exhausted ladder still
                # surfaces the diagnostic.
                if pin_boxed and not self.via_in_pad_supported:
                    mfr_label = self.manufacturer or "<unknown manufacturer>"
                    msg = (
                        "Cannot escape %s pin %s (net %s) to perimeter "
                        "without violating clearance against same-component "
                        "plane-net pads at %.2fmm %s pitch. Manufacturer "
                        "profile %s does not support via-in-pad. "
                        "Resolution options: (a) switch to a manufacturer "
                        "profile that supports via-in-pad "
                        "(e.g. jlcpcb-tier1, pcbway), "
                        "(b) re-route on a 4-layer stackup with inner-layer "
                        "escape, (c) increase pin pitch. (Issue #2880)"
                    )
                    msg_args = (
                        package.ref,
                        pad.pin,
                        pad.net_name,
                        package.pin_pitch,
                        package.package_type.name,
                        mfr_label,
                    )
                    # Issue #2891: demote during in-flight tier escalation.
                    # Keep the wording identical so log forensics still
                    # locate the diagnostic via grep.
                    if getattr(self.rules, "auto_mfr_tier_in_progress", False):
                        logger.debug(msg, *msg_args)
                    else:
                        logger.error(msg, *msg_args)

                # Issue #2756: clip the segment endpoint against
                # neighbour-pad clearance.  When the manufacturer does
                # not support in-pad rescue (the common JLCPCB case) the
                # clipped segment is the right answer -- it stops short
                # of the violating pad and the main router picks up the
                # net cleanly from the safe endpoint.
                escape = self._create_alternating_escape(
                    pad=pad,
                    direction=direction,
                    package=package,
                    pad_clearance_margin=effective_clearance,
                )

                # Issue #2756: if the clipped segment is too short to be
                # useful (heuristic: less than half the original launch
                # distance), defer to the main router rather than
                # emitting a stub that does not meaningfully exit the
                # pin row.  Half the launch distance is the threshold
                # used by the diff-pair coupling path
                # (_escape_diff_pair_segment) and matches the failure
                # mode the curator identified: violating odd-pin
                # parallel-along-the-edge escapes get clipped to ~0
                # while perpendicular even-pin escapes retain most of
                # their original launch length.
                original_launch = self.escape_clearance + self.rules.trace_width * 2
                min_useful_length = original_launch * 0.5
                if escape.segments:
                    seg = escape.segments[0]
                    seg_len = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
                    if seg_len < min_useful_length:
                        skipped_clearance += 1
                        logger.debug(
                            "Escape for %s pin %s skipped: pad-clearance "
                            "clip produced segment of %.3fmm "
                            "(< %.3fmm threshold)",
                            pad.net_name,
                            pad.pin,
                            seg_len,
                            min_useful_length,
                        )
                        continue

                escapes.append(escape)

        if skipped_clearance:
            logger.info(
                "Escape routing for %s (%s): %d pins deferred to main "
                "router due to pad-clearance clip (Issue #2756)",
                package.ref,
                package.package_type.name,
                skipped_clearance,
            )

        return escapes

    def generate_in_pad_rescues_only(
        self,
        package: PackageInfo,
        pin_filter: list[str] | None = None,
    ) -> list[EscapeRoute]:
        """Generate in-pad-only escape rescues for pins on a dense QFP/QFN/TQFP package.

        Issue #3183: A surgical variant of ``_escape_qfp_alternating`` that
        emits *only* the in-pad via rescue for pins whose perpendicular
        surface escape would violate clearance against an adjacent
        same-edge pad, and skips every other pin (no surface escape
        stub).  This is the "contained-blast-radius" companion to the
        full QFP escape pre-pass: the main per-net router still picks up
        the bulk of the package's pins from their original pad
        locations, and only the violating pins get an in-pad via that
        moves their escape onto the inner / back layer.

        For board 03's TQFP-32 at 0.8 mm pitch, this rescues the inner
        signal pins (USB_CC1 / BTN1 / BTN4) whose surface escape would
        otherwise clip an adjacent pin's clearance, while leaving the
        other 12 signal pins untouched so the existing 15/16 reach in
        ``route_all`` is preserved.

        Pre-conditions:
        - ``self.via_in_pad_supported`` must be True (capability gate;
          tier-0 jlcpcb returns an empty list).
        - The opt-in ``extended_pitch_in_pad_fallback`` flag controls
          whether 0.65-0.8 mm packages participate (in addition to the
          original <= 0.55 mm fine-pitch band).  When False, this method
          behaves identically to ``_escape_qfp_alternating``'s in-pad
          rescue branch on the existing 0.55 mm gate.

        Args:
            package: QFP/QFN/TQFP package info from ``analyze_package``.
            pin_filter: Optional explicit list of pin identifiers (e.g.
                ``["13", "14", "26"]``) to restrict the rescue to.  When
                ``None`` (default), the adjacent-signal-neighbour
                predicate is used to auto-select pins.  When provided,
                only the listed pins are rescued (still subject to the
                capability + pitch gates above).  Useful when the
                routing topology is known a priori (e.g. board 03's
                BTN2/BTN3/USB_CC2 pins on U1) and the heuristic would
                otherwise rescue too many or too few pins.

        Returns:
            List of ``EscapeRoute`` objects for the rescued pins only.
            Empty when the manufacturer doesn't support via-in-pad, the
            package pitch is outside the gate, or no pin would have
            violated surface clearance.
        """
        # Capability gate
        if not self.via_in_pad_supported:
            return []

        # Pitch gate (mirrors the gate in _escape_qfp_alternating,
        # extended to 0.8mm via the opt-in flag).
        in_pad_pitch_ceiling = 0.55
        if self.extended_pitch_in_pad_fallback:
            in_pad_pitch_ceiling = 0.8
        if package.pin_pitch > in_pad_pitch_ceiling:
            return []

        rescues: list[EscapeRoute] = []
        center_x, center_y = package.center
        min_x, min_y, max_x, max_y = package.bounding_box

        # Group pads by edge, mirroring _escape_qfp_alternating
        north_pads: list[Pad] = []
        south_pads: list[Pad] = []
        east_pads: list[Pad] = []
        west_pads: list[Pad] = []

        edge_margin = min(max_x - min_x, max_y - min_y) * 0.2

        for pad in package.pads:
            # Skip thermal pad (center)
            if abs(pad.x - center_x) < edge_margin and abs(pad.y - center_y) < edge_margin:
                continue
            # Skip plane-net pads -- they don't escape, they connect via plane stitching
            if pad.net == 0:
                continue

            if abs(pad.y - max_y) < edge_margin:
                north_pads.append(pad)
            elif abs(pad.y - min_y) < edge_margin:
                south_pads.append(pad)
            elif abs(pad.x - max_x) < edge_margin:
                east_pads.append(pad)
            elif abs(pad.x - min_x) < edge_margin:
                west_pads.append(pad)

        # Sort each edge by position
        north_pads.sort(key=lambda p: p.x)
        south_pads.sort(key=lambda p: p.x)
        east_pads.sort(key=lambda p: p.y)
        west_pads.sort(key=lambda p: p.y)

        # Mirror _escape_qfp_alternating's "use_perpendicular_only" rule:
        # pitch >= 0.65 uses perpendicular-only (so direction = primary_dir
        # for every pin); below that, alternating odd/even.
        use_perpendicular_only = package.pin_pitch >= 0.65

        effective_clearance = self.rules.get_clearance_for_component(
            package.ref,
            pin_pitch=package.pin_pitch,
        )
        escape_width = (
            self.rules.min_trace_width
            if self.rules.min_trace_width is not None
            else self._get_trace_width_for_net(package.pads[0].net_name if package.pads else "")
        )

        # Issue #3183: heuristic for "needs in-pad rescue".  The
        # underlying failure mode is the per-net A* laying down a trace
        # through the inter-pad channel (width 0.3 mm on a 0.8 mm-pitch
        # TQFP with 0.5 mm pads) at sub-clearance spacing.  This happens
        # to inner-row signal pins whose neighbour on the same edge is
        # ALSO a signal pin (so both pins need lateral channel access),
        # but NOT to pins whose neighbour is a plane pad (the plane
        # carries no per-net trace, so its half of the channel is
        # available).  Rescue exactly those pins: emit the via-in-pad
        # so the signal escapes vertically, freeing the lateral channel.
        #
        # Corner pins (neighbour off the edge) and pins next to a plane
        # neighbour can use the standard surface escape on F.Cu without
        # violating clearance, so they are skipped to keep the rescue
        # blast radius minimal.
        for pads, primary_dir, alt_dir_cw, alt_dir_ccw in [
            (north_pads, EscapeDirection.NORTH, EscapeDirection.EAST, EscapeDirection.WEST),
            (south_pads, EscapeDirection.SOUTH, EscapeDirection.WEST, EscapeDirection.EAST),
            (east_pads, EscapeDirection.EAST, EscapeDirection.SOUTH, EscapeDirection.NORTH),
            (west_pads, EscapeDirection.WEST, EscapeDirection.NORTH, EscapeDirection.SOUTH),
        ]:
            # Build a same-edge index map that includes plane-net pads
            # so the "adjacent signal pin" predicate can see them.  The
            # ``pads`` list filtered them out above; we re-derive here
            # from package.pads using the same edge bucketing.
            edge_with_plane: list[Pad] = []
            for p in package.pads:
                if abs(p.x - center_x) < edge_margin and abs(p.y - center_y) < edge_margin:
                    continue  # thermal pad
                # Same edge as the loop's primary direction?
                if (
                    primary_dir == EscapeDirection.NORTH
                    and abs(p.y - max_y) < edge_margin
                    or primary_dir == EscapeDirection.SOUTH
                    and abs(p.y - min_y) < edge_margin
                    or primary_dir == EscapeDirection.EAST
                    and abs(p.x - max_x) < edge_margin
                    or primary_dir == EscapeDirection.WEST
                    and abs(p.x - min_x) < edge_margin
                ):
                    edge_with_plane.append(p)
            if primary_dir in (EscapeDirection.NORTH, EscapeDirection.SOUTH):
                edge_with_plane.sort(key=lambda p: p.x)
            else:
                edge_with_plane.sort(key=lambda p: p.y)

            for i, pad in enumerate(pads):
                if use_perpendicular_only or i % 2 == 0:
                    direction = primary_dir
                else:
                    direction = alt_dir_cw if (i // 2) % 2 == 0 else alt_dir_ccw

                if pin_filter is not None:
                    if pad.pin not in pin_filter:
                        continue
                else:
                    # Find this pad's position in the edge_with_plane
                    # list so we can identify its IMMEDIATE neighbours
                    # (including plane pads).  Match by ref+pin.
                    pad_key = (pad.ref, pad.pin)
                    neighbour_signal = False
                    for idx, p in enumerate(edge_with_plane):
                        if (p.ref, p.pin) != pad_key:
                            continue
                        if idx > 0 and edge_with_plane[idx - 1].net != 0:
                            neighbour_signal = True
                        if idx < len(edge_with_plane) - 1 and edge_with_plane[idx + 1].net != 0:
                            neighbour_signal = True
                        break

                    if not neighbour_signal:
                        # Pad has no same-edge signal neighbour (it's a
                        # corner, or sandwiched between plane pads).
                        # The surface escape can use the cleared
                        # channel, so skip the rescue.
                        continue

                # Emit the in-pad rescue.  Tier-0 callers never reach
                # here (via_in_pad_supported gate above).
                in_pad_route = self._try_in_pad_escape(
                    pad=pad,
                    direction=direction,
                    effective_clearance=effective_clearance,
                    escape_width=escape_width,
                    package=package,
                    skip_on_clearance_violation=self.strict_in_pad_clearance,
                    # Issue #3470: avoid stub copper conflicts with the
                    # rescues generated earlier in this pass.
                    existing_escapes=rescues,
                )
                if in_pad_route is not None:
                    rescues.append(in_pad_route)
                    logger.info(
                        "In-pad rescue (#3183) for %s pin %s (net %s) "
                        "on %s package at %.2fmm pitch (adjacent-signal "
                        "neighbour predicate).",
                        package.ref,
                        pad.pin,
                        pad.net_name,
                        package.package_type.name,
                        package.pin_pitch,
                    )

        return rescues

    def _create_alternating_escape(
        self,
        pad: Pad,
        direction: EscapeDirection,
        package: PackageInfo,
        pad_clearance_margin: float | None = None,
    ) -> EscapeRoute:
        """Create an escape route with alternating direction.

        Issue #2756: When ``pad_clearance_margin`` is provided, the escape
        segment endpoint is shortened along the launch direction so that the
        segment maintains at least ``pad_clearance_margin`` mm of edge-to-edge
        clearance against every OTHER pad in ``package.pads`` on the same
        layer.  If the maximum safe length is shorter than the requested
        launch distance, the segment is clipped; if no useful length is
        achievable (the pad is fully boxed in), the returned escape carries a
        zero-length segment which the caller can detect and skip.  Passing
        ``None`` (the default) preserves pre-#2756 behaviour exactly for
        callers that have not yet been ported to the clipping API.

        Args:
            pad: The pad to escape
            direction: Escape direction
            package: Package info
            pad_clearance_margin: Optional minimum edge-to-edge clearance
                from the escape segment to every other package pad.  When
                provided, the segment endpoint is clipped to honour this
                margin.

        Returns:
            EscapeRoute for this pad
        """
        dx, dy = self._direction_to_vector(direction)
        min_x, min_y, max_x, max_y = package.bounding_box

        # Calculate escape distance
        escape_dist = self.escape_clearance + self.rules.trace_width * 2
        trace_w = self._get_trace_width_for_net(pad.net_name)

        # Issue #2756: clip the escape distance against neighbour-pad
        # clearance when requested.  This stops the QFP/QFN/HTSSOP
        # alternating-direction emitter from producing segments that run
        # through (or just clip) adjacent pads on the same edge -- the
        # dominant failure mode behind board 05's 105 clearance_pad_segment
        # violations on U3 (DRV8301 HTSSOP-56) and U10 (STM32G431 LQFP-32).
        if pad_clearance_margin is not None:
            safe_dist = self._compute_max_safe_escape_length(
                pad=pad,
                dx=dx,
                dy=dy,
                trace_width=trace_w,
                package_pads=package.pads,
                min_clearance=pad_clearance_margin,
                max_length=escape_dist,
            )
            escape_dist = min(escape_dist, safe_dist)

        escape_x = pad.x + dx * escape_dist
        escape_y = pad.y + dy * escape_dist

        # Create segment
        segment = Segment(
            x1=pad.x,
            y1=pad.y,
            x2=escape_x,
            y2=escape_y,
            width=trace_w,
            layer=pad.layer,
            net=pad.net,
            net_name=pad.net_name,
        )

        return EscapeRoute(
            pad=pad,
            direction=direction,
            escape_point=(escape_x, escape_y),
            escape_layer=pad.layer,
            via_pos=None,
            segments=[segment],
            via=None,
            ring_index=0,
        )

    def _compute_max_safe_escape_length(
        self,
        pad: Pad,
        dx: float,
        dy: float,
        trace_width: float,
        package_pads: list[Pad],
        min_clearance: float,
        max_length: float,
    ) -> float:
        """Find the maximum escape-segment length that respects pad clearance.

        Issue #2756: The escape-pattern endpoint emitter (used by
        ``_create_alternating_escape`` and ``_escape_radial``) historically
        emitted segments of a fixed launch length without checking that the
        segment kept ``pad_to_segment`` clearance to neighbour pads on the
        same package.  When the launch direction is parallel-along-the-edge
        (the ``alt_dir_cw`` / ``alt_dir_ccw`` cases for odd pins in
        ``_escape_qfp_alternating``) the segment runs right past the next
        pad in the row and clips it, producing a ``clearance_pad_segment``
        DRC error.  This helper computes the maximum length ``L`` such that
        the candidate segment from ``pad`` to ``(pad + (dx,dy) * L)`` keeps
        at least ``min_clearance`` mm of edge-to-edge gap from every other
        pad in ``package_pads`` on the same layer.

        The search is a coarse binary search bracketed by 0 and
        ``max_length`` -- a 1-D search is sufficient because the candidate
        segment is a straight line from the pad in a single direction, and
        the clearance function is monotonically non-decreasing as the
        endpoint pulls back toward the originating pad along the launch
        axis (for reasonable launch directions away from neighbours).

        Args:
            pad: Originating pad (segment starts here)
            dx: X component of the unit launch direction
            dy: Y component of the unit launch direction
            trace_width: Width of the candidate segment in mm
            package_pads: All pads on the same package (the originating pad
                is identified by identity and skipped from the check)
            min_clearance: Required minimum edge-to-edge clearance in mm
            max_length: Upper bound on the search (typically the original
                requested launch distance)

        Returns:
            The maximum safe length in mm, in the range
            ``[0.0, max_length]``.  A returned value of 0.0 means even a
            zero-length stub would conflict with a neighbour (only possible
            when ``min_clearance`` is larger than the pad-to-pad spacing
            and the originating pad already touches its neighbour's
            clearance halo).  The caller should treat values below a small
            useful threshold (e.g. ``min_clearance + trace_width``) as a
            defer-to-router signal.
        """
        if max_length <= 0:
            return 0.0

        def _gap_at(length: float) -> float:
            """Minimum edge-to-edge gap from the candidate segment to any
            other pad on the same layer."""
            ex = pad.x + dx * length
            ey = pad.y + dy * length
            candidate = Segment(
                x1=pad.x,
                y1=pad.y,
                x2=ex,
                y2=ey,
                width=trace_width,
                layer=pad.layer,
                net=pad.net,
                net_name=pad.net_name,
            )
            min_gap = float("inf")
            for other in package_pads:
                if other is pad:
                    continue
                # Defensive: skip pads that share coords with the originator
                # (would be a duplicate pad entry; rare but seen in tests).
                if other.x == pad.x and other.y == pad.y:
                    continue
                # Only check pads that touch the segment's layer.  PTH pads
                # touch every copper layer so always check those.
                if not other.through_hole and other.layer != pad.layer:
                    continue
                gap = self._segment_to_pad_edge_gap(candidate, other)
                if gap < min_gap:
                    min_gap = gap
            return min_gap

        # If the full-length segment is already clear, no clipping needed.
        full_gap = _gap_at(max_length)
        if full_gap >= min_clearance - 1e-6:
            return max_length

        # Otherwise, binary-search for the longest length that still clears.
        # If even a zero-length stub conflicts (rare), bail out at 0.
        if _gap_at(0.0) < min_clearance - 1e-6:
            return 0.0

        lo = 0.0
        hi = max_length
        # 12 iterations resolves to ~max_length / 4096 -- well below the
        # router grid resolution for any practical launch distance.
        for _ in range(12):
            mid = (lo + hi) / 2
            if _gap_at(mid) >= min_clearance - 1e-6:
                lo = mid
            else:
                hi = mid
        return lo

    def _escape_fine_pitch_dual_row(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes with alternating layer escapes for fine-pitch dual-row packages.

        For fine-pitch dual-row packages this includes:

        - SSOP / TSSOP (0.65mm or 0.5mm pitch, no mounting tabs).
        - USB-C-class connectors (Issue #2919): 14-16 SMT signal pads at 0.5mm
          pitch arranged in two rows.  USB-C footprints additionally have
          through-hole mounting tabs that are NOT included in the ``package``
          passed here -- ``_escape_usb_c_connector`` filters them out before
          delegation.

        Adjacent signal pins cannot route on the same layer due to clearance conflicts.
        This method implements alternating layer escape routing:

        - Even-indexed pins (0, 2, 4, ...): Escape on F.Cu (top layer)
        - Odd-indexed pins (1, 3, 5, ...): Via down to inner layer, escape there

        Pattern (for horizontal TSSOP-20):
        ```
        Pin row 1:  [1][2][3][4][5][6][7][8][9][10]
                     |  V  |  V  |  V  |  V  |  V    V = Via to inner layer
                    F.Cu  In1 F.Cu In1 F.Cu In1     Alternating layers

        Pin row 2:  [20][19][18][17][16][15][14][13][12][11]
        ```

        This ensures that adjacent pins with signal nets don't conflict with each
        other's escape routes, as they route on different layers.

        Args:
            package: SSOP/TSSOP package info

        Returns:
            List of escape routes with alternating layer assignment
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Separate pads into two rows
        top_row: list[Pad] = []
        bottom_row: list[Pad] = []
        left_col: list[Pad] = []
        right_col: list[Pad] = []

        # Determine orientation by checking Y vs X spread
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        is_horizontal = x_spread > y_spread  # pins arranged horizontally

        if is_horizontal:
            # Split by Y position
            for pad in package.pads:
                if pad.y > center_y:
                    top_row.append(pad)
                else:
                    bottom_row.append(pad)
            # Sort rows by X position
            top_row.sort(key=lambda p: p.x)
            bottom_row.sort(key=lambda p: p.x)

            # Generate escapes for each row with alternating layers
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=top_row,
                    direction=EscapeDirection.NORTH,
                    package=package,
                )
            )
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=bottom_row,
                    direction=EscapeDirection.SOUTH,
                    package=package,
                )
            )
        else:
            # Vertical orientation - split by X position
            for pad in package.pads:
                if pad.x > center_x:
                    right_col.append(pad)
                else:
                    left_col.append(pad)
            # Sort columns by Y position
            left_col.sort(key=lambda p: p.y)
            right_col.sort(key=lambda p: p.y)

            # Generate escapes for each column with alternating layers
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=left_col,
                    direction=EscapeDirection.WEST,
                    package=package,
                )
            )
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=right_col,
                    direction=EscapeDirection.EAST,
                    package=package,
                )
            )

        return escapes

    def _escape_usb_c_connector(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate alternating-layer escape routes for a USB-C-class connector.

        Issue #2919: USB-C receptacles (e.g., GCT_USB4105) have 14-16 SMT
        signal pads at 0.5mm pitch in two rows, plus 2 through-hole shield /
        mounting tabs.  The channel between adjacent SMT pads (e.g., A6/A7 =
        USB_D+/USB_D-) is 0.5mm pitch - 0.25mm pad = 0.25mm, which after a
        2x0.127mm jlcpcb tier-1 clearance leaves zero room for an
        in-channel trace.  The fix is to alternate layers across adjacent
        pads (one stays on F.Cu, the next vias to In1.Cu) so adjacent escape
        traces never share a copper layer.

        This routine:

        1. Filters out through-hole shield/mount pads -- they don't need
           per-pin escape (they're typically GND and connect to a stitched
           plane via the main router's normal pathfinder).
        2. Builds a synthetic ``PackageInfo`` containing only the SMT pads
           with package metadata recomputed (center, bounding box, pitch).
        3. Delegates to ``_escape_fine_pitch_dual_row`` for the actual
           alternating-layer escape generation.  This reuses the SSOP/TSSOP
           code path including its in-pad-via rescue fallback so that at
           higher manufacturer tiers (jlcpcb-tier1+, PCBWay) deferred pins
           still escape via in-pad vias instead of failing silently.

        Args:
            package: USB_C_CONNECTOR package info (mixed SMT + through-hole).

        Returns:
            List of escape routes covering only the SMT signal pads.  The
            through-hole shield/mount pads are intentionally omitted -- the
            main router handles them via standard pathfinding.
        """
        # Filter to SMT pads only -- shield/mounting through-hole tabs are
        # handled by the main router via standard pathfinding.
        smt_pads = [p for p in package.pads if not p.through_hole]

        if not smt_pads:
            return []

        # Issue #3410: column-aligned USB-C connectors do not need escape
        # routes at all.  Board 03's J1 "re-spin" reordered the B-row SMT
        # tails so each multi-pad net (USB_D+ on A6/B6, USB_D- on A7/B7,
        # VBUS, GND) occupies a SINGLE column -- same-net pads are
        # vertically adjacent and tie together with a plain surface stub
        # (the main router's intra-IC consolidation), and every signal
        # exits its outer-row pad straight into open board area.  For
        # such connectors the dual-row alternating-layer escape (vias
        # between 0.5mm-pitch pads) is not only unnecessary, it is
        # actively harmful: the via fanout packs 0.6mm vias at sub-pitch
        # spacing producing clearance_via_via / clearance_segment_via /
        # clearance_segment_segment violations INSIDE the connector
        # footprint (the dominant DRC error cluster in the #3410 audit).
        # Defer the whole connector to the main router instead.
        #
        # Legacy tongue-mirrored footprints (e.g. board 06's USB-C, where
        # USB_D+ sits on A6/B6 at DIFFERENT x positions) fail the
        # column-alignment test and keep the #2919 alternating-layer
        # escape behaviour unchanged.
        if _is_column_aligned_connector(smt_pads):
            return []

        # Rebuild package metadata against the SMT subset so
        # ``_escape_fine_pitch_dual_row`` sees the correct centre / bounding
        # box (the original package centre is biased by the through-hole
        # tabs at y=1.5mm, which would skew "is_horizontal" detection).
        xs = [p.x for p in smt_pads]
        ys = [p.y for p in smt_pads]
        center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
        bounding_box = (min(xs), min(ys), max(xs), max(ys))
        smt_pitch = _calculate_min_pitch(smt_pads)

        from dataclasses import replace as _replace

        smt_package = _replace(
            package,
            pads=smt_pads,
            center=center,
            pin_count=len(smt_pads),
            pin_pitch=smt_pitch,
            bounding_box=bounding_box,
        )

        return self._escape_fine_pitch_dual_row(smt_package)

    def _create_fine_pitch_row_escapes(
        self,
        pads: list[Pad],
        direction: EscapeDirection,
        package: PackageInfo,
        phase_offset: int = 0,
    ) -> list[EscapeRoute]:
        """Create escape routes for fine-pitch SSOP/TSSOP with alternating layers.

        Adjacent pins escape on different layers to avoid clearance violations:
        - Even pins (index 0, 2, 4...): Stay on surface layer (F.Cu)
        - Odd pins (index 1, 3, 5...): Via to inner layer (In1.Cu or B.Cu)

        This is specifically designed for fine-pitch packages where the pitch
        (0.65mm or less) doesn't allow traces to pass between adjacent pads.

        Issue #1778: Escape segments use min_trace_width (manufacturer minimum)
        instead of net-class trace width. These segments are short (< 1mm) and
        only need to clear the pad congestion zone. Using the full trace width
        would violate clearances between adjacent fine-pitch pads.

        Issue #2319: Escape segments are validated against neighboring pad
        copper.  If a segment would violate clearance against an adjacent
        pad, the escape for that pin is omitted (deferred to the main router).
        The escape router also uses ``fine_pitch_clearance`` when configured.

        Issue #3235 (escape layer diversification, negative-results note):
        the ``phase_offset`` parameter flips the alternation parity for this
        row.  With ``phase_offset=0`` (default, historical) even indices stay
        on F.Cu and odd indices via to the inner layer.  With
        ``phase_offset=1`` the parity flips so even indices via to the inner
        layer.  The :meth:`_escape_fine_pitch_dual_row` dispatcher currently
        calls all rows with ``phase_offset=0`` (preserving historical
        behaviour).  A direction-2 spike on the softstart 8/10 → 10/10 lift
        evaluated passing ``phase_offset=1`` to the SECOND row/column (so the
        two halves of a dual-row TSSOP-20 do not converge on the same
        post-escape layer); that REGRESSED softstart to 7/10 across all of
        PYTHONHASHSEED=42/43/44.  Filtering NC pads (``net == 0``) from the
        row buckets before the alternation index assignment ALSO regressed
        softstart to 7/10 (the NC stubs apparently shield signal pads from
        same-layer collisions even though they carry no net).  The parameter
        is preserved as infrastructure so future per-board / per-package
        gating can explore the lever without re-introducing the regression
        on the default path.

        Args:
            pads: Row of pads sorted by position along the row
            direction: Primary escape direction (perpendicular to row)
            package: Package info for bounds
            phase_offset: 0 (default) for ``i % 2 == 1`` via-down behaviour
                (historical), or 1 to flip the parity so ``i % 2 == 0`` via
                down.  See class docstring for rationale.

        Returns:
            List of escape routes with alternating layer assignment
        """
        escapes: list[EscapeRoute] = []
        dx, dy = self._direction_to_vector(direction)

        # Issue #2319: Use per-component clearance (respects fine_pitch_clearance)
        # instead of the raw trace_clearance everywhere.
        # Issue #2350: When fine_pitch_clearance is not configured in DesignRules,
        # auto-derive a clearance for fine-pitch packages based on pin pitch.
        # This method is only called for SSOP/TSSOP (confirmed fine-pitch), so we
        # can safely infer a tighter clearance when the user hasn't set one.
        ref = pads[0].ref if pads else ""
        effective_clearance = self.rules.get_clearance_for_component(
            ref,
            pin_pitch=package.pin_pitch,
        )

        # If get_clearance_for_component returned the default trace_clearance
        # (because fine_pitch_clearance was None), derive a workable clearance
        # from the pad geometry.  For a 0.65mm pitch SSOP with 0.35mm pads the
        # copper gap is (0.65 - 0.35) / 2 = 0.15mm; we use 80% of the
        # copper-to-copper gap to leave manufacturing margin.
        if (
            self.rules.fine_pitch_clearance is None
            and ref not in self.rules.component_clearances
            and package.pin_pitch < self.rules.fine_pitch_threshold
        ):
            # Estimate pad width along the row axis
            pad_widths = [min(p.width, p.height) for p in pads[:4]]
            avg_pad_width = sum(pad_widths) / len(pad_widths) if pad_widths else 0.3
            copper_gap = package.pin_pitch - avg_pad_width
            derived_clearance = copper_gap * 0.8
            if derived_clearance < effective_clearance:
                logger.info(
                    "Fine-pitch auto-clearance for %s: %.3fmm "
                    "(derived from %.2fmm pitch, %.2fmm pad width)",
                    ref,
                    derived_clearance,
                    package.pin_pitch,
                    avg_pad_width,
                )
                effective_clearance = derived_clearance

        # Issue #1778: Use min_trace_width for escape segments in fine-pitch
        # packages. The escape segments are short and only need to clear the
        # pad congestion zone -- using the full trace width would violate
        # clearances between adjacent pads at 0.65mm pitch.
        #
        # Issue #3278: When pads in the same row belong to DIFFERENT net
        # classes (e.g. a USB-C row with GND/Power pads next to
        # USB_D+/USB_D- HighSpeed pads), the historical code used
        # ``pads[0].net_name`` to choose the escape width for the entire
        # row.  If ``pads[0]`` happened to land on a fat-class net (e.g.
        # GND in Power class at 0.5mm), every escape segment in the row
        # inherited that width, producing clearance violations on
        # adjacent fine-class pads (e.g. USB_D+/USB_D- at 0.2mm) whose
        # own net class would have allowed a much narrower escape.
        #
        # The fix splits the width into two values:
        #   - ``row_max_width`` -- worst-case trace width across all
        #     pads in the row, used for ``lateral_offset`` and any
        #     other GEOMETRY that must remain a row-scope constant
        #     (so cross-row via clearance is preserved for the
        #     widest pin in the row).
        #   - per-pad ``pad_escape_width`` (computed inside the loop)
        #     -- used for the ``Segment.width`` of each pin's own
        #     escape geometry and forwarded to the in-pad / lateral
        #     rescue helpers so they emit traces sized for the pad
        #     they're rescuing.
        #
        # When ``min_trace_width`` is set (neck-down path) BOTH values
        # collapse back to that necked width, preserving the
        # manufacturer-minimum behaviour the neck-down path expects.
        if self.rules.min_trace_width is not None:
            row_max_width = self.rules.min_trace_width
        elif pads:
            row_max_width = max(self._get_trace_width_for_net(p.net_name or "") for p in pads)
        else:
            row_max_width = self.rules.trace_width

        # For fine-pitch, use minimal escape distance
        # Vias placed just outside pad clearance zone
        pad_clearance = effective_clearance + package.pin_pitch / 4
        via_offset = pad_clearance + self.rules.via_diameter / 2

        # Issue #1784: Compute lateral fan-out offset for odd-pin vias when
        # adjacent escape traces would violate clearance.  The row direction
        # is perpendicular to the escape direction: if escape is (dx, dy),
        # the row axis is (-dy, dx).  Adjacent pads are separated by
        # pin_pitch along that axis.  Two parallel escape segments (one from
        # an even pin, one surface-segment from an odd pin) have edge-to-edge
        # gap = pin_pitch - escape_width.  When that gap is less than
        # trace_clearance we must shift the odd-pin via laterally.
        #
        # Issue #3278: Use ``row_max_width`` here (NOT the per-pad width)
        # so the lateral offset is sized for the worst-case pin in the
        # row.  A per-pad lateral offset collapses to 0 on the
        # narrow-class pad, which then collides with the via from its
        # fat-class neighbour (the exact regression the issue spec
        # warns about for USB_D+/USB_D- on board 03).
        lateral_clearance = package.pin_pitch - row_max_width
        if lateral_clearance < effective_clearance:
            lateral_offset = (effective_clearance - lateral_clearance + row_max_width) / 2
        else:
            lateral_offset = 0.0

        # Row direction unit vector (perpendicular to escape direction).
        # Sign chosen so that a positive offset moves "forward" along the row.
        row_dx, row_dy = -dy, dx

        skipped_count = 0

        for i, pad in enumerate(pads):
            # Issue #3278: per-pad escape width, sized for THIS pad's
            # own net class.  Only the geometry that must remain a
            # row-scope constant (``lateral_offset``) uses
            # ``row_max_width``; everything else (the four
            # ``Segment(width=...)`` sites below and the
            # ``_try_in_pad_escape`` / ``_try_lateral_via_escape``
            # rescue calls) uses ``pad_escape_width``.  This
            # prevents fat-class pads (e.g. GND at 0.5mm) from
            # forcing adjacent fine-class pads (e.g. USB_D+ at
            # 0.2mm) into 0-gap clearance violations.
            pad_escape_width = (
                self.rules.min_trace_width
                if self.rules.min_trace_width is not None
                else self._get_trace_width_for_net(pad.net_name or "")
            )

            # Determine if this pin needs layer transition.
            # Issue #3235: ``phase_offset`` flips the parity so the second
            # row/column of a dual-row package can use the opposite layer
            # assignment.  Default is 0 (historical strict-by-position
            # alternation).
            needs_via = (i + phase_offset) % 2 == 1

            # Issue #3257: per-pad escape-layer override.  When the caller
            # has supplied an override for this ``(ref, pin)`` tuple, we
            # force the layer assignment to match.  Mapping to ``F.Cu``
            # (the pad's own surface layer on the standard front-side
            # SSOP/TSSOP placement) clears ``needs_via``; mapping to any
            # non-surface layer (B.Cu, In1.Cu, ...) sets it.  This is the
            # surgical lever softstart uses to break the SWDIO/STATUS_LED
            # B.Cu overlap in U1's east column without flipping the
            # alternation globally (which regressed routing reach 8/10 ->
            # 7/10 per #3235's negative-results note).
            override_layer = self.escape_pad_layer_overrides.get((pad.ref, str(pad.pin)))
            if override_layer is not None:
                needs_via = override_layer != pad.layer
                logger.info(
                    "Escape pad layer override (#3257): %s pin %s "
                    "needs_via=%s (override=%s, pad.layer=%s)",
                    pad.ref,
                    pad.pin,
                    needs_via,
                    override_layer.value,
                    pad.layer.value,
                )

            if needs_via:
                # Odd pin: Via to inner layer
                # Calculate via position - place via perpendicular to pin row,
                # with lateral fan-out offset to avoid clearance violations
                # against the adjacent even-pin escape segment.
                # Alternate the lateral offset direction (+/-) based on which
                # neighbour is closer, biasing away from the lower-indexed
                # (even) neighbour.
                sign = 1 if (i // 2) % 2 == 0 else -1
                # Issue #1840: Place via INWARD (toward IC body center)
                # instead of outward. The inward direction has more
                # available space under the IC body for via placement.
                via_x = pad.x - dx * via_offset + row_dx * lateral_offset * sign
                via_y = pad.y - dy * via_offset + row_dy * lateral_offset * sign

                # Issue #1840: Select inner signal layer from LayerStack
                # when available (e.g. In1.Cu on 4-layer boards), falling
                # back to B.Cu when no inner signal layers exist.
                escape_layer = self._select_inner_escape_layer(pad.layer)

                # Escape point is beyond the via on the escape layer,
                # continuing inward (same direction as via placement).
                escape_x = via_x - dx * (self.rules.via_diameter / 2 + effective_clearance)
                escape_y = via_y - dy * (self.rules.via_diameter / 2 + effective_clearance)

                # Create segments
                segments: list[Segment] = []

                # Segment from pad to via on surface layer (may be diagonal
                # when lateral_offset > 0)
                surface_seg = Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=pad_escape_width,
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
                segments.append(surface_seg)

                # Issue #2319: Check segment-to-pad clearance for the surface
                # segment against neighboring pads before committing.
                if self._segment_violates_pad_clearance(
                    surface_seg,
                    i,
                    pads,
                    effective_clearance,
                    # Issue #2755: Also check pads on the OTHER rows/edges
                    # of this footprint plus plane-net pads that were
                    # filtered out of the row-grouping step.
                    extra_pads=self._other_footprint_pads(package, pads),
                ):
                    # Issue #2605: Attempt in-pad via escape as a fallback
                    # before deferring to the main router.  Only enabled
                    # for manufacturers that support via-in-pad processing
                    # (e.g. ``jlcpcb-tier1`` Capability+, PCBWay).
                    # Issue #3033 / #3062: forward the EscapeRouter strict
                    # flag so SSOP/TSSOP callers inherit the same defer-vs-
                    # commit-violation policy as the QFP dispatcher.
                    in_pad_route = self._try_in_pad_escape(
                        pad=pad,
                        direction=direction,
                        effective_clearance=effective_clearance,
                        escape_width=pad_escape_width,
                        package=package,
                        skip_on_clearance_violation=self.strict_in_pad_clearance,
                    )
                    if in_pad_route is not None:
                        escapes.append(in_pad_route)
                        continue
                    # Issue #3063 (sub-B of #3048): lateral re-attempt
                    # when the in-pad rescue deferred.  See the QFP-
                    # alternating dispatcher comment for rationale.
                    # Issue #3080: gate removed -- the lateral helper is
                    # invoked on BOTH the strict and non-strict paths so
                    # the surface-stub necking from PR #3079 reaches
                    # default callers (board 04 stitching depends on this).
                    lateral_route = self._try_lateral_via_escape(
                        pad=pad,
                        direction=direction,
                        effective_clearance=effective_clearance,
                        escape_width=pad_escape_width,
                        package=package,
                    )
                    if lateral_route is not None:
                        escapes.append(lateral_route)
                        continue
                    skipped_count += 1
                    logger.debug(
                        "Escape for pad %s (pin %d) skipped: segment-to-pad "
                        "clearance violation (deferred to main router)",
                        pad.net_name,
                        i,
                    )
                    continue

                # Create via
                via = Via(
                    x=via_x,
                    y=via_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(pad.layer, escape_layer),
                    net=pad.net,
                    net_name=pad.net_name,
                )

                # Segment from via to escape point on inner layer
                segments.append(
                    Segment(
                        x1=via_x,
                        y1=via_y,
                        x2=escape_x,
                        y2=escape_y,
                        width=pad_escape_width,
                        layer=escape_layer,
                        net=pad.net,
                        net_name=pad.net_name,
                    )
                )

                escapes.append(
                    EscapeRoute(
                        pad=pad,
                        direction=direction,
                        escape_point=(escape_x, escape_y),
                        escape_layer=escape_layer,
                        via_pos=(via_x, via_y),
                        segments=segments,
                        via=via,
                        ring_index=0,
                    )
                )
            else:
                # Even pin: Stay on surface layer
                # Simple escape perpendicular to pin row
                escape_dist = self.escape_clearance + self.rules.trace_width
                escape_x = pad.x + dx * escape_dist
                escape_y = pad.y + dy * escape_dist

                # Create segment from pad to escape point
                segment = Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=escape_x,
                    y2=escape_y,
                    width=pad_escape_width,
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )

                # Issue #2319: Check segment-to-pad clearance before committing.
                if self._segment_violates_pad_clearance(
                    segment,
                    i,
                    pads,
                    effective_clearance,
                    # Issue #2755: Also check pads on the OTHER rows/edges
                    # of this footprint plus plane-net pads that were
                    # filtered out of the row-grouping step.
                    extra_pads=self._other_footprint_pads(package, pads),
                ):
                    # Issue #2605: Attempt in-pad via escape as a fallback
                    # before deferring to the main router.
                    # Issue #3033 / #3062: forward the strict flag here too
                    # so the even-pin branch inherits the same defer-vs-
                    # commit-violation policy as the odd-pin/QFP branches
                    # (curator-noted asymmetry from PR #3038 fixed).
                    in_pad_route = self._try_in_pad_escape(
                        pad=pad,
                        direction=direction,
                        effective_clearance=effective_clearance,
                        escape_width=pad_escape_width,
                        package=package,
                        skip_on_clearance_violation=self.strict_in_pad_clearance,
                    )
                    if in_pad_route is not None:
                        escapes.append(in_pad_route)
                        continue
                    # Issue #3063 (sub-B of #3048): lateral re-attempt
                    # when the in-pad rescue deferred.  See the QFP-
                    # alternating dispatcher comment for rationale.
                    # Issue #3080: gate removed -- the lateral helper is
                    # invoked on BOTH the strict and non-strict paths so
                    # the surface-stub necking from PR #3079 reaches
                    # default callers (board 04 stitching depends on this).
                    lateral_route = self._try_lateral_via_escape(
                        pad=pad,
                        direction=direction,
                        effective_clearance=effective_clearance,
                        escape_width=pad_escape_width,
                        package=package,
                    )
                    if lateral_route is not None:
                        escapes.append(lateral_route)
                        continue
                    skipped_count += 1
                    logger.debug(
                        "Escape for pad %s (pin %d) skipped: segment-to-pad "
                        "clearance violation (deferred to main router)",
                        pad.net_name,
                        i,
                    )
                    continue

                escapes.append(
                    EscapeRoute(
                        pad=pad,
                        direction=direction,
                        escape_point=(escape_x, escape_y),
                        escape_layer=pad.layer,
                        via_pos=None,
                        segments=[segment],
                        via=None,
                        ring_index=0,
                    )
                )

        if skipped_count:
            logger.warning(
                "Escape routing for %s: %d of %d pins deferred to main router "
                "(clearance violation)",
                ref,
                skipped_count,
                len(pads),
            )

        # Issue #1784: Post-generation pairwise clearance validation
        # Issue #2319: Use effective_clearance (respects fine_pitch_clearance)
        self._validate_escape_clearances(escapes, effective_clearance, pads)

        return escapes

    @staticmethod
    def _segment_to_pad_edge_gap(seg: Segment, pad: Pad) -> float:
        """Return the minimum edge-to-edge gap between a segment and a pad.

        The pad is modelled as a rectangle centred at (pad.x, pad.y) with
        half-extents (pad.width/2, pad.height/2).  The segment centre-line
        runs from (seg.x1, seg.y1) to (seg.x2, seg.y2).

        The closest distance from the segment centre-line to the pad
        rectangle boundary is computed, then both the segment half-width
        and pad half-extent (in the direction of the closest approach) are
        subtracted to yield the edge-to-edge gap.

        A negative return value means the segment copper overlaps the pad
        copper.
        """
        # Closest point on the segment to the pad centre
        sx, sy = seg.x2 - seg.x1, seg.y2 - seg.y1
        seg_len_sq = sx * sx + sy * sy
        if seg_len_sq < 1e-12:
            # Degenerate segment (zero length)
            cpx, cpy = seg.x1, seg.y1
        else:
            t = max(0.0, min(1.0, ((pad.x - seg.x1) * sx + (pad.y - seg.y1) * sy) / seg_len_sq))
            cpx = seg.x1 + t * sx
            cpy = seg.y1 + t * sy

        # Distance from closest point on segment to the pad rectangle edge.
        # The pad is axis-aligned (no rotation support needed for SOP pads).
        half_w = pad.width / 2
        half_h = pad.height / 2
        dx_abs = abs(cpx - pad.x)
        dy_abs = abs(cpy - pad.y)

        # Signed distance from pad rectangle (negative = inside)
        outside_x = max(0.0, dx_abs - half_w)
        outside_y = max(0.0, dy_abs - half_h)

        if outside_x == 0.0 and outside_y == 0.0:
            # Point is inside the pad rectangle
            rect_dist = -min(half_w - dx_abs, half_h - dy_abs)
        else:
            rect_dist = math.sqrt(outside_x * outside_x + outside_y * outside_y)

        # Edge-to-edge gap = centre-to-rect distance minus half-segment-width
        return rect_dist - seg.width / 2

    @staticmethod
    def _segment_clears_foreign_via(
        seg: Segment,
        via: Via,
        trace_clearance: float,
        hard_intersection_only: bool = False,
    ) -> bool:
        """Return True iff a segment clears a foreign-net via.

        Issue #3002: Thin wrapper around
        :func:`kicad_tools.router.via_clearance.segment_clears_foreign_via`.
        The body was lifted to the shared module so the main router
        (``Autorouter._update_router_segment_foreign_context``) can
        consume the same predicate without importing :mod:`escape`.
        The wrapper is retained for backward compatibility with PR
        #2999's call sites in :meth:`apply_escape_routes` and with
        ``tests/test_escape_segment_via_clearance.py`` which exercises
        this predicate directly.  See the docstring of the underlying
        helper for the threshold semantics and layer-awareness rules.
        """
        return segment_clears_foreign_via(
            seg,
            via,
            trace_clearance,
            hard_intersection_only=hard_intersection_only,
        )

    @staticmethod
    def _is_pin_boxed_by_plane_neighbours(
        pad: Pad,
        package: PackageInfo,
        plane_nets: set[int] | None = None,
    ) -> bool:
        """Detect a fine-pitch QFP signal pin sandwiched between same-edge
        same-component plane-net pads (Issue #2880).

        A signal pin is "plane-sandwiched" when its two IMMEDIATE
        same-edge neighbours -- BEFORE the plane-net filter applied
        inside ``_escape_qfp_alternating`` -- are both on plane nets.

        Worked example (synthetic LQFP-48 fixture, west-edge pinout
        designed to mirror the board-04 STM32F103 plane-sandwich
        condition):

            pin 6 +3.3V (plane), pin 7 NRST (signal), pin 8 GND (plane)

        ``pin 7`` is plane-sandwiched -- its immediate same-edge
        neighbours on either side are both plane pads.

        The grid's standard pathfinder uses the cell ``net`` field plus
        the ``blocked`` flag; same-net traffic passes through, so the
        signal pad's clearance envelope was painted with its own net
        before the plane pad later marked the cells as ``is_obstacle``
        (without overwriting ``cell.net``).  The pathfinder happily
        threads the signal through the plane pad's envelope and DRC
        catches the resulting trace post-hoc.  The geometric channel is
        too narrow to admit a trace at full manufacturer clearance
        (LQFP-48 0.5mm pitch leaves 0.2 mm gap; jlcpcb-tier1 needs
        0.381 mm), so we cannot fix this on the surface layer -- we
        must escape vertically via via-in-pad.

        This predicate is the trigger for the forced in-pad rescue in
        ``_escape_qfp_alternating`` (Issue #2880).  It is intentionally
        narrow:

        * The pad must NOT itself be on a plane net (we only rescue
          signal pads -- plane pads are stitched via planes).
        * BOTH immediate same-edge neighbours must be on plane nets
          (edge-corner pins with only one neighbour cannot be
          plane-sandwiched and fall through to the standard rescue
          gate which uses the row-level violation check).
        * The neighbours must be on the same footprint (handled
          implicitly: we iterate ``package.pads`` only).

        Note on board-04 applicability: On the current board-04 STM32
        layout the signal pins (OSC_IN, OSC_OUT, NRST) each have at
        least one signal-net immediate neighbour, so this predicate
        does NOT fire on those pins.  Their existing rescue path is
        the row-level violation check in
        ``_escape_qfp_alternating``.  The forced predicate matters
        most for future boards whose pin assignments place plane-net
        pads at BOTH immediate adjacencies -- a configuration that
        is geometrically infeasible at fine pitch and which the
        existing violation check can miss when the unclipped escape
        segment is too short to reach the surrounding plane pads.

        Args:
            pad: The signal pad we are about to escape.
            package: The QFP/QFN package info; ``package.pads`` includes
                the plane-net pads that ``_escape_qfp_alternating``
                filtered out of its iteration list.
            plane_nets: Optional override of which net ids count as
                plane nets.  Defaults to ``{0}`` (matching the io.py
                convention from ``skip_nets`` rewriting at
                ``io.py:2819-2820``).

        Returns:
            True if ``pad`` is a signal pin whose immediate same-edge
            neighbours are both plane-net pads.
        """
        if plane_nets is None:
            plane_nets = {0}

        # Only signal pads can be plane-sandwiched (we never rescue a
        # plane pad with a via-in-pad escape -- plane pads are stitched).
        if pad.net in plane_nets:
            return False

        min_x, min_y, max_x, max_y = package.bounding_box
        center_x, center_y = package.center

        # Edge classification: pick the CLOSEST of the four edges so
        # corner pads get a single canonical edge.  The dispatcher's
        # ordered ``elif`` chain in ``_escape_qfp_alternating`` can
        # mis-classify e.g. west-edge corner pads as "south" because
        # they sit within both edge_margins -- that asymmetry doesn't
        # bite the dispatcher (it filters plane-net pads first), but it
        # would cause this predicate to pull pads from an adjacent edge
        # into the wrong neighbour list and report spurious sandwich
        # hits on the corner of an unrelated edge.
        edge_margin = min(max_x - min_x, max_y - min_y) * 0.2

        def _classify_edge(p: Pad) -> str | None:
            # Skip thermal/center pads.
            if abs(p.x - center_x) < edge_margin and abs(p.y - center_y) < edge_margin:
                return None
            dists = {
                "north": abs(p.y - max_y),
                "south": abs(p.y - min_y),
                "east": abs(p.x - max_x),
                "west": abs(p.x - min_x),
            }
            edge = min(dists, key=lambda k: dists[k])
            # Reject pads that are not actually near any edge (e.g. an
            # unexpected interior pad that slipped past the thermal
            # check above).
            if dists[edge] >= edge_margin:
                return None
            return edge

        pad_edge = _classify_edge(pad)
        if pad_edge is None:
            return False

        # Sort same-edge pads (from the FULL package.pads list -- this
        # is the asymmetry that makes the dispatcher's per-edge
        # iteration miss plane neighbours) along the edge's primary
        # axis.  Note this mirrors the sort keys in
        # ``_escape_qfp_alternating`` (north/south by x, east/west by y).
        same_edge: list[Pad] = []
        for p in package.pads:
            if p is pad:
                same_edge.append(p)
                continue
            if _classify_edge(p) == pad_edge:
                same_edge.append(p)

        if pad_edge in ("north", "south"):
            same_edge.sort(key=lambda q: q.x)
        else:  # east, west
            same_edge.sort(key=lambda q: q.y)

        try:
            idx = same_edge.index(pad)
        except ValueError:
            return False

        # Strict trigger: BOTH immediate same-edge neighbours must be
        # plane-net pads.  Edge-end signal pins (idx 0 or last) cannot
        # be plane-sandwiched -- they have an open exit toward the
        # package corner -- and fall through to the standard rescue
        # gate which uses the row-level violation check.
        if idx == 0 or idx >= len(same_edge) - 1:
            return False

        prev_pad = same_edge[idx - 1]
        next_pad = same_edge[idx + 1]

        return prev_pad.net in plane_nets and next_pad.net in plane_nets

    @staticmethod
    def _other_footprint_pads(
        package: PackageInfo,
        row_pads: list[Pad],
    ) -> list[Pad]:
        """Return pads on the same footprint that are NOT in ``row_pads``.

        Issue #2755: The escape generators group pads into per-edge
        (or per-row) buckets and drop plane-net pads (``net == 0``) before
        running the clearance check.  When a segment from the north edge of
        a TQFP escapes laterally, it can still land on a VCC/GND pad (which
        was filtered out) or an east-edge pad (which is in a different
        bucket).  This helper returns the complement -- every pad on the
        footprint that the row-level check would otherwise miss -- so the
        caller can pass it to ``_segment_violates_pad_clearance`` as
        ``extra_pads``.

        Identification is by object identity, so callers can re-use the
        original ``package.pads`` list (which includes plane-net pads).
        """
        row_ids = {id(p) for p in row_pads}
        return [p for p in package.pads if id(p) not in row_ids]

    def _segment_violates_pad_clearance(
        self,
        seg: Segment,
        pad_index: int,
        pads: list[Pad],
        min_clearance: float,
        extra_pads: list[Pad] | None = None,
    ) -> bool:
        """Check whether *seg* violates clearance against neighbouring pads.

        Issue #2350: Checks ALL pads in the row, not just immediate neighbors.
        On fine-pitch packages (e.g. 20-pin SSOP), a lateral escape may
        violate clearance against pad[i+2] while only pad[i+1] was previously
        checked.  The segment's own pad (at pad_index) is skipped because the
        segment originates from it.

        Issue #2755: Optionally checks ``extra_pads`` (typically the OTHER
        pads on the same footprint -- the ones not in the current edge/row
        ``pads`` list).  Per-edge escape generation previously only checked
        against pads on the SAME edge of a QFP, missing collisions where an
        escape stub from the north edge ran across a pad on the east edge
        (or a plane-net pad that was filtered out of ``pads`` because its
        net was 0).  ``extra_pads`` are checked in addition to ``pads``;
        the source pad is identified by object identity to avoid index
        collisions across the two lists.

        Returns True if any pad in either list violates clearance.
        """
        # Source pad identity for skipping (when in either list).
        source_pad: Pad | None = pads[pad_index] if 0 <= pad_index < len(pads) else None

        for neighbor_idx in range(len(pads)):
            if neighbor_idx == pad_index:
                continue
            neighbor = pads[neighbor_idx]
            # Only check pads on the same layer as the segment
            if neighbor.layer != seg.layer:
                continue
            gap = self._segment_to_pad_edge_gap(seg, neighbor)
            if gap < min_clearance - 1e-6:
                return True

        # Issue #2755: Check the additional pads (other edges of the
        # same footprint, plane-net pads, etc.).  Skip the source pad
        # by identity in case the caller accidentally included it.
        if extra_pads:
            for neighbor in extra_pads:
                if source_pad is not None and neighbor is source_pad:
                    continue
                if neighbor.layer != seg.layer:
                    continue
                gap = self._segment_to_pad_edge_gap(seg, neighbor)
                if gap < min_clearance - 1e-6:
                    return True

        return False

    @staticmethod
    def _min_segment_distance(s1: Segment, s2: Segment) -> float:
        """Return the minimum centre-line distance between two segments.

        Uses closest-point-on-segment computation for each pair of
        endpoints/projections.  This is the geometric distance between the
        two line-segments (not accounting for trace width -- the caller
        subtracts half-widths separately).
        """

        def _dot(ax: float, ay: float, bx: float, by: float) -> float:
            return ax * bx + ay * by

        def _clamp01(v: float) -> float:
            return max(0.0, min(1.0, v))

        def _point_seg_dist(
            px: float, py: float, ax: float, ay: float, bx: float, by: float
        ) -> float:
            abx, aby = bx - ax, by - ay
            apx, apy = px - ax, py - ay
            len_sq = abx * abx + aby * aby
            if len_sq < 1e-12:
                return math.sqrt(apx * apx + apy * apy)
            t = _clamp01(_dot(apx, apy, abx, aby) / len_sq)
            cx, cy = ax + t * abx, ay + t * aby
            dx, dy_val = px - cx, py - cy
            return math.sqrt(dx * dx + dy_val * dy_val)

        # Check all four endpoint-to-segment distances, plus
        # segment-segment closest approach.
        d1 = _point_seg_dist(s1.x1, s1.y1, s2.x1, s2.y1, s2.x2, s2.y2)
        d2 = _point_seg_dist(s1.x2, s1.y2, s2.x1, s2.y1, s2.x2, s2.y2)
        d3 = _point_seg_dist(s2.x1, s2.y1, s1.x1, s1.y1, s1.x2, s1.y2)
        d4 = _point_seg_dist(s2.x2, s2.y2, s1.x1, s1.y1, s1.x2, s1.y2)
        return min(d1, d2, d3, d4)

    def _validate_escape_clearances(
        self,
        escapes: list[EscapeRoute],
        min_clearance: float,
        row_pads: list[Pad] | None = None,
    ) -> None:
        """Validate pairwise clearance between consecutive escape routes.

        Iterates through adjacent escape routes and checks that all
        surface-layer segments maintain at least *min_clearance* edge-to-edge
        distance.  Logs a warning for any violating pair so that regressions
        are visible without silently producing DRC violations.

        Issue #2319: When *row_pads* is provided, also validates each
        segment against neighboring pad copper (segment-to-pad clearance).
        """
        # Segment-to-segment validation (original)
        for idx in range(len(escapes) - 1):
            e1 = escapes[idx]
            e2 = escapes[idx + 1]
            for seg1 in e1.segments:
                for seg2 in e2.segments:
                    if seg1.layer != seg2.layer:
                        continue  # different layers cannot violate
                    centre_dist = self._min_segment_distance(seg1, seg2)
                    edge_gap = centre_dist - (seg1.width + seg2.width) / 2
                    if edge_gap < min_clearance - 1e-6:
                        logger.warning(
                            "Escape clearance violation between pads %s and %s "
                            "on %s: gap=%.4fmm (required %.4fmm)",
                            e1.pad.net_name,
                            e2.pad.net_name,
                            seg1.layer.kicad_name,
                            edge_gap,
                            min_clearance,
                        )

        # Issue #2319: Segment-to-pad validation
        if row_pads:
            # Build a quick lookup: pad -> index in row
            pad_indices: dict[int, int] = {id(p): idx for idx, p in enumerate(row_pads)}
            for escape in escapes:
                pad_idx = pad_indices.get(id(escape.pad))
                if pad_idx is None:
                    continue
                for seg in escape.segments:
                    # Check against neighboring pads (not the escape's own pad)
                    for neighbor_offset in (-1, 1):
                        ni = pad_idx + neighbor_offset
                        if ni < 0 or ni >= len(row_pads):
                            continue
                        neighbor = row_pads[ni]
                        if neighbor.layer != seg.layer:
                            continue
                        gap = self._segment_to_pad_edge_gap(seg, neighbor)
                        if gap < min_clearance - 1e-6:
                            logger.warning(
                                "Escape segment-to-pad clearance violation: "
                                "segment of %s vs pad %s on %s: "
                                "gap=%.4fmm (required %.4fmm)",
                                escape.pad.net_name,
                                neighbor.net_name,
                                seg.layer.kicad_name,
                                gap,
                                min_clearance,
                            )

    def _escape_sop_staggered(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes with staggered vias for SOP/TSSOP/SOIC packages.

        For dual-row packages (SOP, TSSOP, SOIC), pins escape perpendicular to
        the pin row, with vias placed in a staggered pattern to prevent blocking
        adjacent pins.

        Pattern (for horizontal dual-row):
        ```
        Pin row 1:  [1][2][3][4][5][6][7][8]
                     |  |  |  |  |  |  |  |
        Escape:     -+--|--+--|--+--|--+--|
                     |  |  |  |  |  |  |  |
        Via row 1:  [V]    [V]    [V]    [V]  (odd pins)
        Via row 2:     [V]    [V]    [V]    [V] (even pins, offset)

        Pin row 2:  [16][15][14][13][12][11][10][9]
        ```

        The staggered pattern ensures that vias from one pin don't block the
        escape path of adjacent pins, allowing all pins to route out successfully.

        Args:
            package: SOP/TSSOP/SOIC package info

        Returns:
            List of escape routes with staggered via placement
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Separate pads into two rows
        top_row: list[Pad] = []
        bottom_row: list[Pad] = []
        left_col: list[Pad] = []
        right_col: list[Pad] = []

        # Determine orientation.  Issue #3343: the raw bounding-box
        # spread comparison misclassifies packages whose BODY is wider
        # than tall but whose pads form two vertical COLUMNS — e.g. a
        # SOT-23-5 (two columns at dx=2.2 mm, three pads per column at
        # 0.95 mm pitch, y-spread 1.9 mm).  Spread says "horizontal",
        # the pads get split into y-"rows" that each contain stacked
        # same-x pads, and the perpendicular launch from one pad runs
        # straight THROUGH its column neighbour (observed on softstart
        # rev B: the U3/U8 pin-2 escape stubs overlapped pin 3, leaving
        # ISENSE_NEG / V_AC_SENSE_RAW statically unroutable).
        #
        # A dual-row package has exactly TWO coordinate clusters on its
        # cross axis and >2 on the row axis.  Cluster the coordinates
        # and use that signature; fall back to the legacy spread
        # comparison only when the cluster counts are ambiguous (e.g.
        # 2x2 four-pad packages, where either split is geometrically
        # safe).
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        def _cluster_count(vals: list[float], tol: float = 0.2) -> int:
            count = 0
            prev: float | None = None
            for v in sorted(vals):
                if prev is None or v - prev > tol:
                    count += 1
                prev = v
            return count

        x_clusters = _cluster_count(xs)
        y_clusters = _cluster_count(ys)

        if x_clusters == 2 and y_clusters > 2:
            # Two vertical columns (SOT-23-5/6, vertical SOP)
            is_horizontal = False
        elif y_clusters == 2 and x_clusters > 2:
            # Two horizontal rows (typical SOP/SOIC/TSSOP)
            is_horizontal = True
        else:
            # Ambiguous — preserve the legacy spread heuristic.
            is_horizontal = x_spread > y_spread  # pins arranged horizontally

        if is_horizontal:
            # Split by Y position
            for pad in package.pads:
                if pad.y > center_y:
                    top_row.append(pad)
                else:
                    bottom_row.append(pad)
            # Sort rows by X position
            top_row.sort(key=lambda p: p.x)
            bottom_row.sort(key=lambda p: p.x)

            # Generate escapes for each row
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=top_row,
                    direction=EscapeDirection.NORTH,
                    package=package,
                )
            )
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=bottom_row,
                    direction=EscapeDirection.SOUTH,
                    package=package,
                )
            )
        else:
            # Vertical orientation - split by X position
            for pad in package.pads:
                if pad.x > center_x:
                    right_col.append(pad)
                else:
                    left_col.append(pad)
            # Sort columns by Y position
            left_col.sort(key=lambda p: p.y)
            right_col.sort(key=lambda p: p.y)

            # Generate escapes for each column
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=left_col,
                    direction=EscapeDirection.WEST,
                    package=package,
                )
            )
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=right_col,
                    direction=EscapeDirection.EAST,
                    package=package,
                )
            )

        return escapes

    def _rescue_only_band_escapes(
        self,
        pads: list[Pad],
        direction: EscapeDirection,
        package: PackageInfo,
        effective_clearance: float,
        escape_width: float,
        rescue_eligible: bool,
    ) -> list[EscapeRoute]:
        """Consumer-aware in-pad rescues for one rescue-only-band SOP row.

        Issue #3398: P_FP6's naive full-row rescue on UCC27211 SOIC-8
        placed 19 in-pad vias around the softstart FET pairs and dropped
        reach 18 -> 8/30 -- the via field blocked the GATE/UCC/VGATE
        nets whose consumers sit 2-12 mm away (bootstrap caps, TVS
        clamps, gate resistors).  The rescue's value proposition (free
        the launch corridor by escaping vertically) only pays off when
        the net must TRAVERSE the congested neighbourhood to a far
        consumer; for local hops the via + back-layer stub costs more
        corridor than it frees.

        Decision ladder (every "defer" emits no escape geometry at all,
        reproducing the pre-#3398 not-dense behaviour for that pad):

        1. Plane/skipped pads (``net == 0``) -> defer (pours connect
           them; an escape would burn corridor for nothing).
        2. Package not rescue-eligible (no fine-pitch region, tier-0
           manufacturer, pitch/headroom fail) -> defer all.
        3. No off-package consumer information in
           ``net_target_positions`` -> defer (cannot prove the rescue
           helps; the conservative choice is the pre-#3398 behaviour).
        4. Nearest off-package consumer within
           :data:`SOP_LOCAL_CONSUMER_RADIUS_MM` -> defer (local hop).
        5. Row cap (:data:`SOP_RESCUE_MAX_PER_ROW`, env-overridable via
           ``KICAD_TOOLS_SOP_RESCUE_ROW_CAP``): among the surviving
           far-consumer candidates, only the pad(s) with the FARTHEST
           consumer get the rescue; the rest defer.  The DEFAULT cap is
           0 -- defer everything -- because four same-machine paired
           A/B measurements on softstart rev B showed every
           rescue-firing configuration is net-negative under production
           budgets (see the :data:`SOP_RESCUE_MAX_PER_ROW` comment for
           the measurement table and mechanisms).
        6. Winner -> attempt :meth:`_try_in_pad_escape`.  The stub
           direction comes from :meth:`_compute_target_direction`
           (Issue #3428 machinery) but is only honoured when it agrees
           with the row's outward perpendicular -- a stub running ALONG
           the row axis sweeps the row's shared back-layer launch
           corridor (the Jun 9 A/B failure geometry), so perpendicular
           targets fall back to the legacy outward stub.  ``None`` from
           the rescue -> that pad emits nothing and the next-farthest
           candidate is tried (the cap counts SUCCESSFUL rescues).

        Args:
            pads: Row of pads sorted by position.
            direction: Outward perpendicular escape direction for this
                row (legacy stub direction when the target map has no
                opinion).
            package: Package context.
            effective_clearance: Launch clearance from the caller (per-
                ref fine-pitch value when a region matched).
            escape_width: Stub trace width from the caller.
            rescue_eligible: Package-level
                :meth:`_sop_in_pad_rescue_eligible` result.

        Returns:
            At most :data:`SOP_RESCUE_MAX_PER_ROW` in-pad
            ``EscapeRoute`` objects for the row (possibly empty).
        """
        row_cap = _sop_rescue_row_cap()
        if not rescue_eligible or row_cap <= 0:
            # Default (row_cap == 0, Issue #3398): every pad defers and
            # the package routes exactly as it did before entering the
            # dense list.  See :data:`SOP_RESCUE_MAX_PER_ROW` for the
            # empirical A/B basis of the defer-all default.
            return []

        # Steps 1-4: collect far-consumer candidates.
        candidates: list[tuple[float, int, Pad]] = []
        for i, pad in enumerate(pads):
            if pad.net == 0:
                continue
            positions = self.net_target_positions.get(pad.net) or []
            off_package = [(x, y) for x, y, ref in positions if ref != package.ref]
            if not off_package:
                continue
            nearest = min(math.hypot(x - pad.x, y - pad.y) for x, y in off_package)
            if nearest <= SOP_LOCAL_CONSUMER_RADIUS_MM:
                logger.debug(
                    "SOP rescue deferred for %s pin %s (net %s): nearest "
                    "off-package consumer %.2f mm <= %.2f mm locality radius "
                    "(Issue #3398).",
                    package.ref,
                    pad.pin,
                    pad.net_name,
                    nearest,
                    SOP_LOCAL_CONSUMER_RADIUS_MM,
                )
                continue
            candidates.append((nearest, i, pad))

        # Step 5: farthest consumer first; row index breaks exact ties
        # deterministically.
        candidates.sort(key=lambda c: (-c[0], c[1]))

        routes: list[EscapeRoute] = []
        for nearest, _i, pad in candidates:
            if len(routes) >= row_cap:
                logger.debug(
                    "SOP rescue deferred for %s pin %s (net %s): row cap "
                    "of %d rescue(s) already granted (far consumer at "
                    "%.2f mm; Issue #3398).",
                    package.ref,
                    pad.pin,
                    pad.net_name,
                    row_cap,
                    nearest,
                )
                continue

            # Step 6: attempt the rescue for the winner.
            target_dir = self._compute_target_direction(pad, package, direction)
            # Issue #3398 (Jun 9 A/B measurement): an inner stub running
            # ALONG the row axis (target perpendicular to the outward
            # escape direction) sweeps the row's shared back-layer
            # launch corridor and the sibling pads' B.Cu landing zones
            # -- on softstart the SOUTH-pointing stubs from U5/U6 pin 8
            # crossed the pin 5-7 column and degraded the FET-bus
            # neighbourhood even with the row cap in place.  Only keep
            # the target-aware override when it agrees with the outward
            # perpendicular; otherwise fall back to the legacy
            # perpendicular stub (``target_direction=None``) and let
            # the main router turn toward the consumer in open space.
            if target_dir is not None and target_dir != direction:
                logger.debug(
                    "SOP rescue stub for %s pin %s (net %s): target "
                    "direction %s runs along the row axis; using outward "
                    "perpendicular %s instead (Issue #3398).",
                    package.ref,
                    pad.pin,
                    pad.net_name,
                    target_dir.name,
                    direction.name,
                )
                target_dir = None
            in_pad_route = self._try_in_pad_escape(
                pad=pad,
                direction=direction,
                effective_clearance=effective_clearance,
                escape_width=escape_width,
                package=package,
                skip_on_clearance_violation=self.strict_in_pad_clearance,
                target_direction=target_dir,
            )
            if in_pad_route is None:
                logger.debug(
                    "SOP rescue infeasible for %s pin %s (net %s): "
                    "_try_in_pad_escape declined; trying next-farthest "
                    "candidate (Issue #3398).",
                    package.ref,
                    pad.pin,
                    pad.net_name,
                )
                continue
            logger.info(
                "SOP in-pad rescue for %s pin %s (net %s): rescue-only "
                "band, far consumer at %.2f mm, stub direction %s, "
                "in-pad via at (%.3f, %.3f) (Issue #3398).",
                package.ref,
                pad.pin,
                pad.net_name,
                nearest,
                (target_dir or direction).name,
                in_pad_route.via_pos[0] if in_pad_route.via_pos else 0.0,
                in_pad_route.via_pos[1] if in_pad_route.via_pos else 0.0,
            )
            routes.append(in_pad_route)
        return routes

    def _sop_in_pad_rescue_eligible(
        self,
        package: PackageInfo,
        pads: list[Pad],
    ) -> bool:
        """Return True when the SOP staggered path should attempt the in-pad rescue.

        Issue #3381 / P_FP6 -- gate function for wiring
        :meth:`_try_in_pad_escape` into the SOP staggered dispatcher
        (``_create_staggered_row_escapes``).  This mirrors the QFP
        dispatcher pattern (``_escape_qfp_alternating``) which already
        consults the in-pad rescue when fine-pitch clearance pressure
        cannot be resolved by the legacy escape geometry.

        Four conjunctive conditions must hold for the rescue to be
        eligible:

        1. **Manufacturer capability** -- ``self.via_in_pad_supported``
           must be True (jlcpcb-tier1, pcbway).  Tier-0 jlcpcb returns
           False and the rescue is skipped.  Mirrors the QFP gate.
        2. **Pitch band** -- ``package.pin_pitch <= 1.5`` mm.  This
           covers SOIC-8 (1.27 mm pitch family) plus tighter SSOP/TSSOP
           variants but excludes 2.54 mm-pitch SOPs that have plenty of
           routing channel and don't need the rescue.  The 1.5 mm
           threshold matches :data:`FINE_PITCH_THRESHOLD_MM` used by the
           fine-pitch package classifier.
        3. **Fine-pitch region installed** -- a
           :class:`~kicad_tools.router.fine_pitch_escape.FinePitchRegion`
           must match ``package.ref``.  This is the opt-in switch: only
           packages the board author or detector flagged for fine-pitch
           treatment participate.  Without an installed region the
           rescue is a no-op and SOP escape geometry is bit-identical
           to the pre-P_FP6 path.
        4. **Geometric headroom** -- the pad long-axis dimension must
           accommodate the manufacturer's minimum via OD (drill + 2x
           annular ring).  UCC27211 SOIC-8 pads are 0.30 x 1.55 mm; the
           long-axis 1.55 mm comfortably exceeds the JLCPCB tier-1
           minimum via OD of 0.60 mm.  This guard short-circuits
           ``_try_in_pad_escape``'s internal long-axis check so the
           dispatcher can decide eligibility without speculatively
           invoking the rescue helper.

        Args:
            package: Package info from ``analyze_package``.
            pads: Row of pads under consideration; the first pad supplies
                geometry for the long-axis headroom check (all pads in
                an SOP row share identical width/height per the package
                classifier).

        Returns:
            True iff all four conditions hold; False otherwise.
        """
        # (1) Manufacturer capability.
        if not self.via_in_pad_supported:
            return False

        # (2) Pitch band -- gate at the FINE_PITCH_THRESHOLD_MM cap so
        # 1.27 mm SOIC qualifies but 2.54 mm SOPs do not.  The architect
        # proposal pinned the boundary at 1.5 mm; we re-use the existing
        # module constant for source-of-truth alignment.  ``getattr``
        # defends against synthetic test ``PackageInfo`` stand-ins that
        # may not carry every attribute the real classifier produces.
        pin_pitch = getattr(package, "pin_pitch", None)
        if pin_pitch is None or pin_pitch > FINE_PITCH_THRESHOLD_MM:
            return False

        # (3) Fine-pitch region must match this package ref.  We re-use
        # ``_escape_clearance_for_ref`` as the "is there a region for
        # this ref?" probe: when a region matched AND the narrow-channel
        # guard allowed the shrink, the per-ref clearance is strictly
        # less than ``rules.trace_clearance``.  This is the same
        # detection signal P_FP5 already uses one block below for the
        # launch-step shrink, so the two gates stay in lock-step.
        per_ref_clearance = self._escape_clearance_for_ref(package.ref, pads)
        if per_ref_clearance >= self.rules.trace_clearance - 1e-9:
            return False

        # (4) Geometric headroom.  ``_try_in_pad_escape`` validates the
        # long-axis dimension internally, but checking here lets the
        # caller defer the more expensive _try_in_pad_escape invocation
        # for pads that clearly cannot host an in-pad via on this
        # manufacturer tier.
        if self._mfr_limits is not None:
            min_via_od = self._mfr_limits.min_via_diameter
        else:
            min_via_od = self.rules.via_diameter
        if not pads:
            return False
        long_axis = max(pads[0].width, pads[0].height)
        if long_axis < min_via_od - 1e-6:
            return False

        return True

    def _create_staggered_row_escapes(
        self,
        pads: list[Pad],
        direction: EscapeDirection,
        package: PackageInfo,
    ) -> list[EscapeRoute]:
        """Create escape routes for a row of pads with staggered via placement.

        Args:
            pads: Row of pads sorted by position
            direction: Primary escape direction (perpendicular to row)
            package: Package info for bounds

        Returns:
            List of escape routes with staggered vias
        """
        escapes: list[EscapeRoute] = []
        dx, dy = self._direction_to_vector(direction)

        # Issue #3371 / P_FP5: when this package sits in an installed
        # fine-pitch escape region (e.g. UCC27211 SOIC-8 at strict 0.20mm
        # clearance), the per-component helper returns the region's tighter
        # ``escape_clearance`` (e.g. 0.14mm at jlcpcb-tier1).  The narrow-
        # channel guard inside the helper declines the shrink when the
        # corridor cannot accommodate it, so this never produces an
        # infeasible escape stub on packages outside the fine-pitch ladder.
        #
        # Detection: we treat ``per_ref_clearance < rules.trace_clearance``
        # as the signal that a region matched (because
        # ``get_clearance_for_component`` falls back to ``trace_clearance``
        # for non-fine-pitch refs / when no region is installed).  When
        # detected, we use the tight clearance for the launch step;
        # otherwise we preserve the pre-P_FP5 ``self.escape_clearance``
        # exactly so existing boards see bit-identical SOP escape geometry.
        per_ref_clearance = self._escape_clearance_for_ref(package.ref, pads)
        legacy_clearance = self.escape_clearance
        if per_ref_clearance < self.rules.trace_clearance - 1e-9:
            # Fine-pitch region matched and the narrow-channel guard
            # allowed the shrink; use the tighter clearance.
            effective_escape_clearance = per_ref_clearance
        else:
            # No region matched (or guard declined the shrink); preserve
            # the legacy launch distance bit-for-bit.
            effective_escape_clearance = legacy_clearance

        # Issue #3381 / P_FP6: in-pad rescue for the SOP staggered path.
        # When the package + manufacturer + region + pad geometry all
        # cooperate, attempt :meth:`_try_in_pad_escape` AS A FIRST TRY
        # for every non-plane pad in the row; on success the in-pad
        # route REPLACES the staggered geometry (the via is placed
        # dead-centre on the pad and escapes vertically onto the
        # inner / back layer, freeing the F.Cu launch corridor for
        # the main router).  On ``None`` (geometric infeasibility,
        # neighbour-pad clipping that even the long-axis nudge cannot
        # resolve), fall through to the legacy staggered geometry so
        # the pre-P_FP6 behaviour is preserved for non-rescued pads.
        #
        # The architect proposal (Issue #3381) mirrors the QFP
        # dispatcher pattern at ``_escape_qfp_alternating`` ~line 2494
        # but raises the trigger from "only when the legacy clips" to
        # "always when eligible".  The latter is required because the
        # SOP staggered path produces a perpendicular launch that
        # NEVER clips a same-row neighbour (the trace runs straight
        # away from the row, the launch X coordinate never moves
        # laterally), so the QFP-style violation trigger would never
        # fire on SOP geometry.  The downstream-routing congestion
        # that the rescue addresses cannot be detected from the
        # escape phase alone -- so the gate is geometric-feasibility-
        # plus-region-opt-in.
        in_pad_rescue_eligible = self._sop_in_pad_rescue_eligible(package, pads)
        # Escape width and rescue clearance mirror the values used in
        # ``_escape_qfp_alternating``'s in-pad invocation (the latter is
        # the per-component clearance, not the post-via-trace clearance,
        # because the rescue replaces the surface launch with an
        # in-pad via and an inner-layer stub).
        rescue_escape_width = (
            self.rules.min_trace_width
            if self.rules.min_trace_width is not None
            else self._get_trace_width_for_net(pads[0].net_name if pads else "")
        )

        # Issue #3398: rescue-only band detection.  A dual-row SMD package
        # whose pitch sits ABOVE both the always-dense 0.75 mm cap and the
        # dynamic between-pin-trace threshold is NOT geometrically dense --
        # a trace fits between adjacent pins at the active design rules.
        # Such packages (UCC27211 / LM393 SOIC-8 at 1.27 mm pitch under
        # 0.30/0.20 rules) only enter the dense list via the #3398
        # SOIC-8-class band in :func:`is_dense_package`, and they enter it
        # for exactly ONE reason: to give the P_FP6 in-pad rescue a chance
        # to free the launch corridor for far-away consumers.  Emitting the
        # legacy staggered via geometry for them would CREATE congestion
        # where none existed (the pre-#3398 router placed no escape
        # geometry at all and reached 18/30 on softstart; naive full-row
        # rescue dropped it to 8/30).  Therefore rescue-only-band packages
        # are "rescue or nothing": each pad either gets a consumer-aware
        # in-pad rescue (far target only) or NO escape geometry.
        dynamic_threshold = 2 * (self.rules.trace_width + self.rules.trace_clearance)
        package_pitch = getattr(package, "pin_pitch", None) or 0.0
        rescue_only_band = package_pitch > 0.75 + 1e-9 and package_pitch >= dynamic_threshold - 1e-9

        # Issue #3398: rescue-only band -- never fall through to the
        # staggered geometry.  Pads with local consumers, plane pads,
        # ineligible packages, geometric rescue failures, and row-cap
        # losers all emit NOTHING, which reproduces the pre-#3398
        # (not-dense) routing behaviour bit-for-bit for those pads.
        # At most ``_sop_rescue_row_cap()`` pad(s) per row -- the one(s)
        # with the farthest off-package consumer -- get an in-pad
        # rescue; the production default cap is 0 (defer all; see the
        # :data:`SOP_RESCUE_MAX_PER_ROW` measurement table).
        if rescue_only_band:
            # Issue #3900: when the rescue cap is 0 (the production default)
            # no escape is *attempted* for this band -- every pad defers and
            # the package routes exactly as it did before entering the dense
            # list.  Record that fact so ``generate_escapes`` reports "not
            # attempted" instead of the misleading "failed clearance
            # validation" when the resulting escape list is empty.
            if _sop_rescue_row_cap() == 0:
                self._escape_not_attempted_rescue_band = True
            escapes.extend(
                self._rescue_only_band_escapes(
                    pads=pads,
                    direction=direction,
                    package=package,
                    effective_clearance=effective_escape_clearance,
                    escape_width=rescue_escape_width,
                    rescue_eligible=in_pad_rescue_eligible,
                )
            )
            return escapes

        # Calculate base escape distance and stagger offset
        base_escape_dist = effective_escape_clearance + self.rules.trace_width
        stagger_offset = self.via_spacing / 2

        for i, pad in enumerate(pads):
            # Stagger: odd pins get extra offset (two via rows)
            is_odd = i % 2 == 1
            escape_dist = base_escape_dist + (stagger_offset if is_odd else 0)

            # Calculate via position (perpendicular to pin row)
            via_x = pad.x + dx * escape_dist
            via_y = pad.y + dy * escape_dist

            # Issue #3381 / P_FP6: try the in-pad rescue first when
            # eligible.  Plane-net pads (net == 0) are skipped: they
            # connect via plane stitching, not via escape, so a rescue
            # would burn a via budget on a net that doesn't need one.
            # This mirrors the ``_escape_qfp_alternating`` plane-net
            # guard at line 2340.  When the rescue returns ``None``
            # (geometric infeasibility, neighbour-pad clipping that
            # even the long-axis nudge cannot resolve, or strict-mode
            # deferral), the pad falls through to the legacy
            # staggered geometry below.
            if in_pad_rescue_eligible and pad.net != 0:
                in_pad_route = self._try_in_pad_escape(
                    pad=pad,
                    direction=direction,
                    effective_clearance=effective_escape_clearance,
                    escape_width=rescue_escape_width,
                    package=package,
                    skip_on_clearance_violation=self.strict_in_pad_clearance,
                )
                if in_pad_route is not None:
                    package_type_name = getattr(
                        getattr(package, "package_type", None), "name", "SOP"
                    )
                    logger.info(
                        "SOP in-pad rescue for %s pin %s (net %s): "
                        "%s package, placed in-pad via at (%.3f, %.3f) "
                        "(Issue #3381 / P_FP6).",
                        package.ref,
                        pad.pin,
                        pad.net_name,
                        package_type_name,
                        in_pad_route.via_pos[0] if in_pad_route.via_pos else 0.0,
                        in_pad_route.via_pos[1] if in_pad_route.via_pos else 0.0,
                    )
                    escapes.append(in_pad_route)
                    continue

            # Escape point is beyond the via.  P_FP5 (#3371): the post-via
            # clearance is governed by ``rules.trace_clearance`` (the
            # neighbour-trace clearance on the destination escape layer);
            # we deliberately do NOT use the tighter
            # ``effective_escape_clearance`` here because the post-via
            # trace sits in the open space beyond the package, where the
            # standard clearance applies.  Preserving ``trace_clearance``
            # keeps the pre-P_FP5 escape-point geometry bit-identical for
            # non-fine-pitch packages.  The launch step (above) is where
            # the fine-pitch shrink saves corridor width inside the halo.
            escape_x = via_x + dx * (self.rules.via_diameter + self.rules.trace_clearance)
            escape_y = via_y + dy * (self.rules.via_diameter + self.rules.trace_clearance)

            # Determine escape layer (alternate layers for denser routing)
            escape_layer = Layer.B_CU if is_odd else Layer.F_CU

            # Create segments
            segments: list[Segment] = []

            # Segment from pad to via
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            # Create via
            via = Via(
                x=via_x,
                y=via_y,
                drill=self.rules.via_drill,
                diameter=self.rules.via_diameter,
                layers=(pad.layer, escape_layer),
                net=pad.net,
                net_name=pad.net_name,
            )

            # Segment from via to escape point on escape layer
            segments.append(
                Segment(
                    x1=via_x,
                    y1=via_y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=escape_layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            escapes.append(
                EscapeRoute(
                    pad=pad,
                    direction=direction,
                    escape_point=(escape_x, escape_y),
                    escape_layer=escape_layer,
                    via_pos=(via_x, via_y),
                    segments=segments,
                    via=via,
                    ring_index=0,
                )
            )

        return escapes

    def _escape_multi_row_connector(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate BGA-style fanout escape routes for multi-row connectors.

        Multi-row through-hole connectors (e.g., 2x20 pin headers at 2.54mm
        pitch) cannot use simple radial escape because inner-row pads are
        blocked by outer-row escape paths.

        Strategy (row-aware, analogous to BGA ring escape):
        - Rows are sorted by distance from package center (outermost first).
        - Outer rows (ring_index 0): escape perpendicular on the surface
          layer (F.Cu).  No via needed.
        - Inner rows (ring_index >= 1): short trace to a staggered via,
          then escape on an inner/back layer selected via
          ``_select_inner_escape_layer()``.
        - Via positions within each inner row are staggered along the row
          axis to maintain via-to-via clearance.

        Works for 2xN, 3xN, and 4xN through-hole arrangements.

        Issue #3310: When the connector is board-edge-aligned (one side
        has dramatically less routing space than the other -- e.g. chorus
        J2 is the 40-pin RPi GPIO header on the east board edge with 0
        foreign-component pads east of it and 251 west), the original
        "outermost first" sort produces an inverted escape pattern: the
        row closest to the populated side escapes toward the populated
        side (correct), but the far-side row escapes AWAY from the
        populated side into empty/edge space, AND its 20 vias all land
        in a single 47mm-tall column just outside the package edge.
        The escape endpoints sit OFF the board with no destinations to
        reach -- the main router fails with ``blocked_path`` because
        the destination side is on the OPPOSITE side of the via wall.

        For board-edge-aligned connectors the rebuilt geometry:
          - Both rows escape toward the populated side.
          - The closer row escapes on F_CU (no via -- shortest path).
          - The far row uses a SHORT-trace via escape: a short trace
            from the pad to a via placed on the empty side, then a
            short stub (one trace-clearance) past the via on the inner
            layer.  The endpoint sits adjacent to the via, on the same
            side, so the inner-layer trace stays short (~0.5 mm) and
            does NOT run along the via column.  Long inner-layer
            traces would intersect peer pads' vias and get dropped by
            the apply_escape_routes foreign-via gate.
          - Adjacent inner-row pads ALTERNATE between two inner layers
            (e.g. In1.Cu and B.Cu on a 4-layer board).  Layer
            alternation gives the main router two independent routing
            planes through the connector region for cross-traffic.
          - Via positions are staggered PERPENDICULAR to the row axis
            (one row at via_offset_base + perp_stagger, the other at
            via_offset_base) so the via wall has lateral gaps the
            main router can squeeze through.  The perpendicular
            stagger amount accounts for the widest trace any row pad
            might produce (power-net trace widths) so inner-layer
            escape segments still meet edge-to-edge clearance against
            adjacent escapes.

        Args:
            package: MULTI_ROW_CONNECTOR package info (>= 20 pins, multi-row, TH)

        Returns:
            List of escape routes with layer-aware escape
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Determine connector orientation from pad positions
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        # "horizontal" means the long axis is X (many columns, few rows of Y)
        is_horizontal = x_spread > y_spread

        # Group pads into rows.  For a horizontal connector the rows are
        # distinguished by their Y coordinate; for vertical, by X.
        if is_horizontal:
            row_coords = sorted({round(p.y, 2) for p in package.pads})
            rows_map: dict[float, list[Pad]] = {rc: [] for rc in row_coords}
            for pad in package.pads:
                rows_map[round(pad.y, 2)].append(pad)
            for rc in row_coords:
                rows_map[rc].sort(key=lambda p: p.x)
        else:
            row_coords = sorted({round(p.x, 2) for p in package.pads})
            rows_map = {rc: [] for rc in row_coords}
            for pad in package.pads:
                rows_map[round(pad.x, 2)].append(pad)
            for rc in row_coords:
                rows_map[rc].sort(key=lambda p: p.y)

        # Issue #3310: Board-edge detection.  When the connector is
        # asymmetrically positioned (board edge, panel mount, etc.) we
        # need to route ALL rows toward the populated side rather than
        # away from the package center.  ``populated_dir`` is +1 if
        # populated side is the high-coord side, -1 if the low-coord
        # side, 0 if balanced (use original center-based strategy).
        populated_dir = self._detect_populated_routing_side(package, is_horizontal)

        if populated_dir != 0:
            # Sort rows in the OPPOSITE direction from the populated
            # side so the closest-to-populated row is processed last and
            # treated as the surface-layer outer row.
            if is_horizontal:
                sorted_coords = sorted(
                    row_coords,
                    key=lambda c: (c - center_y) * populated_dir,
                )
            else:
                sorted_coords = sorted(
                    row_coords,
                    key=lambda c: (c - center_x) * populated_dir,
                )
        else:
            # Symmetric case: sort rows by distance from center (outermost
            # first) and let each row escape outward.
            if is_horizontal:
                sorted_coords = sorted(row_coords, key=lambda c: abs(c - center_y), reverse=True)
            else:
                sorted_coords = sorted(row_coords, key=lambda c: abs(c - center_x), reverse=True)

        # Select inner escape layer once (not hardcoded)
        inner_escape_layer = self._select_inner_escape_layer(Layer.F_CU)

        # Issue #3310: For board-edge-aligned multi-row connectors, the
        # end-exit pattern routes ALL same-half inner-row pads to the
        # same connector end on a single inner layer.  Their inner-layer
        # traces would overlap if forced to the same layer.  When a
        # second routable inner layer (B.Cu on a 4-layer board) is
        # available, alternate between layers so adjacent pads land on
        # DIFFERENT layers and don't conflict on the long inner-layer
        # run.
        alt_inner_escape_layer: Layer | None = None
        if populated_dir != 0 and inner_escape_layer != Layer.B_CU:
            # Verify the alt layer is actually routable in this stack
            # (signal or mixed).  Plane layers (e.g. In2.Cu on chorus)
            # are excluded.
            if self.grid.layer_stack is not None:
                try:
                    alt_def = self.grid.layer_stack.get_layer_by_name(Layer.B_CU.kicad_name)
                    if alt_def is not None and alt_def.layer_type in (
                        LayerType.SIGNAL,
                        LayerType.MIXED,
                    ):
                        alt_inner_escape_layer = Layer.B_CU
                except Exception:  # noqa: BLE001
                    alt_inner_escape_layer = None
            else:
                # No layer stack info -- assume B.Cu is usable.
                alt_inner_escape_layer = Layer.B_CU

        escape_dist = self.escape_clearance + self.rules.trace_width
        via_offset_base = (
            self.rules.via_diameter / 2 + self.rules.via_clearance + self.rules.trace_clearance
        )

        # Issue #3310: For board-edge-aligned connectors, the "outer"
        # role goes to the row CLOSEST to the populated side.  This is
        # the LAST element in ``sorted_coords`` under the
        # populated-side sort.  All earlier rows are "far rows" that
        # need vias + short-trace inner-layer escapes.  Reverse so
        # ring_idx semantics are preserved (ring_idx 0 = outer
        # surface row).
        if populated_dir != 0:
            sorted_coords = list(reversed(sorted_coords))

        for ring_idx, coord in enumerate(sorted_coords):
            row_pads = rows_map[coord]
            is_outer = ring_idx == 0

            # Determine perpendicular escape direction for this row.
            if populated_dir != 0:
                # Issue #3310: all rows escape toward the populated side.
                if is_horizontal:
                    direction = (
                        EscapeDirection.NORTH if populated_dir > 0 else EscapeDirection.SOUTH
                    )
                else:
                    direction = EscapeDirection.EAST if populated_dir > 0 else EscapeDirection.WEST
            else:
                # Symmetric center-sorted strategy.
                if is_horizontal:
                    direction = EscapeDirection.NORTH if coord > center_y else EscapeDirection.SOUTH
                else:
                    direction = EscapeDirection.EAST if coord > center_x else EscapeDirection.WEST

            dx, dy = self._direction_to_vector(direction)

            for i, pad in enumerate(row_pads):
                trace_width = self._get_trace_width_for_net(pad.net_name)

                if is_outer:
                    # Outer row: surface escape, no via
                    ep_x = pad.x + dx * escape_dist
                    ep_y = pad.y + dy * escape_dist

                    segment = Segment(
                        x1=pad.x,
                        y1=pad.y,
                        x2=ep_x,
                        y2=ep_y,
                        width=trace_width,
                        layer=pad.layer,
                        net=pad.net,
                        net_name=pad.net_name,
                    )

                    escapes.append(
                        EscapeRoute(
                            pad=pad,
                            direction=direction,
                            escape_point=(ep_x, ep_y),
                            escape_layer=pad.layer,
                            via_pos=None,
                            segments=[segment],
                            via=None,
                            ring_index=0,
                        )
                    )
                else:
                    # Inner row: via to alternate layer.  Lateral stagger
                    # along the row axis prevents adjacent vias from
                    # colliding.
                    # Issue #3310: When alt_inner_escape_layer is set,
                    # alternate between two inner layers so adjacent
                    # pads' long inner-layer traces (running to the
                    # connector end on the same column) land on
                    # different layers and don't conflict.
                    if alt_inner_escape_layer is not None:
                        effective_inner_layer = (
                            inner_escape_layer if i % 2 == 0 else alt_inner_escape_layer
                        )
                    else:
                        effective_inner_layer = inner_escape_layer
                    stagger = (self.via_spacing / 2) * (1.0 if i % 2 == 0 else -1.0)
                    # Issue #3310: ALSO stagger perpendicular to the
                    # row axis so adjacent inner-layer traces don't
                    # violate clearance.  We must account for the
                    # widest trace any pad in this row might produce
                    # (e.g. power nets at 0.5mm vs signals at 0.2mm)
                    # so two adjacent inner-layer escapes -- one wide
                    # power trace, one narrow signal -- still meet
                    # edge-to-edge clearance.  Compute the max trace
                    # width across the row and size the stagger to
                    # max_trace + clearance + a small safety margin.
                    row_max_trace_width = max(
                        (self._get_trace_width_for_net(rp.net_name) for rp in row_pads),
                        default=self.rules.trace_width,
                    )
                    perp_stagger_amount = (
                        row_max_trace_width + self.rules.trace_clearance + self.rules.via_clearance
                    )
                    perp_stagger = perp_stagger_amount if i % 2 == 0 else 0.0

                    if populated_dir != 0:
                        # Issue #3310: For board-edge connectors, place
                        # the via on the side OPPOSITE the populated
                        # direction (i.e., away from where routing must
                        # exit).  This keeps the via out of the
                        # populated-side escape lane.
                        via_perp_sign = -populated_dir
                    else:
                        # Symmetric case: via on the escape side (same
                        # as direction).
                        via_perp_sign = 1 if (dx + dy) > 0 else -1
                        if is_horizontal:
                            via_perp_sign = 1 if dy > 0 else -1
                        else:
                            via_perp_sign = 1 if dx > 0 else -1

                    if is_horizontal:
                        # Long axis = X.  Vias offset in +/- Y direction.
                        via_x = pad.x + stagger
                        via_y = pad.y + via_perp_sign * (via_offset_base + perp_stagger)
                    else:
                        # Long axis = Y.  Vias offset in +/- X direction.
                        via_x = pad.x + via_perp_sign * (via_offset_base + perp_stagger)
                        via_y = pad.y + stagger

                    if populated_dir != 0:
                        # Issue #3310: For board-edge connectors the
                        # via sits on the OPPOSITE side from the
                        # populated direction.  The escape endpoint is
                        # placed SHORT (immediately adjacent to the via,
                        # on the via's offset side away from the pad).
                        # The main router then routes from this endpoint
                        # on the inner layer -- it can navigate around
                        # the connector's pads via the inner-layer gaps
                        # between vias OR exit past the connector ends.
                        # A SHORT inner-layer escape trace is critical
                        # because long traces (one running the full row
                        # length to a connector end) would intersect
                        # other pads' vias and get dropped by the
                        # apply_escape_routes foreign-via gate.
                        endpoint_offset = self.rules.via_diameter / 2 + self.rules.trace_clearance
                        if is_horizontal:
                            ep_x = via_x
                            ep_y = via_y + via_perp_sign * endpoint_offset
                        else:
                            ep_x = via_x + via_perp_sign * endpoint_offset
                            ep_y = via_y
                    else:
                        # Symmetric: escape point beyond via on inner layer
                        ep_x = via_x + dx * (
                            self.rules.via_diameter / 2 + self.rules.trace_clearance
                        )
                        ep_y = via_y + dy * (
                            self.rules.via_diameter / 2 + self.rules.trace_clearance
                        )

                    segments: list[Segment] = [
                        Segment(
                            x1=pad.x,
                            y1=pad.y,
                            x2=via_x,
                            y2=via_y,
                            width=trace_width,
                            layer=pad.layer,
                            net=pad.net,
                            net_name=pad.net_name,
                        ),
                        Segment(
                            x1=via_x,
                            y1=via_y,
                            x2=ep_x,
                            y2=ep_y,
                            width=trace_width,
                            layer=effective_inner_layer,
                            net=pad.net,
                            net_name=pad.net_name,
                        ),
                    ]

                    via = Via(
                        x=via_x,
                        y=via_y,
                        drill=self.rules.via_drill,
                        diameter=self.rules.via_diameter,
                        layers=(pad.layer, effective_inner_layer),
                        net=pad.net,
                        net_name=pad.net_name,
                    )

                    escapes.append(
                        EscapeRoute(
                            pad=pad,
                            direction=direction,
                            escape_point=(ep_x, ep_y),
                            escape_layer=effective_inner_layer,
                            via_pos=(via_x, via_y),
                            segments=segments,
                            via=via,
                            ring_index=ring_idx,
                        )
                    )

        # Validate pairwise clearances within each row
        self._validate_escape_clearances(escapes, self.rules.trace_clearance)

        return escapes

    def _detect_populated_routing_side(
        self,
        package: PackageInfo,
        is_horizontal: bool,
    ) -> int:
        """Detect which side of a multi-row connector has the populated
        routing area (Issue #3310).

        Returns +1 if the populated side is the high-coord side of the
        perpendicular axis (east for vertical, north for horizontal),
        -1 if the low-coord side (west / south), or 0 when the routing
        space is roughly balanced.

        Detection uses board-wide foreign-component pad counts.  If one
        side has < 10% of the total foreign pads OR fewer than 5 pads,
        the connector is classified as edge-aligned and the populated
        direction is the opposite side.

        Args:
            package: The multi-row connector package being escaped.
            is_horizontal: Whether the connector's long axis is X.  When
                True, "sides" are north / south; when False, east / west.

        Returns:
            +1, -1, or 0.
        """
        # Need foreign-pad access via the grid's pad registry.  When the
        # registry is empty (synthetic test fixtures) or unavailable,
        # fall back to the symmetric center-based strategy.
        pads = getattr(self.grid, "_pads", None)
        if not pads:
            return 0

        package_pad_ids = {id(p) for p in package.pads}
        center_x, center_y = package.center

        low_count = 0
        high_count = 0
        for p in pads:
            if id(p) in package_pad_ids:
                continue
            if is_horizontal:
                # Perpendicular axis = Y.
                if p.y < center_y:
                    low_count += 1
                else:
                    high_count += 1
            else:
                # Perpendicular axis = X.
                if p.x < center_x:
                    low_count += 1
                else:
                    high_count += 1

        total = low_count + high_count
        if total < 10:
            # Not enough foreign pads to make a confident classification.
            return 0

        # Threshold: < 10% on one side OR fewer than 5 pads -> edge-aligned.
        low_frac = low_count / total
        high_frac = high_count / total
        edge_pad_threshold = 5
        edge_frac_threshold = 0.10

        if low_count < edge_pad_threshold or low_frac < edge_frac_threshold:
            # Low-coord side is empty; populated side is high.
            return 1
        if high_count < edge_pad_threshold or high_frac < edge_frac_threshold:
            # High-coord side is empty; populated side is low.
            return -1
        return 0

    def _escape_radial(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate simple radial escapes for non-dense packages.

        Each pin escapes directly outward from package center.

        Issue #2756: When neighbour pads sit close enough to the launch
        line that the escape stub would clip them (the dominant failure
        mode on TO-220 MOSFETs Q5/Q6 on board 05), the segment endpoint
        is clipped to honour pad-to-segment clearance.  Stubs that get
        clipped below a useful threshold are dropped so the main router
        can pick the pad up cleanly instead of having to fight an
        already-violating escape segment.

        Args:
            package: Package info

        Returns:
            List of escape routes
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Issue #2756: resolve the effective clearance once per package.
        effective_clearance = self.rules.get_clearance_for_component(
            package.ref,
            pin_pitch=package.pin_pitch,
        )

        # Useful-length threshold for the clipped stub: half the original
        # launch distance.  Matches the heuristic in
        # ``_escape_qfp_alternating``.
        min_useful_length = self.escape_clearance * 0.5

        for pad in package.pads:
            # Issue #2513: Skip plane-net pads (net=0) -- they are stitched
            # via planes, not routed via escapes.
            if pad.net == 0:
                continue

            direction = self._get_quadrant_direction(pad.x, pad.y, center_x, center_y)
            dx, dy = self._direction_to_vector(direction)
            trace_w = self._get_trace_width_for_net(pad.net_name)

            # Issue #2756: clip the radial escape against neighbour pads.
            requested_dist = self.escape_clearance
            safe_dist = self._compute_max_safe_escape_length(
                pad=pad,
                dx=dx,
                dy=dy,
                trace_width=trace_w,
                package_pads=package.pads,
                min_clearance=effective_clearance,
                max_length=requested_dist,
            )
            escape_dist = min(requested_dist, safe_dist)

            # Drop stubs that are too short to exit the pad halo.
            if escape_dist < min_useful_length:
                logger.debug(
                    "Radial escape for %s pin %s skipped: clipped length "
                    "%.3fmm < %.3fmm threshold (Issue #2756)",
                    pad.net_name,
                    pad.pin,
                    escape_dist,
                    min_useful_length,
                )
                continue

            escape_x = pad.x + dx * escape_dist
            escape_y = pad.y + dy * escape_dist

            segment = Segment(
                x1=pad.x,
                y1=pad.y,
                x2=escape_x,
                y2=escape_y,
                width=trace_w,
                layer=pad.layer,
                net=pad.net,
                net_name=pad.net_name,
            )

            escapes.append(
                EscapeRoute(
                    pad=pad,
                    direction=direction,
                    escape_point=(escape_x, escape_y),
                    escape_layer=pad.layer,
                    segments=[segment],
                )
            )

        return escapes

    def staggered_via_fanout(
        self,
        pads: list[Pad],
        stagger_distance: float | None = None,
        foreign_pads: list[Pad] | None = None,
        foreign_tracks: list[Segment] | None = None,
    ) -> list[Via]:
        """Generate staggered via pattern under dense package.

        Places vias in a dog-bone pattern, offsetting via positions
        based on row/column to prevent via-to-via DRC violations.

        Issue #2948: Forwards optional foreign-net pad / track context to
        :meth:`_can_place_via` so the world-coordinate clearance check
        from Issue #2944 can reject candidates whose envelope overlaps
        adjacent foreign copper.  When omitted (the existing legacy
        behavior), only the grid-cell bounds / obstacle check is run.

        Args:
            pads: Pads to create fanout vias for.  Each via inherits the
                parent pad's ``net`` so the predicate can filter
                ``foreign_pads`` / ``foreign_tracks`` down to the truly
                foreign-net subset.
            stagger_distance: Offset distance for stagger (defaults to
                ``via_spacing / 2``).
            foreign_pads: Optional list of nearby pads (board-wide pad
                registry minus the package being fanned out, ideally) to
                validate each candidate via against.  Pads whose ``net``
                matches the parent pad are skipped automatically.
            foreign_tracks: Optional list of pre-existing track segments
                to validate against.  Segments whose ``net`` matches the
                parent pad's net are skipped automatically.

        Returns:
            List of Via objects in staggered pattern.
        """
        if not pads:
            return []

        stagger = stagger_distance or (self.via_spacing / 2)
        vias: list[Via] = []

        # Group by approximate row/column
        rows = self._group_pads_to_grid(pads)

        # Issue #3855: drill registry from through-hole pad context so a
        # staggered escape via is rejected if its drill would sit within
        # ``min_hole_to_hole`` of a through-hole pad drill (any net).  The
        # registry also accumulates vias placed earlier in THIS fan so two
        # staggered escape vias never form a sub-minimum drill pair.
        existing_drills: list[tuple[float, float, float]] = []
        if foreign_pads:
            for fp_pad in foreign_pads:
                if getattr(fp_pad, "through_hole", False) and fp_pad.drill > 0:
                    existing_drills.append((fp_pad.x, fp_pad.y, fp_pad.drill))

        for row_idx, row in enumerate(rows):
            for col_idx, pad in enumerate(row):
                # Offset based on row and column parity
                offset_x = (col_idx % 2) * stagger
                offset_y = (row_idx % 2) * stagger

                via_x = pad.x + offset_x
                via_y = pad.y + offset_y

                # Issue #2948: forward foreign-copper context (own net
                # filtering happens inside ``_can_place_via``).  When
                # ``foreign_pads`` / ``foreign_tracks`` are omitted the
                # call collapses to the legacy grid-cell-only check.
                # Issue #3855: also forward the drill registry so the
                # hole-to-hole guard rejects sub-fab-minimum drill pairs.
                if self._can_place_via(
                    via_x,
                    via_y,
                    net=pad.net,
                    foreign_pads=foreign_pads,
                    foreign_tracks=foreign_tracks,
                    existing_drills=existing_drills,
                ):
                    via = Via(
                        x=via_x,
                        y=via_y,
                        drill=self.rules.via_drill,
                        diameter=self.rules.via_diameter,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=pad.net,
                        net_name=pad.net_name,
                    )
                    vias.append(via)
                    # Issue #3855: subsequent candidates in this fan must
                    # also clear the via just placed (hole-to-hole).
                    existing_drills.append((via_x, via_y, via.drill))

        return vias

    def _group_pads_to_grid(self, pads: list[Pad]) -> list[list[Pad]]:
        """Group pads into a 2D grid structure."""
        if not pads:
            return []

        # Find unique Y positions (rows)
        y_positions = sorted({round(p.y, 2) for p in pads})

        rows: list[list[Pad]] = []
        for y in y_positions:
            row = [p for p in pads if abs(p.y - y) < 0.1]
            row.sort(key=lambda p: p.x)
            rows.append(row)

        return rows

    def _can_place_via(
        self,
        x: float,
        y: float,
        net: int | None = None,
        foreign_pads: list[Pad] | None = None,
        foreign_tracks: list[Segment] | None = None,
        clearance: float | None = None,
        via_diameter: float | None = None,
        existing_drills: list[tuple[float, float, float]] | None = None,
        via_drill: float | None = None,
    ) -> bool:
        """Check if a via can be placed at the given position.

        Issue #2944: The grid-cell check that historically lived here was
        too coarse for fine-pitch QFP/SSOP escape routing -- a via that
        lands on a "free" grid cell can still sit within trace/pad
        clearance of an adjacent foreign-net pad or segment in world
        coordinates.  When ``foreign_pads`` / ``foreign_tracks`` are
        supplied, the shared world-coordinate predicate from
        :mod:`kicad_tools.router.via_clearance` is consulted as well.

        Args:
            x: Proposed via X in mm (world coordinates).
            y: Proposed via Y in mm (world coordinates).
            net: Net number of the via being placed (used to filter
                ``foreign_pads`` / ``foreign_tracks`` to the truly
                foreign-net subset).  When ``None`` the pad / segment
                lists are treated as already pre-filtered to foreign
                nets.
            foreign_pads: Optional list of nearby pads to validate the
                via against.  Pads whose ``net`` equals ``net`` are
                skipped automatically when ``net`` is provided.
            foreign_tracks: Optional list of nearby segments to validate
                against.  Segments whose ``net`` equals ``net`` are
                skipped automatically.
            clearance: Required minimum clearance from foreign copper
                (mm).  Defaults to the design rules' via clearance.
            via_diameter: Via pad diameter (mm).  Defaults to the design
                rules' via diameter.
            existing_drills: Optional board-wide drill registry as
                ``(x, y, drill_diameter)`` over existing vias (any net) +
                through-hole pad drills.  Issue #3855: when supplied, the
                candidate via's DRILL is checked edge-to-edge against every
                entry using the canonical hole-to-hole formula
                (:func:`kicad_tools.router.via_clearance.drill_hole_to_hole_clear`),
                rejecting any candidate that would emit a sub-fab-minimum
                drill pair.  ``None`` (the legacy default) skips the check,
                preserving back-compat for callers without drill context.
            via_drill: Candidate via drill diameter (mm) for the hole-to-hole
                check.  Defaults to the design rules' via drill.

        Returns:
            True if the position is clear; False if blocked.
        """
        # Check grid bounds.  Issue #3063: use origin-aware bounds so
        # boards whose world coordinates don't start at (0, 0) (e.g.
        # the board-04 STM32 PCB, whose origin sits around (95, 90))
        # have their candidates correctly validated.  The pre-#3063
        # form (``0 <= x <= grid.width``) was implicitly correct only
        # for grids constructed with ``origin_x=origin_y=0``; on other
        # boards every world-coord candidate fell out-of-bounds and
        # the predicate defaulted to False, which masked the lateral
        # re-attempt path (every off-pad candidate was rejected on
        # bounds and the rescue never fired).
        origin_x = getattr(self.grid, "origin_x", 0.0)
        origin_y = getattr(self.grid, "origin_y", 0.0)
        if not (
            origin_x <= x <= origin_x + self.grid.width
            and origin_y <= y <= origin_y + self.grid.height
        ):
            return False

        # Check for obstacles in grid
        #
        # Issue #2963: Post-PR #2928, isolated pad-metal cells are
        # marked ``is_obstacle=True`` on first touch.  Without an
        # own-net filter here, every via candidate that lands inside
        # the destination pad's footprint is rejected -- including
        # when ``net`` is the pad's own net (e.g. NRST/BOOT0 endpoint
        # pads on board 04).  Mirror PR #2965's pattern: only reject
        # when the cell's net is a *different* net from the via's.
        # When ``net`` is ``None`` the caller has no net context, so
        # preserve the original (conservative) hard reject.
        gx, gy = self.grid.world_to_grid(x, y)
        if 0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows:
            for layer_idx in range(self.grid.num_layers):
                cell = self.grid.grid[layer_idx][gy][gx]
                if cell.blocked and cell.is_obstacle:
                    if net is None or cell.net != net:
                        return False

        # Issue #2944: World-coordinate clearance check against foreign
        # copper.  Only runs when the caller supplies pad / track
        # context -- preserves existing behavior for call sites that
        # only have grid-cell information.
        if foreign_pads or foreign_tracks:
            eff_clearance = clearance if clearance is not None else self.rules.via_clearance
            eff_diameter = via_diameter if via_diameter is not None else self.rules.via_diameter

            # Pre-filter to foreign-net pads/tracks when the caller
            # supplied the via's own net.  This keeps the predicate
            # focused on truly-foreign copper and avoids spurious
            # rejections on same-net targets.
            #
            # Issue #2951: pass (x, y, width, height, net) 5-tuples so
            # ``point_clear_of_copper`` uses rect-distance for oblong
            # fine-pitch pads.  The old ``max(width, height) / 2``
            # disc-bound made 0.3 x 1.4mm LQFP fingers look like 1.4mm
            # discs and rejected every nudged via candidate on 0.5mm
            # pitch -- in production this kept PR #2950's in-pad nudge
            # from ever finding a valid offset (see
            # ``EscapeRouter._via_clears_other_pads`` for the matching
            # rect template).
            pad_tuples: list[tuple[float, float, float, float, int]] = []
            if foreign_pads:
                for p in foreign_pads:
                    if net is not None and p.net == net:
                        continue
                    pad_tuples.append((p.x, p.y, p.width, p.height, p.net))

            seg_list: list[Segment] = []
            if foreign_tracks:
                for s in foreign_tracks:
                    if net is not None and s.net == net:
                        continue
                    seg_list.append(s)

            # Adapt Segment (x1/y1/x2/y2) to the predicate's expected
            # start_x/start_y/end_x/end_y interface.
            adapted_segs = [
                _SegmentAdapter(
                    start_x=s.x1,
                    start_y=s.y1,
                    end_x=s.x2,
                    end_y=s.y2,
                    width=s.width,
                )
                for s in seg_list
            ]

            if not point_clear_of_copper(
                x=x,
                y=y,
                via_size=eff_diameter,
                clearance=eff_clearance,
                other_net_tracks=adapted_segs,
                other_net_pads=pad_tuples,
            ):
                return False

        # Issue #3855: drill hole-to-hole guard.  The copper-clearance check
        # above operates on PAD diameters; it does not enforce the fab's
        # drill-to-drill (hole-to-hole) minimum, so an escape via could land
        # within ``min_hole_to_hole`` of a through-hole pad / existing via
        # drill and trip a ``dimension_drill_clearance`` DRC error.  Reject
        # such candidates (callers treat ``False`` as "try the next site").
        if existing_drills:
            from .via_clearance import drill_hole_to_hole_clear

            eff_via_drill = via_drill if via_drill is not None else self.rules.via_drill
            min_h2h = getattr(self.rules, "min_hole_to_hole", 0.5)
            if not drill_hole_to_hole_clear(x, y, eff_via_drill, existing_drills, min_h2h):
                return False

        return True

    def _via_clears_other_pads(
        self,
        x: float,
        y: float,
        via_diameter: float,
        clearance: float,
        other_pads: list[Pad],
        same_net: int,
    ) -> bool:
        """Return True iff a via at (x, y) clears every foreign-net pad.

        Issue #2944: helper for the in-pad escape rescue path.  The
        ``_try_in_pad_escape`` method places a via dead-centre on a
        fine-pitch SMD pad; on packages where the pin pitch is below
        ``via_diameter + 2 * clearance`` this puts the via inside the
        neighboring foreign-net pads' clearance envelope.  Calling this
        helper before committing the in-pad via lets us reject the
        rescue and let the main router try a different approach
        (typically the next-ring escape).

        Geometry: SMD pads are axis-aligned rectangles, so a
        worst-case-circle ``max(w,h)/2`` approximation is too pessimistic
        for oblong fine-pitch pads.  We compute the distance from the
        via center to the actual pad rectangle (closest point on the
        rectangle to the via center) and require::

            rect_dist >= via_radius + clearance

        Args:
            x: Proposed via X (mm).
            y: Proposed via Y (mm).
            via_diameter: Via pad diameter (mm).
            clearance: Required minimum clearance (mm).
            other_pads: Pads on the same footprint (or any other context)
                to validate against.  Same-net pads are skipped via
                ``same_net``.
            same_net: Net of the via being placed; pads with this net are
                treated as same-net and skipped.

        Returns:
            True when every foreign-net pad clears the proposed via.
        """
        via_radius = via_diameter / 2
        required = via_radius + clearance

        for p in other_pads:
            if p.net == same_net:
                continue

            # Distance from via center to the pad rectangle (axis-aligned).
            half_w = p.width / 2
            half_h = p.height / 2
            dx_abs = abs(x - p.x)
            dy_abs = abs(y - p.y)
            outside_x = max(0.0, dx_abs - half_w)
            outside_y = max(0.0, dy_abs - half_h)

            if outside_x == 0.0 and outside_y == 0.0:
                # Via center is inside the pad rectangle.  This is the
                # legitimate "via in pad" case ONLY when the pad's net
                # matches the via's net -- which we've already filtered
                # out above.  Foreign-net interior means an immediate
                # violation.
                return False

            rect_dist = math.sqrt(outside_x * outside_x + outside_y * outside_y)
            if rect_dist < required - 1e-9:
                return False

        return True

    def _get_quadrant_direction(
        self,
        x: float,
        y: float,
        center_x: float,
        center_y: float,
    ) -> EscapeDirection:
        """Determine escape direction based on quadrant relative to center."""
        dx = x - center_x
        dy = y - center_y

        # Determine primary direction based on which axis is dominant
        if abs(dx) > abs(dy):
            if dx > 0:
                return EscapeDirection.EAST
            else:
                return EscapeDirection.WEST
        else:
            if dy > 0:
                return EscapeDirection.NORTH
            else:
                return EscapeDirection.SOUTH

    def _select_in_pad_via_position(
        self,
        pad: Pad,
        via_diameter: float,
        min_annular: float,
        effective_clearance: float,
        package: PackageInfo | None,
    ) -> tuple[float, float, bool]:
        """Select an in-pad via position with a long-axis clearance nudge.

        Issue #2946: When dead-centre placement on a fine-pitch QFP pad
        produces clearance violations to adjacent foreign-net pads
        (board-04 OSC_OUT at 0.5mm-pitch LQFP-48), iterate offsets along
        the pad's **long axis** seeking the smallest-magnitude offset
        whose candidate via passes both the world-coordinate clearance
        predicate (:meth:`_can_place_via`) and the pad-copper
        containment check.

        Stencil safety: solder-paste stencil apertures key off the pad's
        geometry, not the via's position, so an offset via inside the
        pad does NOT corrupt the stencil aperture as long as the via
        barrel + annular ring stay entirely inside the pad copper
        rectangle.  The containment check enforces this constraint --
        the via center must lie within the pad's interior such that::

            |offset| + via_radius + min_annular <= long_dim / 2

        i.e. the maximum nudge is
        ``(long_dim - via_diameter) / 2 - min_annular``.

        Search strategy:
        1. Dead-centre ``(pad.x, pad.y)`` is the FIRST candidate -- if
           it passes the clearance predicate, no nudge is attempted.
           This preserves the existing behavior on the common case
           where no neighboring pad is close enough to violate.
        2. Otherwise iterate offsets ``[+s, -s, +2s, -2s, ...]`` with
           ``s = 0.05 mm`` along the pad's long axis (X axis when
           ``pad.width > pad.height``, Y axis otherwise).  The first
           offset whose candidate via passes BOTH checks is returned.
        3. If no candidate passes, fall back to dead-centre.  The
           caller (``_try_in_pad_escape``) emits the structured warning
           in that case, preserving the PR #2945 "place anyway, defer
           DRC to the user" semantics for the unfixable cases.

        Args:
            pad: The pad whose surface escape was just rejected.
            via_diameter: Effective via diameter for in-pad placement
                (manufacturer min_via_diameter or design rules fallback).
            min_annular: Minimum annular ring (pad copper around the via
                barrel) that must remain after placement.  Used as the
                pad-copper containment safety margin.
            effective_clearance: Clearance value passed through to
                :meth:`_can_place_via`.
            package: Optional package context.  When ``None`` the nudge
                rescue is disabled and dead-centre is returned (no
                neighbor pad / track context available to validate
                offsets against).

        Returns:
            A tuple ``(via_x, via_y, nudged)`` where ``via_x``/``via_y``
            are the chosen via center coordinates in world space and
            ``nudged`` is ``True`` iff the position differs from
            dead-centre AND the candidate passes the clearance
            predicate (i.e. the nudge rescue succeeded).
        """
        via_x = pad.x
        via_y = pad.y

        # Without package context we have no foreign-net pads to
        # validate against; preserve legacy dead-centre behavior.
        if package is None:
            return via_x, via_y, False

        # Foreign-net pads on the same footprint (the pads the in-pad
        # rescue is most likely to clip on a fine-pitch QFP).
        foreign_pads = [p for p in package.pads if p is not pad and p.net != pad.net]

        # Quick path: dead-centre.  If the existing clearance predicate
        # accepts it, no nudge is needed.  This is the common case for
        # the majority of in-pad rescues and keeps behavior identical
        # to PR #2944 / #2945 when neighbor clearance is fine.
        if self._can_place_via(
            x=via_x,
            y=via_y,
            net=pad.net,
            foreign_pads=foreign_pads,
            clearance=effective_clearance,
            via_diameter=via_diameter,
        ):
            return via_x, via_y, False

        # Determine pad long-axis direction.  The Pad primitive does not
        # carry rotation; ``width``/``height`` already encode the post-
        # rotation footprint (KiCad emits oriented bounding-box extents
        # when the loader projects pad geometry to world coordinates).
        # The longer extent is the long axis.
        if pad.width >= pad.height:
            long_dim = pad.width
            axis_x, axis_y = 1.0, 0.0
        else:
            long_dim = pad.height
            axis_x, axis_y = 0.0, 1.0

        # Stencil-safety budget: the via center may travel along the
        # long axis until the via's barrel + annular ring is about to
        # exit the pad's long-edge copper.  We require strict interior
        # containment so the SMT stencil aperture remains valid.
        via_radius = via_diameter / 2
        max_offset = (long_dim - via_diameter) / 2 - min_annular
        if max_offset <= 0.0:
            # No room to nudge -- fall back to dead-centre (caller will
            # emit the diagnostic warning).
            return via_x, via_y, False

        # NOTE: we deliberately do NOT check short-axis containment.
        # The parent ``_try_in_pad_escape`` already validates the LARGER
        # dimension covers ``drill + 2 * annular`` and documents the
        # short axis as exempt: a via-in-pad's *pad landing* (diameter)
        # may extend off the SMT pad's short edges because the via is
        # filled and plated, but the drill must remain inside pad
        # copper.  The nudge here only translates along the long axis,
        # so short-axis containment is invariant -- whatever was true
        # at dead-centre remains true after the offset.

        # Iterate offsets [+s, -s, +2s, -2s, ...] until either an
        # offset passes the clearance predicate or we exceed the
        # stencil-safety budget.  The step size is 0.05 mm to match
        # the grid resolution used elsewhere in the router.
        step = 0.05
        n_steps = int(max_offset / step) + 1

        for i in range(1, n_steps + 1):
            for sign in (+1.0, -1.0):
                offset = sign * i * step
                if abs(offset) > max_offset + 1e-9:
                    continue

                cand_x = pad.x + axis_x * offset
                cand_y = pad.y + axis_y * offset

                # Pad-copper containment: the via center plus radius
                # plus annular ring must remain inside the pad
                # rectangle along the long axis.  (Short-axis
                # containment is independent of the offset and was
                # validated above.)
                if abs(offset) + via_radius + min_annular > long_dim / 2 + 1e-9:
                    continue

                # Clearance predicate against foreign-net pads on the
                # same footprint.  We pass through the manufacturer-
                # effective via diameter and clearance so the check
                # mirrors the geometry that will land on the PCB.
                if self._can_place_via(
                    x=cand_x,
                    y=cand_y,
                    net=pad.net,
                    foreign_pads=foreign_pads,
                    clearance=effective_clearance,
                    via_diameter=via_diameter,
                ):
                    logger.info(
                        "In-pad rescue NUDGED for pad %s (ref=%s pin=%s): "
                        "dead-centre (%.3f, %.3f) violated clearance; "
                        "long-axis offset=%+.3fmm accepted "
                        "(via at (%.3f, %.3f); budget=%.3fmm).  "
                        "Stencil aperture unaffected -- via barrel + "
                        "annular ring remain inside pad copper.",
                        pad.net_name,
                        pad.ref,
                        pad.pin,
                        pad.x,
                        pad.y,
                        offset,
                        cand_x,
                        cand_y,
                        max_offset,
                    )
                    return cand_x, cand_y, True

        # No offset succeeded.  Return dead-centre; the caller emits
        # the diagnostic warning and proceeds (preserves PR #2945
        # last-resort behavior).
        return via_x, via_y, False

    def _compute_target_direction(
        self,
        pad: Pad,
        package: PackageInfo,
        primary_dir: EscapeDirection,
        allow_into_package: bool = False,
    ) -> EscapeDirection | None:
        """Snap the direction toward the net's nearest off-package pad.

        Issue #3428 (follow-on to #3411): the QFP-alternating dispatcher
        chooses the in-pad rescue's inner-stub direction purely from the
        pin's index parity in the sorted same-edge pad list.  On board 04
        that emitted U2.6 OSC_OUT's B.Cu stub TOWARD U2.7 (NRST), whose
        candidate escape via then failed clearance against the stub --
        stranding NRST.  This helper consults the board-wide
        ``net_target_positions`` map and returns the cardinal direction
        pointing at the net's actual routing target so the stub leaves
        the contested channel instead of blocking it.

        Selection rules (all deterministic, required for the ``--seed``
        byte-identical reproducibility AC):

        1. Candidate targets are the net's pads NOT on ``package``
           (matched by ref).  No candidates / no map entry -> ``None``
           (caller keeps the legacy parity direction).
        2. The nearest candidate wins; exact distance ties break by
           ``(x, y)`` ascending.
        3. The delta to the winner snaps to the axis with the larger
           ``|component|``.  On an exact ``|dx| == |dy|`` tie the fixed
           precedence ``NORTH > EAST > SOUTH > WEST`` picks between the
           two candidate cardinals.
        4. Into-package guard: when the snapped direction points INTO
           the package body (negative dot product with ``primary_dir``,
           the outward perpendicular for this edge), return ``None`` --
           an inner stub under the package would collide with the other
           edges' escapes.  Along-edge results (zero dot product) are
           allowed; redirecting the stub along the edge AWAY from a
           needy neighbour is precisely the board-04 fix.  Callers that
           EXPLICITLY want an under-package inner stub (the
           pocket-escape rescue, see ``_escape_qfp_alternating``) pass
           ``allow_into_package=True`` to skip this guard -- on the
           inner escape layer the area under an SMD package body is
           open routing space, and a net whose target lies across the
           package must traverse it anyway.

        Args:
            pad: The pad being rescued.
            package: The package the pad belongs to (its ref filters
                same-package pads out of the candidate target set).
            primary_dir: Outward perpendicular escape direction for the
                pad's package edge (used for the into-package guard).
            allow_into_package: When True, skip the into-package guard
                (step 4) and return the snapped target direction even
                when it points across the package body.  Default False
                preserves the conservative behaviour for the standard
                violation-triggered rescue path.

        Returns:
            The snapped cardinal ``EscapeDirection``, or ``None`` when no
            target information is available or the target points into
            the package body (caller falls back to legacy behaviour).
        """
        positions = self.net_target_positions.get(pad.net)
        if not positions:
            return None
        candidates = [(x, y) for x, y, ref in positions if ref != package.ref]
        if not candidates:
            return None

        # Deterministic nearest-candidate selection: squared distance,
        # then (x, y) ascending on exact ties.
        tx, ty = min(
            candidates,
            key=lambda p: ((p[0] - pad.x) ** 2 + (p[1] - pad.y) ** 2, p[0], p[1]),
        )
        dx = tx - pad.x
        dy = ty - pad.y
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return None

        # NOTE: EscapeDirection.NORTH maps to (0, +1) in this module
        # (see ``_direction_to_vector``), i.e. toward LARGER y in the
        # KiCad y-down world frame.  We stay consistent with that
        # convention here rather than the schematic-intuitive one.
        horizontal = EscapeDirection.EAST if dx > 0 else EscapeDirection.WEST
        vertical = EscapeDirection.NORTH if dy > 0 else EscapeDirection.SOUTH
        if abs(dx) > abs(dy) + 1e-9:
            target = horizontal
        elif abs(dy) > abs(dx) + 1e-9:
            target = vertical
        else:
            # Exact |dx| == |dy| tie: documented fixed precedence
            # NORTH > EAST > SOUTH > WEST between the two candidates.
            precedence = {
                EscapeDirection.NORTH: 0,
                EscapeDirection.EAST: 1,
                EscapeDirection.SOUTH: 2,
                EscapeDirection.WEST: 3,
            }
            target = min((vertical, horizontal), key=lambda d: precedence[d])

        # Into-package fallback guard: reject directions whose dot
        # product with the outward perpendicular is negative.
        if not allow_into_package:
            pvx, pvy = self._direction_to_vector(primary_dir)
            tvx, tvy = self._direction_to_vector(target)
            if pvx * tvx + pvy * tvy < 0:
                return None
        return target

    def _neighbour_claimed_in_pad_via(
        self,
        pad: Pad,
        package: PackageInfo,
        escapes: list[EscapeRoute],
    ) -> bool:
        """Check whether an adjacent same-package pin claimed an in-pad via.

        Issue #3428 (pocket-escape rescue trigger): when a fine-pitch
        pin's immediate neighbour was just rescued with an in-pad via,
        the inter-pin surface channel is contested -- the neighbour's
        via plus its inner stub consume the clearance budget that this
        pin's own escape via would need.  Combined with a routing target
        on the FAR side of the package (see the caller's dot-product
        check), the standard perpendicular surface escape leads into a
        dead-end pocket (board 04: U2.7 NRST escaping WEST into the
        crystal-cluster pocket while its target J1.5 lies EAST).

        Adjacency is physical, not list-index based: a neighbour
        qualifies when its pad centre lies within ``1.25 x pin_pitch``
        (Chebyshev) of this pad.  The 1.25 factor tolerates sub-grid
        coordinate noise while excluding next-but-one pins (2.0 x
        pitch away).

        Args:
            pad: The pad being considered for a pocket-escape rescue.
            package: Package context (supplies ``pin_pitch``).
            escapes: Escape routes accumulated so far in this dispatch
                pass (earlier-indexed pins on this and previous edges).

        Returns:
            True when at least one already-rescued in-pad via belongs to
            an immediately adjacent pin on the same component.
        """
        pitch = package.pin_pitch
        if not pitch or pitch <= 0:
            return False
        limit = pitch * 1.25
        for er in escapes:
            if er.via is None or not getattr(er.via, "in_pad", False):
                continue
            if er.pad is pad or er.pad.ref != pad.ref:
                continue
            if abs(er.pad.x - pad.x) <= limit and abs(er.pad.y - pad.y) <= limit:
                return True
        return False

    def _adjacent_in_pad_via_conflict(
        self,
        x: float,
        y: float,
        via_diameter: float,
        clearance: float,
        same_net: int,
        existing_escapes: list[EscapeRoute] | None,
    ) -> EscapeRoute | None:
        """Detect a via-via spacing conflict with a sibling in-pad via.

        Issue #3429 (adjacent-pin via-in-pad conflict detection): the
        per-pad in-pad rescue (:meth:`_try_in_pad_escape`) validates a
        candidate via only against the footprint's PADS
        (:meth:`_via_clears_other_pads`) -- sibling escapes' vias placed
        earlier in the SAME dispatch pass are invisible to that check
        because the routing grid is not yet populated.  At fine pitch
        (e.g. LQFP-48 0.5 mm) two adjacent foreign-net pins can each be
        flagged for an in-pad rescue, but their two via barrels + annular
        rings physically cannot coexist: the required center-to-center
        spacing is::

            via_r_A + via_r_B + clearance

        which on jlcpcb-tier1 (0.6 mm OD, 0.127 mm clearance) is
        0.3 + 0.3 + 0.127 = 0.727 mm -- already wider than the 0.5 mm
        pitch.  Before this guard the conflict surfaced only at the
        :meth:`apply_escape_routes` commit-time cross-validation, where
        the losing escape was dropped with no retry (a silent gap).

        This helper performs the cheap pairwise check BEFORE the second
        via is committed so the dispatcher can refuse it and fall through
        to the lateral / surface escape path.  Distinct from #3470's
        :meth:`_in_pad_stub_conflicts`, which guards the inner-layer STUB
        copper; this guards the VIA-BARREL spacing, which the stub-flip
        retry cannot fix (flipping the stub does not move the via).

        Args:
            x: Proposed via center X (mm).
            y: Proposed via center Y (mm).
            via_diameter: Proposed via pad diameter (mm).
            clearance: Required minimum via-to-via clearance (mm).
            same_net: Net id of the via being placed; sibling vias on the
                same net are skipped (same-net copper may share spacing).
            existing_escapes: Escape routes accumulated so far in this
                dispatch pass.  ``None`` (legacy call sites) disables the
                check entirely, preserving byte-for-byte behaviour.

        Returns:
            The conflicting sibling :class:`EscapeRoute` when a foreign-net
            in-pad via lies closer than the required center spacing, else
            ``None``.
        """
        if not existing_escapes:
            return None

        via_radius = via_diameter / 2
        for er in existing_escapes:
            via = er.via
            if via is None or not getattr(via, "in_pad", False):
                continue
            # Same-net vias do not need foreign-clearance spacing.
            if via.net == same_net:
                continue
            sibling_radius = via.diameter / 2
            required = via_radius + sibling_radius + clearance
            dist = math.hypot(via.x - x, via.y - y)
            if dist < required - 1e-9:
                return er
        return None

    def _lateral_via_sibling_conflict(
        self,
        x: float,
        y: float,
        via_diameter: float,
        clearance: float,
        same_net: int,
        existing_escapes: list[EscapeRoute] | None,
    ) -> bool:
        """Barrel-spacing check for a LATERAL via against sibling escapes.

        Issue #3430 (auto-lateral-via fallback): the recovery half of the
        adjacent-pin conflict story.  When #3429's detection refuses an
        in-pad rescue, the dispatcher falls through to
        :meth:`_try_lateral_via_escape`, which pushes the via OFF the pad
        along the outward escape direction.  Those off-pad candidates are
        validated by :meth:`_can_place_via` only against footprint PADS --
        the vias of sibling escapes generated earlier in the SAME pass are
        invisible there (the routing grid is not populated until commit
        time).  This predicate closes that gap.

        It differs from :meth:`_adjacent_in_pad_via_conflict` (which guards
        the FIRST, in-pad via and therefore only considers sibling *in-pad*
        vias) in ONE respect: a lateral via must clear EVERY foreign-net
        sibling via -- whether that sibling escaped in-pad or also took a
        lateral offset.  Two off-pad vias can collide just as readily as an
        in-pad / off-pad pair, so the ``in_pad`` flag is not consulted
        here.  The center-spacing requirement is identical::

            via_r_self + via_r_sibling + clearance

        Args:
            x: Proposed lateral via center X (mm).
            y: Proposed lateral via center Y (mm).
            via_diameter: Proposed via pad diameter (mm).
            clearance: Required minimum via-to-via clearance (mm).
            same_net: Net id of the via being placed; same-net sibling vias
                are skipped (same-net copper may share spacing).
            existing_escapes: Escape routes accumulated so far in this
                dispatch pass.  ``None`` disables the check (legacy /
                unit-fixture callers), preserving byte-for-byte behaviour.

        Returns:
            ``True`` when a foreign-net sibling via lies closer than the
            required center spacing, else ``False``.
        """
        if not existing_escapes:
            return False

        via_radius = via_diameter / 2
        for er in existing_escapes:
            via = er.via
            if via is None:
                continue
            if via.net == same_net:
                continue
            sibling_radius = via.diameter / 2
            required = via_radius + sibling_radius + clearance
            dist = math.hypot(via.x - x, via.y - y)
            if dist < required - 1e-9:
                return True
        return False

    def _try_in_pad_escape(
        self,
        pad: Pad,
        direction: EscapeDirection,
        effective_clearance: float,
        escape_width: float,
        package: PackageInfo | None = None,
        skip_on_clearance_violation: bool = False,
        target_direction: EscapeDirection | None = None,
        existing_escapes: list[EscapeRoute] | None = None,
        enforce_adjacent_via_spacing: bool = False,
    ) -> EscapeRoute | None:
        """Attempt an in-pad via escape for a fine-pitch SSOP/TSSOP pad.

        Issue #2605: For manufacturers that support via-in-pad (filled and
        plated), placing a via dead-centre on a fine-pitch pad lets the
        escape happen vertically into an inner layer (or B.Cu on 2-layer
        boards), bypassing the surface-real-estate constraint that forced
        deferral with the alternating-layer strategy.

        Issue #2944: When ``package`` is supplied, the candidate in-pad
        via is validated against every other pad on the same footprint
        using the shared world-coordinate predicate from
        :mod:`kicad_tools.router.via_clearance`.  This rejects in-pad
        rescues that would land within
        ``via_radius + neighbor_radius + clearance`` of an adjacent
        foreign-net pad -- the exact failure mode seen on board 04 LQFP
        OSC_OUT (0.5mm pitch, 0.6mm vias) where the in-pad rescue
        violated clearance to OSC_IN and NRST by 0.05mm.

        Issue #2946: When dead-centre placement violates clearance to a
        neighboring foreign-net pad, the in-pad via is nudged along the
        pad's **long axis** by increments of 0.05 mm before falling back
        to the dead-centre placement.  The nudge is intentionally
        constrained to the long axis (and to remain entirely within the
        pad's copper rectangle) because solder-paste stencil apertures
        key off the pad's geometry, not the via's position -- so an
        offset via inside the pad does NOT corrupt the stencil aperture
        as long as the via barrel + annular ring stay inside the pad
        copper (a precondition of the existing
        ``via_in_pad_supported`` capability gate, which itself implies
        the via is filled and plated).  The stencil-safety budget is::

            max_offset = (long_dim - via_diameter) / 2 - min_annular

        A 0.3 x 1.4 mm LQFP-48 pad with a 0.45 mm via and 0.05 mm
        annular ring yields ~0.42 mm of safe in-pad travel -- vastly
        more than the 0.05 mm clearance gap board 04 OSC_OUT fails by.

        Pre-conditions (return ``None`` when violated):
        - ``self.via_in_pad_supported`` must be ``True`` (set from manufacturer
          capability flags during ``__init__``).
        - The pad must be physically large enough to host a via with annular
          ring: ``min(pad.width, pad.height) >= via_diameter`` (we require
          the pad copper to fully cover the via diameter; the pad's own
          copper provides the annular ring).
        - When ``package`` is supplied, the candidate via must clear all
          foreign-net pads on the same footprint.

        On success the returned ``EscapeRoute`` contains:
        - A ``Via`` placed at ``(via_x, via_y)`` with ``in_pad=True``,
          which is normally dead-centre on the pad but may be offset
          along the long axis when a clearance-rescue nudge succeeds.
        - A single inner-layer segment from the via to a normal escape point
          chosen in the same direction the deferred surface escape would
          have used.

        Args:
            pad: The pad whose surface escape was just rejected.
            direction: The original escape direction (used to pick the
                inner-layer escape point so downstream routing still flows
                outward).
            effective_clearance: Clearance value to use for the inner-layer
                escape point offset.
            escape_width: Trace width to use for the inner-layer segment.
            package: Optional package context.  When supplied, the
                proposed in-pad via is validated against neighboring
                foreign-net pads (Issue #2944) and the long-axis nudge
                rescue (Issue #2946) is attempted before the
                dead-centre fallback.  When ``None`` (legacy call sites),
                only the original geometry preconditions run and the
                via is placed dead-centre without the neighbor check.
            skip_on_clearance_violation: When True (default False),
                return ``None`` instead of placing a violating via when
                the long-axis nudge fails AND dead-centre would clip a
                neighbouring foreign-net pad.  Opt-in flag for callers
                that prefer surfacing the deferral as an explicit gap
                over committing a DRC violation that cascades into
                adjacent-pin routing failures.  Issue #3033 / #3062.
            target_direction: Optional target-aware override for the
                inner-layer stub direction (Issue #3428).  When supplied
                (typically from ``_compute_target_direction``), the
                inner-layer escape segment and ``escape_point`` use this
                direction instead of ``direction``.  Via placement is
                unaffected -- it is direction-independent.  ``None``
                (default) preserves legacy behaviour byte-for-byte for
                every existing call site.
            existing_escapes: Optional list of escape routes already
                generated in this pass (Issue #3470).  When supplied, the
                proposed inner-layer stub is checked for copper clearance
                against every FOREIGN-net stub/via in the list; on
                conflict the stub direction is retried (opposite, then
                perpendiculars) and the first conflict-free direction
                wins.  This prevents two adjacent fine-pitch in-pad
                rescues from emitting stubs that physically overlap and
                mutually block their nets' escape endpoints (board-05
                U3 pin31/pin33 ISENSE_B-/ISENSE_A-).  ``None`` (default)
                preserves legacy behaviour byte-for-byte.
            enforce_adjacent_via_spacing: When True (Issue #3429), refuse
                the rescue (return ``None``) if the candidate via barrel
                would land closer than ``via_r + sibling_via_r +
                clearance`` to a FOREIGN-net in-pad via already present in
                ``existing_escapes``.  This is the via-VS-via spacing
                guard the #3470 stub-flip cannot fix (flipping the stub
                does not move the barrel).  It is gated to the LEGACY
                violation / pin_boxed rescue paths, where the dispatcher
                has a lateral / surface fallback to take when the rescue
                is refused; it is intentionally left ``False`` for the
                #3428 pocket-escape path, whose divergent target-aware
                stubs are designed to let two adjacent in-pad vias
                coexist.  ``False`` (default) preserves legacy behaviour
                byte-for-byte for every existing call site.

        Returns:
            An ``EscapeRoute`` with the in-pad via and inner-layer segment,
            or ``None`` if in-pad escape is unavailable, geometrically
            infeasible, or (when ``skip_on_clearance_violation=True``)
            the rescue would introduce a foreign-pad clearance violation.
        """
        if not self.via_in_pad_supported:
            return None

        # Use the manufacturer's minimum via drill (with a small annular
        # ring) when available, falling back to the design rules' via
        # geometry otherwise.  For via-in-pad processing the pad copper
        # IS the via's landing -- the drill must fit inside the pad with
        # a manufacturer-defined annular ring, but the via's nominal
        # "diameter" pad doesn't have to fit because there's no separate
        # landing pad printed for an in-pad via.
        if self._mfr_limits is not None:
            via_drill = self._mfr_limits.min_via_drill
            via_diameter = self._mfr_limits.min_via_diameter
            min_annular = self._mfr_limits.min_via_annular
        else:
            via_drill = self.rules.via_drill
            via_diameter = self.rules.via_diameter
            min_annular = (via_diameter - via_drill) / 2

        # Geometry check: the drill must fit inside the pad with an
        # annular ring of pad copper around it.  Typical fine-pitch SSOP
        # pads are oblong (e.g. 0.35x1.45mm); the long axis nearly always
        # has room, but the short axis often does not.  We use the LARGER
        # dimension as the limiting factor here -- the via is placed at
        # pad centre and the long axis provides the annular ring (the
        # short axis is exempt because the pad copper extends fully
        # along the short edges).  Reject only when even the long axis
        # cannot host drill + 2 * annular.
        required_long_dim = via_drill + 2 * min_annular
        larger_dim = max(pad.width, pad.height)
        if larger_dim < required_long_dim - 1e-6:
            logger.debug(
                "In-pad escape for pad %s skipped: pad %.3fx%.3f mm "
                "too small for drill=%.3fmm + 2x annular=%.3fmm "
                "(needed long-axis dim >= %.3fmm)",
                pad.net_name,
                pad.width,
                pad.height,
                via_drill,
                min_annular,
                required_long_dim,
            )
            return None

        # Issue #2946: select the in-pad via position.  Default to
        # dead-centre on the pad; if that violates clearance to a
        # neighboring foreign-net pad, iterate offsets along the pad's
        # long axis seeking the smallest-magnitude offset whose
        # candidate via passes BOTH the clearance predicate
        # (``_can_place_via``) AND the pad-copper containment check
        # (the via barrel + annular ring must stay inside the pad
        # rectangle so the solder-paste stencil aperture remains valid).
        via_x, via_y, nudged = self._select_in_pad_via_position(
            pad=pad,
            via_diameter=via_diameter,
            min_annular=min_annular,
            effective_clearance=effective_clearance,
            package=package,
        )

        # Issue #2944 / #2946: Diagnostic clearance check against the
        # neighboring foreign-net pads on the same footprint.  When the
        # nudge rescue succeeded the dead-centre would have failed but
        # the offset position passes -- no warning is emitted in that
        # case (the via is DRC-clean).  When the nudge rescue could not
        # find any passing offset (e.g. dense plane-sandwich, all
        # offsets blocked), ``_select_in_pad_via_position`` falls back
        # to dead-centre and we emit the structured warning so users
        # (and tier-escalation logic) can decide whether to accept the
        # local DRC violation or escalate to a smaller-via tier / wider-
        # pitch footprint.
        # Issue #3118: track whether the micro-via fallback fired so the
        # emitted Via can be tagged (``is_micro=True``) for the
        # dimensions DRC exemption and the (via micro ...) serialisation
        # introduced by #3124/#3126.
        is_micro_via_used = False
        if package is not None and not nudged:
            other_pads = [p for p in package.pads if p is not pad]
            if not self._via_clears_other_pads(
                x=via_x,
                y=via_y,
                via_diameter=via_diameter,
                clearance=effective_clearance,
                other_pads=other_pads,
                same_net=pad.net,
            ):
                # Issue #3118: BEFORE falling through to the strict-mode
                # defer or the legacy "proceed anyway" warning, try the
                # micro-via rescue.  When the manufacturer supports
                # in-pad vias AND the caller opted into the fallback,
                # recompute via_drill / via_diameter / min_annular with
                # the micro-via triple (default 0.3 / 0.15 / derived) and
                # re-run the geometry + clearance gates.  The micro-via
                # OD is small enough on a 0.5 mm pitch to clear adjacent
                # foreign-net pads where the standard 0.6 mm via cannot
                # (pitch - micro_radius - neighbour_short/2 = 0.50 -
                # 0.15 - 0.15 = 0.20 mm >= 0.15 mm clearance, board-04
                # OSC_OUT geometry).
                if self.micro_via_in_pad_fallback and self.micro_via_diameter < via_diameter:
                    mv_diameter = self.micro_via_diameter
                    mv_drill = self.micro_via_drill
                    mv_min_annular = max((mv_diameter - mv_drill) / 2, 0.0)
                    mv_required_long_dim = mv_drill + 2 * mv_min_annular
                    if larger_dim + 1e-6 >= mv_required_long_dim:
                        mv_via_x, mv_via_y, mv_nudged = self._select_in_pad_via_position(
                            pad=pad,
                            via_diameter=mv_diameter,
                            min_annular=mv_min_annular,
                            effective_clearance=effective_clearance,
                            package=package,
                        )
                        if self._via_clears_other_pads(
                            x=mv_via_x,
                            y=mv_via_y,
                            via_diameter=mv_diameter,
                            clearance=effective_clearance,
                            other_pads=other_pads,
                            same_net=pad.net,
                        ):
                            logger.info(
                                "In-pad MICRO-VIA rescue for pad %s "
                                "(ref=%s pin=%s) at (%.3f, %.3f): "
                                "standard via clipped neighbour, retried "
                                "with %.3fmm OD / %.3fmm drill and "
                                "clearance passes.  Tagged is_micro "
                                "for the dimensions DRC exemption "
                                "(Issue #3118).",
                                pad.net_name,
                                pad.ref,
                                pad.pin,
                                mv_via_x,
                                mv_via_y,
                                mv_diameter,
                                mv_drill,
                            )
                            via_x, via_y, nudged = mv_via_x, mv_via_y, mv_nudged
                            via_diameter = mv_diameter
                            via_drill = mv_drill
                            is_micro_via_used = True
                # Issue #3033 / #3062: ``skip_on_clearance_violation``
                # switches the "proceed anyway" branch to "return None"
                # so the caller can surface the rescue failure as an
                # explicit deferral instead of producing downstream DRC
                # noise that cascades into NRST/GND corner-pad routing
                # failures (board-04 LQFP-48 OSC_OUT cluster).
                if not is_micro_via_used:
                    if skip_on_clearance_violation:
                        logger.info(
                            "In-pad rescue DEFERRED for pad %s (ref=%s pin=%s) at "
                            "(%.3f, %.3f): dead-centre clips neighbour pad on %s "
                            "and long-axis nudge cannot rescue (short-axis "
                            "violation).  Returning None per Issue #3033 strict "
                            "mode so the caller can surface the deferral instead "
                            "of committing a DRC violation.",
                            pad.net_name,
                            pad.ref,
                            pad.pin,
                            via_x,
                            via_y,
                            pad.ref,
                        )
                        return None
                    logger.warning(
                        "In-pad rescue for pad %s (ref=%s pin=%s) at (%.3f, %.3f) "
                        "violates clearance to a neighboring foreign-net pad on "
                        "%s.  Proceeding anyway (no fallback path on QFP/LQFP); "
                        "the resulting via will trigger DRC errors at the "
                        "manufacturer's clearance rule -- consider a smaller-via "
                        "tier or a wider-pitch footprint (Issue #2944).",
                        pad.net_name,
                        pad.ref,
                        pad.pin,
                        via_x,
                        via_y,
                        pad.ref,
                    )

        # Issue #3429: adjacent-pin via-in-pad conflict detection.  The
        # pad-clearance check above only validates the candidate via
        # against the footprint's PADS; a sibling fine-pitch pin that
        # already claimed an in-pad via in THIS pass is invisible there
        # (the routing grid is not populated until commit time).  At
        # sub-pitch spacing the two via barrels cannot coexist -- refuse
        # the second rescue here so the dispatcher falls through to the
        # lateral / surface escape path, instead of emitting a pair that
        # ``apply_escape_routes`` later silently drops one of.  The check
        # is a no-op when ``existing_escapes`` is None (legacy callers),
        # preserving byte-for-byte behaviour.
        conflicting = (
            self._adjacent_in_pad_via_conflict(
                x=via_x,
                y=via_y,
                via_diameter=via_diameter,
                clearance=effective_clearance,
                same_net=pad.net,
                existing_escapes=existing_escapes,
            )
            if enforce_adjacent_via_spacing
            else None
        )
        if conflicting is not None:
            self.adjacent_in_pad_via_conflicts_refused += 1
            # ``_adjacent_in_pad_via_conflict`` only returns an EscapeRoute
            # whose ``.via`` is non-None (it skips ``via is None`` siblings),
            # so narrowing here is sound -- the local lets mypy prove the
            # attribute access below type-checks (Via | None -> Via).
            sibling_via = conflicting.via
            assert sibling_via is not None
            logger.info(
                "In-pad rescue REFUSED for pad %s (ref=%s pin=%s) at "
                "(%.3f, %.3f): via barrel (OD %.3fmm) conflicts with the "
                "in-pad via already claimed by sibling pin %s (net %s) at "
                "(%.3f, %.3f) -- center spacing below via_r+via_r+clearance "
                "at this pitch.  Returning None so the dispatcher takes the "
                "lateral / surface escape path rather than committing a pair "
                "that apply_escape_routes would later drop (Issue #3429).",
                pad.net_name,
                pad.ref,
                pad.pin,
                via_x,
                via_y,
                via_diameter,
                conflicting.pad.pin,
                conflicting.pad.net_name,
                sibling_via.x,
                sibling_via.y,
            )
            return None

        # Select inner escape layer (In1.Cu on 4-layer, B.Cu on 2-layer).
        escape_layer = self._select_inner_escape_layer(pad.layer)

        # Inner-layer escape point: continue inward toward the package
        # body (same direction the deferred surface escape would have
        # used) so the main router can pick up from there.
        #
        # Issue #3428: when the caller supplied a target-aware
        # ``target_direction`` (net's actual routing target, computed by
        # ``_compute_target_direction``), the stub vector uses it instead
        # of the parity-derived ``direction`` so the stub does not block
        # an adjacent fine-pitch pin's via slot.  ONLY the stub vector
        # (and hence ``escape_point``) changes -- via placement above is
        # direction-independent.
        effective_direction = target_direction if target_direction is not None else direction
        # Use a modest offset -- one via radius plus clearance plus a
        # trace width buffer is enough room for the main router to
        # connect onto the inner-layer endpoint without colliding with
        # the via barrel itself.
        offset = via_diameter / 2 + effective_clearance + self.rules.trace_width

        in_pad_via = Via(
            x=via_x,
            y=via_y,
            drill=via_drill,
            diameter=via_diameter,
            layers=(pad.layer, escape_layer),
            net=pad.net,
            net_name=pad.net_name,
            in_pad=True,
            # Issue #3118: tag the micro-via fallback so
            # ``validate/rules/dimensions.py`` skips the standard
            # min-via floors for it (the manufacturer's published
            # Capability+ floor is 0.1 mm drill / 0.2 mm OD which
            # the 0.15 / 0.3 default comfortably clears).  The flag
            # is named ``is_micro`` per the schema convention from
            # #3126 (which added the round-trip ``(via micro ...)``
            # serialisation through the finalize pipeline).
            is_micro=is_micro_via_used,
        )

        # Issue #3428: neck the TARGET-AWARE stub down to the manufacturer
        # minimum trace width.  At 0.5 mm pitch the dispatcher-supplied
        # ``escape_width`` can be the full net trace width (0.5 mm on
        # power-derived defaults); two adjacent redirected stubs at that
        # width touch edge-to-edge (0.000 mm clearance) no matter which
        # directions they point.  Necking mirrors the PR #3079 lateral-
        # stub precedent (``_try_lateral_via_escape``).  The legacy
        # ``target_direction is None`` path keeps the dispatcher width
        # byte-for-byte.
        stub_width = escape_width
        if target_direction is not None and self._mfr_limits is not None:
            mfr_min_trace = self._mfr_limits.min_trace
            if mfr_min_trace is not None and mfr_min_trace < stub_width:
                stub_width = mfr_min_trace

        # Issue #3470: conflict-aware stub direction.  When the caller
        # supplied the escapes generated so far, validate the proposed
        # stub against every foreign-net escape stub/via and retry with
        # the opposite then perpendicular directions on conflict.  On
        # board 05's U3 this is what stops pin 31 (ISENSE_B-) and pin 33
        # (ISENSE_A-) from emitting mutually-overlapping inner stubs that
        # strand both nets.  Retried (non-primary) stubs are necked to
        # the manufacturer minimum trace width like the #3428 redirected
        # stubs, for the same adjacency reason.
        dx, dy = self._direction_to_vector(effective_direction)
        escape_x = via_x + dx * offset
        escape_y = via_y + dy * offset
        if existing_escapes is not None:
            necked_width = stub_width
            if self._mfr_limits is not None:
                mfr_min_trace = self._mfr_limits.min_trace
                if mfr_min_trace is not None and mfr_min_trace < necked_width:
                    necked_width = mfr_min_trace
            candidates: list[EscapeDirection] = [effective_direction]
            opposite = self._OPPOSITE_DIRECTIONS.get(effective_direction)
            if opposite is not None:
                candidates.append(opposite)
            candidates.extend(self._PERPENDICULAR_DIRECTIONS.get(effective_direction, ()))
            for cand_index, cand_dir in enumerate(candidates):
                cand_width = stub_width if cand_index == 0 else necked_width
                cdx, cdy = self._direction_to_vector(cand_dir)
                cand_x = via_x + cdx * offset
                cand_y = via_y + cdy * offset
                if not self._in_pad_stub_conflicts(
                    via_x,
                    via_y,
                    cand_x,
                    cand_y,
                    cand_width,
                    escape_layer,
                    pad.net,
                    effective_clearance,
                    existing_escapes,
                ):
                    if cand_index != 0:
                        logger.info(
                            "In-pad stub for %s pin %s (net %s) redirected "
                            "%s -> %s: primary stub direction conflicts "
                            "with an already-generated foreign-net escape "
                            "stub/via (Issue #3470).",
                            pad.ref,
                            pad.pin,
                            pad.net_name,
                            effective_direction.name,
                            cand_dir.name,
                        )
                        effective_direction = cand_dir
                        stub_width = cand_width
                        escape_x, escape_y = cand_x, cand_y
                    break
            else:
                logger.warning(
                    "In-pad stub for %s pin %s (net %s): every candidate "
                    "stub direction conflicts with an already-generated "
                    "foreign-net escape; keeping primary direction %s "
                    "(Issue #3470).",
                    pad.ref,
                    pad.pin,
                    pad.net_name,
                    effective_direction.name,
                )

        inner_seg = Segment(
            x1=via_x,
            y1=via_y,
            x2=escape_x,
            y2=escape_y,
            width=stub_width,
            layer=escape_layer,
            net=pad.net,
            net_name=pad.net_name,
        )

        logger.info(
            "In-pad escape generated for pad %s (%s ref=%s pin=%s): "
            "via at (%.3f, %.3f) -> %s, stub dir=%s%s",
            pad.net_name,
            pad.layer.kicad_name,
            pad.ref,
            pad.pin,
            via_x,
            via_y,
            escape_layer.kicad_name,
            effective_direction.name,
            " (target-aware, Issue #3428)" if target_direction is not None else "",
        )

        return EscapeRoute(
            pad=pad,
            direction=effective_direction,
            escape_point=(escape_x, escape_y),
            escape_layer=escape_layer,
            via_pos=(via_x, via_y),
            segments=[inner_seg],
            via=in_pad_via,
            ring_index=0,
        )

    def _try_lateral_via_escape(
        self,
        pad: Pad,
        direction: EscapeDirection,
        effective_clearance: float,
        escape_width: float,
        package: PackageInfo | None = None,
        max_offset_mm: float | None = None,
        step_mm: float = 0.05,
        existing_escapes: list[EscapeRoute] | None = None,
    ) -> EscapeRoute | None:
        """Probe off-pad via candidates along the pin's escape direction.

        Issue #3063 (sub-B of #3048): when ``_try_in_pad_escape`` returns
        ``None`` in strict mode (``skip_on_clearance_violation=True``),
        the caller has no rescue path -- the in-pad via would clip a
        foreign neighbour and the strict policy refuses to commit it.
        This helper provides the lateral re-attempt: starting from the
        pad center, step outward along ``direction`` at ``step_mm``
        increments up to ``max_offset_mm``, looking for the first
        position where :meth:`_can_place_via` accepts a candidate via
        against the same foreign-pad context the in-pad rescue used.

        The returned route is geometrically equivalent to an in-pad
        rescue except the via has been pushed off the pad by a small
        lateral offset: an L-shaped surface stub from the pad to the
        via location, then an inner-layer escape segment continuing
        the same outward direction so the main router picks up the net
        cleanly.

        Search strategy:
        1. Skip offset 0 (that's what ``_try_in_pad_escape`` already
           tried -- pointless to re-test the dead-centre position).
        2. Step ``i = 1, 2, ...`` with ``offset = i * step_mm``.  At each
           step, try the position ``(pad.x + dx * offset, pad.y + dy * offset)``
           where ``(dx, dy)`` is the direction's unit vector.
        3. Validate the candidate against the same foreign-pad set the
           in-pad rescue uses (other pads on the same footprint that
           belong to a different net).
        4. The first candidate that passes is returned.  If none pass
           up to ``max_offset_mm``, return ``None``.

        This mirrors the surface-stub-then-via pattern from
        :meth:`_create_alternating_escape` plus an inner-layer
        continuation, but the via is placed AT the candidate position
        rather than dead-centre on the pad.

        Args:
            pad: The pad whose in-pad rescue was just rejected.
            direction: Escape direction inherited from the dispatcher;
                the via search walks along this vector.
            effective_clearance: Clearance value used by the dispatcher;
                forwarded to ``_can_place_via`` for the foreign-copper
                check.
            escape_width: Trace width to use for both the surface stub
                and the inner-layer escape segment.
            package: Optional package context.  When supplied, the
                foreign-pad set is restricted to other pads on the
                same footprint with different nets (the same context
                :meth:`_select_in_pad_via_position` uses).  When
                ``None``, no neighbour-pad validation is performed
                (the grid-cell check still runs inside ``_can_place_via``).
            max_offset_mm: Maximum lateral travel distance in mm.
                When ``None`` (default), the budget is auto-derived
                from the pad geometry so the search reaches AT LEAST
                ``pad_long_dim/2 + via_radius + clearance`` along
                ``direction`` -- this is the minimum distance needed
                to clear the own pad copper plus a neighbour's
                clearance halo when the escape direction is along
                the pad's long axis (the common LQFP-48 / fine-pitch
                QFP case where the long axis points outward from the
                chip body).  A 0.5 mm floor mirrors the issue spec
                for small / square pads.  Pass an explicit value to
                override for unit-test geometry where the auto-budget
                would be unnecessarily large.
            step_mm: Step granularity in mm.  Default 0.05 mm matches
                the grid resolution and the existing in-pad nudge step.
            existing_escapes: Escape routes accumulated so far in this
                dispatch pass (Issue #3430).  Each off-pad via candidate is
                validated against the in-pad / lateral vias of these sibling
                escapes via the same ``via_r + via_r + clearance`` barrel-
                spacing predicate the in-pad rescue uses
                (:meth:`_adjacent_in_pad_via_conflict`).  Without this the
                lateral fallback is blind to sibling escapes generated in
                the same pass -- it would merely move the silent commit-time
                drop from the refused in-pad via to its lateral replacement.
                ``None`` (legacy callers / unit fixtures) disables the
                sibling check, preserving byte-for-byte behaviour.

        Returns:
            An ``EscapeRoute`` with the laterally-offset via and the
            inner-layer escape segment when a valid candidate is found,
            or ``None`` when every candidate inside the search budget
            is rejected.
        """
        if not self.via_in_pad_supported:
            # Mirror ``_try_in_pad_escape`` -- without a via-in-pad-capable
            # manufacturer the lateral re-attempt cannot ship either
            # (the resulting via would land on a fine-pitch pad neighbour
            # without filled/plated processing).  Returning None here
            # preserves the existing "defer to main router" behaviour
            # for manufacturers that never supported the in-pad path
            # to begin with.
            return None

        # Pull manufacturer-effective via geometry, mirroring the
        # in-pad helper above so the lateral and in-pad rescues use
        # geometrically-consistent vias.
        if self._mfr_limits is not None:
            via_drill = self._mfr_limits.min_via_drill
            via_diameter = self._mfr_limits.min_via_diameter
        else:
            via_drill = self.rules.via_drill
            via_diameter = self.rules.via_diameter

        dx, dy = self._direction_to_vector(direction)
        if dx == 0.0 and dy == 0.0:
            # VIA_DOWN or unknown direction -- no axis to walk along.
            return None

        # Auto-derive search budget when caller didn't specify.  The
        # binding constraint for fine-pitch QFP/SSOP pads is the OWN
        # pad's long-axis extent: a via at offset ``L`` along the
        # escape direction is only clear of the pad's own copper plus
        # a neighbour pad's clearance halo when L is greater than the
        # pad's half-extent in that direction PLUS the via radius PLUS
        # clearance.  We compute the half-extent in the SPECIFIC
        # direction by projecting the pad's half-width / half-height
        # onto the unit vector ``(dx, dy)`` -- the same projection the
        # in-pad rescue uses for its long-axis nudge.  Floors at 0.5
        # mm so square pads (where the calculation gives a tiny value)
        # still get the spec-mandated minimum search budget.
        if max_offset_mm is None:
            half_x = pad.width / 2
            half_y = pad.height / 2
            # Projected half-extent of the pad rectangle along (dx, dy).
            # For axis-aligned escape directions this is exactly
            # ``half_x`` (E/W) or ``half_y`` (N/S); for diagonals it
            # blends both extents proportionally.
            proj_half = abs(dx) * half_x + abs(dy) * half_y
            auto_budget = proj_half + via_diameter / 2 + effective_clearance + step_mm
            max_offset_mm = max(0.5, auto_budget)

        # Foreign-net pads on the same footprint, mirroring the
        # ``_select_in_pad_via_position`` filter so the lateral probe
        # validates against the SAME neighbour set the in-pad rescue
        # was rejected by.  This keeps the strict-mode contract
        # symmetric: a lateral candidate "accepted" here would also
        # have been accepted by the in-pad rescue's clearance check
        # if it had landed at the same coordinates.
        foreign_pads: list[Pad] | None = None
        if package is not None:
            foreign_pads = [p for p in package.pads if p is not pad and p.net != pad.net]

        # Issue #3073: The surface stub from the pad to the lateral via
        # must fit through the channel between same-row neighbour pads
        # without violating ``effective_clearance``.  On 0.5mm-pitch LQFP
        # the inter-pad copper gap is only ~0.2mm, so a full-width
        # net trace (commonly 0.5mm for power-derived nets) cannot fit.
        # We neck the stub down to the manufacturer-minimum trace width
        # when the dispatcher-supplied ``escape_width`` would violate
        # neighbour-pad clearance; if even the necked width fails, this
        # candidate is rejected and the next offset is tried.  When all
        # candidates fail, the helper returns None (the caller falls
        # back to "defer to main router", matching pre-#3063 behaviour).
        mfr_min_trace = self._mfr_limits.min_trace if self._mfr_limits is not None else None
        narrow_width = mfr_min_trace if mfr_min_trace is not None else escape_width

        # Iterate offsets [step, 2*step, ..., max_offset]; skip 0 because
        # the in-pad rescue already tested that position.
        n_steps = int(round(max_offset_mm / step_mm))
        for i in range(1, n_steps + 1):
            offset = i * step_mm
            cand_x = pad.x + dx * offset
            cand_y = pad.y + dy * offset

            if self._can_place_via(
                x=cand_x,
                y=cand_y,
                net=pad.net,
                foreign_pads=foreign_pads,
                clearance=effective_clearance,
                via_diameter=via_diameter,
            ):
                # Issue #3430: ``_can_place_via`` validates only against
                # footprint pads (and populated grid cells), NOT against
                # the vias of sibling escapes generated earlier in THIS
                # pass -- those live only in ``existing_escapes`` until
                # commit time.  Without this guard the lateral fallback
                # would merely relocate the silent commit-time drop from
                # the refused in-pad via to its off-pad replacement: the
                # offset via could still land inside a sibling via's
                # barrel-spacing halo.  We check ANY foreign-net sibling
                # via (in-pad OR a lateral via placed for an earlier pin
                # this pass), using the same ``via_r + via_r + clearance``
                # predicate the in-pad rescue uses (#3429), so a candidate
                # "accepted" here would also survive
                # ``apply_escape_routes``.  No-op when ``existing_escapes``
                # is None (legacy callers / unit fixtures), preserving
                # byte-for-byte behaviour.
                if self._lateral_via_sibling_conflict(
                    x=cand_x,
                    y=cand_y,
                    via_diameter=via_diameter,
                    clearance=effective_clearance,
                    same_net=pad.net,
                    existing_escapes=existing_escapes,
                ):
                    continue

                # Found a via location that satisfies foreign-pad
                # clearance.  Before committing, validate that the
                # surface stub from the pad center to this via location
                # ALSO clears neighbour pads.  Issue #3073: without
                # this check the stub at the dispatcher-supplied
                # ``escape_width`` (which may be the full net trace
                # width on packages where ``rules.min_trace_width`` is
                # None) overshoots the channel between same-row neighbour
                # pads and creates pad-segment DRC violations.  Try the
                # dispatcher width first; if it fails, try necking down
                # to the manufacturer minimum.  Reject only if BOTH fail.
                chosen_stub_width: float | None = None
                if foreign_pads is None:
                    # No neighbour context to validate against; fall back
                    # to the dispatcher width (legacy behaviour preserved
                    # for unit-test fixtures without package context).
                    chosen_stub_width = escape_width
                else:
                    for try_width in (escape_width, narrow_width):
                        trial = Segment(
                            x1=pad.x,
                            y1=pad.y,
                            x2=cand_x,
                            y2=cand_y,
                            width=try_width,
                            layer=pad.layer,
                            net=pad.net,
                            net_name=pad.net_name,
                        )
                        stub_ok = True
                        for neighbour in foreign_pads:
                            if neighbour.layer != trial.layer:
                                continue
                            gap = self._segment_to_pad_edge_gap(trial, neighbour)
                            if gap < effective_clearance - 1e-6:
                                stub_ok = False
                                break
                        if stub_ok:
                            chosen_stub_width = try_width
                            break

                if chosen_stub_width is None:
                    # Stub at this candidate position would violate
                    # neighbour clearance even at the manufacturer
                    # minimum trace width.  Skip to the next offset.
                    continue

                # Found a passing candidate -- build the EscapeRoute.
                escape_layer = self._select_inner_escape_layer(pad.layer)

                # Surface stub: pad → via location.  Width is the value
                # chosen above (dispatcher-supplied when it fits the
                # channel, otherwise necked to the manufacturer minimum
                # to satisfy fine-pitch inter-pad clearance).
                surface_seg = Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=cand_x,
                    y2=cand_y,
                    width=chosen_stub_width,
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )

                # Via from surface to inner escape layer.  ``in_pad=False``
                # because the via is geometrically OFF the pad copper
                # (that's the whole point of the lateral offset).
                lateral_via = Via(
                    x=cand_x,
                    y=cand_y,
                    drill=via_drill,
                    diameter=via_diameter,
                    layers=(pad.layer, escape_layer),
                    net=pad.net,
                    net_name=pad.net_name,
                    in_pad=False,
                )

                # Inner-layer escape point: continue the same direction
                # past the via so the main router has a clean landing
                # to pick up from, mirroring ``_try_in_pad_escape``.
                inner_offset = via_diameter / 2 + effective_clearance + self.rules.trace_width
                escape_x = cand_x + dx * inner_offset
                escape_y = cand_y + dy * inner_offset

                inner_seg = Segment(
                    x1=cand_x,
                    y1=cand_y,
                    x2=escape_x,
                    y2=escape_y,
                    width=escape_width,
                    layer=escape_layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )

                logger.info(
                    "Lateral via-escape rescue for pad %s (ref=%s pin=%s): "
                    "in-pad deferred; off-pad via at (%.3f, %.3f) "
                    "accepted at lateral offset=%.3fmm along %s. "
                    "L-stub width=%.3fmm on %s -> via -> %s "
                    "(Issue #3063, #3073).",
                    pad.net_name,
                    pad.ref,
                    pad.pin,
                    cand_x,
                    cand_y,
                    offset,
                    direction.name,
                    chosen_stub_width,
                    pad.layer.kicad_name,
                    escape_layer.kicad_name,
                )

                return EscapeRoute(
                    pad=pad,
                    direction=direction,
                    escape_point=(escape_x, escape_y),
                    escape_layer=escape_layer,
                    via_pos=(cand_x, cand_y),
                    segments=[surface_seg, inner_seg],
                    via=lateral_via,
                    ring_index=0,
                )

        # No candidate in the budget passed.  Caller (dispatcher) will
        # treat this as "defer to main router" -- the same outcome the
        # strict branch had before this helper existed, but now we've
        # at least attempted the local rescue first.
        logger.debug(
            "Lateral via-escape rescue for pad %s (ref=%s pin=%s) failed: "
            "no candidate in [%.3fmm, %.3fmm] along %s passes clearance "
            "(Issue #3063).",
            pad.net_name,
            pad.ref,
            pad.pin,
            step_mm,
            max_offset_mm,
            direction.name,
        )
        return None

    def _select_inner_escape_layer(self, surface_layer: Layer) -> Layer:
        """Select the best inner layer for via escape routing.

        Queries the grid's LayerStack for available inner signal layers.
        Prefers the first inner signal layer (typically In1.Cu on 4-layer
        boards) over B.Cu, since inner layers provide shorter via stubs
        and better signal integrity.

        Falls back to B.Cu when no inner signal layers are available
        (e.g., on 2-layer boards or when all inner layers are planes).

        Args:
            surface_layer: The surface layer the pad is on (used as fallback
                reference -- the via must transition away from this layer).

        Returns:
            The selected escape layer (inner signal layer or B.Cu fallback).
        """
        if self.grid.layer_stack is not None:
            inner_indices = self.grid.layer_stack.get_inner_layer_indices()
            for idx in inner_indices:
                layer_def = self.grid.layer_stack.get_layer(idx)
                if layer_def is not None and layer_def.layer_type == LayerType.SIGNAL:
                    return layer_def.layer_enum
        # Fallback: use B.Cu (opposite outer layer)
        return Layer.B_CU

    # Issue #3470: opposite / perpendicular direction tables for the
    # conflict-aware in-pad stub retry in ``_try_in_pad_escape``.
    _OPPOSITE_DIRECTIONS = {
        EscapeDirection.NORTH: EscapeDirection.SOUTH,
        EscapeDirection.SOUTH: EscapeDirection.NORTH,
        EscapeDirection.EAST: EscapeDirection.WEST,
        EscapeDirection.WEST: EscapeDirection.EAST,
        EscapeDirection.NORTHEAST: EscapeDirection.SOUTHWEST,
        EscapeDirection.NORTHWEST: EscapeDirection.SOUTHEAST,
        EscapeDirection.SOUTHEAST: EscapeDirection.NORTHWEST,
        EscapeDirection.SOUTHWEST: EscapeDirection.NORTHEAST,
    }

    _PERPENDICULAR_DIRECTIONS = {
        EscapeDirection.NORTH: (EscapeDirection.EAST, EscapeDirection.WEST),
        EscapeDirection.SOUTH: (EscapeDirection.EAST, EscapeDirection.WEST),
        EscapeDirection.EAST: (EscapeDirection.NORTH, EscapeDirection.SOUTH),
        EscapeDirection.WEST: (EscapeDirection.NORTH, EscapeDirection.SOUTH),
        EscapeDirection.NORTHEAST: (EscapeDirection.NORTHWEST, EscapeDirection.SOUTHEAST),
        EscapeDirection.NORTHWEST: (EscapeDirection.NORTHEAST, EscapeDirection.SOUTHWEST),
        EscapeDirection.SOUTHEAST: (EscapeDirection.SOUTHWEST, EscapeDirection.NORTHEAST),
        EscapeDirection.SOUTHWEST: (EscapeDirection.SOUTHEAST, EscapeDirection.NORTHWEST),
    }

    def _in_pad_stub_conflicts(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        stub_width: float,
        layer,
        net: int,
        clearance: float,
        existing_escapes: list[EscapeRoute],
    ) -> bool:
        """Check a proposed in-pad escape stub against already-generated escapes.

        Issue #3470: on board 05's DRV8301 (U3) the in-pad rescues for
        pin 31 (ISENSE_B-) and pin 33 (ISENSE_A-) emitted inner-layer
        stubs pointing TOWARD each other across the intervening pin 32
        column.  The stubs physically overlapped (DRC actual -0.3135 mm)
        and each net's escape endpoint (virtual pad) landed inside the
        other net's stub copper -- making both nets deterministically
        unroutable (stable across seeds 7/42/123) and leaving overlap
        stub copper in partial outputs (the single blocking violation on
        board 05's committed snapshot).

        Returns True when the proposed stub (centerline ``(x1,y1)``
        -> ``(x2,y2)``, width ``stub_width``, on ``layer``) violates
        copper clearance against any FOREIGN-net segment or via barrel
        of an already-generated escape route.
        """
        from .geometry import segment_to_segment_distance

        eps = 1e-6
        for esc in existing_escapes:
            esc_net = esc.pad.net if esc.pad is not None else 0
            if esc_net == net:
                continue  # Same-net copper may merge freely.
            for seg in esc.segments:
                if seg.layer != layer:
                    continue
                required = (stub_width + seg.width) / 2 + clearance
                dist = segment_to_segment_distance(x1, y1, x2, y2, seg.x1, seg.y1, seg.x2, seg.y2)
                if dist < required - eps:
                    return True
            via = getattr(esc, "via", None)
            if via is not None:
                required = stub_width / 2 + via.diameter / 2 + clearance
                dist = point_to_segment_distance(via.x, via.y, x1, y1, x2, y2)
                if dist < required - eps:
                    return True
        return False

    def _direction_to_vector(self, direction: EscapeDirection) -> tuple[float, float]:
        """Convert escape direction to unit vector."""
        vectors = {
            EscapeDirection.NORTH: (0, 1),
            EscapeDirection.SOUTH: (0, -1),
            EscapeDirection.EAST: (1, 0),
            EscapeDirection.WEST: (-1, 0),
            EscapeDirection.NORTHEAST: (0.707, 0.707),
            EscapeDirection.NORTHWEST: (-0.707, 0.707),
            EscapeDirection.SOUTHEAST: (0.707, -0.707),
            EscapeDirection.SOUTHWEST: (-0.707, -0.707),
            EscapeDirection.VIA_DOWN: (0, 0),
        }
        return vectors.get(direction, (0, 0))

    def apply_escape_routes(self, escapes: list[EscapeRoute]) -> list[Route]:
        """Apply escape routes to the grid and return as Route objects.

        Marks escape paths on the grid to reserve them for routing,
        and converts escape routes to standard Route objects.

        Issue #2998: Before committing each escape, validate the escape's
        segments against foreign-net vias from (a) already-committed
        routes in ``self.grid.routes`` (from earlier escape passes or
        previously-routed nets) and (b) earlier escapes committed in this
        same call.  This is the symmetric sibling of PR #2952's
        via-vs-foreign-segment check: a new SEGMENT must clear a foreign
        VIA, not just the reverse direction PR #2952 already covered.

        Issue #3013 (TWO-PASS COMMIT): the original PR #2999 single-pass
        loop interleaved via-commit and segment-validation per escape,
        so an escape processed EARLY in the list (e.g. SWDIO) had its
        segment validated against an incomplete via universe -- LATER
        escapes (e.g. BOOT0's in-pad rescue via) had not yet committed
        their vias to ``self.grid.routes`` when SWDIO's segment was
        gated.  Result: SWDIO's segment committed, then BOOT0's via
        landed on top of it.  The fix splits the loop into two passes:

        * **Pass A (probe)** walks ``escapes`` and collects every
          escape's planned via into an in-memory probe list, without
          mutating ``self.grid.routes``.  No segment commits occur in
          Pass A.  This makes the entire via universe for this call
          visible before any segment is validated.

        * **Pass B (validate + commit)** walks ``escapes`` again and,
          for each escape, validates its segments against
          ``self.grid.routes`` PLUS the Pass A probe list (threaded
          via the optional ``extra_routes`` kwarg on
          :meth:`_segment_violates_foreign_via_clearance`).  Survivors
          commit normally via ``grid.mark_route``.  Rejected escapes
          leave no orphan grid state because Pass A never mutated the
          grid -- only the survivors' vias land on the grid.

        Threshold choice (HARD INTERSECTION ONLY): the gate flags only
        escapes whose segment copper physically OVERLAPS a foreign-net
        via's copper (negative edge-to-edge clearance).  Marginal
        sub-clearance violations -- segment copper edge within
        ``trace_clearance`` mm of via copper edge but NOT overlapping
        -- are kept and reported as DRC violations downstream,
        mirroring the existing "in-pad rescue ... violates clearance"
        warning semantics (PR #2945 / Issue #2944 last-resort policy).

        This narrow threshold prevents a more aggressive predicate from
        regressing fine-pitch LQFP boxed-in pads (e.g. board-04 NRST on
        a 0.5mm-pitch LQFP-48 west edge) whose only viable escape is
        the in-pad rescue and whose sub-clearance violation is part of
        the existing allowlist baseline.  Dropping such an escape leaves
        the pad unroutable and regresses 9/9 net completion -- the
        cure becomes worse than the disease.

        IMPORTANT: when an escape is rejected by the clearance gate, it
        is REMOVED IN PLACE from ``escapes`` so the caller's downstream
        override loop in :meth:`Autorouter.generate_escape_routes`
        (``core.py:10127``) sees only the committed escapes.  Without
        this in-place mutation, ``_escape_pad_overrides`` would be
        populated for dropped escapes, pointing the main router at a
        virtual escape endpoint whose escape segment was never actually
        committed -- producing a connectivity gap between the original
        pad and the virtual endpoint.

        Args:
            escapes: List of escape routes to apply.  Mutated in place
                (offending escapes removed) so the caller's override
                loop iterates only the committed subset.

        Returns:
            List of Route objects representing the escapes that passed
            clearance validation.  Escapes whose segments clip a foreign
            via with HARD intersection (negative clearance) are skipped;
            the main router picks up the pad cleanly from the original
            pad position rather than from a clipped escape endpoint.
        """
        routes: list[Route] = []

        # Issue #2998: trace_clearance used for the segment-vs-foreign-via
        # gate.  Mirrors the predicate the C++ post-route validator uses
        # at ``cpp/src/grid.cpp:510-536`` (block 1c).
        trace_clearance = self.rules.trace_clearance

        # Issue #2998: counters for diagnostics on dropped escapes.
        skipped_seg_vs_via = 0

        # Issue #2998: track which escapes survived the gate so we can
        # mutate ``escapes`` in place after iteration (Python list
        # mutation during iteration is brittle).
        committed_escapes: list[EscapeRoute] = []

        # Issue #3013 -- Pass A: collect every planned via into an
        # in-memory probe list.  No grid mutation here.  Each entry is
        # a synthetic Route holding ONE via (and no segments), so the
        # standard ``_segment_violates_foreign_via_clearance`` iterator
        # -- which walks ``route.vias`` regardless of segment count --
        # accepts it uniformly with the grid's own routes.  The probe
        # list is threaded into Pass B via the predicate's
        # ``extra_routes`` kwarg; iteration order does not matter for
        # the probe (vias are atomic geometric primitives -- one round
        # piece of copper per layer span), so the call's full via
        # universe is visible to every segment validated in Pass B
        # regardless of escape order.
        probe_via_routes: list[Route] = []
        for escape in escapes:
            if escape.via is None:
                continue
            probe_via_routes.append(
                Route(
                    net=escape.pad.net,
                    net_name=escape.pad.net_name,
                    segments=[],
                    vias=[escape.via],
                )
            )

        # Issue #3013 -- Pass B: validate segments against the union of
        # ``self.grid.routes`` and the Pass A probe list, then commit
        # survivors.  Rejected escapes leave no orphan state because
        # Pass A never mutated the grid -- only survivors' vias land
        # via the normal ``grid.mark_route`` call below.
        for escape in escapes:
            # Build the foreign-via list lazily once per escape.  Vias
            # from already-committed routes in ``self.grid.routes``
            # include both (a) vias from prior escape commits in earlier
            # ``apply_escape_routes`` calls (e.g. for other packages) and
            # (b) vias from routes committed earlier IN THIS CALL via
            # the ``grid.mark_route`` below.  Pass A's probe list adds
            # (c) vias from escapes processed LATER in this call --
            # closing the SWDIO-first / BOOT0-second ordering hole that
            # PR #2999's single-pass loop left open (Issue #3013).
            current_net = escape.pad.net
            if escape.segments:
                violation = False
                for seg in escape.segments:
                    # HARD-INTERSECTION threshold: only flag copper
                    # overlap (negative clearance).  See predicate
                    # docstring for the rationale (board-04 NRST
                    # regression risk).
                    if self._segment_violates_foreign_via_clearance(
                        seg,
                        current_net,
                        trace_clearance,
                        hard_intersection_only=True,
                        extra_routes=probe_via_routes,
                    ):
                        violation = True
                        break
                if violation:
                    skipped_seg_vs_via += 1
                    logger.info(
                        "Escape commit: deferred %s pin %s (ref=%s) to main "
                        "router -- segment overlaps foreign-net via copper "
                        "(Issue #2998 -- the SWDIO/BOOT0 family).",
                        escape.pad.net_name,
                        escape.pad.pin,
                        escape.pad.ref,
                    )
                    continue

            route = Route(
                net=escape.pad.net,
                net_name=escape.pad.net_name,
                segments=escape.segments,
                vias=[escape.via] if escape.via else [],
            )

            # Mark on grid
            self.grid.mark_route(route)
            routes.append(route)
            committed_escapes.append(escape)

        # Issue #2998: mutate ``escapes`` in place so the caller's
        # override loop (``Autorouter.generate_escape_routes``) sees only
        # committed escapes.  Dropping an escape from the override map is
        # essential -- a stale override would redirect the main router to
        # a non-existent escape endpoint and leave the original pad
        # unconnected, regressing completion.
        if skipped_seg_vs_via:
            escapes[:] = committed_escapes
            logger.info(
                "Escape commit: %d escape(s) deferred to main router due to "
                "hard intersection with foreign-net via (Issue #2998)",
                skipped_seg_vs_via,
            )

        return routes

    def _segment_violates_foreign_via_clearance(
        self,
        seg: Segment,
        current_net: int,
        trace_clearance: float,
        hard_intersection_only: bool = False,
        extra_routes: list[Route] | None = None,
    ) -> bool:
        """Return True iff ``seg`` violates clearance against any
        foreign-net via committed to ``self.grid.routes`` (or in the
        optional ``extra_routes`` probe list).

        Issue #2998: helper for ``apply_escape_routes`` pre-commit gate.
        Iterates over every committed via on every committed route,
        filters out same-net vias (caller filters via ``current_net``),
        and applies the layer-aware ``_segment_clears_foreign_via``
        predicate.  Returns True on the first violation.

        Issue #3013 (``extra_routes``): the two-pass commit in
        ``apply_escape_routes`` builds an in-memory probe list of
        planned vias for all escapes in the current call BEFORE any
        segment is validated, then threads that list here so the
        predicate sees the full via universe for the call regardless
        of escape iteration order.  Without this, the SWDIO-first /
        BOOT0-second ordering on board-04's U2 produced a clearance
        violation at B.Cu (43.8, 19.7): SWDIO's segment was validated
        against an empty foreign-via universe (BOOT0's via had not yet
        committed) and committed; then BOOT0's via landed on top of
        the SWDIO segment.

        Args:
            seg: The candidate escape segment.
            current_net: Net id of the segment being committed.
                Foreign-net vias have ``via.net != current_net``.
            trace_clearance: Manufacturer minimum copper-to-copper
                clearance in mm.
            hard_intersection_only: When True, only flag escapes whose
                segment copper physically overlaps the foreign via's
                copper (negative clearance).  Forwarded to the
                ``_segment_clears_foreign_via`` predicate.  See its
                docstring for the rationale.
            extra_routes: Optional in-memory probe list of additional
                Route objects whose vias should participate in the
                clearance check.  Used by the two-pass commit in
                ``apply_escape_routes`` (Issue #3013) to surface vias
                planned for later-iterated escapes in the same call.
                Same-net filtering still applies via ``current_net``.

        Returns:
            True if any foreign-net via fails the clearance predicate.
        """
        # Iterate committed routes for foreign-net vias.  The grid stores
        # routes in insertion order; vias from earlier escapes in this
        # same call are already present here.  Issue #3013: extra_routes
        # supplies the planned-but-not-yet-committed via universe for
        # the current ``apply_escape_routes`` call (two-pass commit).
        for route in self.grid.routes:
            if route.net == current_net:
                continue  # Same-net via -- skipped by convention.
            for via in route.vias:
                if not self._segment_clears_foreign_via(
                    seg,
                    via,
                    trace_clearance,
                    hard_intersection_only=hard_intersection_only,
                ):
                    return True
        if extra_routes:
            for route in extra_routes:
                if route.net == current_net:
                    continue  # Same-net via -- skipped by convention.
                for via in route.vias:
                    if not self._segment_clears_foreign_via(
                        seg,
                        via,
                        trace_clearance,
                        hard_intersection_only=hard_intersection_only,
                    ):
                        return True
        return False

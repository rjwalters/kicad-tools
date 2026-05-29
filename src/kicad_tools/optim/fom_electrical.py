"""Electrical FOM soft terms.

Issue #3186: this module implements the four electrical-flavoured terms
of the hybrid FOM:

* Weighted via count (term 2)
* Match-group skew (term 5)
* Diff-pair clearance margin (term 6)
* Decoupling proximity (term 7)

Each function takes the shared :class:`~kicad_tools.optim.fom_features.BoardFeatures`
snapshot and returns a float >= 0 with 0 = perfect.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.optim.fom_features import BoardFeatures, euclidean

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "weighted_via_count",
    "match_group_skew",
    "diff_pair_clearance_margin",
    "decoupling_proximity",
]


# Via cost weights -- matches the cost split in the issue body:
# standard = 1.0, micro = 3.0, blind/buried = 5-10 per tier.
VIA_WEIGHTS = {
    None: 1.0,  # standard through-hole
    "micro": 3.0,
    "blind": 5.0,
    "buried": 8.0,
}


def weighted_via_count(features: BoardFeatures) -> float:
    """Sum via costs weighted by class.

    Normalisation
    -------------
    Returns the raw sum of via cost weights.  0 = no vias; larger = more
    expensive (or more numerous) vias.

    Weights follow JLCPCB-class pricing roughly:
    standard through-hole = 1.0; micro = 3.0; blind = 5.0; buried = 8.0.
    """
    total = 0.0
    for via_list in features.vias_by_net.values():
        for via in via_list:
            vtype = via.via_type  # None for through, else "micro"/"blind"/"buried"
            total += VIA_WEIGHTS.get(vtype, 1.0)
    return total


def match_group_skew(pcb: PCB, default_tolerance_mm: float = 0.1) -> float:
    """Sum of per-group ``max_skew / spec_tolerance``.

    Reuses the producer-side wiring in
    :mod:`kicad_tools.validate.match_group_skew` (PR #3145 / issue #2710).

    Normalisation
    -------------
    Per-group: ``group_skew_mm / group_tolerance_mm``.  Groups whose
    skew is at-or-below tolerance contribute 0.  Groups in violation
    contribute a number that says "how many tolerance-widths over."
    0 = every declared group meets its spec; larger = more violation.

    When the PCB has no declared match groups (no net class map, no
    explicit declarations), the function returns 0 -- there is nothing
    to penalise.
    """
    try:
        from kicad_tools.validate.match_group_skew import derive_group_skew_data
    except ImportError:
        return 0.0

    # We don't have a NetClassRouting map here -- the FOM is computed off
    # the placement/routing pair without the autorouter's session state.
    # Pass None to let derive_group_skew_data degrade to a no-op (matching
    # the producer-side gracef-degrade convention).
    try:
        group_skews, _groups, thresholds = derive_group_skew_data(
            pcb,
            net_class_map=None,
        )
    except Exception:
        return 0.0

    total = 0.0
    for name, skew in group_skews.items():
        tol = thresholds.get(name, default_tolerance_mm)
        if tol <= 1e-9:
            tol = default_tolerance_mm
        violation = max(0.0, skew - tol) / tol
        total += violation
    return total


def diff_pair_clearance_margin(
    features: BoardFeatures,
    pcb: PCB,
    target_clearance_mm: float = 0.2,
) -> float:
    """Sum of ``max(0, target - actual)`` for each detected diff pair.

    For each detected diff pair, we compute the minimum centre-to-centre
    distance between any P-segment and any N-segment of the pair (on the
    same layer) and penalise the shortfall against ``target_clearance_mm``.

    Normalisation
    -------------
    Reported in mm of total clearance shortfall.  0 = all pairs meet or
    beat the target clearance; larger = pairs running too tight.

    When no diff pairs are detected, returns 0.
    """
    try:
        from kicad_tools.router.diffpair import detect_differential_pairs
    except ImportError:
        return 0.0

    net_names: dict[int, str] = {nid: name for nid, name in features.net_names.items() if name}
    if not net_names:
        return 0.0

    try:
        pairs = detect_differential_pairs(net_names)
    except Exception:
        return 0.0

    if not pairs:
        return 0.0

    total = 0.0
    for pair in pairs:
        # detect_differential_pairs returns DifferentialPair which has
        # positive / negative -> DifferentialSignal (.net_id).
        try:
            p_net = pair.positive.net_id
            n_net = pair.negative.net_id
        except AttributeError:
            continue

        p_segs = features.segments_by_net.get(p_net, [])
        n_segs = features.segments_by_net.get(n_net, [])
        if not p_segs or not n_segs:
            continue

        # Find min seg-pair distance on the same layer.
        min_dist = math.inf
        for ps in p_segs:
            for ns in n_segs:
                if ps.layer != ns.layer:
                    continue
                d = _segment_segment_distance(ps.start, ps.end, ns.start, ns.end)
                if d < min_dist:
                    min_dist = d

        if math.isfinite(min_dist):
            shortfall = max(0.0, target_clearance_mm - min_dist)
            total += shortfall
    return total


def _segment_segment_distance(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> float:
    """Minimum Euclidean distance between two 2D line segments.

    Returns 0 if the segments intersect.  Otherwise returns the minimum
    of the four endpoint-to-segment distances.
    """
    if _segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        _point_segment_distance(a1, b1, b2),
        _point_segment_distance(a2, b1, b2),
        _point_segment_distance(b1, a1, a2),
        _point_segment_distance(b2, a1, a2),
    )


def _segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """Standard strict segment intersection test."""

    def ccw(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)

    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _point_segment_distance(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Distance from point p to the segment a-b."""
    abx = b[0] - a[0]
    aby = b[1] - a[1]
    apx = p[0] - a[0]
    apy = p[1] - a[1]
    denom = abx * abx + aby * aby
    if denom <= 1e-12:
        return euclidean(p, a)
    t = (apx * abx + apy * aby) / denom
    t = max(0.0, min(1.0, t))
    cx = a[0] + t * abx
    cy = a[1] + t * aby
    return math.hypot(p[0] - cx, p[1] - cy)


# Power-rail name heuristics for the decoupling proximity term.
POWER_RAIL_HINTS = (
    "VCC",
    "VDD",
    "+3V3",
    "+3V",
    "+5V",
    "+12V",
    "VBAT",
    "VBUS",
    "VAA",
    "AVDD",
    "DVDD",
    "VCCIO",
    "VDDIO",
    "PWR",
)


def _looks_like_power_net(net_name: str) -> bool:
    name = (net_name or "").upper()
    return any(hint in name for hint in POWER_RAIL_HINTS)


def _looks_like_capacitor(reference: str) -> bool:
    """Reference designator looks like a capacitor (C1, C42, etc.)."""
    ref = (reference or "").upper()
    return ref.startswith("C") and (len(ref) > 1 and ref[1].isdigit())


def _looks_like_ic(reference: str) -> bool:
    """Reference looks like an IC (U1, U42, etc.) -- has power pins to decouple."""
    ref = (reference or "").upper()
    return ref.startswith("U") and (len(ref) > 1 and ref[1].isdigit())


def decoupling_proximity(features: BoardFeatures) -> float:
    """Sum of distances from each IC power pin to the nearest cap on the same net.

    For each pad on a power-looking net (VCC/VDD/+3V3/etc.) that belongs to
    an IC (reference starts with U), find the nearest cap pad on the same
    net and add the Euclidean distance.

    Normalisation
    -------------
    Reported in mm.  0 = perfect (every IC power pin has a co-located
    cap); larger = sloppy decoupling.  When no IC/power/cap structure
    exists, the result is 0.

    Hand-engineered "good analog care" -- sub-mm matters; the term grows
    linearly with distance, so a 10mm separation is 100x worse than a
    0.1mm separation.
    """
    total = 0.0
    matched_pins = 0
    for net_id, pads in features.nets_to_pads.items():
        net_name = features.net_names.get(net_id, "")
        if not _looks_like_power_net(net_name):
            continue

        ic_pads = [p for p in pads if _looks_like_ic(p.reference)]
        cap_pads = [p for p in pads if _looks_like_capacitor(p.reference)]
        if not ic_pads or not cap_pads:
            continue

        for ic_pad in ic_pads:
            min_dist = math.inf
            for cap_pad in cap_pads:
                d = euclidean((ic_pad.x, ic_pad.y), (cap_pad.x, cap_pad.y))
                if d < min_dist:
                    min_dist = d
            if math.isfinite(min_dist):
                total += min_dist
                matched_pins += 1
    # No normalisation by pin count -- larger boards with more pins should
    # have proportionally more decoupling work to do, and the magnitude
    # difference is what we want to surface.
    return total

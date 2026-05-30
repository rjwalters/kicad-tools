"""Shared geometric/electrical features used by the FOM terms.

This module centralises feature extraction from a :class:`~kicad_tools.schema.pcb.PCB`
so each soft-term function in :mod:`kicad_tools.optim.fom_geometry`,
:mod:`kicad_tools.optim.fom_electrical`, and :mod:`kicad_tools.optim.fom_thermal`
can request the structure it needs without re-walking the schema repeatedly.

Issue #3186: hybrid FOM with hard-constraint gate + multi-term soft objectives.

The functions here are intentionally thin: they extract, transform, and group
existing PCB data into shapes convenient for FOM term implementations. They do
NOT compute scores -- scoring lives in the per-term modules. This separation
keeps the per-term functions individually testable on synthetic feature inputs,
and lets us share an O(footprints + pads + segments) walk across many terms.

This module is also the home of features that issue #3187 (learned predictor)
plans to consume as inputs to its model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment, Via


@dataclass
class PadFeature:
    """A pad in absolute board coordinates with its parent footprint metadata."""

    x: float
    y: float
    net_number: int
    net_name: str
    reference: str
    pad_number: str
    layers: tuple[str, ...]
    pad_type: str  # smd, thru_hole

    @property
    def is_through_hole(self) -> bool:
        return self.pad_type == "thru_hole"


@dataclass
class FootprintFeature:
    """A footprint with derived bbox and pad positions in absolute coords."""

    reference: str
    value: str
    name: str  # library footprint name
    x: float  # position in board-relative coords
    y: float
    rotation: float
    layer: str
    locked: bool
    is_fixed: bool  # connectors, mounting holes, edge-fixed parts
    pad_features: list[PadFeature] = field(default_factory=list)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Bounding box in absolute coords (min_x, min_y, max_x, max_y).

        Falls back to a 0-size box at the footprint's centre if no pads exist.
        """
        if not self.pad_features:
            return (self.x, self.y, self.x, self.y)
        xs = [p.x for p in self.pad_features]
        ys = [p.y for p in self.pad_features]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class BoardFeatures:
    """All features the FOM terms need, extracted once.

    Each FOM term receives one of these and pulls what it needs.  The
    structure is intentionally flat -- terms that need higher-level
    derived quantities (Steiner lower bound, congestion grid) compute
    them on demand from the primitives here.
    """

    footprints: list[FootprintFeature] = field(default_factory=list)
    # Net id -> list of pads on that net (across all footprints)
    nets_to_pads: dict[int, list[PadFeature]] = field(default_factory=dict)
    # Net id -> net name (for human-readable diagnostics)
    net_names: dict[int, str] = field(default_factory=dict)
    # Board bbox (min_x, min_y, max_x, max_y) -- from board outline if present,
    # else from footprint extent.  Used to size the congestion grid.
    board_bbox: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0)
    # All routed segments grouped by net id
    segments_by_net: dict[int, list[Segment]] = field(default_factory=dict)
    # All vias grouped by net id
    vias_by_net: dict[int, list[Via]] = field(default_factory=dict)

    @property
    def total_pad_count(self) -> int:
        return sum(len(fp.pad_features) for fp in self.footprints)

    @property
    def fixed_footprints(self) -> list[FootprintFeature]:
        """Connectors, mounting holes, and locked parts (define essential exterior)."""
        return [fp for fp in self.footprints if fp.is_fixed]


# Reference prefixes for parts whose position is typically fixed by mechanical
# design.  Used for the "essential exterior" estimate in the compactness term
# and as a hint elsewhere.
FIXED_REF_PREFIXES = ("J", "MK", "MH", "TP", "X", "SW", "BT")


def _is_fixed_footprint(fp: Footprint) -> bool:
    """Heuristic: is this footprint position-constrained by mechanical design?

    Locked footprints count as fixed; otherwise we look at the reference
    prefix.  Connectors (J), mounting holes (MK/MH), test points (TP), and
    crystals/oscillators (X) are typical "must be here" parts.
    """
    if fp.locked:
        return True
    ref = (fp.reference or "").upper()
    for prefix in FIXED_REF_PREFIXES:
        if ref.startswith(prefix) and (len(ref) == len(prefix) or ref[len(prefix)].isdigit()):
            return True
    return False


def _pad_absolute_position(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Compute the pad's absolute (board-relative) position.

    Footprint pad coords are relative to the footprint origin and rotated by
    the footprint's rotation (CCW in KiCad's coord convention).  This helper
    converts to board-relative coords.
    """
    px, py = pad.position
    if fp.rotation:
        theta = math.radians(fp.rotation)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        rx = px * cos_t - py * sin_t
        ry = px * sin_t + py * cos_t
    else:
        rx, ry = px, py
    return (fp.position[0] + rx, fp.position[1] + ry)


def extract_features(pcb: PCB) -> BoardFeatures:
    """Build a :class:`BoardFeatures` snapshot from a PCB.

    This is the single shared walk over the PCB the FOM terms rely on.
    Repeated calls re-walk; callers that compute multiple terms should
    cache the result and pass it in.
    """
    features = BoardFeatures()

    # Pull net names for diagnostics.
    for net_num, net in pcb.nets.items():
        features.net_names[net_num] = getattr(net, "name", "") or ""

    # Walk footprints, build :class:`FootprintFeature` + pad list.
    for fp in pcb.footprints:
        ff = FootprintFeature(
            reference=fp.reference,
            value=fp.value,
            name=fp.name,
            x=fp.position[0],
            y=fp.position[1],
            rotation=fp.rotation,
            layer=fp.layer,
            locked=fp.locked,
            is_fixed=_is_fixed_footprint(fp),
        )

        for pad in fp.pads:
            ax, ay = _pad_absolute_position(fp, pad)
            pf = PadFeature(
                x=ax,
                y=ay,
                net_number=pad.net_number,
                net_name=pad.net_name,
                reference=fp.reference,
                pad_number=pad.number,
                layers=tuple(pad.layers),
                pad_type=pad.type,
            )
            ff.pad_features.append(pf)

            # Index by net (ignore net 0 = unconnected).
            if pad.net_number > 0:
                features.nets_to_pads.setdefault(pad.net_number, []).append(pf)

        features.footprints.append(ff)

    # Group segments and vias by net.
    for seg in pcb.segments:
        if seg.net_number > 0:
            features.segments_by_net.setdefault(seg.net_number, []).append(seg)
    for via in pcb.vias:
        if via.net_number > 0:
            features.vias_by_net.setdefault(via.net_number, []).append(via)

    # Compute board bbox from footprint pad extent.  This is a coarse
    # approximation -- the precise outline lives on Edge.Cuts but reading
    # graphics in this hot path is more work than the FOM gains.
    all_xs: list[float] = []
    all_ys: list[float] = []
    for fp in features.footprints:
        for pf in fp.pad_features:
            all_xs.append(pf.x)
            all_ys.append(pf.y)
    if all_xs and all_ys:
        margin = 5.0  # mm -- room around pads so grid covers routing area
        features.board_bbox = (
            min(all_xs) - margin,
            min(all_ys) - margin,
            max(all_xs) + margin,
            max(all_ys) + margin,
        )

    return features


# ------------------------------------------------------------------
# Helpers used by multiple terms
# ------------------------------------------------------------------


def manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Manhattan distance between two 2D points."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def euclidean(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Euclidean distance between two 2D points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def segment_length(seg: Segment) -> float:
    """Euclidean length of a routed segment."""
    return euclidean(seg.start, seg.end)


def routed_net_length(features: BoardFeatures, net_number: int) -> float:
    """Total routed length of a net (segments only; vias treated as zero-length)."""
    segs = features.segments_by_net.get(net_number, [])
    return sum(segment_length(s) for s in segs)


def steiner_lower_bound(pads: list[PadFeature]) -> float:
    """Manhattan-distance lower bound on routed length for a multi-pad net.

    We use the Rectilinear Steiner Minimum Tree (RSMT) length via
    :func:`kicad_tools.router.algorithms.steiner.build_rsmt` when possible,
    falling back to the MST cost otherwise.

    For 0 or 1 pads we return 0 (no routing needed).  For 2 pads the lower
    bound is Manhattan distance.  For 3+ pads we delegate to the RSMT solver.
    """
    n = len(pads)
    if n < 2:
        return 0.0

    coords = [(p.x, p.y) for p in pads]
    if n == 2:
        return manhattan(coords[0], coords[1])

    # Delegate to the router's RSMT solver.  We construct lightweight
    # adapter objects that quack like router.primitives.Pad: only x/y
    # are accessed in the multi-terminal case we care about.
    try:
        from kicad_tools.router.algorithms.steiner import build_rsmt
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Pad as RouterPad

        # Build minimal pad-like objects.  The Steiner solver only reads
        # x/y and (for the virtual-pad output) net/net_name/layer/ref/pin --
        # we supply sensible defaults via a duck-typed namedtuple alike.
        router_pads = [
            RouterPad(
                x=p.x,
                y=p.y,
                width=0.1,
                height=0.1,
                net=p.net_number,
                net_name=p.net_name,
                layer=Layer.F_CU,
                ref=p.reference,
                pin=p.pad_number,
                through_hole=p.is_through_hole,
                drill=0.0,
            )
            for p in pads
        ]
        all_pads, edges = build_rsmt(router_pads)
        total = 0.0
        for i, j in edges:
            total += manhattan((all_pads[i].x, all_pads[i].y), (all_pads[j].x, all_pads[j].y))
        return total
    except Exception:
        # Fall back to MST cost as a conservative lower bound.
        return _mst_cost_manhattan(coords)


def _mst_cost_manhattan(points: list[tuple[float, float]]) -> float:
    """Manhattan-distance MST cost using Prim's algorithm."""
    n = len(points)
    if n < 2:
        return 0.0
    in_tree = [False] * n
    in_tree[0] = True
    min_edge = [manhattan(points[0], points[j]) for j in range(n)]
    min_edge[0] = 0.0
    total = 0.0
    for _ in range(n - 1):
        # Pick the unvisited node with the smallest edge to the tree.
        best_j = -1
        best_cost = math.inf
        for j in range(n):
            if not in_tree[j] and min_edge[j] < best_cost:
                best_cost = min_edge[j]
                best_j = j
        if best_j < 0:
            break
        in_tree[best_j] = True
        total += best_cost
        # Update edge costs.
        for k in range(n):
            if not in_tree[k]:
                c = manhattan(points[best_j], points[k])
                if c < min_edge[k]:
                    min_edge[k] = c
    return total


# ------------------------------------------------------------------
# Phase 0 learned-predictor feature vector (issue #3187)
# ------------------------------------------------------------------
#
# The functions below produce a fixed-length, numeric feature vector
# suitable as input to a gradient-boosted classifier.  They are
# *placement-only* (no segments / vias / zones required) because the
# Phase 0 predictor's job is to estimate manufacturability *before*
# we pay the routing compute cost.
#
# The feature set mirrors the issue body's enumeration:
#
# 1.  4   component density per quadrant
# 2.  3   pin density in dense-package neighbourhoods (top-3 dense pkgs)
# 3.  1   max free-channel-width estimate
# 4.  1   rectilinear Steiner-minimum-tree total length over signal nets
# 5.  3   component-to-edge min, pin-to-pin min, decoupling proximity median
# 6.  1   dense-package count
# 7.  2   analog / digital net inter-component proximity (means)
# 8.  3   bbox aspect ratio, convex-hull area, wasted-space ratio
# 9.  2   pour-net pad coverage, isolated-pad count
# ----
#         20 features total
#
# Each feature is finite and float-valued.  NaNs are mapped to 0.0
# at the caller boundary so downstream estimators don't choke.


PHASE0_FEATURE_NAMES: tuple[str, ...] = (
    "comp_density_q1",
    "comp_density_q2",
    "comp_density_q3",
    "comp_density_q4",
    "pin_density_pkg1",
    "pin_density_pkg2",
    "pin_density_pkg3",
    "free_channel_width_max",
    "steiner_signal_length",
    "comp_to_edge_min",
    "pin_to_pin_min",
    "decoupling_proximity_median",
    "dense_package_count",
    "analog_inter_comp_mean",
    "digital_inter_comp_mean",
    "bbox_aspect_ratio",
    "convex_hull_area",
    "wasted_space_ratio",
    "pour_pad_coverage",
    "isolated_pad_count",
)


def _safe(x: float) -> float:
    """Coerce NaN/inf to 0.0 so the feature vector is always finite."""
    if x is None:
        return 0.0
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(xf) or math.isinf(xf):
        return 0.0
    return xf


def _component_density_per_quadrant(features: BoardFeatures) -> tuple[float, float, float, float]:
    """Components per mm^2 in each of the 4 board-bbox quadrants."""
    min_x, min_y, max_x, max_y = features.board_bbox
    mid_x = 0.5 * (min_x + max_x)
    mid_y = 0.5 * (min_y + max_y)
    half_w = max(max_x - mid_x, 1e-6)
    half_h = max(max_y - mid_y, 1e-6)
    area_q = half_w * half_h
    counts = [0, 0, 0, 0]
    for fp in features.footprints:
        if fp.x >= mid_x and fp.y >= mid_y:
            counts[0] += 1
        elif fp.x < mid_x and fp.y >= mid_y:
            counts[1] += 1
        elif fp.x < mid_x and fp.y < mid_y:
            counts[2] += 1
        else:
            counts[3] += 1
    return (
        counts[0] / area_q,
        counts[1] / area_q,
        counts[2] / area_q,
        counts[3] / area_q,
    )


def _pin_density_dense_packages(features: BoardFeatures, top_k: int = 3) -> list[float]:
    """For the top-K densest footprints (pins / bbox area), report pins/mm^2.

    Returns ``top_k`` values, padded with zeros if there are fewer footprints.
    A "dense package" is just whichever footprints have the largest
    pins-per-bbox-area; this captures BGAs, QFNs, fine-pitch QFPs.
    """
    densities: list[float] = []
    for fp in features.footprints:
        n_pads = len(fp.pad_features)
        if n_pads < 4:
            continue
        bx0, by0, bx1, by1 = fp.bbox
        area = max((bx1 - bx0) * (by1 - by0), 1e-4)
        densities.append(n_pads / area)
    densities.sort(reverse=True)
    out = densities[:top_k]
    while len(out) < top_k:
        out.append(0.0)
    return out


def _max_free_channel_width(features: BoardFeatures) -> float:
    """Largest gap between adjacent footprint bboxes along the X axis.

    A crude proxy for routing channels: project all footprint bboxes
    onto the X axis, sort, and find the maximum free width between
    consecutive intervals.  Returns 0 if there are fewer than 2 footprints
    or all footprints overlap horizontally.
    """
    if len(features.footprints) < 2:
        return 0.0
    intervals = []
    for fp in features.footprints:
        bx0, _, bx1, _ = fp.bbox
        intervals.append((bx0, bx1))
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    if len(merged) < 2:
        return 0.0
    gaps = [merged[i + 1][0] - merged[i][1] for i in range(len(merged) - 1)]
    return max(0.0, max(gaps))


def _steiner_total_signal_length(features: BoardFeatures) -> float:
    """Sum of RSMT lower-bound lengths across all multi-pad signal nets.

    Power nets (heuristically detected by name) are excluded so the
    feature captures signal congestion rather than plane-net pad count.
    """
    total = 0.0
    for net_num, pads in features.nets_to_pads.items():
        if len(pads) < 2:
            continue
        name = features.net_names.get(net_num, "") or ""
        if _looks_like_power_net(name):
            continue
        total += steiner_lower_bound(pads)
    return total


def _looks_like_power_net(name: str) -> bool:
    """Crude power-net heuristic (avoid importing the heavier classifier here)."""
    if not name:
        return False
    upper = name.upper()
    return (
        upper.startswith("+")
        or upper.startswith("GND")
        or upper in {"VCC", "VDD", "VSS", "VBUS"}
        or upper.startswith(("VCC", "VDD", "VSS"))
    )


def _component_to_edge_min(features: BoardFeatures) -> float:
    """Minimum distance from any footprint centre to the board bbox edge."""
    min_x, min_y, max_x, max_y = features.board_bbox
    best = math.inf
    for fp in features.footprints:
        d = min(fp.x - min_x, max_x - fp.x, fp.y - min_y, max_y - fp.y)
        if d < best:
            best = d
    return 0.0 if best is math.inf else max(0.0, best)


def _pin_to_pin_min(features: BoardFeatures) -> float:
    """Minimum Euclidean distance between any two pads on different footprints.

    Within-footprint pads (same reference) are excluded; if there are <2
    cross-footprint pads we return 0.  O(N^2) is fine at the scale we
    care about (< ~5K pads per board for Phase 0 seeds).
    """
    all_pads: list[PadFeature] = []
    for fp in features.footprints:
        all_pads.extend(fp.pad_features)
    if len(all_pads) < 2:
        return 0.0
    best = math.inf
    n = len(all_pads)
    # Bail out for huge boards: O(N^2) only up to 4000 pads (~16M comps).
    cap = min(n, 4000)
    for i in range(cap):
        pi = all_pads[i]
        for j in range(i + 1, cap):
            pj = all_pads[j]
            if pi.reference == pj.reference:
                continue
            d = euclidean((pi.x, pi.y), (pj.x, pj.y))
            if d < best:
                best = d
    return 0.0 if best is math.inf else best


def _decoupling_proximity_median(features: BoardFeatures) -> float:
    """Median distance from each decoupling-cap pad to its nearest IC pad.

    Heuristic: any footprint whose reference starts with "C" and that
    sits on a power-looking net is treated as a decoupling cap.
    The IC set is any footprint with >= 8 pads.  When there are no
    decoupling caps or no ICs we return 0.0.
    """
    ic_pads: list[PadFeature] = []
    for fp in features.footprints:
        if len(fp.pad_features) >= 8:
            ic_pads.extend(fp.pad_features)
    if not ic_pads:
        return 0.0
    distances: list[float] = []
    for fp in features.footprints:
        if not (fp.reference or "").upper().startswith("C"):
            continue
        # Only score caps that touch a power-looking net on any pad
        if not any(_looks_like_power_net(p.net_name) for p in fp.pad_features):
            continue
        for cap_pad in fp.pad_features:
            d_best = min(euclidean((cap_pad.x, cap_pad.y), (p.x, p.y)) for p in ic_pads)
            distances.append(d_best)
    if not distances:
        return 0.0
    distances.sort()
    mid = len(distances) // 2
    if len(distances) % 2 == 1:
        return distances[mid]
    return 0.5 * (distances[mid - 1] + distances[mid])


def _dense_package_count(features: BoardFeatures) -> int:
    """Number of footprints with >= 16 pads (BGAs, large QFPs, etc.)."""
    return sum(1 for fp in features.footprints if len(fp.pad_features) >= 16)


def _analog_digital_inter_component(features: BoardFeatures) -> tuple[float, float]:
    """Mean inter-footprint distance restricted to analog vs digital nets.

    Heuristic: a net whose name contains "ADC", "DAC", "ANA", "SIG", "AIN"
    is analog; nets whose name starts with "D" (e.g. D0, DIO, DATA) or
    contain "SCK", "MOSI", "MISO", "I2C", "UART", "USB" are digital.
    Everything else is ignored.  Returns (analog_mean, digital_mean) of
    *per-net pair-distance means* across qualifying nets.  Zero when no
    such nets exist.
    """
    analog: list[float] = []
    digital: list[float] = []
    analog_kw = ("ADC", "DAC", "ANA", "SIG", "AIN", "AOUT")
    digital_kw = ("D0", "D1", "D2", "DIO", "DATA", "SCK", "MOSI", "MISO", "I2C", "UART", "USB")
    for net_num, pads in features.nets_to_pads.items():
        name = (features.net_names.get(net_num, "") or "").upper()
        if not name:
            continue
        if any(k in name for k in analog_kw):
            target = analog
        elif name.startswith(digital_kw) or any(k in name for k in digital_kw):
            target = digital
        else:
            continue
        refs = {p.reference for p in pads}
        if len(refs) < 2:
            continue
        # Mean pair distance between distinct footprints on this net
        pairs: list[float] = []
        # Aggregate one pad per ref for a coarse measure
        ref_to_xy: dict[str, tuple[float, float]] = {}
        for p in pads:
            ref_to_xy.setdefault(p.reference, (p.x, p.y))
        refs_list = list(ref_to_xy)
        for i in range(len(refs_list)):
            for j in range(i + 1, len(refs_list)):
                pairs.append(euclidean(ref_to_xy[refs_list[i]], ref_to_xy[refs_list[j]]))
        if pairs:
            target.append(sum(pairs) / len(pairs))
    a_mean = (sum(analog) / len(analog)) if analog else 0.0
    d_mean = (sum(digital) / len(digital)) if digital else 0.0
    return a_mean, d_mean


def _bbox_aspect_ratio(features: BoardFeatures) -> float:
    """Aspect ratio of the board bbox (max(w,h) / min(w,h))."""
    min_x, min_y, max_x, max_y = features.board_bbox
    w = max_x - min_x
    h = max_y - min_y
    if w <= 0 or h <= 0:
        return 1.0
    return max(w, h) / min(w, h)


def _convex_hull_area(features: BoardFeatures) -> float:
    """Area of the convex hull of all footprint centres (mm^2).

    Uses the monotone-chain algorithm; no external deps.
    """
    pts = [(fp.x, fp.y) for fp in features.footprints]
    if len(pts) < 3:
        return 0.0
    pts = sorted(set(pts))
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    # Shoelace.
    n = len(hull)
    s = 0.0
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _wasted_space_ratio(features: BoardFeatures) -> float:
    """1 - (sum of footprint bbox areas / board bbox area).

    Higher = more empty space (which may correlate with both
    sparse-easy-to-route boards and poorly-organised layouts).
    """
    min_x, min_y, max_x, max_y = features.board_bbox
    board_area = max((max_x - min_x) * (max_y - min_y), 1e-4)
    fp_area_total = 0.0
    for fp in features.footprints:
        bx0, by0, bx1, by1 = fp.bbox
        fp_area_total += max(bx1 - bx0, 0.0) * max(by1 - by0, 0.0)
    ratio = 1.0 - fp_area_total / board_area
    return max(0.0, min(1.0, ratio))


def _pour_pad_coverage(features: BoardFeatures) -> float:
    """Fraction of pads that lie on a power-looking net.

    A high coverage means most ground/power pads can be served by a
    pour rather than traces, which usually correlates with easier
    routing.
    """
    total = 0
    pour = 0
    for fp in features.footprints:
        for p in fp.pad_features:
            total += 1
            if _looks_like_power_net(p.net_name):
                pour += 1
    if total == 0:
        return 0.0
    return pour / total


def _isolated_pad_count(features: BoardFeatures) -> int:
    """Number of pads whose net has only one pad (or no net assignment).

    These are dead-end pads -- either truly unconnected or the only
    consumer of a single-pad net, both of which look unusual to the
    classifier.
    """
    # Count pads per net first
    net_pad_counts: dict[int, int] = {}
    for fp in features.footprints:
        for p in fp.pad_features:
            net_pad_counts[p.net_number] = net_pad_counts.get(p.net_number, 0) + 1
    count = 0
    for fp in features.footprints:
        for p in fp.pad_features:
            if p.net_number == 0 or net_pad_counts.get(p.net_number, 0) == 1:
                count += 1
    return count


def extract_phase0_features(features: BoardFeatures) -> dict[str, float]:
    """Compute the Phase 0 numeric feature vector.

    Returns a dict with one entry per name in :data:`PHASE0_FEATURE_NAMES`.
    Order is deterministic (matches :data:`PHASE0_FEATURE_NAMES`).
    """
    q = _component_density_per_quadrant(features)
    pin_dens = _pin_density_dense_packages(features, top_k=3)
    a_mean, d_mean = _analog_digital_inter_component(features)

    values: dict[str, float] = {
        "comp_density_q1": _safe(q[0]),
        "comp_density_q2": _safe(q[1]),
        "comp_density_q3": _safe(q[2]),
        "comp_density_q4": _safe(q[3]),
        "pin_density_pkg1": _safe(pin_dens[0]),
        "pin_density_pkg2": _safe(pin_dens[1]),
        "pin_density_pkg3": _safe(pin_dens[2]),
        "free_channel_width_max": _safe(_max_free_channel_width(features)),
        "steiner_signal_length": _safe(_steiner_total_signal_length(features)),
        "comp_to_edge_min": _safe(_component_to_edge_min(features)),
        "pin_to_pin_min": _safe(_pin_to_pin_min(features)),
        "decoupling_proximity_median": _safe(_decoupling_proximity_median(features)),
        "dense_package_count": float(_dense_package_count(features)),
        "analog_inter_comp_mean": _safe(a_mean),
        "digital_inter_comp_mean": _safe(d_mean),
        "bbox_aspect_ratio": _safe(_bbox_aspect_ratio(features)),
        "convex_hull_area": _safe(_convex_hull_area(features)),
        "wasted_space_ratio": _safe(_wasted_space_ratio(features)),
        "pour_pad_coverage": _safe(_pour_pad_coverage(features)),
        "isolated_pad_count": float(_isolated_pad_count(features)),
    }
    # Sanity: keys match exactly the canonical names.
    assert set(values) == set(PHASE0_FEATURE_NAMES)
    return values


def extract_phase0_features_from_pcb(pcb: PCB) -> dict[str, float]:
    """Convenience wrapper: extract features then build the Phase 0 vector."""
    return extract_phase0_features(extract_features(pcb))

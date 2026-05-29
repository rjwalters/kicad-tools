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

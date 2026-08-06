"""Pairwise (net-pair) clearance resolver for HV isolation routing (Issue #4431).

Phase 1 of the "scalar clearance -> pairwise clearance" epic (mirrors the
diff-pair clearance epic #2556). The router's clearance model is
*scalar-per-net* (:attr:`kicad_tools.router.rules.DesignRules.trace_clearance`
and :attr:`~kicad_tools.router.rules.NetClassRouting.clearance`), which cannot
express the HV-isolation requirement: a mains/bank net needs IEC-creepage
spacing (1.3-1.6 mm @150 V PD2) from *low-voltage* copper but only DRU-functional
spacing (0.3-0.4 mm) from its *own cluster's* nets. A single scalar forces the
false choice between "unroutable TO-220 field" (blanket-wide) and "111 board
creepage fails" (cluster-relaxed).

This module adds the shared **data carrier + resolver** and a **Python
post-route validator** primitive. It is a *consumption* problem, not a new
algorithm: the delta-V -> creepage lookup, the HV threshold gating and the
fail-loud out-of-table contract are all reused verbatim from the already-merged
placement derivation
(:func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`, itself
backed by :meth:`kicad_tools.creepage.standards.CreepageStandard.required_creepage`).
Feeding that builder a per-net ``{net_name: |V|}`` map -- each net treated as its
own "domain" -- yields the order-independent ``{(net_a, net_b): required_mm}``
matrix the router resolves against, so the router, placement and the post-route
creepage census all agree by construction.

Explicitly OUT OF SCOPE here (deferred to follow-up architect phases):

* **Phase 2** -- search-time avoidance (widening the A* grid-reservation halo
  around HV copper) plus the C++ ``validate_route`` extension. That is what
  actually lets an HV board *converge* instead of thrashing the negotiator;
  Phase 1 is a diagnostic/foundation only and does NOT by itself make an HV
  board route cleanly.
* **Phase 3** -- KiCad netclass-pair custom ``(rule ...)`` export for
  ``kicad-cli pcb drc`` referee-enforceability.

Backward compatibility: absent a voltage map, :attr:`DesignRules.pairwise_clearance`
is ``None`` and every consumer falls through to the pre-existing scalar path
byte-identically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, NamedTuple, Sequence

from kicad_tools.core.geometry import segment_to_segment_distance

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.primitives import Route, Segment
    from kicad_tools.schema.pcb import Footprint

# A pairwise conflict is only reported when the edge-to-edge gap falls short of
# the requirement by more than this tolerance (mm).  Mirrors the census'
# ``_PASS_TOLERANCE`` -- a sub-micron shortfall is FP noise, not a real defect.
_PASS_TOLERANCE = 1e-4

# Extra necking distance beyond the footprint courtyard (or pad-bounds
# fallback).  Kept as data rather than CLI policy so the same flat regions can
# cross the Python/C++ boundary in Phase 2.
ATTACH_ZONE_MARGIN_MM = 0.5


@dataclass(frozen=True)
class AttachZone:
    """Layer-agnostic rated-footprint necking region.

    ``net_names`` contains every connected net terminating on the footprint.
    Pairwise widening is waived only when the gap point is inside the bbox and
    *both* nets are members; the scalar DRU check has already run separately.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    net_names: frozenset[str]

    def exempts(self, x: float, y: float, net_a: str, net_b: str) -> bool:
        a = _norm_net_key(net_a)
        b = _norm_net_key(net_b)
        return (
            self.min_x <= x <= self.max_x
            and self.min_y <= y <= self.max_y
            and a in self.net_names
            and b in self.net_names
        )


def build_attach_zones(
    footprints: Iterable[Footprint],
    *,
    margin: float = ATTACH_ZONE_MARGIN_MM,
) -> tuple[AttachZone, ...]:
    """Build flat courtyard-bbox attach zones once for a routing session.

    The canonical courtyard polygon resolver supplies geometry when possible.
    Footprints without a resolvable courtyard fall back to the transformed pad
    bounding box, including pad extents.  Empty/unconnected/single-net
    footprints cannot exempt a pair and are omitted.
    """
    from shapely.geometry import Polygon  # type: ignore[import-untyped]

    from kicad_tools.geometry.courtyard import _courtyard_polygon, _fp_transform

    zones: list[AttachZone] = []
    for footprint in footprints:
        names = frozenset(_norm_net_key(pad.net_name) for pad in footprint.pads if pad.net_name)
        if len(names) < 2:
            continue

        bounds: list[tuple[float, float, float, float]] = []
        for side in ("F", "B"):
            polygon = _courtyard_polygon(footprint, side, Polygon)
            if polygon is not None:
                min_x, min_y, max_x, max_y = polygon.bounds
                bounds.append((float(min_x), float(min_y), float(max_x), float(max_y)))

        if not bounds:
            transform = _fp_transform(footprint)
            for pad in footprint.pads:
                x, y = transform(pad.position)
                # Pad rotation is absolute in the board frame (it already
                # includes the parent footprint rotation).  Project all four
                # rectangular corners onto board axes so the fallback never
                # excludes rotated pad copper.
                angle = math.radians(-getattr(pad, "rotation", 0.0))
                cos_rot = math.cos(angle)
                sin_rot = math.sin(angle)
                half_w = abs(cos_rot) * pad.size[0] / 2.0 + abs(sin_rot) * pad.size[1] / 2.0
                half_h = abs(sin_rot) * pad.size[0] / 2.0 + abs(cos_rot) * pad.size[1] / 2.0
                bounds.append((x - half_w, y - half_h, x + half_w, y + half_h))
        if not bounds:
            continue

        zones.append(
            AttachZone(
                min(b[0] for b in bounds) - margin,
                min(b[1] for b in bounds) - margin,
                max(b[2] for b in bounds) + margin,
                max(b[3] for b in bounds) + margin,
                names,
            )
        )
    return tuple(zones)


def _attach_zone_exempts(
    zones: Sequence[AttachZone],
    x: float,
    y: float,
    net_a: str,
    net_b: str,
) -> bool:
    return any(zone.exempts(x, y, net_a, net_b) for zone in zones)


def _norm_net_key(name: str) -> str:
    """Normalise a net name for pairwise lookup (drop one leading ``/``).

    KiCad emits hierarchical net names with a leading ``/`` (``/AC_LINE``);
    hand-authored voltage maps may or may not include it.  Stripping a single
    leading slash on both sides makes the lookup robust to either convention --
    identical to the creepage census' ``_norm_net_key`` (#4371) so the router
    and the census key the same nets.
    """
    return name[1:] if name.startswith("/") else name


@dataclass(frozen=True)
class PairwiseClearanceTable:
    """Order-independent net-pair -> required-clearance carrier (Issue #4431).

    Attributes:
        dru: The scalar manufacturer/DRU clearance floor (mm).  Every resolved
            requirement is at least this value -- a pairwise widening never
            *tightens* below the fab's functional spacing.
        net_voltages: ``{net_name: |V|}`` worst-case voltage magnitude per net,
            keyed by the ``/``-stripped net name.  Retained for provenance /
            diagnostics; the resolver reads :attr:`required_by_pair`.
        required_by_pair: ``{(net_a, net_b): required_mm}`` with order-independent
            (sorted, ``/``-stripped) keys, as produced by
            :func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
            over :attr:`net_voltages`.  Pairs below the HV threshold are absent
            (they need no widening beyond ``dru``).
    """

    dru: float
    net_voltages: Mapping[str, float]
    required_by_pair: Mapping[tuple[str, str], float]

    def required_clearance(self, net_a: str, net_b: str) -> float:
        """Return ``max(dru, creepage_lookup(|Va - Vb|))`` for a net pair.

        Same-net queries and pairs below the HV threshold (absent from
        :attr:`required_by_pair`) return :attr:`dru` -- i.e. the scalar path is
        preserved for everything that does not require HV widening.
        """
        a = _norm_net_key(net_a)
        b = _norm_net_key(net_b)
        if a == b:
            return self.dru
        key = (a, b) if a <= b else (b, a)
        return max(self.dru, self.required_by_pair.get(key, 0.0))

    def max_required_clearance(self) -> float:
        """Largest resolved requirement across every HV pair (mm).

        Issue #4511 / Epic #4431 Phase 2b: the search-time spatial index must
        return every candidate within the *widest* pairwise requirement, not
        just the scalar floor, or the domain-aware blocking kernels never see
        the foreign HV copper they must widen against.  Returns :attr:`dru`
        when no pair needs widening (the dormant / no-voltage-map case), so a
        board without HV pairs inflates its R-tree exactly as before.
        """
        if not self.required_by_pair:
            return self.dru
        return max(self.dru, max(self.required_by_pair.values()))


def build_pairwise_clearance_table(
    net_voltages: Mapping[str, float],
    *,
    dru: float,
    standard_id: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
) -> PairwiseClearanceTable:
    """Build a :class:`PairwiseClearanceTable` from a per-net voltage map.

    Reuses :func:`kicad_tools.placement.hv_domains.build_required_by_domain_pair`
    -- the SAME builder placement (#4373) and the creepage census consume --
    treating each net as its own single-net "domain".  Given identical inputs
    the router and placement therefore produce byte-identical matrices (there is
    no forked lookup).  The delta-V -> creepage step is fail-loud: an out-of-table
    ``|Delta V|`` raises
    :class:`~kicad_tools.creepage.standards.StandardLookupError` rather than
    silently extrapolating.

    Args:
        net_voltages: ``{net_name: volts}`` -- signed or magnitude; magnitudes
            are taken internally.  Reserved ``_``-prefixed metadata keys should
            already be stripped by the loader
            (:func:`kicad_tools.placement.hv_domains.load_voltage_map`).
        dru: Scalar clearance floor (mm), typically ``DesignRules.trace_clearance``.
        standard_id: Creepage standard id (``iec60664`` / ``iec62368``).
        pollution_degree: IEC pollution degree (1, 2 or 3).
        material_group: Insulation material group (``I``/``II``/``IIIa``/``IIIb``).
        hv_threshold: Minimum ``|Delta V|`` (volts) for a pair to receive a
            creepage widening; pairs below it keep only the ``dru`` floor.

    Returns:
        A frozen :class:`PairwiseClearanceTable`.

    Raises:
        StandardLookupError: If a cross-pair ``|Delta V|`` exceeds the highest
            tabulated row (no silent extrapolation).
    """
    # Lazy import keeps the router<->placement dependency off module-import time
    # (placement.__init__ pulls router.rules), avoiding any import-order cycle.
    from kicad_tools.placement.hv_domains import build_required_by_domain_pair

    normalised = {_norm_net_key(name): abs(float(v)) for name, v in net_voltages.items()}
    required = build_required_by_domain_pair(
        normalised,
        standard_id=standard_id,
        pollution_degree=pollution_degree,
        material_group=material_group,
        hv_threshold=hv_threshold,
    )
    return PairwiseClearanceTable(
        dru=float(dru),
        net_voltages=normalised,
        required_by_pair=required,
    )


class CppDomainMatrix(NamedTuple):
    """C++-consumable form of a :class:`PairwiseClearanceTable` (Issue #4510).

    Attributes:
        net_to_domain: Indexed by net id; entry is the net's domain index or
            ``-1`` when the net participates in no widening pair.  Net ids
            beyond the list's length are domain-less by construction.
        matrix: Dense symmetric ``matrix[dom_a][dom_b]`` of *widening* values
            (mm).  Entries are the raw creepage requirement WITHOUT the DRU
            floor, so the C++ validator's ``max(effective_scalar, matrix[a][b])``
            never raises a pair above its scalar rule unless HV widening
            genuinely applies -- matching
            :func:`segment_pair_violation`'s ``required <= floor`` skip.
    """

    net_to_domain: list[int]
    matrix: list[list[float]]


def build_cpp_domain_matrix(
    table: PairwiseClearanceTable | None,
    net_name_to_id: Mapping[str, int],
) -> CppDomainMatrix | None:
    """Project a pairwise table onto integer net ids for ``Grid3D`` (Issue #4510).

    ``Grid3D`` has no string table, so the net-name-keyed
    :attr:`PairwiseClearanceTable.required_by_pair` must be re-expressed as a
    per-net domain-id array plus a dense domain-pair matrix.  Phase 1 treats
    each net as its own domain, so the "domains" here are exactly the nets that
    participate in at least one widening pair AND resolve to a board net id.

    Returns ``None`` when nothing needs widening (no table, an empty matrix, or
    fewer than two resolvable participating nets) -- the dormant signal that
    keeps the C++ setter uncalled and ``validate_route`` byte-identical.
    """
    if table is None:
        return None
    pairs = table.required_by_pair
    if not pairs:
        return None

    participating: set[str] = set()
    for (net_a, net_b), required in pairs.items():
        if required > 0.0:
            participating.add(net_a)
            participating.add(net_b)
    if not participating:
        return None

    # A board net name may carry a leading ``/``; the table keys never do.  Two
    # distinct board nets can in principle normalise to the same key, so every
    # matching id joins the same domain.
    ids_by_key: dict[str, list[int]] = {}
    for name, net_id in net_name_to_id.items():
        key = _norm_net_key(name)
        if key in participating and int(net_id) >= 0:
            ids_by_key.setdefault(key, []).append(int(net_id))
    if len(ids_by_key) < 2:
        return None

    domain_keys = sorted(ids_by_key)  # deterministic domain indices
    domain_index = {key: i for i, key in enumerate(domain_keys)}

    max_id = max(net_id for ids in ids_by_key.values() for net_id in ids)
    net_to_domain = [-1] * (max_id + 1)
    for key, ids in ids_by_key.items():
        for net_id in ids:
            net_to_domain[net_id] = domain_index[key]

    size = len(domain_keys)
    matrix = [[0.0] * size for _ in range(size)]
    for (net_a, net_b), required in pairs.items():
        i = domain_index.get(net_a)
        j = domain_index.get(net_b)
        if i is None or j is None:
            continue
        value = float(required)
        matrix[i][j] = value
        matrix[j][i] = value

    return CppDomainMatrix(net_to_domain=net_to_domain, matrix=matrix)


def attach_zones_to_net_ids(
    zones: Sequence[AttachZone],
    net_name_to_id: Mapping[str, int],
) -> list[tuple[float, float, float, float, list[int]]]:
    """Translate net-name-keyed attach zones to net-id tuples (Issue #4510).

    ``Grid3D`` works in integer net ids, so :attr:`AttachZone.net_names` must be
    resolved through the router's reverse map before crossing into C++.  A zone
    may legitimately span more than two nets (a 3-pad rated connector); zones
    that resolve to fewer than two ids cannot exempt any pair and are dropped.
    """
    ids_by_key: dict[str, list[int]] = {}
    for name, net_id in net_name_to_id.items():
        if int(net_id) >= 0:
            ids_by_key.setdefault(_norm_net_key(name), []).append(int(net_id))

    out: list[tuple[float, float, float, float, list[int]]] = []
    for zone in zones:
        net_ids = sorted({nid for key in zone.net_names for nid in ids_by_key.get(key, ())})
        if len(net_ids) < 2:
            continue
        out.append((zone.min_x, zone.min_y, zone.max_x, zone.max_y, net_ids))
    return out


class PairwiseViolation(NamedTuple):
    """A single post-route pairwise-clearance shortfall (Issue #4431).

    Attributes:
        net_a: The moving/route net name.
        net_b: The foreign net name it is too close to.
        actual_mm: Measured edge-to-edge gap (mm).
        required_mm: The derived pairwise requirement (mm).
        x: Violation x-coordinate (mm) -- midpoint of the foreign segment.
        y: Violation y-coordinate (mm).
    """

    net_a: str
    net_b: str
    actual_mm: float
    required_mm: float
    x: float
    y: float


def _segment_edge_gap(seg_a: Segment, seg_b: Segment) -> float:
    """Edge-to-edge gap (mm) between two trace segments' copper on one layer."""
    centre = segment_to_segment_distance(
        seg_a.x1, seg_a.y1, seg_a.x2, seg_a.y2, seg_b.x1, seg_b.y1, seg_b.x2, seg_b.y2
    )
    return centre - seg_a.width / 2.0 - seg_b.width / 2.0


def _closest_gap_midpoint(seg_a: Segment, seg_b: Segment) -> tuple[float, float]:
    """Return the midpoint between the closest points on two line segments."""
    return closest_gap_midpoint_xy(
        seg_a.x1, seg_a.y1, seg_a.x2, seg_a.y2, seg_b.x1, seg_b.y1, seg_b.x2, seg_b.y2
    )


def closest_gap_midpoint_xy(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> tuple[float, float]:
    """Coordinate-level form of :func:`_closest_gap_midpoint` (issue #4602).

    The lattice engine's search-time attach-zone exemption probes the SAME
    closest-gap midpoint the post-route validator reports, so the in-search
    verdict and the #4588 gate agree by construction.  The lattice works on
    bare point tuples, hence this :class:`Segment`-free entry point.
    """
    ux, uy = ax2 - ax1, ay2 - ay1
    vx, vy = bx2 - bx1, by2 - by1
    wx, wy = ax1 - bx1, ay1 - by1
    a = ux * ux + uy * uy
    b = ux * vx + uy * vy
    c = vx * vx + vy * vy
    d = ux * wx + uy * wy
    e = vx * wx + vy * wy

    if a <= 1e-15 and c <= 1e-15:
        return ((ax1 + bx1) / 2.0, (ay1 + by1) / 2.0)
    if a <= 1e-15:
        t = max(0.0, min(1.0, e / c))
        closest_b = (bx1 + t * vx, by1 + t * vy)
        return ((ax1 + closest_b[0]) / 2.0, (ay1 + closest_b[1]) / 2.0)
    if c <= 1e-15:
        s = max(0.0, min(1.0, -d / a))
        closest_a = (ax1 + s * ux, ay1 + s * uy)
        return ((closest_a[0] + bx1) / 2.0, (closest_a[1] + by1) / 2.0)

    denominator = a * c - b * b
    s_num, s_den = denominator, denominator
    t_num, t_den = denominator, denominator

    if denominator <= 1e-15:
        s_num, s_den = 0.0, 1.0
        t_num, t_den = e, c
    else:
        s_num = b * e - c * d
        t_num = a * e - b * d
        if s_num < 0.0:
            s_num, t_num, t_den = 0.0, e, c
        elif s_num > s_den:
            s_num, t_num, t_den = s_den, e + b, c

    if t_num < 0.0:
        t_num = 0.0
        if -d < 0.0:
            s_num, s_den = 0.0, 1.0
        elif -d > a:
            s_num, s_den = 1.0, 1.0
        else:
            s_num, s_den = -d, a
    elif t_num > t_den:
        t_num = t_den
        if -d + b < 0.0:
            s_num, s_den = 0.0, 1.0
        elif -d + b > a:
            s_num, s_den = 1.0, 1.0
        else:
            s_num, s_den = -d + b, a

    s = 0.0 if abs(s_num) <= 1e-15 else s_num / s_den
    t = 0.0 if abs(t_num) <= 1e-15 else t_num / t_den
    closest_a = (ax1 + s * ux, ay1 + s * uy)
    closest_b = (bx1 + t * vx, by1 + t * vy)
    return ((closest_a[0] + closest_b[0]) / 2.0, (closest_a[1] + closest_b[1]) / 2.0)


def segment_pair_violation(
    seg_a: Segment,
    seg_b: Segment,
    table: PairwiseClearanceTable,
    *,
    net_a_name: str | None = None,
    net_b_name: str | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
) -> PairwiseViolation | None:
    """Check one segment pair against its derived pairwise requirement.

    Returns a :class:`PairwiseViolation` when the two segments share a layer and
    their edge-to-edge gap falls short of ``required_clearance(a, b)`` by more
    than ``tolerance``; otherwise ``None``.  Pairs whose requirement does not
    exceed the scalar DRU floor are skipped -- the ordinary scalar clearance
    check already governs them, so this validator only adds the *HV widening*.

    Net names default to the segments' own :attr:`Segment.net_name`; the live
    in-loop validators pass ``net_a_name``/``net_b_name`` explicitly because a
    mid-route segment may not yet carry a populated net-name string (the net id
    is authoritative there and is resolved by the caller).

    Different-layer pairs return ``None`` in Phase 1 (surface creepage is a
    same-layer phenomenon here; cross-layer/through-hole geometry is the census'
    and Phase 2's remit).
    """
    if seg_a.layer != seg_b.layer:
        return None
    a_name = seg_a.net_name if net_a_name is None else net_a_name
    b_name = seg_b.net_name if net_b_name is None else net_b_name
    floor = table.dru if dru is None else dru
    required = table.required_clearance(a_name, b_name)
    if required <= floor + tolerance:
        # No HV widening for this pair -- the scalar path already covers it.
        return None
    gap = _segment_edge_gap(seg_a, seg_b)
    if gap >= required - tolerance:
        return None
    gap_x, gap_y = _closest_gap_midpoint(seg_a, seg_b)
    if gap >= floor - tolerance and _attach_zone_exempts(
        attach_zones, gap_x, gap_y, a_name, b_name
    ):
        return None
    return PairwiseViolation(
        net_a=a_name,
        net_b=b_name,
        actual_mm=gap,
        required_mm=required,
        x=gap_x,
        y=gap_y,
    )


def route_pairwise_violation(
    route: Route,
    exclude_net: int,
    foreign_routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
) -> PairwiseViolation | None:
    """Find the first pairwise-clearance shortfall for a freshly-routed net.

    Walks every segment of ``route`` against every segment of the already-routed
    ``foreign_routes`` (skipping the route's own net), returning the first
    :class:`PairwiseViolation` or ``None`` when the route clears all pairwise
    requirements.  This is the additive HV check threaded into the Python
    post-route validators (``pathfinder`` and ``cpp_backend``); the scalar
    segment/pad/via checks run first and unchanged.

    ``exclude_net`` is the route's own net id (same-net copper never conflicts).
    ``id_to_name`` resolves a net id to its board net name; when omitted the
    routes' own :attr:`Route.net_name` strings are used.  Ids are authoritative
    mid-route (segment/route name strings may be unset), so the live validators
    pass an inverted ``net_name_to_id`` map here.
    """
    if table is None:
        return None
    floor = table.dru if dru is None else dru
    # ``exclude_net`` is the route's own net id and is authoritative mid-route
    # (``route.net``/``route.net_name`` may be unset before finalisation).
    moving_name = _resolve_net_name(id_to_name, exclude_net, route.net_name)
    for other in foreign_routes:
        if other.net == exclude_net:
            continue
        # Cheap prune: skip the whole foreign route when this net pair needs no
        # HV widening beyond the scalar floor.
        foreign_name = _resolve_net_name(id_to_name, other.net, other.net_name)
        required = table.required_clearance(moving_name, foreign_name)
        if required <= floor + tolerance:
            continue
        for seg in route.segments:
            for oseg in other.segments:
                violation = segment_pair_violation(
                    seg,
                    oseg,
                    table,
                    net_a_name=moving_name,
                    net_b_name=foreign_name,
                    dru=floor,
                    attach_zones=attach_zones,
                    tolerance=tolerance,
                )
                if violation is not None:
                    return violation
    return None


def find_pairwise_violations(
    routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
    attach_zones: Sequence[AttachZone] = (),
    tolerance: float = _PASS_TOLERANCE,
) -> list[PairwiseViolation]:
    """Scan a whole routed board for pairwise-clearance shortfalls (board-level).

    Every unordered pair of distinct-net routes is checked segment-vs-segment.
    Returns a deterministically-ordered list of every :class:`PairwiseViolation`
    found (empty when the board satisfies all pairwise requirements).  Intended
    for a post-route board audit / tests; the in-loop validators use
    :func:`route_pairwise_violation` for early-exit.

    ``attach_zones`` (issue #4588) carries the #4506 rated-footprint necking
    regions through to :func:`segment_pair_violation`, exactly as
    :func:`route_pairwise_violation` already does.  Omitting them would make
    this scan report every deliberately-exempted domain-bridging footprint
    (tap resistors, TO-220 packages, optocouplers) as a violation -- a false
    *fail* that renders every HV board unroutable by construction.
    """
    materialised = list(routes)
    floor = table.dru if dru is None else dru
    out: list[PairwiseViolation] = []
    for i in range(len(materialised)):
        ra = materialised[i]
        a_name = _resolve_net_name(id_to_name, ra.net, ra.net_name)
        for j in range(i + 1, len(materialised)):
            rb = materialised[j]
            if ra.net == rb.net:
                continue
            b_name = _resolve_net_name(id_to_name, rb.net, rb.net_name)
            required = table.required_clearance(a_name, b_name)
            if required <= floor + tolerance:
                continue
            for seg in ra.segments:
                for oseg in rb.segments:
                    violation = segment_pair_violation(
                        seg,
                        oseg,
                        table,
                        net_a_name=a_name,
                        net_b_name=b_name,
                        dru=floor,
                        attach_zones=attach_zones,
                        tolerance=tolerance,
                    )
                    if violation is not None:
                        out.append(violation)
    return out


def _resolve_net_name(id_to_name: Mapping[int, str] | None, net_id: int, fallback: str) -> str:
    """Resolve a net id to its board net name, falling back to a known string."""
    if id_to_name is not None:
        name = id_to_name.get(net_id)
        if name:
            return name
    return fallback

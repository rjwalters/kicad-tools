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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, NamedTuple

from kicad_tools.core.geometry import segment_to_segment_distance

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.primitives import Route, Segment

# A pairwise conflict is only reported when the edge-to-edge gap falls short of
# the requirement by more than this tolerance (mm).  Mirrors the census'
# ``_PASS_TOLERANCE`` -- a sub-micron shortfall is FP noise, not a real defect.
_PASS_TOLERANCE = 1e-4


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


def segment_pair_violation(
    seg_a: Segment,
    seg_b: Segment,
    table: PairwiseClearanceTable,
    *,
    net_a_name: str | None = None,
    net_b_name: str | None = None,
    dru: float | None = None,
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
    return PairwiseViolation(
        net_a=a_name,
        net_b=b_name,
        actual_mm=gap,
        required_mm=required,
        x=(seg_b.x1 + seg_b.x2) / 2.0,
        y=(seg_b.y1 + seg_b.y2) / 2.0,
    )


def route_pairwise_violation(
    route: Route,
    exclude_net: int,
    foreign_routes: Iterable[Route],
    table: PairwiseClearanceTable,
    *,
    id_to_name: Mapping[int, str] | None = None,
    dru: float | None = None,
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
    tolerance: float = _PASS_TOLERANCE,
) -> list[PairwiseViolation]:
    """Scan a whole routed board for pairwise-clearance shortfalls (board-level).

    Every unordered pair of distinct-net routes is checked segment-vs-segment.
    Returns a deterministically-ordered list of every :class:`PairwiseViolation`
    found (empty when the board satisfies all pairwise requirements).  Intended
    for a post-route board audit / tests; the in-loop validators use
    :func:`route_pairwise_violation` for early-exit.
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

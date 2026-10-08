"""Bounded inward off-pad access for trapped Kelvin sense terminals (Issue #5398).

Problem
-------
On a dense fine-pitch package (board 05's DRV8301 is the stress fixture) the
escape pre-phase can leave a Kelvin sense terminal's escape *inside its own
pad*: the outward lateral via search found no legal site (the outward channel
is already full of neighbouring escape vias), and the surface stub never left
the land.  The general router then has to reach that pin across the same
congested front-layer channel, and a later route can consume the pin's last
exit.  The sense net ends up stranded even though a legal ordinary via exists
on the *inward* side of the pad (under the package body), where the outward
escapes do not compete.

What this module does
---------------------
:func:`recover_kelvin_escapes` runs during escape generation, before general
routing.  For each escape of a recognised Kelvin net
(:func:`~kicad_tools.router.kelvin.detect_kelvin_topology`, which needs a
real shunt/sense-resistor root -- a sense-like name alone never activates it)
whose surface endpoint is still on its pad:

1. Re-run the existing bounded lateral via search
   (:meth:`EscapeRouter._try_lateral_via_escape`) **outward** with the full
   physical predicate below.  If an outward candidate passes, the original
   escape is left alone: this module only acts when the terminal really is
   trapped.
2. Otherwise run the same bounded search **inward** (opposite direction),
   each offset judged by the same predicate.  A failed search leaves the
   original escape unchanged.

The search budget, step and via geometry are exactly the ones the existing
lateral rescue uses (manufacturer-minimum ordinary via, never a microvia,
never via-in-pad).  Nothing here relaxes a clearance, a layer rule, a process
limit or a budget.

The physical predicate (:func:`kelvin_access_candidate_clear`)
--------------------------------------------------------------
Every distance goes through the shared clearance kernel -- either through the
grid's commit-time validators (``validate_via_clearance`` /
``validate_segment_clearance`` / ``validate_via_to_via_clearance``, which also
apply authored per-net floors, Issue #6243) or directly through
:mod:`kicad_tools.router.clearance_shapes` (kernel shapes; a via barrel is
modelled on every copper layer).  It rejects a candidate that

* is a degenerate layer transition (same-layer via, or a landing outside the
  barrel's span);
* is not a manufacturable ordinary via for the active process (microvia, or
  drill / annular ring below the manufacturer minimum);
* comes within clearance of any physical pad (own-net SMT lands included --
  the via must be genuinely off-pad), committed foreign copper, foreign
  sibling escapes generated in the same pass, fixed fills, or the board edge;
* puts its drill too close to any other drilled hole (same-net vias included:
  same-net copper may merge, holes may not);
* would **merge the sense branch into another same-net branch** (committed
  copper, a sibling escape, or another terminal's pad of the same net) before
  the shunt.  Ordinary validators deliberately allow same-net merges; for a
  Kelvin net that merge would move the tap off the shunt, so it is refused
  here explicitly.

Committed recoveries are reported back to the caller, which adds their nets to
``Autorouter._kelvin_access_protected_nets``: both sibling rip-up variants
(``route_all`` and negotiated) exclude those nets, so a later higher-priority
net's rip-up cannot strip the recovered access.

Ordinary (non-Kelvin) nets never enter this code path.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .clearance_shapes import (
    KEdge,
    KShape,
    copper_gap,
    hole_gap,
    pad_shape,
    segment_shape,
    via_shape,
)
from .kelvin import detect_kelvin_topology
from .pad_geometry import pad_point_distance
from .primitives import Via

if TYPE_CHECKING:
    from .escape import EscapeRoute, EscapeRouter, PackageInfo
    from .primitives import Pad, Segment

EdgeSegments = Sequence[tuple[tuple[float, float], tuple[float, float]]]

#: Tolerance for "this endpoint is still on its pad" (mm).
_ON_PAD_EPS = 1e-6
#: Comparison slack for kernel gaps against a resolved requirement (mm).  The
#: kernel's own ``clear`` uses ``CLEARANCE_EPSILON_MM``; this module compares
#: ``copper_gap`` directly, so it uses a strictly tighter slack (never looser).
_GAP_EPS = 1e-6


def kelvin_net_ids(pads: Sequence[Pad]) -> frozenset[int]:
    """Net ids whose physical terminals form a recognised Kelvin topology."""
    by_net: dict[int, list[Pad]] = defaultdict(list)
    for pad in pads:
        if pad.net:
            by_net[pad.net].append(pad)
    found: set[int] = set()
    for net, terminals in by_net.items():
        if len(terminals) < 3:
            continue
        if detect_kelvin_topology(terminals) is not None:
            found.add(net)
    return frozenset(found)


def _full_stack_via(router: EscapeRouter, via: Via) -> Via:
    """The physical barrel of an ordinary via: every copper layer of the stack.

    An ordinary (non-micro) via is drilled through the whole board; KiCad
    sanitises a through via's layer pair to ``F.Cu``/``B.Cu`` on load whatever
    landing pair the router wrote.  Commit-time validators that honour
    ``via.layers`` must therefore see the full span, not just the landing.
    """
    grid = router.grid
    first = grid.index_to_layer(0)
    last = grid.index_to_layer(grid.num_layers - 1)
    from .layers import Layer

    return Via(
        x=via.x,
        y=via.y,
        drill=via.drill,
        diameter=via.diameter,
        layers=(Layer(first), Layer(last)),
        net=via.net,
        net_name=via.net_name,
        in_pad=via.in_pad,
        is_micro=via.is_micro,
    )


def _via_process_eligible(router: EscapeRouter, via: Via) -> bool:
    """An ordinary via the active process can build (never a microvia)."""
    if via.is_micro or via.in_pad:
        return False
    limits = getattr(router, "_mfr_limits", None)
    if limits is None:
        return via.drill > 0 and via.diameter > via.drill
    annular = (via.diameter - via.drill) / 2
    return bool(
        via.drill >= limits.min_via_drill - _GAP_EPS
        and annular >= limits.min_via_annular - _GAP_EPS
    )


def _layer_transition_ok(candidate: EscapeRoute, router: EscapeRouter) -> bool:
    """A real transition: pad layer and landing differ, both inside the barrel."""
    via = candidate.via
    if via is None:
        return False
    grid = router.grid
    try:
        span = sorted(grid.layer_to_index(layer.value) for layer in via.layers)
        pad_idx = grid.layer_to_index(candidate.pad.layer.value)
        landing_idx = grid.layer_to_index(candidate.escape_layer.value)
    except (KeyError, ValueError):
        return False
    lo, hi = span
    return (
        candidate.escape_layer != candidate.pad.layer
        and lo != hi
        and lo <= pad_idx <= hi
        and lo <= landing_idx <= hi
    )


def _sibling_copper(
    siblings: Sequence[EscapeRoute],
    candidate: EscapeRoute,
    replaced: EscapeRoute | None,
) -> tuple[list[Segment], list[Via], list[Segment], list[Via]]:
    """Split this pass's other escapes into (foreign segs/vias, same-net segs/vias).

    ``replaced`` -- the escape the candidate supersedes -- is excluded: its stub
    is not a separate branch.  Any other escape, even one on the same pad, is
    kept.
    """
    net = candidate.pad.net
    foreign_segs: list[Segment] = []
    foreign_vias: list[Via] = []
    same_segs: list[Segment] = []
    same_vias: list[Via] = []
    for sibling in siblings:
        if sibling is replaced or sibling is candidate:
            continue
        if sibling.pad.net == net:
            same_segs.extend(sibling.segments)
            if sibling.via is not None:
                same_vias.append(sibling.via)
        else:
            foreign_segs.extend(sibling.segments)
            if sibling.via is not None:
                foreign_vias.append(sibling.via)
    return foreign_segs, foreign_vias, same_segs, same_vias


def kelvin_access_candidate_clear(
    router: EscapeRouter,
    candidate: EscapeRoute,
    siblings: Sequence[EscapeRoute],
    pads: Sequence[Pad],
    clearance: float,
    edge_segments: EdgeSegments = (),
    edge_clearance: float = 0.0,
    replaced: EscapeRoute | None = None,
) -> bool:
    """Full physical + topological predicate for one inward-access candidate.

    Args:
        router: The escape router (its grid holds committed copper).
        candidate: The off-pad via escape under test.
        siblings: Every escape of this package generated so far (the
            candidate's own original escape included; it is skipped).
        pads: Every physical pad on the board (not just this package's).
        clearance: The resolved copper requirement for this package.
        edge_segments: Board-outline segments (cut-outs included).
        edge_clearance: Copper-to-edge requirement.
        replaced: The original escape the candidate would replace (excluded
            from ``siblings``).

    Returns:
        True only when every check passes.
    """
    via = candidate.via
    if via is None or not _layer_transition_ok(candidate, router):
        return False
    if not _via_process_eligible(router, via):
        return False
    grid = router.grid
    net = candidate.pad.net
    barrel = _full_stack_via(router, via)

    # Committed foreign copper, fixed fills and authored floors -- the same
    # commit-time validators every other committed via/segment goes through.
    if not grid.validate_via_clearance(barrel, net, clearance)[0]:
        return False
    if not grid.validate_via_to_via_clearance(barrel, net, clearance)[0]:
        return False
    for seg in candidate.segments:
        if not grid.validate_segment_clearance(seg, net, clearance)[0]:
            return False

    k_via = via_shape(via)  # kernel KVia: copper on every layer
    k_segs = [segment_shape(seg) for seg in candidate.segments]

    # Physical pads.  The via must be off every SMT land (own net included --
    # this is an ordinary via, not via-in-pad); an own-net through-hole pad may
    # be touched.  The stub may leave its own pad but must clear every other
    # land, own-net lands included (a merge there would bypass the shunt).
    for pad in pads:
        k_pad = pad_shape(pad)
        own = pad.net == net
        if not (own and pad.through_hole):
            if copper_gap(k_via, k_pad) < clearance - _GAP_EPS:
                return False
        if pad is candidate.pad:
            continue
        for k_seg in k_segs:
            if copper_gap(k_seg, k_pad) < clearance - _GAP_EPS:
                return False

    # Drilled holes stay distinct even on the same net.
    drill_gap = max(router.rules.min_drill_clearance, router.rules.min_hole_to_hole)
    committed_vias = [v for route in grid.routes for v in route.vias]
    foreign_segs, foreign_vias, same_segs, same_vias = _sibling_copper(
        siblings, candidate, replaced
    )
    for other in (*committed_vias, *foreign_vias, *same_vias):
        if hole_gap(k_via, via_shape(other)) < drill_gap - _GAP_EPS:
            return False
    for pad in pads:
        if pad.through_hole and pad.drill > 0:
            if hole_gap(k_via, pad_shape(pad)) < drill_gap - _GAP_EPS:
                return False

    # Foreign sibling escapes from this pass are not on the grid yet.
    for seg in foreign_segs:
        k_other: KShape = segment_shape(seg)
        if copper_gap(k_via, k_other) < clearance - _GAP_EPS:
            return False
        if any(copper_gap(k_seg, k_other) < clearance - _GAP_EPS for k_seg in k_segs):
            return False
    for other in foreign_vias:
        k_other = via_shape(other)
        if copper_gap(k_via, k_other) < clearance - _GAP_EPS:
            return False
        if any(copper_gap(k_seg, k_other) < clearance - _GAP_EPS for k_seg in k_segs):
            return False

    # Kelvin branch isolation: the sense access must not join another branch
    # of its own net (committed copper or a sibling escape) before the shunt.
    committed_same_segs = [s for route in grid.routes if route.net == net for s in route.segments]
    committed_same_vias = [v for route in grid.routes if route.net == net for v in route.vias]
    for seg in (*committed_same_segs, *same_segs):
        k_other = segment_shape(seg)
        if copper_gap(k_via, k_other) < clearance - _GAP_EPS:
            return False
        if any(copper_gap(k_seg, k_other) < clearance - _GAP_EPS for k_seg in k_segs):
            return False
    for other in (*committed_same_vias, *same_vias):
        k_other = via_shape(other)
        if copper_gap(k_via, k_other) < clearance - _GAP_EPS:
            return False
        if any(copper_gap(k_seg, k_other) < clearance - _GAP_EPS for k_seg in k_segs):
            return False

    # Board outline (cut-outs included) through the kernel's edge model.
    edge_margin = edge_clearance + float(getattr(edge_segments, "max_error_mm", 0.0))
    for (x1, y1), (x2, y2) in edge_segments:
        k_edge = KEdge(points=((x1, y1), (x2, y2)))
        if copper_gap(k_via, k_edge) < edge_margin - _GAP_EPS:
            return False
        if any(copper_gap(k_seg, k_edge) < edge_margin - _GAP_EPS for k_seg in k_segs):
            return False
    # Reject (never clamp) a candidate outside the routable bounds: clamping
    # would move geometry that was just validated.
    if router.board_bounds is not None:
        left, bottom, right, top = router.board_bounds
        edge = router.edge_clearance or 0.0
        points = [(via.x, via.y, via.diameter / 2)]
        for seg in candidate.segments:
            points.append((seg.x1, seg.y1, seg.width / 2))
            points.append((seg.x2, seg.y2, seg.width / 2))
        for x, y, radius in points:
            if not (
                left + edge + radius <= x <= right - edge - radius
                and bottom + edge + radius <= y <= top - edge - radius
            ):
                return False
    return True


def _trapped_on_pad(escape: EscapeRoute) -> bool:
    """The escape never left its land on its own layer (no via, endpoint on pad)."""
    pad = escape.pad
    return (
        escape.via is None
        and not pad.through_hole
        and escape.escape_layer == pad.layer
        and pad_point_distance(pad, *escape.escape_point) <= _ON_PAD_EPS
    )


def recover_kelvin_escapes(
    router: EscapeRouter,
    package: PackageInfo,
    escapes: list[EscapeRoute],
    pads: Sequence[Pad],
    *,
    edge_segments: EdgeSegments | None = None,
    edge_clearance: float = 0.0,
    kelvin_nets: frozenset[int] | None = None,
) -> list[EscapeRoute]:
    """Give a trapped Kelvin terminal a legal inward off-pad layer transition.

    Args:
        router: The package's escape router.
        package: The dense package whose escapes were just generated.
        escapes: Those escapes (not yet applied to the grid).
        pads: Every physical pad on the board -- Kelvin recognition needs the
            original terminals including the shunt root.
        edge_segments: Board-outline segments for the edge check.
        edge_clearance: Copper-to-edge requirement.
        kelvin_nets: Pre-computed :func:`kelvin_net_ids` (optional).

    Returns:
        A new list, element-wise identical to ``escapes`` except where a
        trapped Kelvin escape was replaced by a validated inward via escape.
        Order and length are preserved.
    """
    if kelvin_nets is None:
        kelvin_nets = kelvin_net_ids(pads)
    result = list(escapes)
    if not kelvin_nets:
        return result
    for index, escape in enumerate(result):
        pad = escape.pad
        if pad.net not in kelvin_nets or not _trapped_on_pad(escape):
            continue
        inward = router._OPPOSITE_DIRECTIONS.get(escape.direction)
        if inward is None:
            continue
        clearance = max(
            router.rules.trace_clearance,
            router.rules.via_clearance,
            router.rules.get_clearance_for_component(package.ref, pin_pitch=package.pin_pitch),
        )
        width = router._get_trace_width_for_net(pad.net_name)

        def candidate_clear(
            candidate: EscapeRoute,
            _clearance: float = clearance,
            _replaced: EscapeRoute = escape,
        ) -> bool:
            return kelvin_access_candidate_clear(
                router,
                candidate,
                result,
                pads,
                _clearance,
                edge_segments or (),
                edge_clearance,
                replaced=_replaced,
            )

        outward = router._try_lateral_via_escape(
            pad,
            escape.direction,
            clearance,
            width,
            package=package,
            existing_escapes=result,
            candidate_validator=candidate_clear,
        )
        if outward is not None:
            # Not trapped: a legal outward exit exists, and the general
            # router keeps responsibility for this terminal unchanged.
            continue
        candidate = router._try_lateral_via_escape(
            pad,
            inward,
            clearance,
            width,
            package=package,
            existing_escapes=result,
            candidate_validator=candidate_clear,
        )
        if candidate is not None:
            result[index] = candidate
    return result

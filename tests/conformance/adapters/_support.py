"""Shared plumbing every consumer adapter needs, and nothing else.

This module is the one place that translates a
:class:`~tests.conformance.generator.CopperCase` -- the harness's own,
consumer-free description of a piece of copper -- into the objects the
router and the validator speak (``router.primitives.Pad`` / ``Segment`` /
``Via``, ``router.rules.DesignRules``, ``router.layers.LayerStack``).  Keeping
it in one module means a consumer is driven with *identical* geometry by every
adapter, so a difference between two rows of the table is a difference between
two clearance models rather than between two translations.

Three conventions are load-bearing and are asserted by
``tests/conformance/test_corpus.py``:

**Rule values come from the case, never from a default.**
:class:`~tests.conformance.generator.CaseRules` carries the *router*-side
``trace_clearance`` / ``via_clearance`` / ``min_hole_to_hole`` /
``min_drill_clearance`` / ``grid_resolution` separately from the
``project_clearance`` written into the ``.kicad_pro``.  That separation is the
epic's thesis -- the router and kicad-cli can be applying different numbers --
so :func:`router_rules` copies the router-side values verbatim and never
"fixes" them towards the project value.

**Nets are ids 1..N in the case's own declaration order.**  The generator gives
every object its own net, and a :class:`~tests.conformance.adapters.Verdict`'s
identity is the *pair of net names*, so an adapter only has to map back through
:func:`net_ids`.

**One insertion order, named: via-first.**  #5398 is precisely the observation
that a commit gate's answer for a segment/via pair depends on which object was
committed first.  A table cell has to be one number, so every adapter here
pins the same order -- *the counterpart is existing copper, the candidate is
the object the router would be placing* -- and for a segment/via pair the via
is the existing one (see :func:`pair_contexts`).  The other order is not
measured; it is what the ``issue5398-seg-via-0p18-order`` fixture exists to
record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules
from tests.conformance.adapters import BOARD_EDGE
from tests.conformance.generator import (
    CopperCase,
    PadSpec,
    PairIntent,
    PairKind,
    SegmentSpec,
    ViaSpec,
    ZoneSpec,
)

__all__ = [
    "ALL_PAIR_KINDS",
    "BOARD_EDGE_REF",
    "BoardEdgeRef",
    "PairContext",
    "PairObject",
    "cpp_grid_for",
    "cpp_segment",
    "cpp_via",
    "layer_indexer",
    "layer_of",
    "layer_stack_for",
    "net_ids",
    "pad_pad_pairs",
    "pair_contexts",
    "router_cpp_module",
    "router_grid",
    "router_pad",
    "router_rules",
    "router_segment",
    "router_via",
    "single_object_route",
    "trace_radius_cells",
    "via_radius_cells",
]


@dataclass(frozen=True)
class BoardEdgeRef:
    """The board outline, standing in as a pair's *existing* side.

    ``copper-edge`` pairs have no second copper object -- the counterpart is
    the ``Edge.Cuts`` outline -- but :class:`PairContext` is a two-sided
    structure and every adapter's ``existing`` branch is written as an
    ``isinstance`` chain.  A sentinel keeps that shape intact and carries the
    same :data:`~tests.conformance.adapters.BOARD_EDGE` pseudo-net the oracle
    keys a one-sided ``copper_edge_clearance`` row with.
    """

    net: str = BOARD_EDGE


BOARD_EDGE_REF = BoardEdgeRef()
"""The singleton :class:`BoardEdgeRef` every ``copper-edge`` context carries."""


PairObject = SegmentSpec | ViaSpec | PadSpec | ZoneSpec | BoardEdgeRef

ALL_PAIR_KINDS: frozenset[str] = frozenset(PairKind.ROUTING)
"""Every pair kind that has a *routing candidate*; the default adapter scope.

Deliberately :attr:`~tests.conformance.generator.PairKind.ROUTING` and not
``PairKind.ALL``: ``pad-pad`` is a placement pair with no candidate object a
router could propose (see :func:`pair_contexts`), so an adapter that wraps a
router-side predicate is not consulted about it in production and must not be
scored on it.  The one consumer that *is* pad-pad-only -- group 19's
``drc_cpp`` incremental placement check -- names that kind explicitly.

The #5644 zone and edge kinds are excluded for the same reason: a consumer
with no zone-fill or board-outline term in its arithmetic would be scored on
a question it is never asked in production.  The two groups that *do* consult
those branches -- 18 (`kct_check`) and 10 (`mesh`) -- name the kinds
explicitly.
"""

_LAYER_BY_NAME: dict[str, Layer] = {layer.kicad_name: layer for layer in Layer}


def layer_of(name: str) -> Layer:
    """KiCad layer name -> the router's :class:`Layer` enum member."""
    try:
        return _LAYER_BY_NAME[name]
    except KeyError:  # pragma: no cover - the generator only emits known layers
        raise KeyError(f"unknown copper layer {name!r}") from None


def layer_stack_for(case: CopperCase) -> LayerStack:
    """The router stack matching the case's copper layer count."""
    if case.layers == 2:
        return LayerStack.two_layer()
    if case.layers == 4:
        return LayerStack.four_layer_all_signal()
    raise ValueError(f"unsupported copper layer count {case.layers}")  # pragma: no cover


def net_ids(case: CopperCase) -> dict[str, int]:
    """Map each declared net name onto a router net id (1-based).

    Zero is reserved by the router for "unassigned copper", which blocks every
    net, so ids start at 1.
    """
    return {name: index for index, name in enumerate(case.nets, start=1)}


def router_rules(case: CopperCase) -> DesignRules:
    """Router-side :class:`DesignRules` carrying the case's own values.

    Deliberately *not* derived from ``project_clearance``: the whole point of
    the measurement is that the router's numbers and the project's numbers can
    differ.  ``via_diameter`` / ``via_drill`` are set from the case's first via
    so envelope maths that consults the rule default (rather than the via
    object) sees a value consistent with the copper actually on the board.
    """
    rules = case.rules
    via_diameter = case.vias[0].diameter if case.vias else 0.6
    via_drill = case.vias[0].drill if case.vias else 0.3
    return DesignRules(
        trace_width=rules.min_trace_width,
        trace_clearance=rules.trace_clearance,
        via_clearance=rules.via_clearance,
        via_diameter=via_diameter,
        via_drill=via_drill,
        min_hole_to_hole=rules.min_hole_to_hole,
        min_drill_clearance=rules.min_drill_clearance,
        grid_resolution=rules.grid_resolution,
    )


def router_grid(case: CopperCase) -> RoutingGrid:
    """An empty :class:`RoutingGrid` in the case's own coordinate frame.

    Boards are written with ``center=False`` (see ``tests/conformance/board.py``)
    so the board origin is ``(0, 0)`` and copper coordinates are already
    grid-absolute; no offset is applied here.
    """
    return RoutingGrid(
        width=case.width,
        height=case.height,
        rules=router_rules(case),
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=layer_stack_for(case),
    )


def router_segment(spec: SegmentSpec, nets: dict[str, int]) -> Segment:
    """``SegmentSpec`` -> the router's :class:`Segment`."""
    return Segment(
        x1=spec.start[0],
        y1=spec.start[1],
        x2=spec.end[0],
        y2=spec.end[1],
        width=spec.width,
        layer=layer_of(spec.layer),
        net=nets[spec.net],
        net_name=spec.net,
    )


def router_via(spec: ViaSpec, nets: dict[str, int]) -> Via:
    """``ViaSpec`` -> the router's :class:`Via` (always a through via)."""
    return Via(
        x=spec.x,
        y=spec.y,
        drill=spec.drill,
        diameter=spec.diameter,
        layers=(layer_of(spec.layers[0]), layer_of(spec.layers[1])),
        net=nets[spec.net],
        net_name=spec.net,
    )


def router_pad(spec: PadSpec, nets: dict[str, int]) -> Pad:
    """``PadSpec`` -> the router's :class:`Pad`.

    ``width`` / ``height`` are the pad's *local-frame* dimensions and
    ``rotation`` is the residual board-space angle, which is exactly the
    convention :func:`kicad_tools.router.primitives.pad_half_extents`
    documents (issue #4910): the probe footprints are never axis-swapped by an
    ``io.py`` parser here, so the full board angle is the residual.  ``shape``
    is carried verbatim so a consumer that models a circle as a disc and a
    roundrect as its bounding rectangle does exactly that -- the
    ``roundrect-corner-gap`` fixture depends on it.
    """
    shape = spec.shape
    return Pad(
        x=spec.x,
        y=spec.y,
        width=shape.size[0],
        height=shape.size[1],
        net=nets[spec.net],
        net_name=spec.net,
        layer=layer_of(spec.layer),
        ref=spec.reference,
        pin="1",
        through_hole=False,
        rotation=spec.rotation,
        shape=shape.shape,
    )


def single_object_route(obj: SegmentSpec | ViaSpec, nets: dict[str, int]) -> Route:
    """Wrap one segment or via in a :class:`Route` so it can be marked."""
    net_id = nets[obj.net]
    route = Route(net=net_id, net_name=obj.net)
    if isinstance(obj, SegmentSpec):
        route.segments.append(router_segment(obj, nets))
    else:
        route.vias.append(router_via(obj, nets))
    return route


@dataclass(frozen=True)
class PairContext:
    """One close pair, split into *existing copper* and *candidate*.

    Attributes:
        pair: The generator's record of what it placed.
        existing: The object treated as already-committed copper -- a segment,
            via or pad, or (for the #5644 kinds) the case's
            :class:`~tests.conformance.generator.ZoneSpec` pour or the
            :data:`BOARD_EDGE_REF` outline sentinel.
        candidate: The object the consumer is asked about.  Never a pad: a
            pad is placement output, not something a router proposes.
    """

    pair: PairIntent
    existing: PairObject
    candidate: SegmentSpec | ViaSpec

    @property
    def kind(self) -> str:
        return self.pair.kind

    @property
    def nets(self) -> tuple[str, str]:
        return (self.pair.net_a, self.pair.net_b)


def _objects_by_net(case: CopperCase) -> dict[str, PairObject]:
    return {obj.net: obj for obj in case.copper_objects}


def pad_pad_pairs(case: CopperCase) -> list[tuple[PairIntent, PadSpec, PadSpec]]:
    """Every ``pad-pad`` pair, as ``(intent, pad_a, pad_b)``.

    The complement of :func:`pair_contexts`: these pairs have no routing
    candidate (both sides are placement copper), so they are served separately
    for the one consumer that answers a pad-vs-pad question -- Epic #5509
    group 19's incremental placement DRC.
    """
    by_net = _objects_by_net(case)
    pairs: list[tuple[PairIntent, PadSpec, PadSpec]] = []
    for pair in case.pairs:
        if pair.kind != PairKind.PAD_PAD:
            continue
        first, second = by_net[pair.net_a], by_net[pair.net_b]
        assert isinstance(first, PadSpec) and isinstance(second, PadSpec)
        pairs.append((pair, first, second))
    return pairs


def _one_sided_context(
    case: CopperCase,
    pair: PairIntent,
    by_net: dict[str, PairObject],
) -> PairContext:
    """The :class:`PairContext` for a ``seg-zone`` / ``via-zone`` / ``copper-edge`` pair.

    Exactly one side is a copper object the case declares; the other is the
    pour or the outline.  Which *slot* of the intent holds the copper net is
    not assumed -- it is looked up -- so a future placer that records the pair
    the other way round cannot silently produce a context with the two sides
    swapped.
    """
    copper = by_net.get(pair.net_a) or by_net.get(pair.net_b)
    assert copper is not None, f"{pair.kind} pair {sorted(pair.nets)} declares no copper object"
    assert isinstance(copper, SegmentSpec | ViaSpec), "a pad is never a routing candidate"
    if pair.kind in PairKind.EDGE:
        return PairContext(pair=pair, existing=BOARD_EDGE_REF, candidate=copper)
    assert case.zone is not None, f"{pair.kind} pair on a case with no pour"
    return PairContext(pair=pair, existing=case.zone, candidate=copper)


def pair_contexts(case: CopperCase) -> list[PairContext]:
    """Split every close pair into (existing copper, candidate).

    ``pad-pad`` pairs are **skipped** rather than split: neither side is an
    object a router proposes, so there is no candidate to ask about.  They are
    served by :func:`pad_pad_pairs` instead.  Every adapter filters on
    ``pair_kinds`` anyway, so the skip is invisible to consumers that never
    declare that kind.

    The split rule, in order:

    0. **A zone or edge pair has exactly one copper object**, and that object
       is always the candidate: the pour and the board outline are both
       pre-existing board features a router routes *around* (#5644).  Their
       ``existing`` side is the case's ``ZoneSpec`` or :data:`BOARD_EDGE_REF`.
    1. **A pad is always existing copper.**  Placement puts pads down before
       routing starts; no consumer here is ever asked "may I add this pad?".
    2. **A segment/via pair commits the via first.**  This is the *named*
       insertion order this harness measures (see the module docstring); it is
       the order under which #5398's 0.18 mm pair is accepted, because the
       candidate segment is then compared against ``trace_clearance`` rather
       than the wider ``via_clearance``.
    3. **Otherwise the pair's first-declared object is existing copper** and
       the second is the candidate.

    Every object carries its own net, so the lookup is unambiguous.
    """
    by_net = _objects_by_net(case)
    contexts: list[PairContext] = []
    for pair in case.pairs:
        if pair.kind == PairKind.PAD_PAD:
            continue
        if pair.kind in PairKind.ZONE or pair.kind in PairKind.EDGE:
            contexts.append(_one_sided_context(case, pair, by_net))
            continue
        first, second = by_net[pair.net_a], by_net[pair.net_b]
        if isinstance(first, PadSpec):
            existing, candidate = first, second
        elif isinstance(second, PadSpec):
            existing, candidate = second, first
        elif isinstance(first, SegmentSpec) and isinstance(second, ViaSpec):
            existing, candidate = second, first  # via-first
        elif isinstance(first, ViaSpec) and isinstance(second, SegmentSpec):
            existing, candidate = first, second  # via-first
        else:
            existing, candidate = first, second
        assert not isinstance(candidate, PadSpec), "a pad is never a routing candidate"
        contexts.append(PairContext(pair=pair, existing=existing, candidate=candidate))
    return contexts


# ---------------------------------------------------------------------------
# C++ (``router_cpp``) plumbing
# ---------------------------------------------------------------------------
#
# Three Phase 1c adapters -- groups 2, 3 and 5 -- drive ``router_cpp.Grid3D``
# and ``router_cpp.Pathfinder`` directly, the way
# ``tests/router/test_pairwise_cpp_parity.py`` does.  The translation lives
# here for the same reason the Python one does: a difference between two rows
# of the table must be a difference between two clearance models, never
# between two translations of the same case.


def router_cpp_module():
    """The compiled router extension, or ``None`` when it is not built.

    An adapter reports ``available() is False`` on ``None`` so its group
    renders ``not measured`` rather than a zero-disagreement row -- the same
    "no answer beats a confident wrong answer" rule the oracle applies to a
    missing kicad-cli.
    """
    try:
        from kicad_tools.router import router_cpp
    except ImportError:  # pragma: no cover - depends on the build environment
        return None
    return router_cpp


def layer_indexer(case: CopperCase):
    """KiCad layer name -> the 0-based stack index ``Grid3D`` speaks."""
    stack = layer_stack_for(case)
    by_enum = {layer_of(definition.name): definition.index for definition in stack.layers}

    def index(name: str) -> int:
        return by_enum[layer_of(name)]

    return index


def cpp_grid_for(router_cpp, case: CopperCase, rules: DesignRules):
    """A bare ``Grid3D`` in the case's own coordinate frame.

    Boards are written with ``center=False`` (``tests/conformance/board.py``),
    so the board origin is ``(0, 0)`` and the grid needs no offset.  Column /
    row counts match ``grid_cpp.py``'s construction exactly so the two C++
    rows share one raster.
    """
    resolution = rules.grid_resolution
    cols = int(case.width / resolution) + 1
    rows = int(case.height / resolution) + 1
    return router_cpp.Grid3D(cols, rows, case.layers, resolution, 0.0, 0.0)


def cpp_segment(router_cpp, spec: SegmentSpec, nets: dict[str, int], layer_index):
    """``SegmentSpec`` -> ``router_cpp.Segment``."""
    seg = router_segment(spec, nets)
    out = router_cpp.Segment()
    out.x1, out.y1 = seg.x1, seg.y1
    out.x2, out.y2 = seg.x2, seg.y2
    out.width = seg.width
    out.layer = layer_index(spec.layer)
    out.net = seg.net
    return out


def cpp_via(router_cpp, spec: ViaSpec, nets: dict[str, int], layer_index):
    """``ViaSpec`` -> ``router_cpp.Via``."""
    via = router_via(spec, nets)
    out = router_cpp.Via()
    out.x, out.y = via.x, via.y
    out.drill = via.drill
    out.diameter = via.diameter
    out.layer_from = layer_index(spec.layers[0])
    out.layer_to = layer_index(spec.layers[1])
    out.net = via.net
    return out


def trace_radius_cells(rules: DesignRules) -> int:
    """The trace halo radius, in cells, that the C++ search marks with.

    Verbatim from the production call site --
    ``cpp_backend.py CppPathfinder.route``, which computes
    ``max(1, ceil((trace_width / 2 + trace_clearance) / resolution))`` and
    hands it to ``Grid3D::mark_segment`` as ``clearance_cells``.  Re-deriving
    it here is unavoidable (the radius is computed in Python and the marking
    happens in C++), which is exactly why it is written once, in one place,
    and cited.
    """
    return max(
        1, math.ceil((rules.trace_width / 2 + rules.trace_clearance) / rules.grid_resolution)
    )


def via_radius_cells(rules: DesignRules) -> int:
    """The via halo radius, in cells, that the C++ search marks with.

    The ``mark_via`` sibling of :func:`trace_radius_cells`;
    ``max(1, ceil((via_diameter / 2 + via_clearance) / resolution))``.
    """
    return max(1, math.ceil((rules.via_diameter / 2 + rules.via_clearance) / rules.grid_resolution))

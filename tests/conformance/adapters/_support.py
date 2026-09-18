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

from dataclasses import dataclass

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules
from tests.conformance.generator import (
    CopperCase,
    PadSpec,
    PairIntent,
    PairKind,
    SegmentSpec,
    ViaSpec,
)

__all__ = [
    "ALL_PAIR_KINDS",
    "PairContext",
    "PairObject",
    "layer_of",
    "layer_stack_for",
    "net_ids",
    "pair_contexts",
    "router_grid",
    "router_pad",
    "router_rules",
    "router_segment",
    "router_via",
    "single_object_route",
]

PairObject = SegmentSpec | ViaSpec | PadSpec

ALL_PAIR_KINDS: frozenset[str] = frozenset(PairKind.ALL)
"""Every pair kind the generator places; the default adapter scope."""

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
        existing: The object treated as already-committed copper.
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


def pair_contexts(case: CopperCase) -> list[PairContext]:
    """Split every close pair into (existing copper, candidate).

    The split rule, in order:

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

"""Issue #6502: router barrel readers use ``Via.drilled_layers``.

``Via.layers`` is the LOGICAL layer transition a producer recorded.  A
non-micro router via is a mechanically drilled through via whatever that pair
says (``Via.drilled_layers``, issue #6485), so every reader deciding
clearance, connectivity or drilled length must see the barrel on every copper
layer.  A micro via keeps its logical span everywhere.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.diffpair_routing import _via_stack_span
from kicad_tools.router.drc_nudge import _via_spans_segment_layer
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerDefinition, LayerStack, LayerType
from kicad_tools.router.optimizer.collision import _via_spans_layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, DesignRules
from kicad_tools.router.via_clearance import (
    segment_clears_foreign_via,
    segment_via_deficit,
    via_clears_foreign_segment,
)


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _grid(stack: LayerStack) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=_rules(),
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=stack,
    )


def _scrambled_stack() -> LayerStack:
    """Hand-built stack whose last grid index is not B.Cu (the Judge's case)."""
    return LayerStack(
        name="scrambled",
        layers=[
            LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True),
            LayerDefinition("B.Cu", 1, LayerType.SIGNAL, is_outer=True),
            LayerDefinition("In1.Cu", 2, LayerType.SIGNAL),
        ],
    )


def _via(*, micro: bool = False, layers=(Layer.F_CU, Layer.IN1_CU), net: int = 2) -> Via:
    return Via(x=5.0, y=5.0, drill=0.3, diameter=0.6, layers=layers, net=net, is_micro=micro)


def _seg(layer: Layer, net: int = 1) -> Segment:
    """A foreign segment running straight through the via centre."""
    return Segment(x1=0.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=layer, net=net)


COPPER_4L = (Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU)


# ---------------------------------------------------------------------------
# via_clearance (duck-typed, physical stack order)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", COPPER_4L)
def test_via_clearance_sees_partial_pair_barrel_on_every_layer(layer: Layer) -> None:
    via = _via()
    seg = _seg(layer)
    assert segment_via_deficit(seg, via, 0.15) > 0
    assert not segment_clears_foreign_via(seg, via, 0.15)
    assert not via_clears_foreign_segment(via, seg, 0.15)


@pytest.mark.parametrize("layer", (Layer.F_CU, Layer.IN1_CU, Layer.B_CU))
def test_via_clearance_scrambled_stack_reports_every_copper_layer(layer: Layer) -> None:
    """Grid stack ``[F.Cu, B.Cu, In1.Cu]``: logical pair ``(F.Cu, In1.Cu)``."""
    grid = _grid(_scrambled_stack())
    first, last = grid.index_to_layer(0), grid.index_to_layer(len(grid.layer_stack.layers) - 1)
    via = _via(layers=(Layer(first), Layer(last)))
    assert via.layers == (Layer.F_CU, Layer.IN1_CU)
    seg = _seg(layer)
    assert not via_clears_foreign_segment(via, seg, 0.15)
    assert not segment_clears_foreign_via(seg, via, 0.15)


def test_via_clearance_duck_typed_via_keeps_its_layers() -> None:
    """A board-side via without ``drilled_layers`` uses ``layers`` as the barrel."""

    class _BoardVia:
        x = 5.0
        y = 5.0
        diameter = 0.6
        layers = (Layer.F_CU, Layer.IN1_CU)

    assert segment_clears_foreign_via(_seg(Layer.B_CU), _BoardVia(), 0.15)
    assert not segment_clears_foreign_via(_seg(Layer.IN1_CU), _BoardVia(), 0.15)


# ---------------------------------------------------------------------------
# negotiated: vias_on_layer index (both hooks) and tree-contact cells
# ---------------------------------------------------------------------------


def _neg(grid: RoutingGrid) -> NegotiatedRouter:
    rules = _rules()
    return NegotiatedRouter(grid, Router(grid, rules), rules, DEFAULT_NET_CLASS_MAP)


def _net_routes(via: Via, seg_layer: Layer) -> dict[int, list[Route]]:
    stub = Segment(x1=5.0, y1=5.0, x2=5.5, y2=5.0, width=0.2, layer=Layer.F_CU, net=2)
    return {
        1: [Route(net=1, net_name="A", segments=[_seg(seg_layer)], vias=[])],
        2: [Route(net=2, net_name="B", segments=[stub], vias=[via])],
    }


@pytest.mark.parametrize(
    ("micro", "expected"),
    [(False, True), (True, False)],
)
def test_negotiated_vias_on_layer_uses_drilled_span(micro: bool, expected: bool) -> None:
    grid = _grid(LayerStack.four_layer_all_signal())
    neg = _neg(grid)
    routes = _net_routes(_via(micro=micro), Layer.B_CU)
    seg_via = neg.find_nets_with_segment_via_violations(routes, trace_clearance=0.15)
    via_seg = neg.find_nets_with_via_segment_violations(routes, trace_clearance=0.15)
    assert (1 in seg_via) is expected
    assert (2 in via_seg) is expected


@pytest.mark.parametrize(("micro", "spans_b"), [(False, True), (True, False)])
def test_negotiated_tree_contact_cells_use_drilled_span(micro: bool, spans_b: bool) -> None:
    grid = _grid(LayerStack.four_layer_all_signal())
    neg = _neg(grid)
    cells: set[tuple[int, int, int]] = set()
    neg._collect_route_cells(Route(net=2, net_name="B", vias=[_via(micro=micro)]), cells)
    gx, gy = grid.world_to_grid(5.0, 5.0)
    b_idx = grid.layer_to_index(Layer.B_CU.value)
    in1_idx = grid.layer_to_index(Layer.IN1_CU.value)
    assert (gx, gy, in1_idx) in cells
    assert ((gx, gy, b_idx) in cells) is spans_b


# ---------------------------------------------------------------------------
# drc_nudge, optimizer collision, diff-pair length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("micro", "spans_b"), [(False, True), (True, False)])
def test_drc_nudge_via_spans_segment_layer(micro: bool, spans_b: bool) -> None:
    via = _via(micro=micro)
    assert _via_spans_segment_layer(via, _seg(Layer.IN1_CU))
    assert _via_spans_segment_layer(via, _seg(Layer.B_CU)) is spans_b


@pytest.mark.parametrize(("micro", "spans_b"), [(False, True), (True, False)])
def test_optimizer_collision_via_spans_layer(micro: bool, spans_b: bool) -> None:
    grid = _grid(LayerStack.four_layer_all_signal())
    via = _via(micro=micro)
    assert _via_spans_layer(grid, via, grid.layer_to_index(Layer.IN1_CU.value))
    assert _via_spans_layer(grid, via, grid.layer_to_index(Layer.B_CU.value)) is spans_b


@pytest.mark.parametrize(("micro", "span"), [(False, 3), (True, 1)])
def test_via_stack_span_uses_drilled_span(micro: bool, span: int) -> None:
    assert _via_stack_span(_via(micro=micro), 4) == span


@pytest.mark.parametrize(("micro", "length"), [(False, 1.58), (True, 0.5325)])
def test_via_length_uses_drilled_span_when_blind_buried_supported(
    micro: bool, length: float
) -> None:
    got = DiffPairLengthTracker._via_length(_via(micro=micro), 1.6, 4, blind_buried_supported=True)
    assert got == pytest.approx(length)


def test_logical_pair_already_outer_is_unchanged() -> None:
    """A via recorded on F.Cu/B.Cu (either order) behaves exactly as before."""
    via = _via(layers=(Layer.B_CU, Layer.F_CU))
    assert via.drilled_layers == (Layer.B_CU, Layer.F_CU)
    assert _via_stack_span(via, 4) == 3
    for layer in COPPER_4L:
        assert _via_spans_segment_layer(via, _seg(layer))
        assert not segment_clears_foreign_via(_seg(layer), via, 0.15)

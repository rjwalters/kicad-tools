"""Retune measures copper in board-file order, as the checker does (Issue #7790).

On board 07 the routed file's copper is not in canonical UUID order, so the
canonical-order length model read DQS_P 0.074 mm short of the checker: the
tuner saw skew 0.045 mm (<= 0.05) and left a checker skew of 0.1194 mm alone.
"""

from __future__ import annotations

from kicad_tools.router.kicad_length import KicadLengthModel
from kicad_tools.router.layers import Layer
from kicad_tools.router.pcb_retune import _file_order_fn
from kicad_tools.router.primitives import Route, Segment


def _seg(x1: float, x2: float) -> Segment:
    return Segment(x1, 0.0, x2, 0.0, 0.2, Layer.F_CU, net=1, net_name="N")


def _block(start: int, seg: Segment) -> tuple:
    key = ("seg", (seg.x1, seg.y1), (seg.x2, seg.y2), "F.Cu", 0.2)
    return ("segment", start, start + 10, "N", key)


def test_existing_copper_keeps_file_order_and_new_copper_follows():
    a, b, c = _seg(0, 1), _seg(1, 2), _seg(2, 3)
    new = _seg(5, 6)
    # File holds c, a, b in that order (not canonical order).
    blocks = [_block(10, c), _block(20, a), _block(30, b)]
    order = _file_order_fn(blocks)
    route = Route(net=1, net_name="N", segments=[new, a, b, c], vias=[])
    assert order(route).segments == [c, a, b, new]


def test_model_applies_copper_order_hook():
    seen: list[Route] = []

    def hook(route: Route) -> Route:
        seen.append(route)
        return route

    route = Route(net=1, net_name="N", segments=[_seg(0, 1)], vias=[])
    model = KicadLengthModel.default(1.6, 2)
    plain = model.route_length(route)
    model.copper_order = hook
    assert model.route_length(route) == plain
    assert seen == [route]

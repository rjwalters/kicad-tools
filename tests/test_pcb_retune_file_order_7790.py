"""Retune measures copper in board-file order, as the checker does (Issue #7790).

On board 07 the routed file's copper is not in canonical UUID order, so the
canonical-order length model read DQS_P 0.074 mm short of the checker: the
tuner saw skew 0.045 mm (<= 0.05) and left a checker skew of 0.1194 mm alone.
"""

from __future__ import annotations

import pytest

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


def test_splice_anchors_after_last_block_when_vias_precede_segments():
    """New copper lands after the net's last block by offset, not the last via (#7796)."""
    from kicad_tools.router.pcb_retune import _splice
    from kicad_tools.router.primitives import Via

    text = (
        "(kicad_pcb\n"
        '\t(net 1 "N")\n'
        '\t(via (at 5 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        '\t(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        '\t(arc (start 1 0) (mid 2 1) (end 3 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        ")\n"
    )
    old = Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1, net_name="N")
    via = Via(5, 0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="N")
    new = _seg(7, 8)
    route = Route(net=1, net_name="N", segments=[old, new], vias=[via])
    out = _splice(text, {"N": route}, {"N": 1}, {1: "N"}, False)
    assert out.index("(start 7.0000") > out.index("(arc")


def test_tuner_length_matches_checker_after_splice_when_via_precedes_segments(tmp_path):
    """Tuner and checker lengths agree on the written file (#7796, AC2).

    The via is listed before the segments and trims junction ends, so the
    measured length depends on where the new segment lands in the file.  Anchoring
    on the via would write it ahead of the existing segments while the tuner
    assumes it is appended after them.
    """
    from kicad_tools.router.pcb_retune import _block_keys, _file_order_fn, _splice
    from kicad_tools.router.primitives import Via
    from kicad_tools.schema.pcb import PCB

    def seg_text(x1, y1, x2, y2, layer="F.Cu"):
        return (
            f'\t(segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "{layer}") (net 1))\n'
        )

    text = (
        '(kicad_pcb (version 20240108) (generator "t") (general (thickness 1.6))\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '\t(net 0 "") (net 1 "N")\n'
        '\t(via (at 5.4 0.3) (size 1.2) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        + seg_text(0, 0, 5, 0)
        + seg_text(5, 0, 10, 0)
        + seg_text(5, 0, 5, 5)
        + seg_text(5.4, 0.3, 9, 0.3, "B.Cu")
        + ")\n"
    )
    mk = lambda *p, ly=Layer.F_CU: Segment(*p, 0.2, ly, net=1, net_name="N")  # noqa: E731
    # The B.Cu stub makes the via a two-layer connection, so the tuner's
    # partial-net via charge matches the checker's (the test isolates order).
    segs = [mk(0, 0, 5, 0), mk(5, 0, 10, 0), mk(5, 0, 5, 5), mk(5.4, 0.3, 9, 0.3, ly=Layer.B_CU)]
    new = mk(5, 0, 7, 3)
    via = Via(5.4, 0.3, 0.3, 1.2, (Layer.F_CU, Layer.B_CU), net=1, net_name="N")
    route = Route(net=1, net_name="N", segments=[*segs, new], vias=[via])

    out = _splice(text, {"N": route}, {"N": 1}, {1: "N"}, False)
    path = tmp_path / "b.kicad_pcb"
    path.write_text(out)
    pcb = PCB.load(str(path))
    checker = KicadLengthModel.from_pcb(pcb).net_length_pcb(pcb, 1)

    model = KicadLengthModel.default(1.6, 2)
    model.copper_order = _file_order_fn(_block_keys(text, {"N": 1}, {1: "N"}))
    tuner = model.route_length(route)
    assert tuner == pytest.approx(checker, abs=1e-4)

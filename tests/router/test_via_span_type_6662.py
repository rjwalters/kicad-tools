"""Issue #6662: imported KiCad blind/buried vias keep their drilled span.

KiCad drills a ``(via blind ...)`` / ``(via buried ...)`` only through its
declared ``layers`` pair (#6485, kicad-cli 10.0.6), while a keyword-less via
is a through via whatever its pair says.  Before #6662 the router ``Via``
model had no way to carry the blind/buried keyword, so an imported buried
In1/In2 via read as a full F.Cu..B.Cu barrel in :attr:`Via.drilled_layers`
and ``--preserve-existing`` re-wrote it as a through via.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cache import (
    RoutingCache,
    normalize_routes_to_origin,
    transform_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import parse_vias
from kicad_tools.router.primitives import Route, Via

BOARD = """(kicad_pcb (net 0 "") (net 1 "A")
(via buried (at 1 2) (size 0.6) (drill 0.3) (layers "In1.Cu" "In2.Cu") (uuid "a") (net 1))
(via blind (at 3 2) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (uuid "b") (net 1))
(via (at 5 2) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (uuid "c") (net 1))
(via micro (at 7 2) (size 0.3) (drill 0.1) (layers "F.Cu" "In1.Cu") (uuid "d") (net 1))
)"""


def _by_x() -> dict[float, Via]:
    return {via.x: via for via in parse_vias(BOARD)["A"]}


def test_parse_vias_keeps_blind_and_buried_span():
    vias = _by_x()
    assert vias[1.0].span_type == "buried"
    assert vias[1.0].drilled_layers == (Layer.IN1_CU, Layer.IN2_CU)
    assert vias[3.0].span_type == "blind"
    assert vias[3.0].drilled_layers == (Layer.F_CU, Layer.IN1_CU)
    # Keyword-less partial pair: KiCad drills it through (#6485/#6493).
    assert vias[5.0].span_type is None
    assert vias[5.0].drilled_layers == (Layer.F_CU, Layer.B_CU)
    assert vias[7.0].is_micro and vias[7.0].span_type is None
    assert vias[7.0].drilled_layers == (Layer.F_CU, Layer.IN1_CU)


@pytest.mark.parametrize(
    ("x", "head", "layers"),
    [
        (1.0, "(via buried", '(layers "In1.Cu" "In2.Cu")'),
        (3.0, "(via blind", '(layers "F.Cu" "In1.Cu")'),
        (5.0, "(via\n", '(layers "F.Cu" "B.Cu")'),
        (7.0, "(via micro", '(layers "F.Cu" "In1.Cu")'),
    ],
)
def test_to_sexp_round_trips_the_kicad_via_type(x, head, layers):
    sexp = _by_x()[x].to_sexp()
    assert sexp.startswith(head)
    assert layers in sexp
    # Re-reading the written node yields the same model.
    (reread,) = parse_vias(f'(kicad_pcb (net 1 "A")\n{sexp}\n)')["A"]
    assert reread.span_type == _by_x()[x].span_type
    assert reread.drilled_layers == _by_x()[x].drilled_layers


def test_span_type_rejects_unknown_keyword():
    with pytest.raises(ValueError, match="span_type"):
        Via(x=0, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), span_type="micro")


def test_cache_paths_preserve_span_type(tmp_path):
    via = _by_x()[1.0]
    routes = [Route(net=1, net_name="A", vias=[via])]
    normalized = normalize_routes_to_origin(routes, 0.0, 0.0, 0.0)
    assert normalized[0].vias[0].span_type == "buried"
    transformed = transform_routes(normalized, 0.0, 0.0, 0.0, 1, "A")
    assert transformed[0].vias[0].span_type == "buried"
    cache = RoutingCache(cache_dir=tmp_path)
    restored = cache.deserialize_routes(cache.serialize_routes(routes))
    assert restored[0].vias[0].span_type == "buried"
    assert restored[0].vias[0].drilled_layers == (Layer.IN1_CU, Layer.IN2_CU)

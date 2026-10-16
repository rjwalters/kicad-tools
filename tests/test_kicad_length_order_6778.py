"""Route-form length == routed-board length at a 3+-way junction (Issue #6778).

kicad-cli resolves a junction of three or more track ends in item order, so
the same copper can measure differently depending on the order its tracks are
listed.  The fixture is the junction the #6725 bench found on 06rf
(``PCIE_TX-``, lattice, seed 42): an F.Cu entry and a B.Cu exit both pass
through (74.4, 46.4), inside the 0.6 mm via at (74.2, 46.2) -- four track ends
at one point, worth one 0.2 x 0.2 diagonal stub (0.2828 mm) either way.

The tuners measure a router ``Route``; ``kct check`` measures the routed file,
whose copper order ``kct route`` sets when it canonicalizes UUIDs (#6052):
copper is put in UUID order, the order KiCad's saves keep.  These tests pin
that both sides use one order: the Route form sorts by ``segment_order_key``
whatever ``Route.segments`` order is, and the canonicalizer hands each net its
UUIDs in that same key order, whatever order and UUIDs the router wrote.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from kicad_tools.core.canonical_uuids import canonicalize_pcb_file_uuids
from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.kicad_length import KicadLengthModel, LSeg, LVia, net_length
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroupTracker
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.schema.pcb import PCB

STUB = math.hypot(0.2, 0.2)

# (x1, y1, x2, y2, layer) in path order: F.Cu in, up the via, B.Cu out.
_PATH = [
    (72.0, 46.4, 74.4, 46.4, Layer.F_CU),
    (74.4, 46.4, 74.2, 46.2, Layer.F_CU),
    (74.2, 46.2, 74.4, 46.4, Layer.B_CU),
    (74.4, 46.4, 77.0, 46.4, Layer.B_CU),
]
_VIA = (74.2, 46.2)

#: Orders of the four segments, covering both junction resolutions.
_ORDERS = [[0, 1, 2, 3], [3, 2, 1, 0], [2, 3, 0, 1], [1, 0, 3, 2], [0, 2, 1, 3]]

_EDGE_UUID = "41111111-1111-1111-1111-111111111111"


def _route(order: list[int]) -> Route:
    segs = [
        Segment(x1, y1, x2, y2, 0.2, layer, net=1, net_name="N1")
        for x1, y1, x2, y2, layer in (_PATH[i] for i in order)
    ]
    via = Via(*_VIA, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="N1")
    return Route(net=1, net_name="N1", segments=segs, vias=[via])


def _board(copper: str) -> str:
    return f"""(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "") (net 1 "N1")
  {copper}
  (gr_rect (start 60 40) (end 90 60) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "{_EDGE_UUID}"))
)
"""


def _model() -> KicadLengthModel:
    return KicadLengthModel.default(1.6, 2)


def _routed(tmp_path: Path, copper: str) -> PCB:
    """``copper`` written to a board and canonicalized as ``kct route`` does."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board(copper))
    canonicalize_pcb_file_uuids(path, keep={_EDGE_UUID})
    return PCB.load(str(path))


def _copper_items(copper: str) -> list[str]:
    return re.findall(r"\((?:segment|via)\b.*?\n\t\)", copper, re.S)


class TestJunctionIsOrderDependent:
    def test_fixture_reproduces_the_bench_stub(self):
        # The premise: the neutral measure itself depends on input order here.
        via = LVia(*_VIA, 0.6, ("F.Cu", "B.Cu"))
        lengths = set()
        for order in _ORDERS:
            segs = [
                LSeg(x1, y1, x2, y2, layer.kicad_name)
                for x1, y1, x2, y2, layer in (_PATH[i] for i in order)
            ]
            lengths.add(round(net_length(segs, [via], [], None), 6))
        assert len(lengths) == 2
        assert max(lengths) - min(lengths) == pytest.approx(STUB, abs=1e-6)


class TestRouteFormIsOrderFree:
    def test_route_length_ignores_route_segment_order(self):
        model = _model()
        lengths = {round(model.route_length(_route(order)), 9) for order in _ORDERS}
        assert len(lengths) == 1

    def test_pair_and_group_tuner_measures_ignore_route_segment_order(self):
        model = _model()
        pair = {
            round(DiffPairLengthTracker._measure_route(_route(o), None, 2, length_model=model), 9)
            for o in _ORDERS
        }
        group = {
            round(MatchGroupTracker._measure_route_total(_route(o), None, 2, length_model=model), 9)
            for o in _ORDERS
        }
        assert len(pair) == 1
        assert pair == group


class TestRouteFormEqualsRoutedBoard:
    @pytest.mark.parametrize("order", _ORDERS)
    def test_route_length_equals_kct_check_length(self, tmp_path, order):
        # Whatever order the router held the segments in, and whatever random
        # UUIDs it wrote, the canonicalized file reads what the tuner measured.
        route = _route(order)
        model = _model()
        pcb = _routed(tmp_path, route.to_sexp())
        assert model.net_length_pcb(pcb, 1) == pytest.approx(model.route_length(route), abs=1e-9)

    @pytest.mark.parametrize("order", _ORDERS)
    def test_written_text_order_does_not_matter(self, tmp_path, order):
        items = _copper_items(_route([0, 1, 2, 3]).to_sexp())
        shuffled = [items[i] for i in order] + items[len(_PATH) :]
        model = _model()
        pcb = _routed(tmp_path, "\n\t".join(shuffled))
        assert model.net_length_pcb(pcb, 1) == pytest.approx(
            model.route_length(_route([0, 1, 2, 3])), abs=1e-9
        )

    def test_canonical_file_is_in_uuid_order(self, tmp_path):
        # So a KiCad save (tracks sorted by UUID) keeps the order.
        _routed(tmp_path, _route([3, 2, 1, 0]).to_sexp())
        text = (tmp_path / "board.kicad_pcb").read_text()
        uuids = re.findall(r'\(segment\b.*?\(uuid "([^"]+)"\)', text, re.S)
        assert len(uuids) == len(_PATH)
        assert uuids == sorted(uuids)

    def test_canonicalization_stays_idempotent(self, tmp_path):
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_board(_route([3, 2, 1, 0]).to_sexp()))
        assert canonicalize_pcb_file_uuids(path, keep={_EDGE_UUID}) > 0
        assert canonicalize_pcb_file_uuids(path, keep={_EDGE_UUID}) == 0


def test_route_form_measures_written_coordinates(tmp_path):
    # A meander's 45-degree points are rarely on the 4-decimal grid the file
    # holds; the Route form measures them as written, not as computed.
    third = 1.0 / 3.0
    route = Route(
        net=1,
        net_name="N1",
        segments=[
            Segment(70.0, 46.4, 71.0 + third, 47.4 + third, 0.2, Layer.F_CU, net=1),
            Segment(71.0 + third, 47.4 + third, 75.0 + third, 47.4 + third, 0.2, Layer.F_CU, net=1),
        ],
    )
    model = _model()
    pcb = _routed(tmp_path, route.to_sexp())
    assert model.route_length(route) == pytest.approx(model.net_length_pcb(pcb, 1), abs=1e-12)


def _merged_with_duplicate_via(order: list[int]) -> Route:
    """``_route(order)`` as a tuner holds a net merged from fragments.

    Fragments that meet at a via each carry it, so the merged Route lists the
    via twice (#6792) while its segments still form the 3+-way junction.
    """
    route = _route(order)
    dup = Via(*_VIA, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="N1")
    return Route(net=1, net_name="N1", segments=route.segments, vias=[*route.vias, dup])


@pytest.mark.parametrize("order", _ORDERS)
def test_merged_route_with_duplicate_via_equals_kct_check_length(tmp_path, order):
    # Issue #6812: route_geometry must dedupe shared vias (#6792) *and* read
    # segments in file order (#6778) at once.  Dropping the dedupe charges the
    # via span twice; dropping the sort lets the junction resolve in
    # Route.segments order, which differs from the file for some orders.
    model = _model()
    pcb = _routed(tmp_path, _route(order).to_sexp())
    assert model.route_length(_merged_with_duplicate_via(order)) == pytest.approx(
        model.net_length_pcb(pcb, 1), abs=1e-9
    )

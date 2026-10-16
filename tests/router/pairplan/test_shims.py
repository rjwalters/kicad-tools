"""The old lattice import paths re-export the moved objects (issue #6837).

The aliases must be the *same* objects, not copies: a copy would split the
``pad_waiver_polygon`` cache and let a monkeypatch on one path miss callers
of the other.
"""

from __future__ import annotations

from kicad_tools.geometry import copper
from kicad_tools.router.lattice import coupled, gateway, obstacles, pathfinder
from kicad_tools.router.pairplan import chain, tie
from kicad_tools.router.pairplan import gateway as pp_gateway


def test_coupled_reexports_chain_and_tie() -> None:
    for name in (
        "Couple",
        "PairChain",
        "choose_pair_chain",
        "choose_pair_endpoints",
        "choose_pair_tap_chain",
    ):
        assert getattr(coupled, name) is getattr(chain, name), name
    for name in ("MAX_TIE_STUB_MM", "PlannedTie", "plan_equivalent_pin_tie", "_tie_pad_gap"):
        assert getattr(coupled, name) is getattr(tie, name), name


def test_lattice_gateway_is_an_alias() -> None:
    for name in (
        "HEADINGS",
        "Gateway",
        "GatewaySite",
        "enumerate_gateways",
        "propose_gateway_sites",
        "rank_gateway_combos",
        "handedness_consistent",
        "gateway_side",
    ):
        assert getattr(gateway, name) is getattr(pp_gateway, name), name
    assert pathfinder._gateway_anchor is pp_gateway.gateway_anchor_point


def test_pad_waiver_polygon_has_one_cache() -> None:
    assert obstacles._pad_waiver_polygon is copper.pad_waiver_polygon
    assert copper.pad_waiver_polygon(0.0, 0.0, 1.0, 0.5, "roundrect", 0.0) is None
    poly = copper.pad_waiver_polygon(1.0, 2.0, 1.0, 0.5, "rect", 90.0)
    minx, miny, maxx, maxy = poly.bounds
    assert abs((maxx - minx) - 0.5) < 1e-9 and abs((maxy - miny) - 1.0) < 1e-9

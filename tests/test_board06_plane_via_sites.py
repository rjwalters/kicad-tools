"""Board 06 holds a legal stitch-via site open for every VBUS_USB pad (#5700).

Board 06 / diff-pair ``pours=BROKEN``: after the exact-disc halo (#5660), the
router legally walled ``J1.A4`` (``VBUS_USB``) into a pocket with no legal
via site, and the recipe's bounded escape search (``pour_escape.find_escape``)
reported 147 reachable nodes and zero via sites.  The determinism tests stayed
green through it because nothing asked the question directly.

This asks it, on the recipe's routing input, without routing: the recipe's pre-route
reservation must cover every VBUS_USB SMD pad, and every reserved site must be
a legal via under ``find_escape``'s own, independently written, physical model
(project rules, circumscribed pad envelopes, drill floors, board inset).  The
router-side half -- that neither backend lets signal copper consume a
reserved site -- is ``tests/router/test_plane_via_sites_5700.py``; the
containerized Board 06 / Diff-Pair jobs are the end-to-end gate.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

BOARD = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
# The recipe's own routing input (what both board-06 CI jobs route); the
# committed ``output/`` holds the separate real-hardware demo board.
UNROUTED = BOARD / "regression-fixture" / "diffpair_test.kicad_pcb"
PROJECT = BOARD / "regression-fixture" / "diffpair_test.kicad_pro"
NET = "VBUS_USB"


@pytest.fixture
def recipe(monkeypatch):
    # Board scripts use sibling imports; isolate them from other recipe tests.
    for name in ("generate_pcb", "generate_schematic", "generate_design", "pour_escape"):
        spec = importlib.util.spec_from_file_location(name, BOARD / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return sys.modules["generate_design"]


@pytest.fixture
def reservation(recipe):
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.plane_via_sites import reserve_plane_via_sites

    # Mirrors the router rules in the recipe's ``route_pcb``.
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
        manufacturer="jlcpcb",
        min_trace_width=0.1016,
        neck_down_distance=1.0,
        neck_down_threshold=0.8,
    )
    router, _ = load_pcb_for_routing(str(UNROUTED), skip_nets=list(recipe.POUR_NETS), rules=rules)
    return reserve_plane_via_sites(router.grid, [NET])


def test_every_vbus_pad_gets_a_site(recipe, reservation) -> None:
    pads = recipe._parse_pads(UNROUTED)
    vbus_smd = sorted(p["name"] for p in pads if p["net"] == NET and not p["is_th"])
    assert "J1.A4" in vbus_smd, "the pad #5700 stranded must be in scope"
    assert reservation.unreserved == []
    assert sorted(name for site in reservation.sites for name in site.pads) == vbus_smd


def test_every_reserved_site_is_a_legal_escape_via(recipe, reservation) -> None:
    """``find_escape`` started ON the site returns it as a zero-length via.

    Its origin test (``on_via``) is the full via-legality predicate: clear of
    foreign pad envelopes by the project clearance, off the same-net pad,
    clear of every drill by the hole floor, inside the board inset, and over
    the VBUS pour on another layer.
    """
    escape = sys.modules["pour_escape"]
    rules = escape.EscapeRules.from_project(PROJECT)
    pads = recipe._parse_pads(UNROUTED)
    pad_index = [
        (
            Point(p["x"], p["y"]).buffer(math.hypot(p["w"], p["h"]) / 2),
            p["net"],
            p["layers"],
            p["drill"] / 2,
            (p["x"], p["y"]),
        )
        for p in pads
    ]
    # The VBUS pour is its pads' bbox inflated by the zone generator's
    # margin, on In2.Cu (``zone_assignments`` in the recipe).
    from kicad_tools.zones.generator import DEFAULT_POUR_BBOX_MARGIN_MM as margin

    vbus = [p for p in pads if p["net"] == NET]
    pour = box(
        min(p["x"] for p in vbus) - margin,
        min(p["y"] for p in vbus) - margin,
        max(p["x"] for p in vbus) + margin,
        max(p["y"] for p in vbus) + margin,
    )
    gp = recipe.generate_pcb
    bounds = (
        gp.BOARD_ORIGIN_X + 0.5,
        gp.BOARD_ORIGIN_Y + 0.5,
        gp.BOARD_ORIGIN_X + gp.BOARD_WIDTH - 0.5,
        gp.BOARD_ORIGIN_Y + gp.BOARD_HEIGHT - 0.5,
    )
    by_name = {p["name"]: p for p in pads}
    for site in reservation.sites:
        result = escape.find_escape(
            (site.x, site.y),
            NET,
            "F.Cu",
            pad_index,
            [],
            [],
            [(pour, frozenset({"In2.Cu"}), "pour")],
            bounds,
            rules,
            node_budget=1,
        )
        assert result is not None and result.via, site
        assert result.points == ((site.x, site.y),), site
        # ...and the stub from each served pad clears every foreign pad.
        for name in site.pads:
            pad = by_name[name]
            stub = LineString([(pad["x"], pad["y"]), (site.x, site.y)]).buffer(rules.width / 2)
            for geom, pnet, layers, _hole, _centre in pad_index:
                if pnet != NET and "F.Cu" in layers:
                    assert stub.distance(geom) >= rules.clearance, (name, site)

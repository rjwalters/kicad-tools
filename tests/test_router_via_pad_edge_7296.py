"""Router refuses vias in the mask-dam zone beside an SMD pad (Issue #7296).

``kct check`` warns ``via_at_pad_edge`` (Issue #7220) when a via drill edge sits
closer than the fab's ``min_solder_mask_dam_mm`` to an SMD pad's mask aperture
without overlapping the copper.  The router's in-pad refusal now covers that
zone too, with the same arithmetic in the Python and C++ coupled pathfinders.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_geometry import pad_point_distance
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting
from kicad_tools.router.via_in_pad_eligibility import via_drill_at_pad_edge
from kicad_tools.validate.rules.via_pad_geometry import drill_to_mask_aperture_gap

BACKENDS = [
    "python",
    pytest.param(
        "cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built"),
    ),
]

DAM = 0.1
DRILL = 0.3


# ---------------------------------------------------------------------------
# Rule defaults
# ---------------------------------------------------------------------------


def test_floor_defaults_from_the_manufacturer_dam():
    assert DesignRules(manufacturer="jlcpcb").min_via_pad_edge_gap == pytest.approx(DAM)
    assert DesignRules().min_via_pad_edge_gap == 0.0
    assert DesignRules(manufacturer="jlcpcb", min_via_pad_edge_gap=0.0).min_via_pad_edge_gap == 0.0


# ---------------------------------------------------------------------------
# The predicate agrees with the checker's geometry
# ---------------------------------------------------------------------------


def _checker_pad():
    from kicad_tools.schema.pcb import Footprint
    from kicad_tools.schema.pcb import Pad as PcbPad

    pad = PcbPad("1", "smd", "rect", (0, 0), (1.0, 1.25), ["F.Cu"], net_number=1, net_name="DATA")
    return pad, Footprint("test", "F.Cu", (10, 10), 0, "R1", "test", pads=[pad])


@pytest.mark.parametrize("rotation", [0.0, 90.0])
@pytest.mark.parametrize("offset", [0.0, 0.1, 0.149, 0.15, 0.2, 0.249, 0.25, 0.3, 0.349, 0.4, 0.5])
def test_predicate_matches_drill_to_mask_aperture_gap(offset, rotation):
    """Router and checker cannot disagree outside the 1 um float noise band."""
    from types import SimpleNamespace

    pad, fp = _checker_pad()
    fp.rotation = rotation
    # Copper edge along the offset axis: x=10.5 (rotation 0) or y=10.625
    # (rotation 90: the checker's footprint rotation puts the 1.25 mm side on
    # y).  The router pad is that same absolute rectangle.
    edge = 0.5 if rotation == 0.0 else 0.625
    if rotation == 0.0:
        via = SimpleNamespace(position=(10.0 + edge + offset, 10.0), drill=DRILL)
    else:
        via = SimpleNamespace(position=(10.0, 10.0 + edge + offset), drill=DRILL)
    gap = drill_to_mask_aperture_gap(via, pad, fp)
    assert gap is not None

    router_pad = Pad(
        x=10.0,
        y=10.0,
        width=1.0,
        height=1.25,
        net=1,
        net_name="DATA",
        layer=Layer.F_CU,
    )
    dist = pad_point_distance(router_pad, *via.position)
    in_zone = via_drill_at_pad_edge(dist, DRILL, DAM)
    checker_flags = 0.0 <= gap < DAM
    if abs(gap - DAM) > 1e-6 and abs(gap) > 1e-6:
        assert in_zone == checker_flags, (offset, gap)


# ---------------------------------------------------------------------------
# Both coupled backends refuse the zone
# ---------------------------------------------------------------------------


def _pair(floor: float, backend: str, manufacturer: str | None = "jlcpcb-tier1"):
    """06-like start: 0.3 x 0.8 mm SMD pads at 0.8 mm pitch, goal on B.Cu."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_clearance=0.2,
        via_diameter=0.6,
        via_drill=DRILL,
        grid_resolution=0.1,
        min_hole_to_hole=0.25,
        manufacturer=manufacturer,
        min_via_pad_edge_gap=floor,
    )
    grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
    nc = NetClassRouting(
        name="HS", trace_width=0.2, clearance=0.15, intra_pair_clearance=0.1, coupled_routing=True
    )
    pads = (
        Pad(x=2.0, y=2.0, width=0.3, height=0.8, net=1, net_name="DP+", layer=Layer.F_CU),
        Pad(x=2.3, y=6.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.B_CU),
        Pad(x=2.8, y=2.0, width=0.3, height=0.8, net=2, net_name="DP-", layer=Layer.F_CU),
        Pad(x=2.6, y=6.0, width=0.2, height=0.2, net=2, net_name="DP-", layer=Layer.B_CU),
    )
    for pad in pads:
        grid.add_pad(pad)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        net_class_map={"DP+": nc, "DP-": nc},
    )
    pf._use_cpp_coupled = backend == "cpp"
    return rules, grid, pf.route_coupled(*pads, max_iterations_budget=200_000)


def _edge_zone_vias(grid: RoutingGrid, routes, floor: float) -> list[tuple[float, float]]:
    return [
        (via.x, via.y)
        for route in routes
        for via in route.vias
        for pad in grid._pads
        if not pad.through_hole
        and via_drill_at_pad_edge(pad_point_distance(pad, via.x, via.y), DRILL, floor)
    ]


#: Wide enough that the unconstrained search's via (0.15 mm off the pad
#: copper) lands inside it, so the refusal has something to refuse.
WIDE_FLOOR = 0.25


@pytest.mark.parametrize("backend", BACKENDS)
def test_coupled_search_never_drops_a_via_in_the_dam_zone(backend):
    # Control: without the floor the cheapest layer change is in the zone.
    _rules, grid, control = _pair(0.0, backend, "jlcpcb")
    assert control is not None
    assert _edge_zone_vias(grid, control, WIDE_FLOOR), "control must exercise the zone"

    rules, grid, result = _pair(WIDE_FLOOR, backend, "jlcpcb")
    assert rules.min_via_pad_edge_gap == WIDE_FLOOR
    assert result is not None, "the pair stays routable with a legal via"
    assert all(route.vias for route in result)
    assert _edge_zone_vias(grid, result, WIDE_FLOOR) == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_floor_applies_where_via_in_pad_is_allowed(backend):
    """The floor is independent of ``allow_smd_vias`` (tier1 allows in-pad)."""
    _rules, grid, result = _pair(DAM, backend)
    assert result is not None
    assert _edge_zone_vias(grid, result, DAM) == []


def test_backends_agree_with_the_floor_on():
    if not is_cpp_available():
        pytest.skip("C++ backend not built")
    _, _, py = _pair(WIDE_FLOOR, "python", "jlcpcb")
    _, _, cpp = _pair(WIDE_FLOOR, "cpp", "jlcpcb")
    assert py is not None and cpp is not None
    for a, b in zip(py, cpp, strict=True):
        assert [(v.x, v.y) for v in a.vias] == [(v.x, v.y) for v in b.vias]


# ---------------------------------------------------------------------------
# Single-ended A* (the main router) refuses the zone too
# ---------------------------------------------------------------------------


def _single_ended(floor: float, backend: str, manufacturer: str = "jlcpcb"):
    """F.Cu SMD pad -> B.Cu SMD pad on one net: the route needs a layer change."""
    from kicad_tools.router.cpp_backend import create_hybrid_router

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_clearance=0.2,
        via_diameter=0.6,
        via_drill=DRILL,
        grid_resolution=0.1,
        min_hole_to_hole=0.25,
        manufacturer=manufacturer,
        min_via_pad_edge_gap=floor,
    )
    grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
    start = Pad(x=2.0, y=2.0, width=0.3, height=0.8, net=1, net_name="SIG", layer=Layer.F_CU)
    end = Pad(x=2.3, y=6.0, width=0.2, height=0.2, net=1, net_name="SIG", layer=Layer.B_CU)
    for pad in (start, end):
        grid.add_pad(pad)
    router = create_hybrid_router(grid, rules, force_python=backend == "python")
    return grid, router.route(start, end)


@pytest.mark.parametrize("backend", BACKENDS)
def test_single_ended_search_never_drops_a_via_in_the_dam_zone(backend):
    grid, control = _single_ended(0.0, backend)
    assert control is not None and control.vias
    assert _edge_zone_vias(grid, [control], WIDE_FLOOR), "control must exercise the zone"

    grid, result = _single_ended(WIDE_FLOOR, backend)
    assert result is not None, "the net stays routable with a legal via"
    assert result.vias
    assert _edge_zone_vias(grid, [result], WIDE_FLOOR) == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_single_ended_floor_applies_where_via_in_pad_is_allowed(backend):
    """The floor is independent of ``allow_smd_vias`` (tier1 allows in-pad)."""
    grid, result = _single_ended(DAM, backend, "jlcpcb-tier1")
    assert result is not None
    assert _edge_zone_vias(grid, [result], DAM) == []


def test_single_ended_backends_agree_with_the_floor_on():
    if not is_cpp_available():
        pytest.skip("C++ backend not built")
    _, py = _single_ended(WIDE_FLOOR, "python")
    _, cpp = _single_ended(WIDE_FLOOR, "cpp")
    assert py is not None and cpp is not None
    assert len(py.vias) == len(cpp.vias)
    for a, b in zip(py.vias, cpp.vias, strict=True):
        assert (a.x, a.y) == pytest.approx((b.x, b.y), abs=1e-4)  # float32 noise

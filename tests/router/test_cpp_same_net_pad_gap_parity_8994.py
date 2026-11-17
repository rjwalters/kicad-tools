"""C++ same-net pad slit-band guard parity (Issue #8994).

Board 06's via-banded ``physical_copper_gap`` population is seated by the
C++ pathfinders, so #8974's Python-side producers could not drain it.  This
suite pins the C++ mirror of ``via_clearance.same_net_pad_gap_acceptable``
(``DesignRules.same_net_pad_gap_band_edge_mm`` threaded into
``cpp_backend.py``'s rule marshalling, consumed in
``Pathfinder::is_via_blocked_diag`` and the coupled ``smd_vias_ok`` gate):

* the C++ verdict equals the Python predicate over in-band / overlapping /
  beyond-edge / exact-edge seats, including a non-cardinally-rotated pad and
  a ``circle`` pad -- the two shapes where the #7296 template's exact-rect
  model and Python's rotation-inflated AABB disagree;
* the guard is inert when no stackup edge was resolved (never a literal);
* net scope: a foreign-net SMD pad in the band does not refuse the seat
  (that is ``min_via_pad_edge_gap`` / clearance's job), and a through-hole
  own-net pad is excluded;
* both live searches (single-ended A* and the coupled joint search) keep
  their via annuli out of the band while still routing.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.cli.stitch_cmd import find_all_pad_bboxes
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.via_clearance import same_net_pad_gap_acceptable
from kicad_tools.sexp import parse_string

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# Fixture stack: a small 20 x 20 mm grid at 0.05 mm resolution so every
# probe below lands on the lattice.  Pad centre / board centre is (10, 10).
RES = 0.05
COLS = ROWS = 400
CENTRE = 10.0
NET = 7
FOREIGN_NET = 8

# A caller-resolved edge with a dyadic value (exact in float32 AND float64)
# so the exact-band-edge probes below measure identically on both sides.
EDGE = 0.25
VIA_DIAMETER = 0.5  # annulus half 0.25, also dyadic
VIA_DRILL = 0.25


def _pcb_text(shape: str, rotation: float, width: float, height: float, net: int) -> str:
    """One SMD footprint pad at the board centre, rotated ``rotation`` deg."""
    return (
        "(kicad_pcb\n"
        f'  (footprint "test" (layer "F.Cu") (at {CENTRE} {CENTRE} {rotation})\n'
        f'    (pad "1" smd {shape} (at 0 0) (size {width} {height}) '
        f'(layers "F.Cu") (net {net} "SIG"))\n'
        "  )\n"
        ")"
    )


def _oracle_bboxes(shape: str, rotation: float, width: float, height: float, net: int):
    """The stitcher's own pad AABBs for the fixture (the checker's model)."""
    return [
        bbox[:4]
        for bbox in find_all_pad_bboxes(
            parse_string(_pcb_text(shape, rotation, width, height, net))
        )
        if bbox[4] == net
    ]


def _cpp_pathfinder(edge_mm: float, pads, layers: int = 2):
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, layers, RES, 0.0, 0.0)
    for x, y, width, height, net, layer_idx, rotation, is_circular in pads:
        grid.add_pad(x, y, width, height, net, layer_idx, 0, 0.0, False, rotation, is_circular)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.2
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.allow_smd_vias = True
    rules.same_net_pad_gap_band_edge_mm = edge_mm
    return router_cpp.Pathfinder(grid, rules, False)


def _world_to_grid(x_mm: float) -> int:
    gx = round(x_mm / RES)
    assert abs(gx * RES - x_mm) < 1e-9, f"probe {x_mm} off the {RES} lattice"
    return gx


def _band_rejects(edge_mm: float, pads, x_mm: float, y_mm: float, net: int = NET) -> bool:
    """The BAND's contribution to the C++ via verdict at one world seat."""
    pf_on = _cpp_pathfinder(edge_mm, pads)
    pf_off = _cpp_pathfinder(0.0, pads)
    gx, gy = _world_to_grid(x_mm), _world_to_grid(y_mm)
    blocked_on = pf_on.is_via_blocked(gx, gy, net, True)
    blocked_off = pf_off.is_via_blocked(gx, gy, net, True)
    assert not blocked_off, "guard-off baseline must leave the seat open"
    return blocked_on


# ---------------------------------------------------------------------------
# Direct predicate parity: the C++ verdict equals the Python predicate
# ---------------------------------------------------------------------------


def _rect_pad(rotation: float = 0.0, net: int = NET):
    # 1.0 x 1.0 so the axis-aligned box edge sits at 10.5 and every probe
    # below is dyadic.
    return [(CENTRE, CENTRE, 1.0, 1.0, net, 0, rotation, False)], _oracle_bboxes(
        "rect", rotation, 1.0, 1.0, net
    )


@requires_cpp
@pytest.mark.parametrize(
    ("x_mm", "y_mm", "expected"),
    [
        (10.0, 10.0, True),  # annulus centred on the pad: overlap, union
        (10.5, 10.0, True),  # annulus swallows the edge: overlap, union
        (10.75, 10.0, True),  # gap == 0 exactly: accepted (open band)
        (10.8, 10.0, False),  # gap 0.05 inside (0, 0.25): slit
        (11.0, 10.0, True),  # gap == edge exactly: accepted (open band)
        (11.25, 10.0, True),  # gap 0.50 beyond the edge: clear
        (10.75, 10.75, False),  # corner diagonal in band (gap 0.104)
        (11.0, 11.0, True),  # corner diagonal beyond the edge (gap 0.457)
    ],
)
def test_axis_aligned_pad_verdicts_match_python(x_mm, y_mm, expected):
    """The C++ guard rejects exactly the Python predicate's slit band."""
    pads, bboxes = _rect_pad()
    assert same_net_pad_gap_acceptable(x_mm, y_mm, VIA_DIAMETER, bboxes, EDGE) is expected, (
        "oracle self-check"
    )
    assert _band_rejects(EDGE, pads, x_mm, y_mm) is (not expected)


@requires_cpp
@pytest.mark.parametrize("rotation", [0.0, 30.0, 45.0, 90.0, 200.0])
def test_rotated_pad_fan_parity(rotation):
    """Every swept seat around a rotated pad matches the Python verdict."""
    import math as _math

    # 1.6 x 0.6 mm pad: a long non-square rect whose inflated AABB is the
    # checker's model (find_all_pad_bboxes), not the exact rotated rect.
    width, height = 1.6, 0.6
    pads = [(CENTRE, CENTRE, width, height, NET, 0, rotation, False)]
    bboxes = _oracle_bboxes("rect", rotation, width, height, NET)
    checked = 0
    for angle_deg in range(0, 360, 15):
        for radius in (0.3, 0.5, 0.75, 1.0, 1.25):
            x_mm = round((CENTRE + radius * _math.cos(_math.radians(angle_deg))) / RES) * RES
            y_mm = round((CENTRE + radius * _math.sin(_math.radians(angle_deg))) / RES) * RES
            py_ok = same_net_pad_gap_acceptable(x_mm, y_mm, VIA_DIAMETER, bboxes, EDGE)
            assert _band_rejects(EDGE, pads, x_mm, y_mm) is (not py_ok), (
                rotation,
                angle_deg,
                radius,
                x_mm,
                y_mm,
            )
            checked += 1
    assert checked > 100


@requires_cpp
def test_rotated_pad_fan_bites_against_the_exact_rect_template():
    """The swept set discriminates the #7296 exact-rect model.

    If someone swaps the AABB mirror for the exact-rotated-rect arithmetic of
    the ``min_via_pad_edge_gap`` block (the drift this issue exists to
    prevent), at least one swept seat flips: this pins that the suite bites.
    """

    def exact_rect_gap(x_mm: float, y_mm: float, rotation: float, width: float, height: float):
        angle = rotation * math.pi / 180.0
        c, s = math.cos(angle), math.sin(angle)
        local_x = c * (x_mm - CENTRE) - s * (y_mm - CENTRE)
        local_y = s * (x_mm - CENTRE) + c * (y_mm - CENTRE)
        dx = max(abs(local_x) - width / 2, 0.0)
        dy = max(abs(local_y) - height / 2, 0.0)
        return math.hypot(dx, dy) - VIA_DIAMETER / 2

    rotation, width, height = 45.0, 1.6, 0.6
    bboxes = _oracle_bboxes("rect", rotation, width, height, NET)
    discriminating = 0
    for angle_deg in range(0, 360, 15):
        for radius in (0.3, 0.5, 0.75, 1.0, 1.25):
            x_mm = round((CENTRE + radius * math.cos(math.radians(angle_deg))) / RES) * RES
            y_mm = round((CENTRE + radius * math.sin(math.radians(angle_deg))) / RES) * RES
            aabb_ok = same_net_pad_gap_acceptable(x_mm, y_mm, VIA_DIAMETER, bboxes, EDGE)
            gap_exact = exact_rect_gap(x_mm, y_mm, rotation, width, height)
            exact_ok = not (1e-9 < gap_exact < EDGE - 1e-9)
            if aabb_ok != exact_ok:
                discriminating += 1
    assert discriminating > 0, "fixture no longer separates the two pad models"


@requires_cpp
@pytest.mark.parametrize("shape_size", [("circle", 1.0, 0.4), ("circle", 0.8, 0.8)])
def test_circle_pad_parity(shape_size):
    """A circle pad's box is its exact bounding square (no inflation)."""
    shape, width, height = shape_size
    pads = [(CENTRE, CENTRE, width, height, NET, 0, 45.0, True)]
    bboxes = _oracle_bboxes(shape, 45.0, width, height, NET)
    for x_mm in (10.5, 10.75, 10.85, 11.0, 11.25, 11.5):
        py_ok = same_net_pad_gap_acceptable(x_mm, CENTRE, VIA_DIAMETER, bboxes, EDGE)
        assert _band_rejects(EDGE, pads, x_mm, CENTRE) is (not py_ok), x_mm


# ---------------------------------------------------------------------------
# Net scope and the unresolved-edge contract
# ---------------------------------------------------------------------------


@requires_cpp
def test_foreign_net_pad_in_band_does_not_refuse():
    """A foreign SMD pad in the band is other guards' job, not this one's."""
    pads = [(CENTRE, CENTRE, 1.0, 1.0, FOREIGN_NET, 0, 0.0, False)]
    assert _band_rejects(EDGE, pads, 10.8, 10.0) is False


@requires_cpp
def test_through_hole_own_net_pad_is_excluded():
    """``layer_idx < 0`` is a through-hole annulus, not the SMD population."""
    pads = [(CENTRE, CENTRE, 1.0, 1.0, NET, -1, 0.0, False)]
    assert _band_rejects(EDGE, pads, 10.8, 10.0) is False


@requires_cpp
def test_unresolved_edge_leaves_guard_inert():
    """Edge unresolved (None -> 0.0): every band seat stays open (#8974 AC-3)."""
    pads = _rect_pad()[0]
    for x_mm, y_mm in ((10.8, 10.0), (10.9, 10.9), (11.0, 10.5)):
        pf = _cpp_pathfinder(0.0, pads)
        assert not pf.is_via_blocked(_world_to_grid(x_mm), _world_to_grid(y_mm), NET, True), (
            x_mm,
            y_mm,
        )


# ---------------------------------------------------------------------------
# Live searches keep their annuli out of the band (the board-06 drain)
# ---------------------------------------------------------------------------

BAND = 0.4  # wide enough that the pushed-off-pad seat lands inside it
DAM = 0.35  # #7296 floor: pushes the cheapest seat to drill-edge 0.35 (centre
# distance 0.5 from the pad box), i.e. annulus gap 0.2 -- inside the band.
# This is how the real boards produce the population: the dam keeps the
# drill off the pad, and the annulus then slits beside it.


def _pad_aabb(pad):
    """The checker's AABB for a ``router.primitives Pad`` (cardinal fixture)."""
    return [
        (
            pad.x - pad.width / 2,
            pad.y - pad.height / 2,
            pad.x + pad.width / 2,
            pad.y + pad.height / 2,
        )
    ]


def _in_band_seats(grid, routes, edge_mm: float):
    seats = []
    for route in routes:
        for via in route.vias:
            for pad in grid._pads:
                if pad.through_hole or pad.net != route.net:
                    continue
                if not same_net_pad_gap_acceptable(
                    via.x, via.y, VIA_DIAMETER_A, _pad_aabb(pad), edge_mm
                ):
                    seats.append((via.x, via.y, pad.net_name))
    return seats


VIA_DIAMETER_A = 0.6  # annulus half 0.3


@requires_cpp
def test_single_ended_search_keeps_annuli_out_of_the_band():
    from kicad_tools.router.cpp_backend import create_hybrid_router
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules

    def _route(edge: float):
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_clearance=0.2,
            via_diameter=VIA_DIAMETER_A,
            via_drill=0.3,
            grid_resolution=0.1,
            min_hole_to_hole=0.25,
            min_via_pad_edge_gap=DAM,
            manufacturer="jlcpcb",
            same_net_pad_gap_band_edge_mm=edge,
        )
        grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
        start = Pad(x=2.0, y=2.0, width=0.3, height=0.8, net=1, net_name="SIG", layer=Layer.F_CU)
        end = Pad(x=2.3, y=6.0, width=0.2, height=0.2, net=1, net_name="SIG", layer=Layer.B_CU)
        for pad in (start, end):
            grid.add_pad(pad)
        router = create_hybrid_router(grid, rules)
        return grid, router.route(start, end)

    grid, control = _route(0.0)
    assert control is not None and control.vias, "control must route with vias"
    assert _in_band_seats(grid, [control], BAND), "control must exercise the band"

    grid, result = _route(BAND)
    assert result is not None, "the net stays routable with a legal via"
    assert result.vias, "the route still changes layer"
    assert _in_band_seats(grid, [result], BAND) == []


@requires_cpp
def test_coupled_search_keeps_annuli_out_of_the_band():
    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    def _pair(edge: float):
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_clearance=0.2,
            via_diameter=VIA_DIAMETER_A,
            via_drill=0.3,
            grid_resolution=0.1,
            min_hole_to_hole=0.25,
            min_via_pad_edge_gap=DAM,
            manufacturer="jlcpcb",
            same_net_pad_gap_band_edge_mm=edge,
        )
        grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
        nc = NetClassRouting(
            name="HS",
            trace_width=0.2,
            clearance=0.15,
            intra_pair_clearance=0.1,
            coupled_routing=True,
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
        pf._use_cpp_coupled = True
        return grid, pf.route_coupled(*pads, max_iterations_budget=200_000)

    grid, control = _pair(0.0)
    assert control is not None and all(r.vias for r in control), "control must via down"
    assert _in_band_seats(grid, control, BAND), "control must exercise the band"

    grid, result = _pair(BAND)
    assert result is not None, "the pair stays routable with legal vias"
    assert all(r.vias for r in result), "both rails still change layer"
    assert _in_band_seats(grid, result, BAND) == []

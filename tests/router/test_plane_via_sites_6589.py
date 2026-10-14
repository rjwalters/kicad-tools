"""A reserved plane-via site must stay on its pour's fill (Issue #6589).

The #6401 reservation kept foreign copper at *copper clearance* from each
reserved site.  On board 03 that let an ``ISP_MISO`` through-via land
0.873 mm from the ``VBUS: U1.7`` site: legal copper, but its antipad on the
``In2.Cu`` pour (clearance 0.3 mm, ``min_thickness`` 0.25 mm) pulled the fill
back 0.424 mm from its barrel, so a 0.6 mm via at the site was no longer
wholly on fill and the stitcher's #4432 gate rightly refused it.

The site keep-out now also holds foreign vias (every layer) and tracks on
the plane layer outside the fill pullback.  Tracks on other layers keep the
old copper-clearance disc.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.lattice.obstacles import LatticeKeepoutMask
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.plane_via_sites import (
    PlaneFill,
    PlaneViaReservation,
    PlaneViaSite,
    PlaneViaSiteRules,
    plane_fill_pullback,
    plane_fills_from_pcb,
    plane_via_keepout_areas,
    reserve_router_plane_via_sites,
)
from kicad_tools.router.primitives import Layer, Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.sexp.parser import parse_string

NET = "VBUS"
SIGNAL = 7
CLEARANCE = 0.2
VIA_R = 0.3  # the 0.6 mm foreign through-via of board 03
# Board 03's auto-pour VBUS zone on In2.Cu.
FILL = PlaneFill(layer="In2.Cu", clearance=0.3, min_thickness=0.25)
STACK = LayerStack.four_layer_sig_gnd_pwr_sig()
PLANE_INDEX = STACK.get_layer_by_name("In2.Cu").index  # type: ignore[union-attr]
OTHER_INDEX = STACK.get_layer_by_name("B.Cu").index  # type: ignore[union-attr]


def _reservation() -> PlaneViaReservation:
    rules = PlaneViaSiteRules(via_diameter=0.6, via_drill=0.3, clearance=CLEARANCE)
    return PlaneViaReservation(
        sites=[PlaneViaSite(net_name=NET, x=10.0, y=10.0, pads=("U1.7",))], rules=rules
    )


def _mask(with_fill: bool = True) -> LatticeKeepoutMask:
    fills = {NET: [(PLANE_INDEX, plane_fill_pullback(FILL, CLEARANCE))]} if with_fill else None
    return LatticeKeepoutMask(
        plane_via_keepout_areas(
            _reservation(),
            clearance=CLEARANCE,
            layer_index=STACK.layer_enum_to_index,
            num_layers=STACK.num_layers,
            plane_fills=fills,
        )
    )


def _around(dist: float, k: int, n: int = 16) -> tuple[float, float]:
    ang = 2 * math.pi * k / n
    return (10.0 + dist * math.cos(ang), 10.0 + dist * math.sin(ang))


def test_pullback_comes_from_zone_clearance_and_min_thickness() -> None:
    # 0.3 + 0.25 / 2: board 03 measured 0.424 mm on the filled board.
    assert plane_fill_pullback(FILL, CLEARANCE) == pytest.approx(0.425)
    # Never below the copper clearance floor.
    assert plane_fill_pullback(PlaneFill("In1.Cu", 0.1, 0.0), 0.2) == pytest.approx(0.2)


def test_board03_foreign_via_is_kept_off_the_fill_around_the_site() -> None:
    mask = _mask()
    for k in range(16):
        # The board-03 ISP_MISO via: 0.873 mm from the site is now refused...
        assert mask.via_blocked(_around(0.873, k), SIGNAL, VIA_R)
        # ... and 1.05 mm (outside 0.3 + 0.425 + 0.3) is allowed.
        assert not mask.via_blocked(_around(1.05, k), SIGNAL, VIA_R)


def test_without_fill_data_the_copper_only_keepout_is_unchanged() -> None:
    mask = _mask(with_fill=False)
    assert len(mask.areas) == 1
    # The #6401 behaviour that left board 03's link open.
    assert not mask.via_blocked(_around(0.873, 0), SIGNAL, VIA_R)


def test_tracks_are_held_off_only_on_the_plane_layer() -> None:
    mask = _mask()
    half = 0.1
    # A track whose edge keeps exactly the old copper clearance (+ margin for
    # the circumscribed polygon) from the reserved via.
    dist = 0.3 + CLEARANCE + half + 0.01
    for k in range(16):
        p = _around(dist, k)
        assert not mask.segment_blocked(p, p, OTHER_INDEX, SIGNAL, half)
        assert not mask.segment_blocked(p, p, 0, SIGNAL, half)
        # On the pour's own layer it would cut the fill under the site.
        assert mask.segment_blocked(p, p, PLANE_INDEX, SIGNAL, half)


ZONES = """
(kicad_pcb
  (zone (net 3) (net_name "VBUS") (layer "In2.Cu")
    (connect_pads (clearance 0.3)) (min_thickness 0.25))
  (zone (net 1) (net_name "GND") (layers "F.Cu" "B.Cu")
    (connect_pads (clearance 0.2)) (min_thickness 0.2))
  (zone (net 0) (net_name "") (layer "F.Cu") (keepout (tracks not_allowed)))
)
"""


def test_plane_fills_are_read_from_the_board_zones() -> None:
    fills = plane_fills_from_pcb(parse_string(ZONES), ["VBUS", "GND"])
    assert fills == {
        "VBUS": [PlaneFill("In2.Cu", 0.3, 0.25)],
        "GND": [PlaneFill("F.Cu", 0.2, 0.2), PlaneFill("B.Cu", 0.2, 0.2)],
    }
    assert plane_fills_from_pcb(parse_string(ZONES), ["VCC"]) == {}


def _lqfp_row_grid() -> RoutingGrid:
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )
    grid = RoutingGrid(width=20, height=20, rules=rules, layer_stack=STACK)
    for i in range(9):
        y = 8.0 + 0.5 * i
        pin = str(i + 1)
        net, name = (0, NET) if pin == "5" else (i + 1, f"S{pin}")
        for ref_pin, x, n, nm in ((pin, 10.0, net, name), (f"L{pin}", 4.0, 20 + i, f"L{pin}")):
            grid.add_pad(
                Pad(
                    x=x,
                    y=y,
                    width=1.475,
                    height=0.3,
                    net=n,
                    net_name=nm,
                    layer=Layer.F_CU,
                    ref="U2",
                    pin=ref_pin,
                )
            )
    return grid


def test_router_entry_point_threads_the_plane_fill_through() -> None:
    grid = _lqfp_row_grid()
    router = SimpleNamespace(grid=grid, rules=grid.rules, net_class_map={}, layer_stack=STACK)
    reservation = reserve_router_plane_via_sites(router, [NET], plane_fills={NET: [FILL]})
    (site,) = reservation.sites
    names = [a.name.split(":")[0] for a in router._plane_via_keepout_areas]
    assert names == ["plane-via-site", "plane-via-fill", "plane-via-stub"]
    fill_area = router._plane_via_keepout_areas[1]
    assert fill_area.layers == frozenset({PLANE_INDEX})
    fill_only = LatticeKeepoutMask([fill_area])
    for k in range(16):
        ang = 2 * math.pi * k / 16
        near = (site.x + 0.873 * math.cos(ang), site.y + 0.873 * math.sin(ang))
        far = (site.x + 1.05 * math.cos(ang), site.y + 1.05 * math.sin(ang))
        assert fill_only.via_blocked(near, SIGNAL, VIA_R)
        assert not fill_only.via_blocked(far, SIGNAL, VIA_R)
    # No fill data: the #6401 areas exactly.
    reserve_router_plane_via_sites(router, [NET])
    assert [a.name.split(":")[0] for a in router._plane_via_keepout_areas] == [
        "plane-via-site",
        "plane-via-stub",
    ]

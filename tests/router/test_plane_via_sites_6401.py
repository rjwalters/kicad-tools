"""Pour-pad via-site reservation in ``kct route``, grid and lattice (Issue #6401).

Boards 03, 04 and 05 (issue #6388) routed every signal net, and then left a
pour-net pin of a fine-pitch IC with no legal via site: signal copper had
taken all of them before the post-route oracle ran (03 U1.7 VBUS, 04 U2.36
+3.3V, 05 U1.21 GND).  The #5700 reservation that prevents this ran only in
board 06's generator, only for the grid engine, and its planner found no site
at all beside a 0.5 mm-pitch pin row.

Covered here:

1. which pads are served (the #5700 dense-array caveat as a geometric rule);
2. the exact-geometry planner mode finds a site beside a 0.5 mm-pitch pin
   where the #5700 default cannot;
3. the lattice keep-out projection carries the clearance;
4. the lattice engine: a signal net that would otherwise run over the
   reserved site detours around it, and the pour pad keeps a legal site;
5. the ``Autorouter`` / ``kct route`` wiring.
"""

from __future__ import annotations

import math
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.lattice.geometry import seg_near_polygon, seg_pt_dist
from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.plane_via_sites import (
    PlaneViaSiteRules,
    plane_via_keepout_areas,
    reserve_plane_via_sites,
    reserve_router_plane_via_sites,
    select_plane_via_pads,
)
from kicad_tools.router.primitives import Layer, Pad, Route
from kicad_tools.router.rules import DesignRules

PLANE = "GND"


def _pad(
    ref: str,
    pin: str,
    x: float,
    y: float,
    net: int,
    net_name: str,
    *,
    width: float = 1.0,
    height: float = 1.0,
    through_hole: bool = False,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
        through_hole=through_hole,
    )


# ---------------------------------------------------------------------------
# 1. Pad selection.
# ---------------------------------------------------------------------------


def _qfn_with_exposed_pad() -> list[Pad]:
    """Four pins a side around a centred exposed pad; two pins are GND."""
    pads: list[Pad] = []
    pin = 1
    for i in range(4):
        off = -0.75 + 0.5 * i
        for x, y in ((-2.0, off), (off, 2.0), (2.0, -off), (-off, -2.0)):
            net_name = PLANE if pin in (3, 9) else f"S{pin}"
            pads.append(
                _pad("U1", str(pin), 10 + x, 10 + y, 0 if net_name == PLANE else pin, net_name)
            )
            pin += 1
    pads.append(_pad("U1", "17", 10.0, 10.0, 0, PLANE, width=2.0, height=2.0))
    return pads


def test_perimeter_pins_are_served_and_the_exposed_pad_is_not() -> None:
    selected, skipped = select_plane_via_pads(_qfn_with_exposed_pad(), [PLANE])
    assert sorted(f"{p.ref}.{p.pin}" for p in selected) == ["U1.3", "U1.9"]
    assert skipped == {"U1.17": "interior (exposed) pad"}


def test_area_array_footprint_is_skipped_whole() -> None:
    """A BGA field escapes by trace (#5700): no site beside any of its balls."""
    balls = [
        _pad("U2", f"{r}{c}", 10 + c, 10 + r, 0, PLANE, width=0.4, height=0.4)
        for r in range(4)
        for c in range(4)
    ]
    selected, skipped = select_plane_via_pads(balls, [PLANE])
    assert selected == []
    assert set(skipped) == {f"U2.{r}{c}" for r in range(4) for c in range(4)}
    assert all("area-array" in reason for reason in skipped.values())


def test_split_exposed_pad_is_not_mistaken_for_an_area_array() -> None:
    """Board 05's U2/U3: the exposed pad is many same-numbered copper pieces
    plus unnumbered paste apertures -- still one pad, not a BGA field."""
    pads = _qfn_with_exposed_pad()
    for dx in (-0.6, 0.0, 0.6):
        for dy in (-0.6, 0.0, 0.6):
            pads.append(_pad("U1", "17", 10 + dx, 10 + dy, 0, PLANE, width=0.3, height=0.3))
            pads.append(_pad("U1", "", 10 + dx, 10 + dy, 0, "", width=0.5, height=0.5))
    selected, skipped = select_plane_via_pads(pads, [PLANE])
    assert sorted(f"{p.ref}.{p.pin}" for p in selected) == ["U1.3", "U1.9"]
    assert set(skipped) == {"U1.17"}


def test_two_terminal_parts_and_through_hole_pads_are_not_served() -> None:
    pads = [
        _pad("C1", "1", 10.0, 10.0, 0, PLANE),
        _pad("C1", "2", 12.0, 10.0, 1, "VCC"),
        _pad("J1", "1", 20.0, 10.0, 0, PLANE, through_hole=True),
        _pad("J1", "2", 22.0, 10.0, 2, "A"),
        _pad("J1", "3", 24.0, 10.0, 3, "B"),
    ]
    selected, skipped = select_plane_via_pads(pads, [PLANE])
    assert selected == []
    assert skipped == {"C1.1": "footprint has 2 pad(s)"}


# ---------------------------------------------------------------------------
# 2. Exact-geometry planner on a 0.5 mm-pitch pin row.
# ---------------------------------------------------------------------------

# Board 04's router rules (``kct route --clearance 0.15``, 0.05 mm grid).
_ROUTER_RULES = DesignRules(
    grid_resolution=0.05, trace_width=0.2, trace_clearance=0.15, via_drill=0.3, via_diameter=0.6
)


def _lqfp_row_grid() -> RoutingGrid:
    """A right-hand LQFP pin row, 1.475 x 0.3 mm pins at 0.5 mm pitch.

    Pin 5 is the pour pin, flanked by signal pins; the left column makes the
    part a multi-pad footprint whose right row is its perimeter.
    """
    grid = RoutingGrid(width=20, height=20, rules=_ROUTER_RULES, layer_stack=LayerStack.two_layer())
    for i in range(9):
        y = 8.0 + 0.5 * i
        pin = str(i + 1)
        net, name = (0, PLANE) if pin == "5" else (i + 1, f"S{pin}")
        grid.add_pad(_pad("U2", pin, 10.0, y, net, name, width=1.475, height=0.3))
        grid.add_pad(_pad("U2", f"L{pin}", 4.0, y, 20 + i, f"L{pin}", width=1.475, height=0.3))
    return grid


def test_exact_mode_finds_a_site_beside_a_half_mm_pitch_pin() -> None:
    """The #5700 default treats each pin as a 0.76 mm disc: no site (board 04)."""
    legacy = reserve_plane_via_sites(_lqfp_row_grid(), [PLANE])
    assert legacy.unreserved == ["U2.5"]

    rules = PlaneViaSiteRules(clearance=0.2, exact_pads=True)
    grid = _lqfp_row_grid()
    result = reserve_plane_via_sites(grid, [PLANE], rules)
    assert result.unreserved == []
    (site,) = result.sites
    # Legal against every foreign pin's real copper box at the site clearance.
    for pad in grid._pads:
        if pad.net_name == PLANE:
            continue
        dx = max(abs(site.x - pad.x) - pad.width / 2, 0.0)
        dy = max(abs(site.y - pad.y) - pad.height / 2, 0.0)
        assert math.hypot(dx, dy) >= rules.via_diameter / 2 + rules.clearance - 1e-9, pad.pin
    # Outward, off the pin's own copper, within reach.
    assert site.x > 10.0 + 1.475 / 2
    assert math.hypot(site.x - 10.0, site.y - 10.0) <= rules.max_reach
    (stub,) = result.stubs
    assert (stub.pad, stub.site_x, stub.site_y) == ("U2.5", site.x, site.y)
    assert result.cells_blocked > 0


# ---------------------------------------------------------------------------
# 3. Lattice keep-out projection.
# ---------------------------------------------------------------------------


def test_keepout_areas_hold_via_plus_clearance_and_the_stub_layer() -> None:
    rules = PlaneViaSiteRules(clearance=0.2, exact_pads=True)
    result = reserve_plane_via_sites(_lqfp_row_grid(), [PLANE], rules)
    (site,) = result.sites
    areas = plane_via_keepout_areas(
        result,
        clearance=0.2,
        layer_index=LayerStack.two_layer().layer_enum_to_index,
        num_layers=2,
    )
    disc, stub = areas
    assert disc.layers == frozenset({0, 1}) and stub.layers == frozenset({0})
    assert all(a.blocks_tracks and a.blocks_vias for a in areas)
    mask = LatticeKeepoutMask(areas)
    reach = rules.via_diameter / 2 + 0.2
    for k in range(32):
        ang = 2 * math.pi * k / 32
        # Signal copper (half 0.1) whose edge would breach the clearance.
        inside = (site.x + (reach + 0.05) * math.cos(ang), site.y + (reach + 0.05) * math.sin(ang))
        assert mask.segment_blocked(inside, inside, 1, 7, 0.1)
        # ... and copper that keeps it is not refused by the disc.
        clear = (site.x + (reach + 0.2) * math.cos(ang), site.y + (reach + 0.2) * math.sin(ang))
        assert not disc_blocks(disc, clear, 0.1)


def disc_blocks(area: KeepoutArea, point: tuple[float, float], half: float) -> bool:
    return LatticeKeepoutMask([area]).segment_blocked(point, point, 1, 7, half)


# ---------------------------------------------------------------------------
# 4. Lattice engine: the signal detours, the pour pad keeps its site.
# ---------------------------------------------------------------------------

OUTLINE = [(0.0, 0.0), (24.0, 0.0), (24.0, 20.0), (0.0, 20.0)]
SIGNAL = 7


def _lattice_board() -> tuple[list[Pad], RoutingGrid]:
    """A 6-pin part with a GND pin on its right side, and a signal net whose
    straight run passes just outside that pin -- where its via site goes."""
    pads = [
        _pad("U1", "1", 6.0, 8.0, 1, "A", width=1.5, height=0.6),
        _pad("U1", "2", 6.0, 10.0, 2, "B", width=1.5, height=0.6),
        _pad("U1", "3", 6.0, 12.0, 3, "C", width=1.5, height=0.6),
        _pad("U1", "4", 10.0, 12.0, 4, "D", width=1.5, height=0.6),
        _pad("U1", "5", 10.0, 10.0, 0, PLANE, width=1.5, height=0.6),
        _pad("U1", "6", 10.0, 8.0, 6, "F", width=1.5, height=0.6),
        _pad("R1", "1", 11.4, 2.0, SIGNAL, "SIG"),
        _pad("R2", "1", 11.4, 18.0, SIGNAL, "SIG"),
    ]
    grid = RoutingGrid(width=24, height=20, rules=DesignRules(), layer_stack=LayerStack.two_layer())
    for pad in pads:
        grid.add_pad(pad)
    return pads, grid


def _route_signal(pads: list[Pad], keepouts: LatticeKeepoutMask | None) -> list[tuple]:
    pf = LatticePathfinder(OUTLINE, pads, DesignRules(), LayerStack.two_layer())
    pf.set_keepouts(keepouts)
    sig = [p for p in pads if p.net == SIGNAL]
    routes, stats = pf.route_netset([((SIGNAL, 0), sig[0], sig[1], None)], max_iterations=4)
    assert stats.routed == 1
    copper = []
    for route in routes.values():
        assert isinstance(route, Route)
        copper += [(s.layer, (s.x1, s.y1), (s.x2, s.y2), s.width / 2) for s in route.segments]
        copper += [("via", (v.x, v.y), (v.x, v.y), v.diameter / 2) for v in route.vias]
    return copper


def _site_gap(copper: list[tuple], x: float, y: float, via_r: float) -> float:
    """Smallest copper-edge gap between the routed copper and a via at (x, y)."""
    return min(seg_pt_dist(a, b, (x, y)) - half - via_r for _l, a, b, half in copper)


def test_lattice_signal_detours_around_the_reserved_site() -> None:
    pads, grid = _lattice_board()
    rules = PlaneViaSiteRules(clearance=0.2, exact_pads=True)
    served, _ = select_plane_via_pads(grid._pads, [PLANE])
    reservation = reserve_plane_via_sites(grid, [PLANE], rules, pads=served)
    (site,) = reservation.sites
    assert site.pads == ("U1.5",)
    via_r = rules.via_diameter / 2

    # Premise: unconstrained, the signal takes the site.
    baseline = _route_signal(pads, None)
    assert _site_gap(baseline, site.x, site.y, via_r) < rules.clearance

    areas = plane_via_keepout_areas(
        reservation,
        clearance=rules.clearance,
        layer_index=LayerStack.two_layer().layer_enum_to_index,
        num_layers=2,
    )
    routed = _route_signal(pads, LatticeKeepoutMask(areas))
    # The pour pad still has a legal via at its reserved site...
    assert _site_gap(routed, site.x, site.y, via_r) >= rules.clearance - 1e-9
    # ... and a legal stub to it on its own layer.
    for layer, a, b, half in routed:
        if layer == Layer.F_CU or layer == "via":
            stub_poly = areas[1].polygon
            assert not seg_near_polygon(a, b, stub_poly, half), (a, b)


# ---------------------------------------------------------------------------
# 5. Autorouter / kct route wiring.
# ---------------------------------------------------------------------------


def _fake_router(grid: RoutingGrid, class_clearance: float | None = None) -> SimpleNamespace:
    net_class = SimpleNamespace(clearance=class_clearance) if class_clearance else None
    return SimpleNamespace(
        grid=grid,
        rules=grid.rules,
        net_class_map={PLANE: net_class} if net_class else {},
        layer_stack=LayerStack.two_layer(),
    )


def test_router_entry_point_uses_the_strongest_clearance_and_stores_areas() -> None:
    router = _fake_router(_lqfp_row_grid(), class_clearance=0.22)
    result = reserve_router_plane_via_sites(
        router, [PLANE], via_diameter=0.6, via_drill=0.3, clearance=0.15
    )
    assert result.rules.clearance == 0.22  # the pour net's class clearance wins
    assert result.rules.exact_pads
    assert [s.pads for s in result.sites] == [("U2.5",)]
    assert len(router._plane_via_keepout_areas) == 2


def test_lattice_mask_merges_reserved_sites_with_rule_areas() -> None:
    from kicad_tools.router.core import Autorouter

    rule_area = KeepoutArea(
        polygon=((0, 0), (1, 0), (1, 1)),
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=False,
    )
    site_area = KeepoutArea(
        polygon=((5, 5), (6, 5), (6, 6)),
        layers=frozenset({0, 1}),
        blocks_tracks=True,
        blocks_vias=True,
    )
    fake = SimpleNamespace(_lattice_keepout_projection=lambda: LatticeKeepoutMask([rule_area]))
    # No reservation: the rule-area mask is returned unchanged.
    assert Autorouter._lattice_keepouts_with_plane_via_sites(fake).areas == (rule_area,)
    fake._plane_via_keepout_areas = (site_area,)
    assert Autorouter._lattice_keepouts_with_plane_via_sites(fake).areas == (rule_area, site_area)
    # A board with no rule areas still gets its reservation.
    fake._lattice_keepout_projection = lambda: None
    assert Autorouter._lattice_keepouts_with_plane_via_sites(fake).areas == (site_area,)


def test_apply_reserves_only_zoned_auto_skipped_nets(tmp_path) -> None:
    from kicad_tools.cli.route_cmd import _apply_plane_via_reservation

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    calls: list[list[str]] = []

    def fake_reserve(router, nets, **_kw):
        calls.append(list(nets))
        return SimpleNamespace(sites=[], unreserved=[])

    args = Namespace(_auto_skipped_net_names=["GND", "MOTOR"])  # MOTOR: route_via="manual"
    with (
        patch("kicad_tools.cli.stitch_cmd.find_all_plane_nets", return_value=["GND", "+5V"]),
        patch("kicad_tools.core.sexp_file.load_pcb", return_value=None),
        patch("kicad_tools.router.plane_via_sites.reserve_router_plane_via_sites", fake_reserve),
    ):
        _apply_plane_via_reservation(object(), args, pcb, quiet=True)
        assert calls == [["GND"]]
        _apply_plane_via_reservation(
            object(),
            Namespace(_auto_skipped_net_names=["GND"], no_plane_via_reservation=True),
            pcb,
            quiet=True,
        )
        _apply_plane_via_reservation(object(), Namespace(), pcb, quiet=True)
    assert calls == [["GND"]]


def test_opt_out_flag_survives_main_cli_dispatch(tmp_path) -> None:
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)")
    args = create_parser().parse_args(["route", str(path), "--no-plane-via-reservation"])
    with patch("kicad_tools.cli.route_cmd.main", return_value=0) as main:
        run_route_command(args)
    assert "--no-plane-via-reservation" in main.call_args.args[0]


# ---------------------------------------------------------------------------
# 6. Oracle completion uses the reserved site.
# ---------------------------------------------------------------------------


def _pin_row_obstacles():
    """A 0.5 mm-pitch pin row (1.475 x 0.3 mm pins) around the pour pin at y=10."""
    from kicad_tools.cli.stitch_cmd import PadInfo

    pour = PadInfo("U2", "5", 0, PLANE, 10.0, 10.0, "F.Cu", 1.475, 0.3)
    circles, boxes = [], []
    for i in (-2, -1, 1, 2):
        y = 10.0 + 0.5 * i
        circles.append((10.0, y, math.hypot(1.475, 0.3) / 2, 10 + i))
        boxes.append((10.0 - 0.7375, y - 0.15, 10.0 + 0.7375, y + 0.15, 10 + i))
    return pour, circles, boxes


def test_stitcher_tries_the_reserved_site_against_pad_boxes() -> None:
    from kicad_tools.cli.stitch_cmd import calculate_via_position

    pour, circles, boxes = _pin_row_obstacles()
    kwargs: dict = {
        "offset": 0.5,
        "via_size": 0.6,
        "existing_vias": [],
        "clearance": 0.2,
        "other_net_pads": circles,
        "other_net_pad_bboxes": boxes,
        "trace_width": 0.2,
    }
    site = (11.3, 10.0)  # 0.56 mm from the next pins' copper: legal
    assert calculate_via_position(pour, **kwargs, preferred_positions=[site]) == site
    # The historical offset search never lands there (it does not try it).
    assert calculate_via_position(pour, **kwargs) != site
    # An illegal reserved site is not forced: the search falls through.
    too_close = (10.9, 10.0)
    assert calculate_via_position(pour, **kwargs, preferred_positions=[too_close]) != too_close


def test_reserved_sites_reach_the_oracle_weld(tmp_path) -> None:
    from kicad_tools.cli.route_cmd import _reserved_plane_via_sites
    from kicad_tools.router.oracle_completion import PourLinkCloser
    from kicad_tools.router.plane_via_sites import PlaneViaReservation, PlaneViaSite

    reservation = PlaneViaReservation(
        sites=[
            PlaneViaSite(PLANE, 1.0, 2.0, ("U1.3", "U1.4")),
            PlaneViaSite(PLANE, 5.0, 6.0, ("U2.1",)),
        ]
    )
    sites = _reserved_plane_via_sites(Namespace(_plane_via_reservation=reservation))
    assert sites == {"U1.3": [(1.0, 2.0)], "U1.4": [(1.0, 2.0)], "U2.1": [(5.0, 6.0)]}
    assert _reserved_plane_via_sites(Namespace()) is None

    seen: dict = {}

    def fake_run_stitch(*_a, **kw):
        seen.update(kw)
        return SimpleNamespace(
            vias_added=[], already_connected=False, pads_skipped=[], traces_added=[]
        )

    closer = PourLinkCloser(
        via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2, preferred_sites=sites
    )
    with patch("kicad_tools.cli.stitch_cmd.run_stitch", fake_run_stitch):
        closer._weld_pads(tmp_path / "b.kicad_pcb", {PLANE: {"U1.3"}}, SimpleNamespace(notes=[]))
    assert seen["preferred_sites"] == sites

"""Plane-zone crossing cost, report and hard refusal (Issue #9597).

Fixture board: a 4-layer stack (``LayerStack.four_layer_sig_gnd_pwr_sig``)
whose ``In2.Cu`` is a ``PLANE`` layer carrying a ``+3V3`` zone, with the
``SIG`` pads also on ``In2.Cu`` on either side of it.  ``SIG``'s net class
hard-avoids every other layer (``--strict-layers`` semantics), so the only
possible route crosses the power island:

* default -- the route is committed (the zone is a soft cost, never a
  block) and the post-route report names the crossing;
* ``--protect-plane-zones`` -- the commit is refused, naming the zone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layer_advisories import plane_zone_crossings
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules, NetClassRouting

_STACK = LayerStack.four_layer_sig_gnd_pwr_sig()
_RULES = DesignRules(cost_via=100000.0)
_NEGOTIATED_KWARGS = {"max_iterations": 2, "per_net_timeout": 10.0, "timeout": 60.0}


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 1 1) '
        f'(layers "In2.Cu") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: str, pads: str) -> str:
    return f"""  (footprint "Test:P" (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid}")
    (at 0 0)
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (attr smd)
    {pads}
  )
"""


def _board_text() -> str:
    # +3V3 anchors above and below the SIG corridor, both inside the zone;
    # a SIG track across the middle splits the pour into two pad-bearing
    # pieces.
    pwr = _fp("PWR1", "a1", _pad("1", 20.0, 5.0, 1, "+3V3")) + _fp(
        "PWR2", "a2", _pad("1", 20.0, 15.0, 1, "+3V3")
    )
    sig = _fp("S1", "b1", _pad("1", 6.0, 10.0, 2, "SIG")) + _fp(
        "S2", "b2", _pad("1", 34.0, 10.0, 2, "SIG")
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 40 20)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{pwr}
{sig}
  (zone (net 1) (net_name "+3V3") (layer "In2.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 16 0) (xy 24 0) (xy 24 20) (xy 16 20)))
  )
)
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "plane_zone.kicad_pcb"
    pcb.write_text(_board_text())
    return pcb


def _load(pcb: Path, *, force_python: bool = False):
    router, _ = load_pcb_for_routing(
        str(pcb),
        strategy="grid",
        rules=DesignRules(cost_via=100000.0),
        layer_stack=_STACK,
        skip_nets=["+3V3"],
        force_python=force_python,
    )
    # SIG may only route on In2.Cu (its pads' layer): hard-avoid the rest,
    # the in-process equivalent of --strict-layers with an avoid list.
    router.rules.strict_layers = True
    router.net_class_map["SIG"] = NetClassRouting(name="SIG", avoid_layers=[0, 1, 3])
    return router


def _sig_id(router) -> int:
    return next(n for n, nm in router.net_names.items() if nm == "SIG")


def _sig_routes(router):
    sig = _sig_id(router)
    return [r for r in router.routes if r.net == sig]


def _in2_index(router) -> int:
    return router.grid.layer_to_index(Layer.from_kicad_name("In2.Cu").value)


def _zone_cell(router) -> tuple[int, int]:
    layer = _in2_index(router)
    import numpy as np

    ys, xs = np.nonzero(router.grid._is_zone[layer])
    assert len(xs) > 0, "no zone cells marked on In2.Cu"
    return int(xs[0]), int(ys[0])


class TestPlaneZoneLoading:
    def test_outlines_parsed_for_plane_layers_only(self, board: Path) -> None:
        router = _load(board)
        assert len(router.plane_zones) == 1
        outline = router.plane_zones[0]
        assert outline.net_name == "+3V3"
        assert outline.layer_name == "In2.Cu"
        assert outline.net_number == 1

    def test_zone_cells_marked_on_grid(self, board: Path) -> None:
        router = _load(board)
        gx, gy = _zone_cell(router)
        layer = _in2_index(router)
        assert router.grid.cell_at(layer, gy, gx).is_zone
        # Foreign zone cells carry the zone's net; the paired C++ grid gets
        # the same bulk marks (the verb exists on the v71 binding surface).
        assert router.grid._net[layer, gy, gx] == 1
        cpp_grid = getattr(router.grid, "_cpp_grid", None)
        impl = getattr(cpp_grid, "_impl", None) if cpp_grid is not None else None
        if impl is not None:
            assert hasattr(impl, "mark_zone_cells")
            impl.clear_zone_cells()
            impl.mark_zone_cells([gx], [gy], [layer], [1])

    def test_foreign_plane_zone_is_costed_not_blocked(self, board: Path) -> None:
        router = _load(board, force_python=True)
        gx, gy = _zone_cell(router)
        layer = _in2_index(router)
        pf = router.router
        assert pf._is_zone_blocked(gx, gy, layer, _sig_id(router)) is False
        assert pf._get_zone_cost(gx, gy, layer, _sig_id(router)) == pytest.approx(
            router.rules.cost_zone_foreign_plane
        )

    def test_via_piercing_plane_zone_is_allowed(self, board: Path) -> None:
        # Regression guard for the carve-out: blocking vias through foreign
        # plane zones would refuse nearly every via on a 4-layer board.
        router = _load(board, force_python=True)
        gx, gy = _zone_cell(router)
        assert router.router._can_place_via_in_zones(gx, gy, _sig_id(router)) is True

    def test_plane_free_stack_marks_nothing(self, board: Path) -> None:
        from kicad_tools.router.layers import LayerStack as LS

        router, _ = load_pcb_for_routing(
            str(board),
            strategy="grid",
            rules=DesignRules(cost_via=100000.0),
            layer_stack=LS.four_layer_all_signal(),
            skip_nets=["+3V3"],
        )
        assert router.plane_zones == ()
        import numpy as np

        assert not bool(np.any(router.grid._is_zone))


class TestDefaultCostAndReport:
    def test_route_crosses_power_island_and_is_reported(self, board: Path) -> None:
        router = _load(board)
        router.route_all_negotiated(**_NEGOTIATED_KWARGS)
        sig = _sig_routes(router)
        assert sig, "SIG must route (the zone is a cost, never a block)"
        crossings = plane_zone_crossings(router.routes, router.plane_zones)
        assert [(c.net_name, c.zone_net_name, c.layer_name) for c in crossings] == [
            ("SIG", "+3V3", "In2.Cu")
        ]
        assert "crosses zone +3V3 on In2.Cu" in crossings[0].message

    def test_report_is_empty_for_same_net_copper(self, board: Path) -> None:
        router = _load(board)
        router.route_all_negotiated(**_NEGOTIATED_KWARGS)
        # +3V3's own (skipped) copper never appears in routes; only SIG does.
        for crossing in plane_zone_crossings(router.routes, router.plane_zones):
            assert crossing.net_name != "+3V3"

    def test_python_and_cpp_backends_agree(self, board: Path) -> None:
        pytest.importorskip("kicad_tools.router.router_cpp")
        results = {}
        for backend, force_python in (("cpp", False), ("py", True)):
            router = _load(board, force_python=force_python)
            router.route_all_negotiated(**_NEGOTIATED_KWARGS)
            results[backend] = (
                bool(_sig_routes(router)),
                [
                    (c.net_name, c.zone_net_name, c.layer_name)
                    for c in plane_zone_crossings(router.routes, router.plane_zones)
                ],
            )
        # Both backends commit the crossing (cost, not block) and both
        # reports see it identically.
        assert results["cpp"] == results["py"]
        assert results["py"][0] is True


class TestHardRefusal:
    def test_route_refused_and_zone_named(self, board: Path) -> None:
        router = _load(board)
        router.protect_plane_zones = True
        router.route_all_negotiated(**_NEGOTIATED_KWARGS)
        assert not _sig_routes(router), "the crossing commit must be refused"
        assert router.plane_zone_refusals, "the refusal must be recorded"
        refusal = router.plane_zone_refusals[0]
        assert "SIG" in refusal
        assert "+3V3" in refusal and "In2.Cu" in refusal
        assert "2 pad-bearing pieces" in refusal

    def test_clean_commits_are_untouched(self, board: Path) -> None:
        # A route that does not cross any zone commits normally under the
        # flag: hard-avoid In2 too and route on F.Cu instead.
        router = _load(board)
        router.rules.strict_layers = True
        router.net_class_map["SIG"] = NetClassRouting(name="SIG", avoid_layers=[1, 2, 3])
        # SIG pads live on In2.Cu, so re-point them by routing another net:
        # reuse the board but route nothing across the zone -- the veto
        # simply passes when no candidate touches a zone.
        router.protect_plane_zones = True
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment

        seg = Segment(x1=5.0, y1=1.0, x2=10.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=2)
        assert router._mark_route(Route(net=2, net_name="SIG", segments=[seg])) is True
        assert router.plane_zone_refusals == []


class TestSharedPieceHelper:
    def test_zone_pieces_counts_a_split(self, board: Path) -> None:
        from shapely.geometry import LineString, Polygon

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.plane_split import (
            _Item,
            collect_copper_items,
            zone_pieces,
        )

        pcb = PCB.load(str(board))
        items = collect_copper_items(pcb, ["In2.Cu"], lambda n, name: n)
        outline = Polygon([(16, 0), (24, 0), (24, 20), (16, 20)])
        crossing = _Item(
            "track", LineString([(6, 10), (34, 10)]).buffer(0.1), 2, "SIG", "route:SIG"
        )
        before = zone_pieces(outline, 1, items["In2.Cu"], [], 0.2)
        after = zone_pieces(outline, 1, [*items["In2.Cu"], crossing], [], 0.2)
        assert len(before) == 1
        assert len(after) == 2


class TestCliWiring:
    def test_flag_parses_default_off(self) -> None:
        from kicad_tools.cli.route_cmd import _route_parser

        args = _route_parser().parse_args(["board.kicad_pcb"])
        assert args.protect_plane_zones is False

    def test_audit_prints_crossing_and_refusal(self, board: Path, capsys) -> None:
        from kicad_tools.cli import route_cmd

        router = _load(board)
        # One committed crossing segment plus one recorded refusal.
        from kicad_tools.router.primitives import Route, Segment

        seg = Segment(x1=6.0, y1=10.0, x2=34.0, y2=10.0, width=0.2, layer=Layer.IN2_CU, net=2)
        router.routes.append(Route(net=2, net_name="SIG", segments=[seg]))
        router.plane_zone_refusals.append(
            "net SIG: committing this route would split zone [+3V3] on In2.Cu "
            "into 2 pad-bearing pieces (was 1); refusing the commit"
        )
        found = route_cmd._audit_plane_zone_crossings(router, _STACK)
        out = capsys.readouterr().err
        assert len(found) == 1
        assert "crosses zone +3V3 on In2.Cu" in out
        assert "route refused: net SIG" in out and "[+3V3]" in out

"""Issue #3545: static foreign-pad halo cells must survive rip-up.

Defect chain (routing-diagnostic fixture, surfaced by PR #3537's
occupancy re-roll):

1. ``RoutingGrid._unmark_segment`` / ``_unmark_via`` (and the C++
   ``Grid3D::unmark_segment`` / ``unmark_via``) erased STATICALLY
   blocked pad clearance-halo cells outright (``blocked=False, net=0``)
   whenever a ripped-up route's clearance envelope swept them.  After
   the erasure, foreign nets routed straight through the pad's
   clearance band.
2. The validator's same-component-ref carve-out was net-blind, so the
   resulting sub-clearance copper (NET3 at 0.127mm from foreign-net J1
   pad 1) was masked in-loop and only exact KiCad DRC caught it.
3. Final acceptance had no backstop, so the route shipped.

These tests pin the three fixes:

* rip-up restores static halo cells (Python grid + C++ grid parity),
* the same-component carve-out is net-aware (only active where a
  fine-pitch or relaxed-corridor clearance relaxation actually applies),
* ``Autorouter._demote_pad_clearance_violation_nets`` demotes nets whose
  committed copper genuinely violates foreign-pad clearance beyond
  nudge reach -- never committing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router import DesignRules, load_pcb_for_routing
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment

FIXTURE = Path(__file__).parent / "fixtures" / "routing-diagnostic.kicad_pcb"


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.2,
    )


@pytest.fixture
def grid(rules: DesignRules) -> RoutingGrid:
    # 20mm x 20mm board at 0.2mm resolution.
    return RoutingGrid(100, 100, rules, origin_x=0.0, origin_y=0.0)


def _tht_pad(x: float, y: float, net: int, net_name: str, ref: str, pin: str) -> Pad:
    """1.7mm circular THT pad, mirroring J1 on the routing-diagnostic fixture."""
    return Pad(
        x=x,
        y=y,
        width=1.7,
        height=1.7,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
        through_hole=True,
        drill=1.0,
    )


class TestStaticHaloRipupRestore:
    """Rip-up must restore static halo cells, not erase them."""

    def test_python_ripup_restores_static_halo(self, grid: RoutingGrid, rules: DesignRules) -> None:
        """Mark + unmark a same-net route over a pad halo; halo survives."""
        pad = _tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1")
        grid.add_pad(pad)

        # Halo cell west of the pad (the fixture's violating corridor).
        gx, gy = grid.world_to_grid(7.0, 11.4)
        layer = 0
        assert bool(grid._blocked[layer, gy, gx]) is True
        assert int(grid._net[layer, gy, gx]) == 1

        # A NET1 route passing through its own pad's halo (legal).
        route = Route(net=1, net_name="NET1")
        route.segments.append(
            Segment(x1=7.0, y1=10.0, x2=7.0, y2=12.0, width=0.2, layer=Layer.F_CU, net=1)
        )
        grid.mark_route(route)
        grid.unmark_route(route)

        # Pre-#3545 the unmark erased the halo (blocked=False, net=0),
        # opening the corridor to foreign nets.  Post-fix the static
        # blockage is restored.
        assert bool(grid._blocked[layer, gy, gx]) is True, (
            "static pad halo cell was erased by rip-up"
        )
        assert int(grid._net[layer, gy, gx]) == 1
        # Foreign nets must still see the cell as blocked.
        assert grid.is_blocked_for_net(gx, gy, layer, net=3) is True

    def test_python_ripup_still_frees_route_only_cells(
        self, grid: RoutingGrid, rules: DesignRules
    ) -> None:
        """Cells blocked ONLY by route copper are freed by rip-up."""
        route = Route(net=5, net_name="NET5")
        route.segments.append(
            Segment(x1=4.0, y1=4.0, x2=4.0, y2=8.0, width=0.2, layer=Layer.F_CU, net=5)
        )
        grid.mark_route(route)
        gx, gy = grid.world_to_grid(4.0, 6.0)
        assert bool(grid._blocked[0, gy, gx]) is True
        grid.unmark_route(route)
        assert bool(grid._blocked[0, gy, gx]) is False
        assert int(grid._net[0, gy, gx]) == 0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
    def test_cpp_ripup_restores_static_halo(self) -> None:
        """C++ Grid3D parity: unmark_segment restores static cells."""
        from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

        g = router_cpp.Grid3D(100, 100, 2, 0.2, 0.0, 0.0)
        # Static halo cell (net 1, clearance halo: not pad metal).
        g.mark_blocked(35, 57, 0, 1, True, False)
        cell = g.at(35, 57, 0)
        assert cell.blocked is True
        assert cell.static_blocked is True
        assert cell.original_net == 1

        # Mark + rip a same-net route segment sweeping the halo cell.
        g.mark_segment(35, 50, 35, 60, 0, 1, 2)
        g.unmark_segment(35, 50, 35, 60, 0, 1, 2)

        cell = g.at(35, 57, 0)
        assert cell.blocked is True, "C++ rip-up erased a static halo cell"
        assert cell.net == 1

        # Route-only neighbour cells ARE freed.
        cell2 = g.at(35, 50, 0)
        assert cell2.blocked is False
        assert cell2.net == 0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
    def test_cpp_ripup_restores_static_halo_via(self) -> None:
        """C++ Grid3D parity: unmark_via restores static cells."""
        from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

        g = router_cpp.Grid3D(100, 100, 2, 0.2, 0.0, 0.0)
        g.mark_blocked(40, 40, 0, 7, True, False)
        g.mark_via(40, 41, 7, 3)
        g.unmark_via(40, 41, 7, 3)
        cell = g.at(40, 40, 0)
        assert cell.blocked is True
        assert cell.net == 7


class TestNetAwareSameComponentCarveout:
    """The validator carve-out must not exempt foreign-net pads on
    standard-pitch components."""

    def test_foreign_net_same_component_pad_violation_detected(
        self, grid: RoutingGrid, rules: DesignRules
    ) -> None:
        """The fixture geometry: NET3 segment 0.127mm from J1-1 (NET1)."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        grid.add_pad(_tht_pad(8.0, 14.54, net=3, net_name="NET3", ref="J1", pin="2"))

        seg = Segment(x1=7.0, y1=11.0, x2=7.0, y2=11.6, width=0.2, layer=Layer.F_CU, net=3)

        # Pre-#3545: exclude_refs={"J1"} silently exempted J1-1 (foreign
        # net) and the validator reported the segment as clean.
        is_valid, clearance, _loc = grid.validate_segment_clearance(
            seg, exclude_net=3, exclude_refs={"J1"}
        )
        assert is_valid is False, (
            "net-blind same-component carve-out masked a foreign-net "
            f"sub-clearance violation (clearance={clearance:.3f}mm)"
        )
        # Geometry: 1.077 center distance - 0.85 pad radius - 0.1 half
        # trace = 0.127mm < 0.200mm required.
        assert clearance == pytest.approx(0.127, abs=0.01)

    def test_explicit_component_override_keeps_carveout(self, rules: DesignRules) -> None:
        """Fine-pitch-style explicit override keeps the #1764 carve-out."""
        rules.component_clearances = {"J1": 0.05}
        grid = RoutingGrid(100, 100, rules, origin_x=0.0, origin_y=0.0)
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        grid.add_pad(_tht_pad(8.0, 14.54, net=3, net_name="NET3", ref="J1", pin="2"))

        seg = Segment(x1=7.0, y1=11.0, x2=7.0, y2=11.6, width=0.2, layer=Layer.F_CU, net=3)
        # 0.127mm actual >= 0.05mm required AND the relaxation is in
        # effect, so the carve-out applies and the segment validates.
        is_valid, _clearance, _loc = grid.validate_segment_clearance(
            seg, exclude_net=3, exclude_refs={"J1"}
        )
        assert is_valid is True

    def test_relaxed_corridor_component_keeps_carveout(
        self, grid: RoutingGrid, rules: DesignRules
    ) -> None:
        """Components relaxed by #2452 keep the carve-out."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        grid.add_pad(_tht_pad(8.0, 14.54, net=3, net_name="NET3", ref="J1", pin="2"))
        # Simulate the #2452 corridor relaxation bookkeeping.
        grid._relaxed_clearance_refs.add("J1")

        seg = Segment(x1=7.0, y1=11.0, x2=7.0, y2=11.6, width=0.2, layer=Layer.F_CU, net=3)
        is_valid, _clearance, _loc = grid.validate_segment_clearance(
            seg, exclude_net=3, exclude_refs={"J1"}
        )
        assert is_valid is True

    def test_metal_overlap_never_carved_out(self, grid: RoutingGrid) -> None:
        """#2933 invariant: negative clearance is flagged regardless."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        grid.add_pad(_tht_pad(8.0, 14.54, net=3, net_name="NET3", ref="J1", pin="2"))
        grid._relaxed_clearance_refs.add("J1")

        # Segment centerline crossing J1-1's pad metal.
        seg = Segment(x1=7.5, y1=11.0, x2=7.5, y2=13.0, width=0.2, layer=Layer.F_CU, net=3)
        is_valid, clearance, _loc = grid.validate_segment_clearance(
            seg, exclude_net=3, exclude_refs={"J1"}
        )
        assert is_valid is False
        assert clearance < 0


class TestPadClearanceDemotionBackstop:
    """Finalization backstop: severe foreign-pad violations are demoted,
    never committed."""

    def test_forced_route_through_static_halo_is_demoted(self, rules: DesignRules) -> None:
        """A route forced across a foreign pad's halo is stripped."""
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)
        # NET3 copper slicing through J1 pad 1 (NET1, at 8.0/12.0):
        # clearance = 0.6 - 0.85 - 0.1 = -0.35mm => deficit 0.55mm,
        # far beyond the 0.2mm grid-resolution nudge reach.
        bad = Route(net=3, net_name="NET3")
        bad.segments.append(
            Segment(x1=7.4, y1=10.0, x2=7.4, y2=13.0, width=0.2, layer=Layer.F_CU, net=3)
        )
        router.routes.append(bad)
        net_routes = {3: [bad]}

        demoted = router._demote_pad_clearance_violation_nets(net_routes)

        assert demoted == [3]
        assert net_routes[3] == []
        assert bad not in router.routes

    def test_clean_route_is_not_demoted(self, rules: DesignRules) -> None:
        """A legally placed route survives the backstop untouched."""
        router, _ = load_pcb_for_routing(str(FIXTURE), rules=rules, validate_drc=False)
        # Vertical NET3 run at x=6.0: 2.0mm from J1-1's center, well
        # clear of every pad at full clearance.
        good = Route(net=3, net_name="NET3")
        good.segments.append(
            Segment(x1=6.0, y1=10.0, x2=6.0, y2=11.0, width=0.2, layer=Layer.F_CU, net=3)
        )
        router.routes.append(good)
        net_routes = {3: [good]}

        demoted = router._demote_pad_clearance_violation_nets(net_routes)

        assert demoted == []
        assert net_routes[3] == [good]
        assert good in router.routes

    def test_worst_segment_pad_deficit_geometry(self, grid: RoutingGrid) -> None:
        """Exact-geometry deficit matches the fixture's 0.073mm gap."""
        grid.add_pad(_tht_pad(8.0, 12.0, net=1, net_name="NET1", ref="J1", pin="1"))
        seg = Segment(x1=7.0, y1=11.0, x2=7.0, y2=11.6, width=0.2, layer=Layer.F_CU, net=3)
        deficit, loc = grid.worst_segment_pad_deficit(seg, exclude_net=3)
        assert deficit == pytest.approx(0.2 - 0.127, abs=0.01)
        assert loc == (8.0, 12.0)

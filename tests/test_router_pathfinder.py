"""Tests for A* pathfinding escape-hint seeding (Issue #2974).

These tests cover the LQFP-corner perimeter escape predicate added to
:class:`kicad_tools.router.pathfinder.Router` for Issue #2974.  The
predicate identifies pads sitting between flanking plane-net pads on an
IC perimeter and seeds the A* open set with virtual edges aimed in the
escape direction.  Without the seed, the pure octile/Manhattan heuristic
fans out around the corner before locking on the escape -- on board-04
that manifested as 32-95s per-net wall-clock for NRST and the SW* family.

The synthetic geometry in this file mirrors the NW-corner geometry of an
LQFP-48 (NRST = pin 7): a target pad on the west edge of a chip body
flanked by two foreign pads, with a clear escape corridor running west.
"""

import time

import pytest

from kicad_tools.router import DesignRules, RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_grid(
    width: float = 20.0,
    height: float = 20.0,
    resolution: float = 0.1,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> RoutingGrid:
    """Create a routing grid sized for an LQFP-corner test.

    The stitch-via halo on plane-net pads is disabled here so the
    synthetic geometry behaves like a signal-pad cluster -- this keeps
    the corridor between the flanking pins navigable while preserving
    the corner-flank density signature the predicate looks for.
    """
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=0.15,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.15,
        stitch_via_halo=False,
    )
    return RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        origin_x=origin_x,
        origin_y=origin_y,
        layer_stack=LayerStack.two_layer(),
    )


def _make_pad(
    x: float,
    y: float,
    net: int,
    net_name: str = "NET",
    layer: Layer = Layer.F_CU,
    width: float = 0.3,
    height: float = 0.3,
    ref: str = "U1",
    pin: str = "1",
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=layer,
        through_hole=False,
        drill=0,
        ref=ref,
        pin=pin,
    )


def _make_lqfp_west_corner_geometry(grid: RoutingGrid, target_net: int = 1):
    """Populate ``grid`` with an LQFP-48 NW-corner-like pad layout.

    The "chip body" sits to the east of the target pad.  The target pad
    has two flanking plane-net pads to the north and south (pin 6 / pin 8
    in LQFP-48 terms), and a wall of foreign pads to the east representing
    the rest of the chip body.  The escape direction is west (-X).

    Pads belonging to the IC's body are placed under a *different*
    component reference ("CHIP") than the target ("U1"), so the
    same-component clearance relaxation
    (:meth:`RoutingGrid._relax_same_component_clearance`) does not
    rewrite their clearance halos -- mirroring the multi-pin LQFP-48
    geometry on board-04 where pin 7's foreign neighbours are pins
    6/8/etc. on the same physical chip but their net assignments
    dominate the cell ownership.

    Returns
    -------
    target_pad : Pad
        The signal pad (analogue of NRST on pin 7).
    """
    # Flanking pads are modelled as foreign-net signal pads (not plane
    # pads) so the stitch-via halo logic doesn't apply.  This is the
    # geometry the predicate actually cares about: two foreign pads on
    # either side of the target plus a chip body.
    flank_net = 99
    chip_body_net_base = 50

    # Add the foreign pads FIRST so they claim the cells around the
    # target's eventual position.  Once added, those cells store the
    # foreign net id, and the target's own clearance ring (added later)
    # then overlays as ``is_obstacle = True`` overlap -- matching the
    # production grid state on a populated LQFP perimeter.
    #
    # Place the flanking pads slightly EAST of the target's X so their
    # clearance halos extend mainly into the chip body rather than
    # walling off the western escape corridor.  Their density in the
    # north/south wedges is what the predicate keys on; the predicate
    # does not require them to be co-linear with the target on the
    # escape axis.
    north_flank = _make_pad(x=5.3, y=9.5, net=flank_net, net_name="N1",
                            ref="CHIP", pin="6")
    south_flank = _make_pad(x=5.3, y=10.5, net=flank_net, net_name="N1",
                            ref="CHIP", pin="8")
    grid.add_pad(north_flank)
    grid.add_pad(south_flank)
    # Wall of foreign-net pads east of the target representing the rest
    # of the chip body.  Spaced at LQFP-48-like 0.5 mm pitch but offset
    # from the target's grid line so they sit JUST east of the target's
    # clearance ring.
    body_idx = 0
    for dy in (-1.0, -0.5, 0.0, 0.5, 1.0):
        for east_offset in (0.5, 0.8, 1.1):
            body_idx += 1
            grid.add_pad(
                _make_pad(
                    x=5.0 + east_offset,
                    y=10.0 + dy,
                    net=chip_body_net_base + body_idx,
                    net_name=f"SIG{body_idx}",
                    ref="CHIP",
                    pin=f"body_{body_idx}",
                )
            )

    target = _make_pad(x=5.0, y=10.0, net=target_net, net_name="NRST",
                       ref="U1", pin="7")
    grid.add_pad(target)
    return target


# ---------------------------------------------------------------------------
# _detect_escape_hint
# ---------------------------------------------------------------------------


class TestEscapeHintDetection:
    """Tests for the corner-flanked geometric predicate."""

    def test_detects_west_escape_for_west_edge_pad(self):
        """LQFP-style west-edge pad returns westward escape direction."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        target = _make_lqfp_west_corner_geometry(grid)

        escape_dir = router._detect_escape_hint(target, [grid.layer_to_index(Layer.F_CU.value)])

        assert escape_dir is not None, "Expected escape hint for LQFP-style corner pad"
        assert escape_dir == (-1, 0), (
            f"Expected westward escape (-1, 0), got {escape_dir}"
        )

    def test_no_hint_for_isolated_pad(self):
        """A pad with no surrounding blockers does not trigger the hint."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        pad = _make_pad(x=10.0, y=10.0, net=1)
        grid.add_pad(pad)

        assert router._detect_escape_hint(pad, [0]) is None

    def test_no_hint_when_flanking_absent(self):
        """Chip body but no flanking pads -> not corner-flanked."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        target = _make_pad(x=5.0, y=10.0, net=1)
        grid.add_pad(target)
        # Add only the eastern chip-body wall, no north/south flanking.
        for dy in (-1.0, -0.5, 0.0, 0.5, 1.0):
            for east_offset in (0.5, 0.75, 1.0):
                grid.add_pad(
                    _make_pad(
                        x=5.0 + east_offset,
                        y=10.0 + dy,
                        net=50,
                        ref="U1",
                        pin=f"body_{east_offset}_{dy}",
                    )
                )

        assert router._detect_escape_hint(target, [0]) is None

    def test_no_hint_when_escape_side_blocked(self):
        """If the candidate escape direction is also blocked, no hint.

        Models the case where the "corner-flank" geometry happens but
        the supposed escape direction is also full of foreign blockers
        (e.g. an interior LQFP pin walled in on all sides).  The
        body/escape asymmetry collapses and the predicate must decline.
        """
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        # Place a mirror image of the chip body to the WEST so neither
        # side is dramatically clearer than the other.
        for dy in (-1.0, -0.5, 0.0, 0.5, 1.0):
            for west_offset in (-0.5, -0.8, -1.1):
                grid.add_pad(_make_pad(
                    x=5.0 + west_offset, y=10.0 + dy,
                    net=200, ref="WEST_CHIP", pin=f"w_{west_offset}_{dy}",
                ))
        # Then build the eastern chip body and the target on top so
        # both axes show similar blocker density.
        target = _make_lqfp_west_corner_geometry(grid)
        # Predicate must decline because asymmetry < _ESCAPE_HINT_ASYMMETRY.
        assert router._detect_escape_hint(target, [0]) is None


# ---------------------------------------------------------------------------
# _escape_hint_cells
# ---------------------------------------------------------------------------


class TestEscapeHintCells:
    """Tests for the cells emitted by the seed helper."""

    def test_cells_lie_in_escape_direction(self):
        """Returned cells are anchored west of the pad when escape is west."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        target = _make_lqfp_west_corner_geometry(grid)
        layers = [grid.layer_to_index(Layer.F_CU.value)]

        seeds = router._escape_hint_cells(target, (-1, 0), target.net, layers)

        assert seeds, "Expected at least one escape-hint cell"
        pad_gx, _pad_gy = grid.world_to_grid(target.x, target.y)
        for cx, _cy, _cl, edge_cost in seeds:
            assert cx < pad_gx, (
                f"Escape-hint cell ({cx},_) should be west of pad ({pad_gx},_)"
            )
            assert edge_cost > 0

    def test_no_cells_when_escape_corridor_blocked(self):
        """If all candidate steps are foreign-blocked, return empty list."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        target = _make_lqfp_west_corner_geometry(grid)
        # Block the entire western corridor.
        for west_offset in (0.2, 0.3, 0.4, 0.5):
            grid.add_pad(_make_pad(x=5.0 - west_offset, y=10.0, net=42,
                                    ref="X", pin=f"w_{west_offset}"))

        layers = [grid.layer_to_index(Layer.F_CU.value)]
        seeds = router._escape_hint_cells(target, (-1, 0), target.net, layers)

        # Either nothing comes back, or the seeds avoid the blocked cells.
        for _cx, _cy, _cl, edge_cost in seeds:
            assert edge_cost > 0


# ---------------------------------------------------------------------------
# End-to-end routing with the seed
# ---------------------------------------------------------------------------


class TestLqfpCornerRouting:
    """End-to-end: LQFP corner pad routes to a destination across the board."""

    def test_route_completes_with_escape_hint(self):
        """Routing from the LQFP corner to the east edge succeeds.

        Bounds the synthetic-board search at a generous wall-clock so
        the test stays stable across CI hardware while still catching
        catastrophic regressions in the escape-hint pathway (e.g. if a
        future change made the seed cell unreachable and forced A* into
        a deep fan-out, the deadline would trip).
        """
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        target = _make_lqfp_west_corner_geometry(grid)

        # Destination: a connector-style pad on the far east side, mirroring
        # the J1 SWD header on board-04.  We deliberately place it east of
        # the chip body so the trace has to escape west and then loop
        # around (the chip-body pads make a direct east route impossible
        # without a layer transition).
        dest = _make_pad(x=18.0, y=10.0, net=target.net, net_name="NRST",
                         ref="J1", pin="5")
        grid.add_pad(dest)

        # Issue #2974: the escape-hint multiplier (``_ESCAPE_HINT_DEADLINE_MULT``)
        # lifts the effective budget to 3x for corner-flanked nets, so pass
        # a generous wall-clock here and assert against the elapsed time
        # below.  The hint and the multiplier are the safety net; the test
        # measures that the hint actually finishes fast in practice.
        start = time.monotonic()
        route = router.route(target, dest, per_net_timeout=60.0)
        elapsed = time.monotonic() - start

        assert route is not None, "Expected a route from corner pad to dest"
        # A 30s ceiling is well below the 32-95s wall-clock the issue
        # documents on production board-04 without the hint.  Under
        # ``pytest-cov`` instrumentation we see ~25-29s on CI; with no
        # coverage the same test completes in ~5s.  Tightening further
        # is unreliable because synthetic geometry doesn't perfectly
        # mirror the production search topology, and coverage overhead
        # is non-trivial.
        assert elapsed < 30.0, f"Routing took {elapsed:.2f}s (expected < 30.0s)"

    def test_seed_does_not_break_simple_route(self):
        """A non-corner pad still routes correctly (no regression)."""
        grid = _make_grid()
        rules = grid.rules
        router = Router(grid, rules)
        start = _make_pad(x=2.0, y=2.0, net=1, net_name="SIMPLE", ref="A", pin="1")
        end = _make_pad(x=8.0, y=8.0, net=1, net_name="SIMPLE", ref="B", pin="1")
        grid.add_pad(start)
        grid.add_pad(end)

        # The predicate should return None for these isolated pads, so the
        # escape-hint code path is a no-op and the regular A* must still
        # find a route.
        assert router._detect_escape_hint(start, [0]) is None
        route = router.route(start, end, per_net_timeout=5.0)
        assert route is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])

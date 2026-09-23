"""#4507: copper the coupled search could not see, now measured by the kernel.

Epic #5509 Phase 3c (#5662) switches the coupled diff-pair search path --
group 7 (``CoupledPathfinder::rail_clear``) and group 8
(``DiffPairRouter``'s span/overlap gates) -- onto the shared exact-geometry
clearance kernel.  #4507's summary of the defect was that *"the copper this
board fails on is invisible to both by construction"*, and "by construction"
is the load-bearing phrase: it was not a threshold that was slightly wrong,
it was copper that neither gate was ever shown.

Two independent constructions, one per side:

``rail_clear`` (C++, group 7)
    The old gate ran **only** when ``Grid3D::has_fixed_fills()`` and consulted
    fixed fills alone.  A board with no pour therefore had no rail check at
    all, and committed routes -- registered exactly and on purpose through
    ``add_stored_segment`` / ``add_stored_via`` -- were invisible to it.  A
    route committed after the last re-sync of the C++ blocked plane was
    invisible *twice*: not in the raster, not in the gate.

``_segment_cells_clear`` (Python, group 8)
    The gate is a raster walk, and the raster only knows copper it was marked
    with.  Copper the coupled constructor builds itself -- the sibling leg, a
    meander tooth, a synthesized tail -- lives in ``_shadow_foreign_universe``
    and is never marked, so a candidate span could be driven straight through
    it and the gate would call it clear.

Each test below fails on the pre-#5662 implementation for that reason alone,
and each is paired with a control that pins what must *not* change: same-net
copper is still the route's own metal, and copper that is genuinely far away
is still accepted.
"""

from __future__ import annotations

import pytest

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder, DiffPairRouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(), reason="group 7 lives in the C++ extension (uv run kct build-native)"
)

# One rule set for both sides, so the two constructions below are the same
# board seen from two languages.
TRACE_WIDTH = 0.2
TRACE_CLEARANCE = 0.2
RESOLUTION = 0.1

BOARD_W = 10.0
BOARD_H = 10.0

OWN_NET = 1
FOREIGN_NET = 2


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=TRACE_CLEARANCE,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=TRACE_CLEARANCE,
        grid_resolution=RESOLUTION,
    )


# ---------------------------------------------------------------------------
# Group 7 -- CoupledPathfinder::rail_clear, in C++
# ---------------------------------------------------------------------------


def _cpp_pathfinder(router_cpp, grid):
    cpp_rules = router_cpp.DesignRules()
    cpp_rules.trace_width = TRACE_WIDTH
    cpp_rules.trace_clearance = TRACE_CLEARANCE
    cpp_rules.via_diameter = 0.6
    cpp_rules.via_drill = 0.3
    cpp_rules.via_clearance = TRACE_CLEARANCE
    cpp_rules.grid_resolution = RESOLUTION
    return router_cpp.CoupledPathfinder(grid, cpp_rules, 2, 0, 1, 1, 0, 0.0, 1.0)


def _cpp_grid(router_cpp):
    cols = int(BOARD_W / RESOLUTION) + 1
    rows = int(BOARD_H / RESOLUTION) + 1
    return router_cpp.Grid3D(cols, rows, 2, RESOLUTION, 0.0, 0.0)


@requires_cpp
def test_rail_clear_sees_stored_route_copper_with_no_fixed_fill() -> None:
    """The #4507 construction: a board with no pour, and a committed route.

    The stored segment lies directly across the candidate rail step, far
    inside its clearance envelope.  Nothing is marked into the blocked plane
    -- that is the whole point, and it is the state a route committed after
    the last ``CppGrid.from_routing_grid`` sync leaves the grid in.

    Pre-#5662 this returned ``True``: the gate was wrapped in
    ``if (grid_.has_fixed_fills())`` and this grid has none, so no check ran
    at all.
    """
    from kicad_tools.router import router_cpp

    grid = _cpp_grid(router_cpp)
    grid.add_stored_segment(5.0, 1.0, 5.0, 9.0, TRACE_WIDTH, 0, FOREIGN_NET)
    assert not grid.has_fixed_fills(), "setup guard: the defect needs a board with no pour"

    pathfinder = _cpp_pathfinder(router_cpp, grid)
    # A rail step that walks straight across the stored copper.
    assert not pathfinder.rail_clear_world(
        4.5, 5.0, 5.5, 5.0, 0, OWN_NET, -1, TRACE_WIDTH / 2, TRACE_CLEARANCE, False
    )


@requires_cpp
def test_rail_clear_still_accepts_own_and_distant_copper() -> None:
    """The controls: same-net metal and genuinely-clear copper still pass.

    Without these the test above would be satisfied by a gate that refuses
    everything.  Same-net copper is the route's own metal (a rail lands on
    it), and a step a comfortable margin away from foreign copper is exactly
    what the coupled search exists to find.
    """
    from kicad_tools.router import router_cpp

    grid = _cpp_grid(router_cpp)
    grid.add_stored_segment(5.0, 1.0, 5.0, 9.0, TRACE_WIDTH, 0, FOREIGN_NET)
    pathfinder = _cpp_pathfinder(router_cpp, grid)

    same_net = _cpp_grid(router_cpp)
    same_net.add_stored_segment(5.0, 1.0, 5.0, 9.0, TRACE_WIDTH, 0, OWN_NET)
    same_net_pf = _cpp_pathfinder(router_cpp, same_net)
    assert same_net_pf.rail_clear_world(
        4.5, 5.0, 5.5, 5.0, 0, OWN_NET, -1, TRACE_WIDTH / 2, TRACE_CLEARANCE, False
    )

    # 2 mm away from the stored copper -- ten times the requirement.
    assert pathfinder.rail_clear_world(
        2.5, 5.0, 3.0, 5.0, 0, OWN_NET, -1, TRACE_WIDTH / 2, TRACE_CLEARANCE, False
    )


@requires_cpp
def test_rail_clear_exempts_the_partner_rail() -> None:
    """A diff pair's two rails are not foreign copper to each other.

    Within-pair spacing is the coupled search's own spacing constraint plus
    the commit-time intra-pair gate, both of which know the pair's target
    spacing.  Holding the partner to the board's foreign-copper minimum here
    would refuse the very geometry a differential pair is: ``rail_clear``
    takes ``partner_net`` so it can say so explicitly rather than by
    accident.
    """
    from kicad_tools.router import router_cpp

    grid = _cpp_grid(router_cpp)
    partner_net = 3
    grid.add_stored_segment(5.0, 1.0, 5.0, 9.0, TRACE_WIDTH, 0, partner_net)
    pathfinder = _cpp_pathfinder(router_cpp, grid)

    step = (4.5, 5.0, 5.5, 5.0, 0, OWN_NET)
    assert not pathfinder.rail_clear_world(*step, -1, TRACE_WIDTH / 2, TRACE_CLEARANCE, False), (
        "setup guard: without the exemption this step is refused"
    )
    assert pathfinder.rail_clear_world(*step, partner_net, TRACE_WIDTH / 2, TRACE_CLEARANCE, False)


@requires_cpp
def test_rail_clear_grid_and_world_forms_agree() -> None:
    """The bound grid form is the world form plus a coordinate conversion.

    The conformance adapter drives ``rail_clear_world`` so grid quantisation
    cannot move a verdict; the search drives ``rail_clear``.  They must be one
    gate, or the oracle would be measuring something the router does not run.
    """
    from kicad_tools.router import router_cpp

    grid = _cpp_grid(router_cpp)
    grid.add_stored_segment(5.0, 1.0, 5.0, 9.0, TRACE_WIDTH, 0, FOREIGN_NET)
    pathfinder = _cpp_pathfinder(router_cpp, grid)

    for gx in range(30, 70, 3):
        gy = 50
        world = pathfinder.rail_clear_world(
            *grid.grid_to_world(gx, gy),
            *grid.grid_to_world(gx + 1, gy),
            0,
            OWN_NET,
            -1,
            TRACE_WIDTH / 2,
            TRACE_CLEARANCE,
            False,
        )
        cells = pathfinder.rail_clear(
            gx, gy, gx + 1, gy, 0, OWN_NET, -1, TRACE_WIDTH / 2, TRACE_CLEARANCE, False
        )
        assert world == cells, f"grid/world disagreement at cell ({gx}, {gy})"


# ---------------------------------------------------------------------------
# Group 8 -- DiffPairRouter's span gates, in Python
# ---------------------------------------------------------------------------


def _router() -> tuple[Autorouter, DiffPairRouter]:
    router = Autorouter(
        width=BOARD_W,
        height=BOARD_H,
        origin_x=0.0,
        origin_y=0.0,
        rules=_rules(),
        layer_stack=LayerStack.two_layer(),
        force_python=True,
        physics_enabled=False,
    )
    return router, DiffPairRouter(router)


def _foreign_route() -> Route:
    """A vertical foreign-net trace across the middle of the board."""
    route = Route(net=FOREIGN_NET, net_name="FOREIGN")
    route.segments.append(
        Segment(
            x1=5.0,
            y1=1.0,
            x2=5.0,
            y2=9.0,
            width=TRACE_WIDTH,
            layer=Layer.F_CU,
            net=FOREIGN_NET,
            net_name="FOREIGN",
        )
    )
    return route


def test_segment_cells_clear_sees_shadow_copper_the_raster_never_saw() -> None:
    """The Python half of #4507: constructor-built copper, never marked.

    ``_shadow_foreign_copper`` is how the coupled constructor tells its own
    gates about copper it has just built -- the sibling leg, a meander tooth.
    That copper is deliberately *not* in the grid, so the raster walk cannot
    see it, and before #5662 nothing else measured a candidate span against a
    foreign **segment** either (the #4571 gate covers pads and the #4575 gate
    covers vias).  A span straight through it was accepted.
    """
    router, dpr = _router()
    router.routes.append(_foreign_route())
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    # Nothing is marked, so the raster is empty and says "clear".
    assert dpr._segment_cells_clear(pathfinder, 4.0, 5.0, 6.0, 5.0, 0, OWN_NET), (
        "setup guard: the raster must be blind to this copper, or the test "
        "would pass for the wrong reason"
    )

    with dpr._shadow_foreign_copper():
        assert not dpr._segment_cells_clear(pathfinder, 4.0, 5.0, 6.0, 5.0, 0, OWN_NET)


def test_segment_cells_clear_shadow_gate_leaves_clear_spans_alone() -> None:
    """The control: the shadow pass is an overlap test, not a new clearance rule.

    A span that merely passes *near* the constructor's own copper is still
    accepted -- deciding how near is too near needs a resolved requirement
    (the intra-pair value for a partner leg, the board minimum for everyone
    else), which is Phase 2's resolver and not this phase's to invent.
    """
    router, dpr = _router()
    router.routes.append(_foreign_route())
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    with dpr._shadow_foreign_copper():
        # 1.5 mm clear of the foreign trace: no overlap, so no refusal.
        assert dpr._segment_cells_clear(pathfinder, 1.0, 3.0, 3.0, 3.0, 0, OWN_NET)
        # Own-net copper may always be touched (a tail lands on it).
        assert dpr._segment_cells_clear(pathfinder, 4.0, 5.0, 6.0, 5.0, 0, FOREIGN_NET)


def test_segment_cells_clear_refines_a_raster_block_with_exact_geometry() -> None:
    """The other direction the kernel buys: the raster's over-block is re-decided.

    The grid marks a square, cell-quantised halo, so it blocks measurably more
    than the copper it stands for.  Where every blocking cell is attributable
    to copper the grid can name exactly -- here, one committed route -- the
    kernel's exact gap decides instead.  This can only ever accept more, which
    is why it cannot cost reach.

    The span sits ``TRACE_CLEARANCE`` plus a hair away from the committed
    trace's copper: legal by the rule the constructor itself applies, but
    inside the marked halo.
    """
    router, dpr = _router()
    route = _foreign_route()
    router.grid.mark_route(route)
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    # Edge-to-edge gap of exactly TRACE_CLEARANCE + 1 um between the two
    # traces' copper: centre-to-centre is two half-widths plus the gap.
    offset = TRACE_WIDTH + TRACE_CLEARANCE + 0.001
    y = 5.0
    x = 5.0 + offset

    blocked = any(
        pathfinder._is_cell_blocked(gx, gy, 0, OWN_NET)
        for gx, gy in [router.grid.world_to_grid(x, y)]
    )
    assert blocked, "setup guard: the raster halo must cover this legal span"

    assert dpr._segment_cells_clear(pathfinder, x, y - 1.0, x, y + 1.0, 0, OWN_NET)


def test_segment_cells_clear_still_refuses_unattributable_blocks() -> None:
    """Refinement never bypasses occupancy the grid cannot account for.

    An obstacle marks cells blocked without stamping a net or registering any
    geometry, so nothing can say what that copper is.  A gate that refined it
    anyway would be trading #4507 for a worse bug -- routing through a
    keep-out -- so an unattributable block still declines the span, exactly as
    before this phase.
    """
    from kicad_tools.router.primitives import Obstacle

    router, dpr = _router()
    router.grid.add_obstacle(
        Obstacle(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, clearance=0.0)
    )
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    assert not dpr._segment_cells_clear(pathfinder, 4.0, 5.0, 6.0, 5.0, 0, OWN_NET)


def test_pad_marked_cell_attributes_every_cell_the_pad_marked() -> None:
    """Attribution reproduces the marking rectangle, not a containment test.

    ``_add_pad_unsafe`` rounds each envelope corner to a cell index and then
    blocks the whole inclusive rectangle, so a border cell can be marked while
    its **centre** lies up to half a resolution outside the continuous
    envelope.  An attribution built as "is this cell centre inside the
    envelope" therefore leaves exactly those border cells unexplained -- and
    an unexplained cell declines the span, which is the over-rejection
    Epic #5509 Phase 3c set out to remove (it was the whole of group 8's
    residual ``pad-seg`` over-rejection against kicad-cli).

    Every cell the pad marked must be attributable to it, so the property is
    asserted over the pad's whole neighbourhood rather than at one hand-picked
    coordinate.  The counted border cells make the test fail loudly if a
    future change makes the rectangle so tight that the interesting case
    stops being exercised.
    """
    from kicad_tools.router.primitives import Pad

    router, _dpr = _router()
    grid = router.grid
    pad = Pad(
        x=5.0,
        y=5.0,
        width=0.9,
        height=0.55,
        layer=Layer.F_CU,
        net=FOREIGN_NET,
        net_name="FOREIGN",
        ref="U1",
        pin="1",
    )
    grid.add_pad(pad)

    cx, cy = grid.world_to_grid(pad.x, pad.y)
    reach = int(2.0 / RESOLUTION)
    marked = 0
    for gy in range(cy - reach, cy + reach + 1):
        for gx in range(cx - reach, cx + reach + 1):
            if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                continue
            if not grid.cell_at(0, gy, gx).blocked:
                continue
            marked += 1
            assert grid.pad_marked_cell(gx, gy, 0), (
                f"cell ({gx}, {gy}) is blocked by the only pad on the board but "
                "pad_marked_cell cannot attribute it -- a span crossing it would "
                "be refused with no copper to justify the refusal"
            )
    assert marked > 0, "setup guard: the pad must actually block something"


def test_pad_marked_cell_does_not_attribute_distant_cells() -> None:
    """The control: attribution is not a blanket yes.

    Reproducing the marking rectangle is safe precisely because it is
    bounded by that rectangle.  A cell well outside every pad's halo must
    stay unattributed, or the refinement would authorise bypassing occupancy
    whose cause it never identified.
    """
    from kicad_tools.router.primitives import Pad

    router, _dpr = _router()
    grid = router.grid
    grid.add_pad(
        Pad(
            x=5.0,
            y=5.0,
            width=0.9,
            height=0.55,
            layer=Layer.F_CU,
            net=FOREIGN_NET,
            net_name="FOREIGN",
            ref="U1",
            pin="1",
        )
    )

    far_gx, far_gy = grid.world_to_grid(1.0, 1.0)
    assert not grid.pad_marked_cell(far_gx, far_gy, 0)
    # A through-hole pad marks every layer; this SMD one must not.
    cx, cy = grid.world_to_grid(5.0, 5.0)
    assert grid.pad_marked_cell(cx, cy, 0)
    assert not grid.pad_marked_cell(cx, cy, 1)


# ---------------------------------------------------------------------------
# Attribution soundness: a pad halo may not launder a co-located keep-out
# ---------------------------------------------------------------------------

# Judge's reproduction for the #5676 review finding, in world coordinates: a
# 1x1 mm foreign pad at the board centre, and a keep-out sliver standing on
# the eastern edge of that pad's marked halo.  Every cell the span walks is
# inside BOTH the pad's marking rectangle and the obstacle's blocked region.
_HALO_PAD_X = 5.0
_HALO_PAD_Y = 5.0
_HALO_PAD_SIZE = 1.0
# x = pad centre + half the pad + (trace_clearance + trace_width / 2), i.e.
# the last column ``_add_pad_unsafe`` marks -- and exactly ``TRACE_CLEARANCE``
# of real copper-to-copper gap, so the kernel pass finds the span legal
# against the pad alone.
_HALO_SPAN_X = 5.8


def _halo_pad():
    from kicad_tools.router.primitives import Pad

    return Pad(
        x=_HALO_PAD_X,
        y=_HALO_PAD_Y,
        width=_HALO_PAD_SIZE,
        height=_HALO_PAD_SIZE,
        layer=Layer.F_CU,
        net=FOREIGN_NET,
        net_name="FOREIGN",
        ref="U1",
        pin="1",
    )


def _halo_obstacle():
    from kicad_tools.router.primitives import Obstacle

    return Obstacle(
        x=_HALO_SPAN_X,
        y=_HALO_PAD_Y,
        width=0.1,
        height=1.2,
        layer=Layer.F_CU,
        clearance=0.0,
    )


def test_segment_cells_clear_refines_a_pad_halo_with_exact_geometry() -> None:
    """The refinement this phase exists for: a pad halo, re-decided exactly.

    The span runs down the last column the pad's marking pass wrote, where
    the real copper-to-copper gap is exactly ``TRACE_CLEARANCE``.  The raster
    blocks it (the halo is a square, cell-quantised envelope); the kernel,
    measuring the pad's actual metal, does not.

    This is the control for the keep-out test below: the fix for that defect
    must not be "stop refining pad halos", which would undo the phase.
    """
    router, dpr = _router()
    router.grid.add_pad(_halo_pad())
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    gx, gy = router.grid.world_to_grid(_HALO_SPAN_X, _HALO_PAD_Y)
    assert pathfinder._is_cell_blocked(gx, gy, 0, OWN_NET), (
        "setup guard: the pad halo must cover this legal span, or the test "
        "passes for the wrong reason"
    )

    assert dpr._segment_cells_clear(pathfinder, _HALO_SPAN_X, 4.45, _HALO_SPAN_X, 5.55, 0, OWN_NET)


@pytest.mark.parametrize("pad_first", [True, False], ids=["pad-then-obstacle", "obstacle-then-pad"])
def test_segment_cells_clear_refuses_a_keep_out_hidden_inside_a_pad_halo(
    pad_first: bool,
) -> None:
    """A keep-out co-located with a pad halo must still refuse the span.

    The #5676 review finding.  ``pad_marked_cell`` is a *geometric* rectangle
    test: it establishes that a pad marked this cell, never that the pad is
    the only reason the cell is blocked.  When a keep-out's cells fall inside
    a foreign pad's marking rectangle, every blocking cell looks attributable,
    the kernel pass measures the pad and nothing else -- ``add_obstacle``
    registers no geometry to measure -- and the span is accepted straight
    through the keep-out.

    Insertion order is parametrised because the raster keeps no trace of it:
    the pad's marking pass overwrites ``cell.net`` 0 -> 2 either way, so the
    obstacle's own signature is erased and a ``cell.net`` guard cannot see it.
    """
    router, dpr = _router()
    grid = router.grid
    if pad_first:
        grid.add_pad(_halo_pad())
        grid.add_obstacle(_halo_obstacle())
    else:
        grid.add_obstacle(_halo_obstacle())
        grid.add_pad(_halo_pad())
    pathfinder = CoupledPathfinder(grid, router.rules, 2)

    gx, gy = grid.world_to_grid(_HALO_SPAN_X, _HALO_PAD_Y)
    assert pathfinder._is_cell_blocked(gx, gy, 0, OWN_NET), "setup guard: cell must be blocked"
    assert grid.pad_marked_cell(gx, gy, 0), (
        "setup guard: the keep-out must sit inside the pad's marking rectangle, "
        "or the geometric attribution never fires and the defect is not exercised"
    )

    assert not dpr._segment_cells_clear(
        pathfinder, _HALO_SPAN_X, 4.45, _HALO_SPAN_X, 5.55, 0, OWN_NET
    ), "span accepted through a registered keep-out the kernel never measured"


def test_segment_cells_clear_refuses_a_keep_out_with_no_pad_in_sight() -> None:
    """The third row of the reproduction: the obstacle alone, still refused.

    Pins that the keep-out itself was never refinable -- so the failure above
    is the pad halo laundering it, not a change in how obstacles are read.
    """
    router, dpr = _router()
    router.grid.add_obstacle(_halo_obstacle())
    pathfinder = CoupledPathfinder(router.grid, router.rules, 2)

    assert not dpr._segment_cells_clear(
        pathfinder, _HALO_SPAN_X, 4.45, _HALO_SPAN_X, 5.55, 0, OWN_NET
    )


def test_segment_cells_clear_refuses_a_keepout_rectangle_inside_a_pad_halo() -> None:
    """The same laundering, through ``add_keepout`` rather than ``add_obstacle``.

    Attribution soundness is a property of the *class* of blockers the exact
    kernel cannot measure, not of one entry point.  A keepout registers no
    geometry either, so a keepout rectangle overlapping a pad's marking
    rectangle must decline the span for the same reason.
    """
    router, dpr = _router()
    grid = router.grid
    grid.add_pad(_halo_pad())
    grid.add_keepout(
        _HALO_SPAN_X - 0.05,
        _HALO_PAD_Y - 0.6,
        _HALO_SPAN_X + 0.05,
        _HALO_PAD_Y + 0.6,
        layers=[Layer.F_CU],
    )
    pathfinder = CoupledPathfinder(grid, router.rules, 2)

    gx, gy = grid.world_to_grid(_HALO_SPAN_X, _HALO_PAD_Y)
    assert grid.pad_marked_cell(gx, gy, 0), "setup guard: the keepout must sit inside the pad halo"

    assert not dpr._segment_cells_clear(
        pathfinder, _HALO_SPAN_X, 4.45, _HALO_SPAN_X, 5.55, 0, OWN_NET
    )

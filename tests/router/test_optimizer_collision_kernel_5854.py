"""Group 15 on the clearance kernel -- Epic #5509 Phase 4a (#5854).

``router/optimizer/collision.py`` is the post-route trace optimizer's clearance
model: ``TraceOptimizer._path_is_clear`` forwards every candidate shortcut to
one of the two ``CollisionChecker`` implementations here and composes no gap of
its own.  Phase 4a made both of them ask the shared exact-geometry kernel
(:mod:`kicad_tools.router.clearance_kernel`, through
:mod:`kicad_tools.router.clearance_shapes`) for every verdict they reach.

Two properties are pinned here, and they pull in opposite directions on
purpose:

* **The kernel is the authority where geometry is registered.**  Routed copper
  already had an exact narrow phase (#5625); *pad* copper did not -- both
  checkers judged it off the raster, where a pad's blocked footprint is its
  metal grown by the pad's own clearance halo AND the candidate is grown again
  by ``width / 2 + trace_clearance``.  That is roughly twice the requirement,
  quantised outwards, and it is where group 15's ``pad-seg`` over-rejection in
  ``docs/clearance-conformance.md`` came from.
* **The raster still decides what it alone records.**  A cell blocked by
  something that registers no geometry -- a keepout, an obstacle, a region
  bound, the board-edge band, all reported by
  :meth:`RoutingGrid.raster_only_blocked_cell` -- keeps its conservative
  reject, and so does a hard-blocked cell on a grid with an empty pad registry.
  Without that half, "switch to the kernel" would silently authorise walking
  through blockage nobody can re-measure.

``tests/conformance/`` is where agreement with kicad-cli is measured (group 15
is a hard gate there since this phase).  This module is the unit-level
companion: it states the *predicate's* contract on a real
:class:`~kicad_tools.router.grid.RoutingGrid`, with no kicad-cli needed, so a
regression names the branch that broke rather than a corpus seed.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM, KSegment, clear, make_pad
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import (
    GridCollisionChecker,
    VectorCollisionChecker,
    _obstacle_cell_is_accountable_pad_copper,
    _pad_required_clearance,
    _stitch_via_reservation,
)
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

BOARD_W = 20.0
BOARD_H = 14.0

TRACE_WIDTH = 0.2
TRACE_CLEARANCE = 0.2
VIA_CLEARANCE = 0.2

PAD_NET = 7
OWN_NET = 26

#: Comfortably outside the requirement, comfortably inside the raster halo.
#: ``trace_clearance + trace_width / 2`` is the pad's own halo radius and the
#: candidate is dilated by the same amount again, so a gap anywhere below
#: ``2 * 0.2 + 0.2 = 0.6`` mm used to be refused.
CLEAR_GAP = 0.25


def _grid(resolution: float = 0.05) -> RoutingGrid:
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=TRACE_WIDTH,
        trace_clearance=TRACE_CLEARANCE,
        via_clearance=VIA_CLEARANCE,
    )
    return RoutingGrid(BOARD_W, BOARD_H, rules)


def _pad(
    x: float,
    y: float,
    *,
    net: int = PAD_NET,
    shape: str = "rect",
    size: tuple[float, float] = (1.0, 1.0),
    rotation: float = 0.0,
    layer: Layer = Layer.F_CU,
    ref: str = "P1",
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=size[0],
        height=size[1],
        net=net,
        net_name=f"N{net}" if net else "GND",
        layer=layer,
        ref=ref,
        pin="1",
        through_hole=False,
        rotation=rotation,
        shape=shape,
    )


def _both_checkers(grid: RoutingGrid) -> list[GridCollisionChecker | VectorCollisionChecker]:
    """Every implementation of the protocol, so neither can drift alone.

    ``VectorCollisionChecker`` delegates to ``GridCollisionChecker`` whenever
    the per-layer segment R-tree is unavailable or unpopulated, which is the
    state of a grid carrying only pads -- so driving both is how a test on a
    pad-only grid still covers the vector class's own
    ``_check_obstacles_clear`` rather than only the delegate.
    """
    return [GridCollisionChecker(grid), VectorCollisionChecker(grid)]


def _probe(
    checker: GridCollisionChecker | VectorCollisionChecker,
    y: float,
    *,
    x1: float = 3.0,
    x2: float = 11.0,
    exclude_net: int = OWN_NET,
    width: float = TRACE_WIDTH,
) -> bool:
    return checker.path_is_clear(
        x1=x1,
        y1=y,
        x2=x2,
        y2=y,
        layer=Layer.F_CU,
        width=width,
        exclude_net=exclude_net,
    )


# ---------------------------------------------------------------------------
# The pad narrow phase: exact, and the kernel's
# ---------------------------------------------------------------------------


def test_pad_gate_accepts_a_path_the_kernel_measures_clear() -> None:
    """A path inside the pad's raster halo but outside its clearance is clear.

    The headline of Phase 4a.  The pad's halo plus the candidate's own
    dilation covered ``CLEAR_GAP``; the exact measurement does not, and
    ``clearance_kernel.clear`` agrees.
    """
    grid = _grid()
    pad = _pad(7.0, 7.0)
    grid.add_pad(pad)

    probe_y = pad.y + pad.height / 2 + CLEAR_GAP + TRACE_WIDTH / 2

    # Ground truth for this probe, straight from the kernel.
    assert clear(
        KSegment(3.0, probe_y, 11.0, probe_y, TRACE_WIDTH),
        make_pad("rect", pad.width, pad.height, cx=pad.x, cy=pad.y),
        TRACE_CLEARANCE,
    )

    for checker in _both_checkers(grid):
        assert _probe(checker, probe_y) is True, type(checker).__name__


def test_pad_gate_rejects_a_path_inside_the_requirement() -> None:
    """Measuring exactly is not the same as being permissive.

    The complement of the test above: a gap the kernel calls short is still
    refused, so the de-quantisation cannot be read as "the pad gate was
    removed".
    """
    grid = _grid()
    pad = _pad(7.0, 7.0)
    grid.add_pad(pad)

    short_gap = TRACE_CLEARANCE - 0.05
    probe_y = pad.y + pad.height / 2 + short_gap + TRACE_WIDTH / 2

    assert not clear(
        KSegment(3.0, probe_y, 11.0, probe_y, TRACE_WIDTH),
        make_pad("rect", pad.width, pad.height, cx=pad.x, cy=pad.y),
        TRACE_CLEARANCE,
    )

    for checker in _both_checkers(grid):
        assert _probe(checker, probe_y) is False, type(checker).__name__


def test_path_through_pad_metal_is_always_refused() -> None:
    """Overlapping copper is a negative gap, so no refinement can rescue it.

    Issue #2757's guarantee -- a pad whose net was rewritten to 0 for a
    skipped pour still blocks -- restated on the exact model: ``net=0`` is
    foreign to every real net, and the candidate's centreline runs through
    the metal.
    """
    grid = _grid()
    grid.add_pad(_pad(7.0, 7.0, net=0))

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is False, type(checker).__name__


def test_own_net_pad_does_not_gate_its_own_route() -> None:
    """A route may terminate in its own pad's metal."""
    grid = _grid()
    grid.add_pad(_pad(7.0, 7.0, net=OWN_NET))

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is True, type(checker).__name__


def test_rotated_roundrect_is_measured_on_its_outline_not_its_box() -> None:
    """The ``roundrect-corner-gap`` mechanism, at unit scale.

    A 1x1 mm roundrect (``rratio`` 0.25 -> 0.25 mm corner radius) rotated 45
    degrees reaches ``support`` along its corner diagonal, while the bounding
    rectangle reaches ``0.7071`` mm.  A track placed 0.22 mm from the true
    outline is clean for kicad-cli and for the kernel, and was rejected by
    every raster-bounded model -- this consumer included, until Phase 4a.
    """
    grid = _grid()
    pad = _pad(7.0, 7.0, shape="roundrect", rotation=45.0)
    grid.add_pad(pad)

    radius = 0.25 * min(pad.width, pad.height)
    # Half-diagonal of the Minkowski core (the 0.5x0.5 mm inner box), plus the
    # corner radius: how far the real copper reaches along +X at 45 degrees.
    support = math.hypot(pad.width / 2 - radius, pad.height / 2 - radius) + radius
    assert support < math.hypot(pad.width / 2, pad.height / 2)

    exact_gap = 0.22
    probe_x = pad.x + support + exact_gap + TRACE_WIDTH / 2

    kernel_pad = make_pad("roundrect", pad.width, pad.height, 0.25, 45.0, cx=pad.x, cy=pad.y)
    assert clear(KSegment(probe_x, 5.0, probe_x, 9.0, TRACE_WIDTH), kernel_pad, TRACE_CLEARANCE)

    for checker in _both_checkers(grid):
        assert (
            checker.path_is_clear(
                x1=probe_x,
                y1=5.0,
                x2=probe_x,
                y2=9.0,
                layer=Layer.F_CU,
                width=TRACE_WIDTH,
                exclude_net=OWN_NET,
            )
            is True
        ), type(checker).__name__


def test_smd_pad_on_another_layer_does_not_gate() -> None:
    """``_pad_copper_clear`` keeps ``_add_pad_unsafe``'s own layer rule."""
    grid = _grid()
    grid.add_pad(_pad(7.0, 7.0, layer=Layer.B_CU))

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is True, type(checker).__name__


# ---------------------------------------------------------------------------
# The requirement the pad gate measures against (PR #5874 review)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("gap", [0.3, 0.5])
def test_per_component_override_is_not_dropped_by_the_exact_gate(strict: bool, gap: float) -> None:
    """A ``component_clearances`` override still gates, exactly as the raster did.

    PR #5874 review, blocking finding 1.  Both in-repo authorities on
    segment-vs-pad clearance resolve the requirement per pad --
    ``RoutingGrid.worst_segment_pad_deficit`` (the #3545 backstop) and
    ``DiffPairRouter._kernel_pad_deficit`` (Phase 3c) both call
    ``rules.get_clearance_for_component(pad.ref, pin_pitch)`` -- and so does
    the halo this narrow phase refines, via
    ``RoutingGrid._clearance_for_pin_pitch`` under ``strict_pad_clearance``.
    A flat ``rules.trace_clearance`` here would accept a 0.3 mm and a 0.5 mm
    gap against U1's 0.6 mm override, with nothing left to catch it: the #3545
    backstop runs *before* the optimizer.

    Parametrised over ``strict_pad_clearance`` because that flag is live usage
    (boards 07/09) and is what makes the raster halo itself carry the override.
    """
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=TRACE_WIDTH,
        trace_clearance=TRACE_CLEARANCE,
        via_clearance=VIA_CLEARANCE,
        component_clearances={"U1": 0.6},
        strict_pad_clearance=strict,
    )
    grid = RoutingGrid(BOARD_W, BOARD_H, rules)
    pad = _pad(7.0, 7.0, ref="U1")
    grid.add_pad(pad)

    assert _pad_required_clearance(grid, pad) == pytest.approx(0.6)

    # The gap clears ``trace_clearance`` -- so a flat-scalar gate accepts it --
    # and is short of the override.
    assert gap > TRACE_CLEARANCE
    probe_y = pad.y + pad.height / 2 + gap + TRACE_WIDTH / 2
    assert clear(
        KSegment(3.0, probe_y, 11.0, probe_y, TRACE_WIDTH),
        make_pad("rect", pad.width, pad.height, cx=pad.x, cy=pad.y),
        TRACE_CLEARANCE,
    )

    for checker in _both_checkers(grid):
        assert _probe(checker, probe_y) is False, type(checker).__name__

    # Above the override it is clear again, so the gate is the override's
    # value and not a blanket refusal.
    wide_y = pad.y + pad.height / 2 + 0.65 + TRACE_WIDTH / 2
    for checker in _both_checkers(grid):
        assert _probe(checker, wide_y) is True, type(checker).__name__


def test_plane_pad_keeps_its_stitch_via_reservation() -> None:
    """The #2842 reservation is a floor on the exact pad gate.

    PR #5874 review, blocking finding 2.  ``kct stitch`` drops a via on every
    plane-net pad's centre, so foreign copper has to stay
    ``rules.stitch_via_halo_radius()`` from that centre -- 0.425 mm here, i.e.
    0.275 mm from a 0.3 mm pad's metal edge.  Measuring the pad's metal against
    ``trace_clearance`` alone would hand that space back (the raster refuses the
    0.25 mm probe below on ``main``), and the symptom would land in ``kct
    stitch`` rather than in this module.
    """
    grid = _grid()
    pad = _pad(7.0, 7.0, net=0, size=(0.3, 0.3))
    grid.add_pad(pad)

    reservation = grid.rules.stitch_via_halo_radius() - pad.height / 2
    assert reservation == pytest.approx(0.275)
    assert _stitch_via_reservation(grid, pad) == pytest.approx(reservation)
    assert _pad_required_clearance(grid, pad) == pytest.approx(reservation)
    assert reservation > TRACE_CLEARANCE  # otherwise the floor is vacuous

    short_y = pad.y + pad.height / 2 + 0.25 + TRACE_WIDTH / 2
    # The kernel calls this gap clear against ``trace_clearance`` -- the
    # reservation, not the geometry, is what refuses it.
    assert clear(
        KSegment(3.0, short_y, 11.0, short_y, TRACE_WIDTH),
        make_pad("rect", pad.width, pad.height, cx=pad.x, cy=pad.y),
        TRACE_CLEARANCE,
    )
    for checker in _both_checkers(grid):
        assert _probe(checker, short_y) is False, type(checker).__name__

    clear_y = pad.y + pad.height / 2 + 0.30 + TRACE_WIDTH / 2
    for checker in _both_checkers(grid):
        assert _probe(checker, clear_y) is True, type(checker).__name__


def test_signal_pad_of_the_same_size_takes_no_stitch_floor() -> None:
    """The floor is scoped to the pads ``kct stitch`` will bond.

    ``_add_pad_unsafe`` applies the halo only for ``pad.net == 0``; the same
    0.3 mm pad on a real net keeps the ordinary requirement, so the probe the
    test above refuses is accepted here.  Without this control the floor could
    be a blanket re-inflation of the pad gate Phase 4a exists to de-quantise.
    """
    grid = _grid()
    pad = _pad(7.0, 7.0, size=(0.3, 0.3))
    grid.add_pad(pad)

    assert _stitch_via_reservation(grid, pad) == 0.0
    assert _pad_required_clearance(grid, pad) == pytest.approx(TRACE_CLEARANCE)

    probe_y = pad.y + pad.height / 2 + 0.25 + TRACE_WIDTH / 2
    for checker in _both_checkers(grid):
        assert _probe(checker, probe_y) is True, type(checker).__name__


# ---------------------------------------------------------------------------
# What the raster still owns
# ---------------------------------------------------------------------------


def test_hard_blocked_cell_outside_every_pad_rectangle_is_not_refined() -> None:
    """Attribution is per CELL, not merely "some pad is registered".

    PR #5874 review, blocking finding 2's authorisation half.  A non-empty pad
    registry plus ``raster_only_blocked_cell() is False`` does not establish
    that a *pad's* marking pass wrote this cell: ``_apply_stitch_via_halo``
    (#2842) and ``_apply_narrow_channel_halo`` (#2878) both write
    ``_is_obstacle`` for reasons pad metal cannot be re-measured from.
    :meth:`RoutingGrid.pad_marked_cell` -- the same exclusive attribution
    ``DiffPairRouter._pad_attributable_cell`` uses (#5662, hardened by the PR
    #5676 review) -- reproduces only ``_add_pad_unsafe``'s own halo rectangle,
    so a cell no pad marked keeps its conservative reject.
    """
    grid = _grid()
    far_pad = _pad(2.0, 2.0)
    grid.add_pad(far_pad)

    gx, gy = grid.world_to_grid(7.0, 7.0)
    cell = grid.cell_at(0, gy, gx)
    cell.blocked = True
    cell.is_obstacle = True
    cell.net = PAD_NET

    assert grid.pads == (far_pad,)
    assert grid.raster_only_blocked_cell(gx, gy, 0) is False
    assert grid.pad_marked_cell(gx, gy, 0) is False
    assert _obstacle_cell_is_accountable_pad_copper(grid, gx, gy, 0) is False

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is False, type(checker).__name__


def test_board_edge_keepout_keeps_its_conservative_reject() -> None:
    """Edge keep-out copper registers no geometry, so it may not be refined away.

    ``add_edge_keepout`` is the one production writer that sets
    ``is_obstacle`` *without* a pad behind it, and it records every cell it
    touches as registry-less (#5662) for exactly this reason.  The pad
    registry is deliberately non-empty here (a pad far from the probe):
    without it the predicate would answer ``False`` through the "nothing to
    account for" condition instead of the ``raster_only_blocked_cell`` one
    this test is about.
    """
    grid = _grid()
    grid.add_pad(_pad(2.0, 2.0))
    blocked = grid.add_edge_keepout([((6.0, 7.0), (8.0, 7.0))], clearance=0.3)
    assert blocked > 0

    gx, gy = grid.world_to_grid(7.0, 7.0)
    assert grid.cell_at(0, gy, gx).is_obstacle is True
    assert grid.raster_only_blocked_cell(gx, gy, 0) is True
    assert _obstacle_cell_is_accountable_pad_copper(grid, gx, gy, 0) is False

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is False, type(checker).__name__


def test_hard_blocked_cell_is_not_refined_when_no_pad_is_registered() -> None:
    """An empty pad registry accounts for nothing -- fail closed.

    A hard-blocked cell with no registered pad behind it must keep its
    pre-#5854 reject rather than being laundered into "clear" by a walk over
    zero pads.  This is the condition that keeps
    ``tests/router/test_collision_obstacle_scan_parity_5240.py``'s
    hand-painted fixtures meaningful.
    """
    grid = _grid()
    gx, gy = grid.world_to_grid(7.0, 7.0)
    cell = grid.cell_at(0, gy, gx)
    cell.blocked = True
    cell.is_obstacle = True
    cell.net = PAD_NET

    assert grid.pads == ()
    assert _obstacle_cell_is_accountable_pad_copper(grid, gx, gy, 0) is False

    for checker in _both_checkers(grid):
        assert _probe(checker, 7.0) is False, type(checker).__name__


# ---------------------------------------------------------------------------
# Routed copper and vias: same kernel, unchanged rule values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gap", [0.05, 0.19, 0.21, 0.4])
def test_segment_narrow_phase_matches_the_kernel(gap: float) -> None:
    """Foreign routed copper: the verdict IS ``clearance_kernel.clear``.

    Driven through ``GridCollisionChecker``, whose raster broad phase defers
    to the exact narrow phase for route copper accountable to ``grid.routes``
    (#5625).  The boundary values straddle ``trace_clearance`` so the
    parametrisation is not vacuous.
    """
    grid = _grid()
    other_y = 7.0
    other = Segment(
        x1=1.0,
        y1=other_y,
        x2=19.0,
        y2=other_y,
        width=TRACE_WIDTH,
        layer=Layer.F_CU,
        net=PAD_NET,
        net_name="N7",
    )
    grid.mark_route(Route(net=PAD_NET, net_name="N7", segments=[other], vias=[]))

    probe_y = other_y + TRACE_WIDTH + gap
    expected = clear(
        KSegment(3.0, probe_y, 11.0, probe_y, TRACE_WIDTH),
        KSegment(other.x1, other.y1, other.x2, other.y2, other.width, Layer.F_CU.value),
        TRACE_CLEARANCE,
    )
    assert expected is (gap >= TRACE_CLEARANCE - CLEARANCE_EPSILON_MM)
    assert _probe(GridCollisionChecker(grid), probe_y) is expected


def test_via_narrow_phase_keeps_its_resolved_requirement() -> None:
    """The via requirement is still ``max(trace_clearance, via_clearance)``.

    Scope guard #1: this phase moved the *geometry* onto the kernel and left
    rule selection to Phase 2.  Pinned with ``via_clearance`` raised above
    ``trace_clearance`` so a silent drop of the ``max(...)`` would show up as
    a flipped verdict rather than as nothing at all.
    """
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=TRACE_WIDTH,
        trace_clearance=0.15,
        via_clearance=0.30,
    )
    grid = RoutingGrid(BOARD_W, BOARD_H, rules)
    via = Via(
        x=7.0,
        y=7.0,
        drill=0.3,
        diameter=0.6,
        layers=(Layer.F_CU, Layer.B_CU),
        net=PAD_NET,
        net_name="N7",
    )
    grid.mark_route(Route(net=PAD_NET, net_name="N7", segments=[], vias=[via]))

    # 0.20 mm of copper gap: above ``trace_clearance``, below ``via_clearance``.
    probe_y = via.y + via.diameter / 2 + 0.20 + TRACE_WIDTH / 2
    assert _probe(GridCollisionChecker(grid), probe_y) is False

    # Above the widened requirement, it is clear again.
    wide_y = via.y + via.diameter / 2 + 0.35 + TRACE_WIDTH / 2
    assert _probe(GridCollisionChecker(grid), wide_y) is True

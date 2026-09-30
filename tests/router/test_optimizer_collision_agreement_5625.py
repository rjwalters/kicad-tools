"""Issue #5625: the optimizer's two collision checkers must answer alike.

``router/optimizer/collision.py`` ships two implementations of one
``CollisionChecker`` protocol, and ``VectorCollisionChecker``'s docstring
advertises itself as a drop-in, ~10x faster replacement for
``GridCollisionChecker`` -- *same answer, less work*.  Epic #5509's Phase 1c
conformance harness (group 15) measured that this was not true near the
clearance threshold:

* ``VectorCollisionChecker`` measures routed copper **exactly** -- an R-tree
  broad phase plus segment-to-segment / point-to-segment distance.
* ``GridCollisionChecker`` read routed copper off the raster, where
  ``RoutingGrid._mark_route`` has *already* dilated every committed segment by
  ``width / 2 + trace_clearance`` (plus the issue #1666 safety cell), and then
  dilated the candidate path by ``width / 2 + trace_clearance`` a **second**
  time.  Two traces separated by rather more than one clearance were therefore
  rejected, because each half of the doubled envelope only had to reach the
  other's.

The issue's cited evidence is corpus seed 1, pair kind ``seg-seg``: the grid
checker rejected, the vector checker accepted, and ``kicad-cli pcb drc`` on
the same board was clean.  The tests below drive both checkers over the
harness's own seeded corpus (the same generator group 15's adapter uses, so
the geometry is identical to the measured row) and assert:

* the two checkers never disagree -- the issue's title;
* copper that clears the router's own ``trace_clearance`` is **accepted** by
  both, so agreement was not bought by making the vector checker as
  conservative as the raster was (the non-vacuity guard);
* copper genuinely inside the clearance is still rejected by both; and
* a blocked cell whose occupancy the exact model cannot account for -- a
  keepout, an obstacle, a board-edge band -- is still rejected outright,
  because the raster is the only record that it exists.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import (
    GridCollisionChecker,
    VectorCollisionChecker,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules
from tests.conformance.adapters._support import (
    layer_of,
    net_ids,
    pair_contexts,
    router_grid,
    router_pad,
    router_segment,
    single_object_route,
)
from tests.conformance.adapters.optimizer import PATH_CANDIDATE_KINDS
from tests.conformance.generator import PadSpec, PairKind, SegmentSpec, generate_case

#: Seeds swept by the agreement test.  Small enough to stay well inside the
#: CI ``--timeout=60`` budget (no kicad-cli process is launched here -- the
#: oracle is the *other* checker, not KiCad), large enough that seeds 1, 3, 7,
#: 8, 12, 13, 15 and 19 each contributed a pre-fix disagreement.
CORPUS_SEEDS = tuple(range(20))


def _path_probes(seed: int):
    """Yield ``(context, grid, args)`` for every case the optimizer is asked.

    Mirrors ``tests/conformance/adapters/optimizer.py`` exactly: one grid per
    pair, the counterpart committed as existing copper, the candidate passed
    to ``path_is_clear`` -- so a disagreement found here is a disagreement the
    measured group-15 row would see.
    """
    case = generate_case(seed)
    nets = net_ids(case)
    for context in pair_contexts(case):
        if context.kind not in PATH_CANDIDATE_KINDS:
            continue
        grid = router_grid(case)
        existing = context.existing
        if isinstance(existing, PadSpec):
            grid.add_pad(router_pad(existing, nets))
        else:
            grid.mark_route(single_object_route(existing, nets))

        candidate = context.candidate
        assert isinstance(candidate, SegmentSpec)
        seg = router_segment(candidate, nets)
        args = (
            seg.x1,
            seg.y1,
            seg.x2,
            seg.y2,
            layer_of(candidate.layer),
            seg.width,
            nets[candidate.net],
        )
        yield context, grid, args


@pytest.mark.parametrize("seed", CORPUS_SEEDS)
def test_the_two_checkers_agree_over_the_corpus(seed: int) -> None:
    """Grid and vector verdicts are identical on every generated probe."""
    for context, grid, args in _path_probes(seed):
        grid_clear = GridCollisionChecker(grid).path_is_clear(*args)
        vector_clear = VectorCollisionChecker(grid).path_is_clear(*args)
        assert grid_clear == vector_clear, (
            f"seed {seed}, {context.kind} pair at "
            f"{context.pair.target_gap_mm:.4f} mm: grid={grid_clear} "
            f"vector={vector_clear}"
        )


def test_copper_clearing_the_router_rule_is_accepted_by_both() -> None:
    """Non-vacuity: agreement is at the *permissive*, correct verdict.

    Every seeded pair whose intended copper gap clears the router's own
    ``trace_clearance`` (the rule ``path_is_clear`` resolves; the corpus's
    ``required_mm`` is the wider ``.kicad_pro`` value, which is Epic #5509
    Phase 2's separate axis) must be accepted by BOTH checkers.  Before the
    fix the grid checker rejected sixteen of these across seeds 0-19, so this
    is the assertion that would fail if agreement were ever restored by making
    the vector checker as conservative as the raster instead.
    """
    checked = 0
    for seed in CORPUS_SEEDS:
        for context, grid, args in _path_probes(seed):
            # Scoped to ``seg-seg``, the issue's own cited kind and the one
            # whose requirement is exactly ``trace_clearance``.  ``seg-via``
            # is measured against the wider ``max(trace_clearance,
            # via_clearance)`` and ``pad-seg`` is still gated by the pad's
            # raster halo (both checkers consult it, so they agree -- Epic
            # #5509's group-15 scope leaves retiring that halo to the phase
            # that moves this module onto ``clearance_kernel``).
            if context.kind != PairKind.SEG_SEG:
                continue
            # A 0.02 mm margin keeps the assertion off the exact tie; the
            # claim is about copper that is comfortably legal.
            if context.pair.target_gap_mm < grid.rules.trace_clearance + 0.02:
                continue
            checked += 1
            assert GridCollisionChecker(grid).path_is_clear(*args), (
                f"seed {seed}: grid checker rejected a {context.kind} pair at "
                f"{context.pair.target_gap_mm:.4f} mm against a "
                f"{grid.rules.trace_clearance} mm rule"
            )
            assert VectorCollisionChecker(grid).path_is_clear(*args), (
                f"seed {seed}: vector checker rejected a {context.kind} pair at "
                f"{context.pair.target_gap_mm:.4f} mm against a "
                f"{grid.rules.trace_clearance} mm rule"
            )
    assert checked >= 10, (
        f"only {checked} clearing pairs in the corpus -- the sweep no longer "
        "exercises the over-rejection this test pins"
    )


def _grid_with_foreign_trace() -> tuple[RoutingGrid, Segment]:
    """A grid carrying one committed foreign-net trace along ``y = 5.0``."""
    rules = DesignRules(grid_resolution=0.127, trace_width=0.2, trace_clearance=0.15)
    grid = RoutingGrid(12.0, 10.0, rules)
    existing = Segment(x1=2.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=7)
    grid.mark_route(Route(net=7, net_name="FOREIGN", segments=[existing]))
    return grid, existing


@pytest.mark.parametrize("checker_cls", [GridCollisionChecker, VectorCollisionChecker])
def test_copper_inside_the_clearance_is_still_rejected(checker_cls) -> None:
    """The fix must not buy agreement by under-rejecting real violations."""
    grid, existing = _grid_with_foreign_trace()
    # Edge-to-edge gap 0.05 mm against a 0.15 mm rule.
    y = existing.y1 + existing.width / 2 + 0.05 + 0.2 / 2
    checker = checker_cls(grid)
    assert not checker.path_is_clear(3.0, y, 9.0, y, Layer.F_CU, 0.2, 26)


@pytest.mark.parametrize("checker_cls", [GridCollisionChecker, VectorCollisionChecker])
def test_copper_outside_the_clearance_is_accepted(checker_cls) -> None:
    """The companion of the test above: 0.25 mm of gap is legal at 0.15 mm."""
    grid, existing = _grid_with_foreign_trace()
    y = existing.y1 + existing.width / 2 + 0.25 + 0.2 / 2
    checker = checker_cls(grid)
    assert checker.path_is_clear(3.0, y, 9.0, y, Layer.F_CU, 0.2, 26)


def test_unaccountable_blockage_is_never_refined_away() -> None:
    """A keepout over routed copper keeps the conservative raster verdict.

    ``add_keepout`` / ``add_obstacle`` / the board-edge band register no
    geometry anywhere (Epic #5509 Phase 3c, #5662): the raster mark is their
    only record, so a cell they touched can never be re-decided from exact
    copper.  The grid checker must therefore keep rejecting a path whose only
    blocked cells are inside such a region, even though the exact
    segment-to-segment measurement alone would clear it.
    """
    grid, existing = _grid_with_foreign_trace()
    y = existing.y1 + existing.width / 2 + 0.25 + 0.2 / 2
    checker = GridCollisionChecker(grid)
    # Control: exactly the probe the previous test accepts.
    assert checker.path_is_clear(3.0, y, 9.0, y, Layer.F_CU, 0.2, 26)

    grid.add_keepout(3.0, y - 0.3, 9.0, y + 0.3, layers=[Layer.F_CU])
    assert not GridCollisionChecker(grid).path_is_clear(3.0, y, 9.0, y, Layer.F_CU, 0.2, 26)

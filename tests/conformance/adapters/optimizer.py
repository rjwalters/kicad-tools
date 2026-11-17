"""Group 15 -- the post-route optimizer's collision checkers.

``router/optimizer/collision.py`` carries two independent implementations of
one ``CollisionChecker`` protocol, and the trace optimizer uses them to decide
whether a *shortened* path may replace a routed one:

* ``VectorCollisionChecker.path_is_clear`` (``:340``) -- R-tree broad phase
  plus exact segment-to-segment distance.  The production choice, and the one
  driven here.
* ``GridCollisionChecker.path_is_clear`` (``:170``) -- Bresenham over the
  raster with a cell buffer for width + clearance.

Both citations are exercised by this single row, because
``VectorCollisionChecker`` **delegates** to ``GridCollisionChecker`` whenever
the R-tree is unavailable or unpopulated for the queried layer
(``collision.py:383``).  On these cases the counterpart is a single marked
object, so which branch answers depends on whether ``mark_route`` populated
the per-layer index -- exactly as it does in production on a sparsely routed
board.  Measuring the vector checker therefore measures the composite the
optimizer really consults; splitting them into two rows would report a
dispatch decision as a clearance disagreement.

**Pair kinds: the candidate must be a segment.**  ``path_is_clear`` takes
``(x1, y1, x2, y2, layer, width, exclude_net)`` -- a *path*.  There is no via
form of the predicate (the optimizer never proposes a via; it shortens
existing trace runs), so ``via-via`` and ``pad-via``, whose candidate is a
via under this harness's via-first insertion order, are out of scope.  The
scope is ``seg-seg``, ``seg-via`` and ``pad-seg``.

**``ignore_overflow`` stays at its default.**  The optimizer sets it to
``True`` only after negotiated routing leaves residual overflow, to stop it
fragmenting routes through overused cells.  These cases have no negotiated
history, so the default ``False`` is the honest setting -- and it is the
stricter one, so the row is not flattered by the choice.

Epic #5509 Phase 4a (#5854) switched this consumer onto the shared clearance
kernel, so the row is now a **merge gate** (``report.MIGRATED_GROUPS``) rather
than report-only.  What that phase moved is visible precisely in the
``pad-seg`` cases: both checkers used to judge pad copper off the raster, where
a pad's blocked footprint is its metal grown by the pad's own clearance halo
*and* the candidate is grown again by ``width / 2 + trace_clearance``, so a
path a comfortable 0.247 mm from pad metal was refused against a 0.20 mm
requirement.  The pad is now measured through the kernel's exact pad model, the
same one the migrated diff-pair and mesh consumers use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    layer_of,
    net_ids,
    pair_contexts,
    project_rules,
    router_grid,
    router_pad,
    router_segment,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["OptimizerCollisionAdapter"]

#: The kinds whose candidate is a segment -- the only shape ``path_is_clear``
#: takes.
PATH_CANDIDATE_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.PAD_SEG})


class OptimizerCollisionAdapter:
    """Drives ``VectorCollisionChecker.path_is_clear`` (grid fallback included)."""

    name = "optimizer_collision"
    group = 15
    pair_kinds = PATH_CANDIDATE_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, None)

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same unmodified consumer, driven at the clearance kicad-cli applies.

        The **gated** reading (:func:`~tests.conformance.adapters._support.project_rules`),
        required of every group in ``report.MIGRATED_GROUPS``.  This row's
        published percentages keep the router's own ``trace_clearance`` /
        ``via_clearance``, and both of its cells are partly that choice: the
        0.2 mm ``via_clearance`` default widens a ``seg-via`` requirement to
        ``max(trace_clearance, via_clearance)``, and the 0.15 mm
        ``trace_clearance`` sits below the project's 0.20 mm ``Default``
        netclass (#5398 / #5654).  Pinning the rule axis to ground truth's own
        number leaves **geometry** as the only thing a disagreement can be --
        which is what ``test_corpus.py`` hard-gates now that Epic #5509
        Phase 4a (#5854) has switched this consumer onto the kernel.
        """
        return self._verdicts(case, project_rules(case))

    def _verdicts(self, case: CopperCase, rules: DesignRules | None) -> set[Verdict]:
        from kicad_tools.router.optimizer.collision import VectorCollisionChecker

        nets = net_ids(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue

            grid = router_grid(case, rules)
            existing = context.existing
            if isinstance(existing, PadSpec):
                grid.add_pad(router_pad(existing, nets))
            else:
                grid.mark_route(single_object_route(existing, nets))

            candidate = context.candidate
            assert isinstance(candidate, SegmentSpec)
            seg = router_segment(candidate, nets)
            checker = VectorCollisionChecker(grid)

            if not checker.path_is_clear(
                seg.x1,
                seg.y1,
                seg.x2,
                seg.y2,
                layer_of(candidate.layer),
                seg.width,
                nets[candidate.net],
            ):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the predicate answers yes/no
                        required_mm=grid.rules.trace_clearance,
                    )
                )
        return found

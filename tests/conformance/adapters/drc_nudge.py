"""Group 17 -- the post-route DRC nudge's destination gate.

``router/drc_nudge.py`` translates already-routed copper by up to
``max_displacement`` to repair a DRC finding, and every proposal must pass a
destination gate before it is committed.
``_post_nudge_introduces_foreign_via_violation`` (``:370``) is the clearance
half of that gate: it walks every via in ``router.routes``, skips same-net
ones, and asks the shared clearance kernel about the rest at
``router.rules.trace_clearance``.  It reads nothing else off the router, so
the duck-typed stand-in below carries exactly ``routes`` and ``rules`` -- the
same shape ``tests/test_drc_nudge_foreign_via_gate.py:43``'s
``_StubAutorouter`` uses, and the reason no real ``Autorouter`` is built here.

**Pair kinds: ``seg-via`` only, and the other two entry points are recorded
rather than faked.**  The epic's group-17 inventory names three functions;
only one of them answers a clearance question this oracle can score:

* ``_via_drill_overlaps_bbox`` (``:1204``) is an *overlap* detector, not a
  clearance predicate -- it asks whether a drill circle intersects a pad
  land, with no clearance term at all (its threshold is the via-in-pad DRC
  rule, ``validate/rules/via_pad_geometry.via_inside_pad``).  Every pair in
  this corpus is placed at a positive copper gap, so it would answer "no
  overlap" on all of them: a guaranteed zero that means nothing.
* ``_via_edge_sweep_clear`` (``:2243``) is a *displacement certificate*
  against the board outline: it proves a proposed move introduces no new edge
  violation anywhere along the swept path, comparing the swept minimum to the
  pre-move minimum. It needs a before/after position pair and a board-edge
  polyline, neither of which a static close pair has.  The generator places
  no copper-to-edge pair at all (every slot sits 5 mm inside the outline), so
  there is nothing to score.

Both are ``not measured`` sub-notes on this row.  Scoring them would have
required inventing a production configuration, and this phase measures
consumers as they are.  Epic #5509 Phase 4c switched them onto the kernel
alongside the gate this row drives, and their verdict-for-verdict equivalence
with the pre-migration arithmetic is pinned directly instead, by
``tests/router/test_drc_nudge_kernel_5856.py``.

**Gated reading (Epic #5509 Phase 4c, #5856).**  Since the destination gate
moved onto the shared clearance kernel, this group is in
``report.MIGRATED_GROUPS`` and :meth:`DrcNudgeAdapter.verdicts_at_project_rules`
drives the same unmodified gate with ``trace_clearance`` moved onto
``case.rules.project_clearance``
(:func:`~tests.conformance.adapters._support.project_rules`), so the merge gate
is a **geometry** statement rather than a restatement of the #5398 / #5654
rule-resolution gap -- which is exactly what this row's published
under-rejection cell is: the gate is driven at the router's own 0.15 mm
``trace_clearance`` there, below the project's 0.20 mm ``Default`` netclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    net_ids,
    pair_contexts,
    project_rules,
    router_rules,
    router_segment,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PairKind, SegmentSpec, ViaSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["DrcNudgeAdapter"]

#: The one kind whose candidate is a segment and whose counterpart is a via.
SEG_VS_VIA_KINDS = frozenset({PairKind.SEG_VIA})


@dataclass
class _StubRouter:
    """The two attributes ``_post_nudge_introduces_foreign_via_violation`` reads.

    Mirrors ``tests/test_drc_nudge_foreign_via_gate.py``'s ``_StubAutorouter``.
    Building a real ``Autorouter`` would drag in a grid, a layer stack and an
    obstacle model that the gate never consults, and any of those could shift
    the answer for reasons that are not this consumer's arithmetic.
    """

    routes: list
    rules: object


class DrcNudgeAdapter:
    """Drives the nudge destination gate's foreign-via clearance check."""

    name = "drc_nudge"
    group = 17
    pair_kinds = SEG_VS_VIA_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same unmodified gate, at the clearance kicad-cli applies.

        The **gated** reading (Epic #5509 Phase 4c, #5856), required of every
        group in ``report.MIGRATED_GROUPS``.  Only the copper clearance moves
        onto ``case.rules.project_clearance``; which rule the gate resolves for
        a trace-vs-via pair is unchanged, because that is Phase 2's axis and
        not this migration's.
        """
        return self._verdicts(case, project_rules(case))

    def _verdicts(self, case: CopperCase, rules: DesignRules) -> set[Verdict]:
        from kicad_tools.router.drc_nudge import (
            _post_nudge_introduces_foreign_via_violation,
        )

        nets = net_ids(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            candidate, existing = context.candidate, context.existing
            assert isinstance(candidate, SegmentSpec) and isinstance(existing, ViaSpec)

            router = _StubRouter(routes=[single_object_route(existing, nets)], rules=rules)
            if _post_nudge_introduces_foreign_via_violation(
                router_segment(candidate, nets),
                router,  # type: ignore[arg-type]
            ):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the gate answers yes/no
                        required_mm=rules.trace_clearance,
                    )
                )
        return found

"""Group 9 -- the lattice engine's committed-copper spacing model.

Epic #5509 section 1's group 9 cites ``lattice/obstacles.py:583 seg_clear``,
``:656 node_clear`` and ``:709 via_clear``.  Those three line numbers land on
:class:`~kicad_tools.router.lattice.obstacles.CommittedCopper`, **not** on
``LatticeObstacleModel`` -- see the module-level correction note below.  That
matters for this adapter in two concrete ways, and both are why the row exists
at all rather than reading ``needs engine plumbing``.

**The predicates are directly constructible.**  ``CommittedCopper`` takes only
scalars (``num_layers`` plus the five derived gap values) and is populated
through ``add_run`` / ``add_via``.  No board file, no ``from_board``, no
triangulated lattice: the octilinear lattice and its pad masks are a
*different* model in the same module, consulted for site availability rather
than for clearance.  So this row needs none of the plumbing the phase spec
anticipated.

**The five gap values are the consumer's own, copied from its one production
call site.**  ``LatticePathfinder._fresh_committed`` (``pathfinder.py:460``)
builds the model, and ``pathfinder.py:252-258`` derives what it passes.  The
derivations are reproduced verbatim in :func:`_committed_for` with that
citation, because they are computed in the *pathfinder* and handed to the
obstacle model -- the same unavoidable re-derivation, handled the same way, as
``_support.trace_radius_cells``.

**Pads are out of scope, and that is a property of the consumer.**  None of
the three predicates consults pad copper: ``seg_clear`` reads ``fixed_fills``,
``self.copper[layer]`` (committed traces) and ``self.vias``; ``via_clear``
reads the same three plus ``via_copper``.  Pad keep-outs live on
``LatticeObstacleModel.pad_rects`` / ``node_pads`` and gate *site availability*
(``node_blocked`` / ``edge_blocked``), which is group 9's masking half and not
its clearance arithmetic.  Scoring ``pad-seg`` / ``pad-via`` here would
therefore measure a predicate that was never asked -- exactly what
``ConsumerAdapter.pair_kinds`` exists to prevent -- so the row declares routed
copper only.

**Net ids, not names.**  ``CommittedCopper`` keys nets by integer id
throughout (``cnet == net`` same-net exemptions), which is what
``_support.net_ids`` already provides; the adapter maps back to names when it
builds the :class:`~tests.conformance.adapters.Verdict`.

The pairwise projection is left ``None`` -- the default, and always the case
without ``--voltage-map``.  With it set the predicates widen to an HV pair
requirement, which is Phase 2's corpus (group 14's row says the same about
``set_pairwise_domains``).

Two rule settings, one consumer (Epic #5509 Phase 3d)
-----------------------------------------------------
Group 9 is switched onto the shared clearance kernel in Phase 3d, so its rows
stop being report-only.  That flip needs the *geometry* question separated
from the *rule-resolution* question, because only the first one moved:

* :meth:`LatticeAdapter.verdicts` -- unchanged, and still the table's row.
  It drives the consumer at the **router's** own numbers
  (``rules.trace_clearance`` = 0.15 mm in this corpus), which is why that row
  reports a large under-rejection rate: every one of those pairs sits in the
  0.15-0.20 mm band where the router requires less than the project's
  ``Default`` netclass does.  That is the #5398 / #5654 rule defect, measured
  on purpose, and it is not Phase 3d's to fix.
* :meth:`LatticeAdapter.verdicts_at_project_rules` -- the **gated** reading.
  Same unmodified consumer, same entry points, driven at the clearance
  kicad-cli itself applies (``case.rules.project_clearance``).  With the rule
  axis pinned to ground truth's own value, a disagreement can only be
  geometry -- exactly what this phase changed -- so
  ``test_corpus.test_adapter_agrees_with_kicad_cli`` asserts it hard for this
  group instead of xfailing it.

Neither reading patches or relaxes the consumer: the adapter has always
chosen which rule values to hand ``CommittedCopper``, and this is the same
choice made twice.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    layer_indexer,
    net_ids,
    pair_contexts,
    router_rules,
    router_segment,
    router_via,
)
from tests.conformance.generator import CopperCase, PairKind, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["LatticeAdapter", "ROUTED_COPPER_KINDS"]

#: Routed copper only -- the three predicates never consult a pad (see the
#: module docstring).
ROUTED_COPPER_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.VIA_VIA})


class LatticeAdapter:
    """Drives ``CommittedCopper.seg_clear`` / ``via_clear`` per close pair."""

    name = "lattice"
    group = 9
    pair_kinds = ROUTED_COPPER_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same consumer, driven at the clearance kicad-cli applies.

        The gated reading (see the module docstring).  Only the two copper
        clearances move onto ``project_clearance``; ``min_hole_to_hole`` and
        the via geometry stay the case's own, because those feed a *different*
        requirement (the drill floor) that kicad-cli scores under a different
        verdict kind and this row does not claim.
        """
        rules = router_rules(case)
        return self._verdicts(
            case,
            dataclasses.replace(
                rules,
                trace_clearance=case.rules.project_clearance,
                via_clearance=case.rules.project_clearance,
            ),
        )

    def _verdicts(self, case: CopperCase, rules: DesignRules) -> set[Verdict]:
        nets = net_ids(case)
        layer_index = layer_indexer(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue

            committed = _committed_for(case, rules)
            _commit_existing(committed, context.existing, nets, layer_index)

            candidate = context.candidate
            net = nets[candidate.net]
            if isinstance(candidate, SegmentSpec):
                seg = router_segment(candidate, nets)
                clear = committed.seg_clear(
                    (seg.x1, seg.y1),
                    (seg.x2, seg.y2),
                    layer_index(candidate.layer),
                    net,
                    seg.width / 2.0,
                    rules.trace_clearance,
                )
                required = rules.trace_clearance
            else:
                via = router_via(candidate, nets)
                clear = committed.via_clear((via.x, via.y), net, rules.via_clearance)
                required = rules.via_clearance

            if not clear:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        # The predicates answer yes/no and nothing else.
                        # Since Phase 3d they measure an edge-to-edge gap
                        # internally (through the kernel), but none of it is
                        # returned, so reporting a number here would mean
                        # re-deriving one -- this harness's arithmetic wearing
                        # the consumer's name.
                        gap_mm=None,
                        required_mm=required,
                    )
                )
        return found


def _committed_for(case: CopperCase, rules):
    """A fresh ``CommittedCopper`` carrying the case's own derived gaps.

    Verbatim from ``LatticePathfinder.__init__`` (``pathfinder.py:252-258``)
    and ``_fresh_committed`` (``:460``), which is the model's only production
    construction site.  ``pairwise`` stays ``None`` -- the pre-#4602 scalar
    path, and the only path reachable without ``--voltage-map``.
    """
    from kicad_tools.router.lattice.obstacles import CommittedCopper

    return CommittedCopper(
        case.layers,
        trace_half=rules.trace_width / 2.0,
        clearance=rules.trace_clearance,
        via_radius=rules.via_diameter / 2.0,
        via_via_gap=rules.via_diameter + rules.trace_clearance,
        same_net_via_gap=rules.via_drill + rules.min_hole_to_hole,
    )


def _commit_existing(committed, existing, nets: dict[str, int], layer_index) -> None:
    """Commit the pair's counterpart as the negotiator would have.

    ``add_run`` for a segment (at its true half-width and the board clearance,
    the ``_conn_geometry`` default), ``add_via`` for a via.  A pad is never
    reached: ``pair_kinds`` excludes every pad kind.
    """
    if isinstance(existing, SegmentSpec):
        seg = router_segment(existing, nets)
        committed.add_run(
            layer_index(existing.layer),
            [(seg.x1, seg.y1), (seg.x2, seg.y2)],
            seg.net,
            seg.width / 2.0,
        )
    else:
        via = router_via(existing, nets)
        committed.add_via((via.x, via.y), via.net, radius=via.diameter / 2.0)

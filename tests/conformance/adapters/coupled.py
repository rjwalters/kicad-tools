"""Group 7 -- the C++ coupled diff-pair search's rail clearance gate.

Phase 1c shipped this group as the epic's **one unwired row**: at the time
``CoupledPathfinder::rail_clear`` was a lambda inside ``route()``'s expansion
loop, so ``bindings.cpp`` could not reach it and ``report.NOT_MEASURED_REASONS``
recorded it as *unexposed* rather than merely unmeasured.  Epic #5509 Phase 3c
(#5662) -- the phase that migrates this consumer onto the shared clearance
kernel -- promoted it to a named method and bound it, which is what makes this
adapter possible.  Nineteen of nineteen groups are measured now.

**What the gate answers.**  For one candidate rail step it asks whether the
copper that step would lay down keeps clearance from (a) the grid's fixed
fills and (b) its **stored route geometry** -- the committed segments and
barrels registered through ``add_stored_segment`` / ``add_stored_via``.  (b)
is new in this phase and is the #4507 fix: the old lambda consulted fixed
fills alone, so copper routed earlier in the same session, before the C++
blocked plane was re-synced, was invisible to the coupled search *by
construction*.

**Driven in world millimetres, on purpose.**  ``rail_clear`` takes grid cells
because the search speaks grid cells; ``rail_clear_world`` is the same gate
one conversion earlier.  Driving the grid form here would snap the corpus's
copper onto a 0.127 mm raster before the kernel ever saw it, and this row
would then measure the *quantisation* rather than group 7's clearance model.
The two entry points share one body, so nothing is bypassed by choosing the
world one.

**Pair kinds: routed copper only.**  ``rail_clear`` never consults ``pads_``
-- pad copper reaches the coupled search through the blocked plane (groups
1-3) and through the Python constructor's own exact pad gate (group 8).
Scoring ``pad-seg`` / ``pad-via`` here would record the absence of a check
that lives elsewhere, which is exactly what ``ConsumerAdapter.pair_kinds``
exists to prevent.  Zone and edge kinds are out for the same reason: the
fixed-fill branch is group 6's arithmetic, measured on its own row.

**Two readings, one consumer.**  :meth:`CoupledRailAdapter.verdicts` is the
*published* one -- the gate at the router's own resolved rule values, which is
what the table's percentages report.  :meth:`~CoupledRailAdapter.
verdicts_at_project_rules` is the **gated** one: the same unmodified entry
point driven at ``case.rules.project_clearance``, the value kicad-cli itself
applies, so a disagreement can only be geometry -- exactly what this phase
changed.  ``test_corpus.test_adapter_agrees_with_kicad_cli`` asserts that
reading hard for this group instead of xfailing it.

Requires the compiled extension; without it group 7 renders ``not measured``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    cpp_grid_for,
    layer_indexer,
    net_ids,
    pair_contexts,
    router_cpp_module,
    router_rules,
    router_segment,
    router_via,
)
from tests.conformance.generator import CopperCase, PairKind, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["CoupledRailAdapter"]

#: ``rail_clear`` measures against stored segments and stored vias only.
ROUTED_COPPER_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.VIA_VIA})

#: "No diff-pair partner" -- the sentinel ``route()`` passes for a rail whose
#: partner net is not a real net id.  A corpus case declares no pairs, so every
#: counterpart here is ordinary foreign copper.
NO_PARTNER_NET = -1


class CoupledRailAdapter:
    """Drives ``CoupledPathfinder::rail_clear_world`` on a bare ``Grid3D``."""

    name = "coupled_rail"
    group = 7
    pair_kinds = ROUTED_COPPER_KINDS

    def available(self) -> bool:
        return router_cpp_module() is not None

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same gate, driven at the clearance kicad-cli itself applies.

        The **gated** reading (Epic #5509 Phase 3d's mechanism, reused here
        for Phase 3c's two groups).  Only the two copper clearances move onto
        ``case.rules.project_clearance``; the via geometry and
        ``min_hole_to_hole`` stay the case's own, because they feed the drill
        floor -- a different requirement kicad-cli scores under a different
        verdict kind, which this row does not claim.

        Nothing is patched or relaxed: ``rail_clear_world`` has always taken
        the rail's clearance as an argument, so this is the same choice the
        search makes, made twice.
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
        router_cpp = router_cpp_module()
        if router_cpp is None:  # pragma: no cover - guarded by available()
            return set()

        nets = net_ids(case)
        layer_index = layer_indexer(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue

            grid = cpp_grid_for(router_cpp, case, rules)
            existing = context.existing
            # Only the physical geometry is registered -- no ``mark_*`` call.
            # That is the point of the row: ``rail_clear`` is the gate that is
            # supposed to see committed copper *without* depending on the
            # blocked plane having been re-synced with it (#4507).
            if isinstance(existing, SegmentSpec):
                seg = router_segment(existing, nets)
                grid.add_stored_segment(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    seg.width,
                    layer_index(existing.layer),
                    seg.net,
                )
            else:
                via = router_via(existing, nets)
                grid.add_stored_via(
                    via.x,
                    via.y,
                    via.drill,
                    via.diameter,
                    via.net,
                    None,
                    layer_index(existing.layers[0]),
                    layer_index(existing.layers[1]),
                )

            pathfinder = _coupled_pathfinder(router_cpp, grid, case, rules)
            candidate = context.candidate

            if isinstance(candidate, SegmentSpec):
                seg = router_segment(candidate, nets)
                clear = pathfinder.rail_clear_world(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    layer_index(candidate.layer),
                    seg.net,
                    NO_PARTNER_NET,
                    seg.width / 2.0,
                    rules.trace_clearance,
                    False,
                )
                required = rules.trace_clearance
            else:
                via = router_via(candidate, nets)
                # A via candidate carries no span: the search asks about the
                # drop cell itself, and the gate reads its copper radius and
                # clearance from the rules (``is_via=True`` ignores the two
                # rail overrides), which is why ``router_rules`` seeds
                # ``via_diameter`` from the case's own via.
                clear = pathfinder.rail_clear_world(
                    via.x,
                    via.y,
                    via.x,
                    via.y,
                    layer_index(candidate.layers[0]),
                    via.net,
                    NO_PARTNER_NET,
                    -1.0,
                    -1.0,
                    True,
                )
                required = rules.via_clearance

            if not clear:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the gate answers yes/no, not a distance
                        required_mm=required,
                    )
                )
        return found


def _coupled_pathfinder(router_cpp, grid, case: CopperCase, rules):
    """A ``CoupledPathfinder`` over ``grid``, constructed as production does.

    Every constructor scalar except the grid and the rules is a *search*
    parameter -- target/min spacing, the trail radii, the heuristic weight --
    and ``rail_clear`` reads none of them.  They are set to the neutral values
    ``diffpair_routing.py``'s C++ hand-off passes for a one-cell pair so the
    construction is a real one rather than a bag of zeros.
    """
    cpp_rules = router_cpp.DesignRules()
    cpp_rules.trace_width = rules.trace_width
    cpp_rules.trace_clearance = rules.trace_clearance
    cpp_rules.via_diameter = rules.via_diameter
    cpp_rules.via_drill = rules.via_drill
    cpp_rules.via_clearance = rules.via_clearance
    cpp_rules.grid_resolution = rules.grid_resolution
    cpp_rules.min_hole_to_hole = rules.min_hole_to_hole
    cpp_rules.min_drill_clearance = rules.min_drill_clearance
    del case
    return router_cpp.CoupledPathfinder(
        grid,
        cpp_rules,
        1,  # target_spacing_cells
        0,  # min_spacing_cells
        1,  # trace_half_width_cells
        1,  # via_extra_cells
        0,  # via_drill_cells
        0.0,  # spacing_penalty_factor
        1.0,  # heuristic_weight
    )

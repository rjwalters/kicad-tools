"""Group 8 -- the diff-pair constructor's three clearance gates.

Epic #5509 section 1's group 8 names four entry points on
``DiffPairRouter``.  Three of them are gates the coupled constructor consults
before it will emit copper, and this row drives all three:

=========================  =====================================================
``_segment_cells_clear``   Walks the candidate span's grid cells through the
                           **Python** ``CoupledPathfinder._is_cell_blocked``.
                           Raster model -- the halo the Python grid marked.
``_segment_pad_clear``     The #4571 *exact* foreign-pad gate, via
                           ``RoutingGrid.worst_segment_pad_deficit``.  True
                           geometry, and it deliberately passes
                           ``exclude_net`` with **no** ``exclude_refs`` so the
                           #3545 same-component carve-out cannot exempt a
                           partner pad.
``_route_via_clear``       The #4575 foreign-via gate over an assembled
                           ``Route``.
=========================  =====================================================

A candidate is flagged when **any** of the three applicable gates refuses it,
which is exactly how the constructor uses them: they are consulted in series
and the first refusal declines the span.  Reporting them as one row rather
than three is what the epic's inventory asks for, and the sub-entry-point the
row does *not* cover is named in ``report.NOTES``.

**The fourth entry point, ``find_intra_pair_clearance_violations``
(``:1198``), is deliberately not driven here.**  It measures a P/N pair
against *itself* -- intra-pair spacing between two nets that are declared
partners -- and this corpus has no declared diff pairs: every generated object
carries its own independent net precisely so a kicad-cli row maps onto a pair
by net alone.  Feeding it two unrelated nets would score it on a question it
is never asked in production.  Recorded as a stated gap rather than a silent
one.

**Construction, and why it is honest.**  The precedent is
``tests/test_diffpair_phase_b.py:135`` -- ``Autorouter(width, height,
rules=...)`` then ``DiffPairRouter(router)`` -- with the board dimensions,
origin and layer stack taken from the case so the grid raster is the same one
group 1 measures.  ``force_python=True`` is set for one reason: the entry
point under test takes the **Python** ``CoupledPathfinder`` (it calls
``pathfinder._is_cell_blocked``), not ``CppCoupledPathfinder``, so a C++
backend on the autorouter would leave the Python grid's blocked plane
authoritative for this gate while silently costing a second marking pass.  No
consumer code is patched, nothing is monkeypatched, and no private helper is
added on the consumer side.

**Every pair kind with a routing candidate is in scope.**  The raster gate
answers about any candidate span; the pad gate adds the exact pad reading for
``pad-seg``; the via gate covers via counterparts.  A via *candidate* has no
span, so it is put through the via and pad gates as a one-object ``Route``
exactly as the constructor does.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    layer_indexer,
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    router_segment,
    router_via,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["DiffPairAdapter"]

#: The consumer's own geometric noise floor for a pad deficit
#: (``diffpair_routing._SHADOW_PAD_DEFICIT_EPS``).  Restated rather than
#: imported so the harness never depends on a private consumer constant.
PAD_DEFICIT_EPS = 1e-4


class DiffPairAdapter:
    """Drives the coupled constructor's raster, pad and via gates per pair."""

    name = "diffpair"
    group = 8
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same three gates, at the clearance kicad-cli itself applies.

        The **gated** reading (Epic #5509 Phase 3c, #5662).  Only the two
        copper clearances move onto ``case.rules.project_clearance``; the via
        geometry and ``min_hole_to_hole`` stay the case's own, because those
        feed the drill floor -- a different requirement kicad-cli scores under
        a different verdict kind, which this row does not claim.

        Nothing is patched: the ``DesignRules`` handed to ``Autorouter`` have
        always been this adapter's own choice, and this is the same choice
        made twice.  The raster gate re-marks its grid from the new rules for
        free, because the router is constructed per pair.
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

            router, dpr = _diffpair_router(case, rules)
            _register_existing(router, context.existing, nets)

            candidate = context.candidate
            clear = True

            if isinstance(candidate, SegmentSpec):
                seg = router_segment(candidate, nets)
                pathfinder = _coupled_pathfinder(router)
                clear = dpr._segment_cells_clear(
                    pathfinder,
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    layer_index(candidate.layer),
                    seg.net,
                )
                if clear:
                    clear = dpr._segment_pad_clear(seg)
                required = rules.trace_clearance
            else:
                # The barrel sibling of ``_segment_pad_clear``: production
                # gates a constructed via against foreign pads through
                # ``_via_pad_deficit`` / ``_route_pad_violation`` (#4571), so
                # a ``pad-via`` pair has a real call to make here.  Omitting
                # it recorded a confident "clear" for the one pair kind only
                # this gate answers (Epic #5509 Phase 3c, #5662).
                clear = dpr._via_pad_deficit(router_via(candidate, nets)) <= PAD_DEFICIT_EPS
                required = rules.via_clearance

            if clear:
                # The via gate is an assembled-route gate in production, so it
                # is fed the same one-object route for either candidate kind --
                # inside the consumer's own arming context manager, because
                # outside it ``_shadow_foreign_universe`` is ``None`` and
                # ``_route_via_violation`` short-circuits to ``(0.0, None)``.
                # Driving it unarmed would have produced a confident zero for
                # this third of the row; ``_shadow_foreign_copper`` builds the
                # universe from ``autorouter.routes`` exactly as the
                # constructor does, so nothing is invented here.
                with dpr._shadow_foreign_copper():
                    clear = dpr._route_via_clear(single_object_route(candidate, nets))

            if not clear:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        # Three gates of two different families (raster cells
                        # and exact deficits) share this row, so there is no
                        # single millimetre answer to attribute to it.
                        gap_mm=None,
                        required_mm=required,
                    )
                )
        return found


def _diffpair_router(case: CopperCase, rules):
    """A fresh ``Autorouter`` + ``DiffPairRouter`` in the case's own frame.

    ``force_python=True``: the gate under test consults the *Python* coupled
    pathfinder against the *Python* grid's blocked plane (see the module
    docstring).  Boards are written with ``center=False``
    (``tests/conformance/board.py``), so the origin is ``(0, 0)``.
    """
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair_routing import DiffPairRouter
    from tests.conformance.adapters._support import layer_stack_for

    router = Autorouter(
        width=case.width,
        height=case.height,
        origin_x=0.0,
        origin_y=0.0,
        rules=rules,
        layer_stack=layer_stack_for(case),
        force_python=True,
        physics_enabled=False,
    )
    return router, DiffPairRouter(router)


def _coupled_pathfinder(router):
    """The Python ``CoupledPathfinder`` over the router's grid.

    Same construction as ``diffpair_routing.py:4494`` --
    ``CoupledPathfinder(grid, rules, 1)`` -- which is the constructor's own
    stub-tail call site.  The spacing argument is irrelevant to
    ``_is_cell_blocked``; 1 is what that call site passes.
    """
    from kicad_tools.router.diffpair_routing import CoupledPathfinder

    return CoupledPathfinder(router.grid, router.rules, 1)


def _register_existing(router, existing, nets: dict[str, int]) -> None:
    """Put the pair's counterpart on the router as existing copper.

    A pad goes through ``RoutingGrid.add_pad`` (the same call group 1's row
    makes), which also appends it to the grid's pad registry -- what
    ``worst_segment_pad_deficit`` reads -- so the exact pad gate and the raster
    gate see one pad, not two models of it.

    A segment or via is marked as a committed route **and** appended to
    ``router.routes``.  Both are required and for different gates: the marking
    feeds the raster gate's blocked plane, and ``router.routes`` is the set
    ``_shadow_foreign_copper`` walks to build the foreign universe the via gate
    measures against.
    """
    if isinstance(existing, PadSpec):
        router.grid.add_pad(router_pad(existing, nets))
        return

    route = single_object_route(existing, nets)
    router.grid.mark_route(route)
    router.routes.append(route)

"""Group 4 -- ``RouteHaloGeometry.clear``, the search-time refinement.

A* does not ask this predicate about every cell: it asks it about a cell that
the raster already says is blocked, to find out whether the block is a real
geometric conflict or just halo.  So the adapter reproduces that setup exactly
-- it shares :mod:`tests.conformance.adapters.grid_py`'s grid construction,
marks the pair's counterpart with ``mark_route`` (which is what *records* the
halo marks and their backing geometry), and then calls::

    grid._route_halo.clear(candidate, router, require_geometry=True)

``require_geometry=True`` is the production setting for the refinement path and
is why the counterpart must be marked first: with no marks the predicate
refuses, by design, rather than authorising a relaxation it cannot justify.
The ``Router`` is needed only as the carrier of ``rules``,
``_route_halo_names``, ``_halo_net_class`` and ``_attach_zones``; the precedent
for building one this way is ``tests/test_python_route_halo_clearance.py``'s
``_context``.

**Pair kinds are narrowed to segment/via pairs.**  ``clear`` walks the halo's
recorded *route* objects; pad copper is never a halo mark (pads are static
blockage laid down by ``add_pad``, and ``cell_known`` deliberately returns
``False`` for those cells), so this consumer is never consulted about a pad in
production either.  Counting ``pad-seg`` / ``pad-via`` pairs here would record
the absence of a check that lives in group 1, not a disagreement in group 4.

The disagreement this row is expected to carry is the one
``search-vs-commit-seg-via-max`` reproduces: for a trace candidate against a
via, ``clear`` raises the requirement to ``max(required, via_clearance)``,
so it refuses a 0.18 mm gap that the commit gates (and kicad-cli, at a 0.15 mm
project class) both accept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    net_ids,
    pair_contexts,
    project_rules,
    router_grid,
    router_segment,
    router_via,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PairKind, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["RouteHaloAdapter"]


class RouteHaloAdapter:
    """Drives ``RouteHaloGeometry.clear`` against marked route copper."""

    name = "route_halo"
    group = 4
    pair_kinds = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.VIA_VIA})

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, None)

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same unmodified consumer, driven at the clearance kicad-cli applies.

        The **gated** reading (:func:`~tests.conformance.adapters._support.project_rules`).
        This row's published percentages keep the router's own
        ``trace_clearance``, and its under-rejection cell is entirely that
        choice: every under-rejected pair is a ``seg-seg`` one sitting in the
        0.15-0.20 mm band the router does not require and the project's
        ``Default`` netclass does (#5398 / #5654).  Pinning the rule axis to
        ground truth's own number leaves **geometry** as the only thing a
        disagreement can be -- which is what
        ``tests/conformance/test_route_halo_refinement_gate.py`` gates #5410's
        repair on.
        """
        return self._verdicts(case, project_rules(case))

    def _verdicts(self, case: CopperCase, rules: DesignRules | None) -> set[Verdict]:
        from kicad_tools.router.pathfinder import Router

        nets = net_ids(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            grid = router_grid(case, rules)
            grid.mark_route(single_object_route(context.existing, nets))

            router = Router(grid, grid.rules)
            router.set_net_name_to_id(dict(nets))

            candidate = context.candidate
            if isinstance(candidate, SegmentSpec):
                probe = router_segment(candidate, nets)
                required = grid.rules.trace_clearance
            else:
                probe = router_via(candidate, nets)
                required = grid.rules.via_clearance

            if not grid._route_halo.clear(probe, router):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the predicate answers yes/no, not a distance
                        required_mm=required,
                    )
                )
        return found

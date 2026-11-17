"""Group 10 -- the mesh engine's per-leg obstacle consult.

Epic #5509 section 1's group 10 is ``mesh/obstacles.py``'s ``is_clear`` -- the
predicate the octilinear 45-fit checks every dogleg leg against before it will
emit a corridor.  The phase spec expected this row to need
``MeshPathfinder.from_board`` and to be limited to seg-vs-zone / seg-vs-edge.
Re-reading the consumer at build time says otherwise on both counts, and the
corrections are what let this row be measured on the existing corpus:

**``ObstacleModel`` is directly constructible** (``outline``, ``keepouts``,
``pours``, plus keyword ``fixed_fills`` / ``layer`` / ``half`` / ``clearance``
/ ``pads`` / ``edge_clearance``).  The triangulated navmesh is a *separate*
object; ``is_clear`` never touches it.  No board file and no ``from_board`` are
needed, so the curved-outline restriction on that constructor does not reach
this row either.

**Pads and routed copper are both in scope.**  Production builds the model in
``mesh/pathfinder.py``'s ``_route_with_portals``: ``pads`` is every
**other-net pad** verbatim (``_foreign_pads``), and ``pours`` is the pour
outlines *plus* the inflated capsule of every **committed cross-net trace**
(``_route_obstacles``).  So the predicate answers ``pad-seg`` and ``seg-seg``,
which is what this row measures.

**Two kinds stay out of scope, and both are properties of the consumer.**  A
via counterpart is not in the model at all -- committed vias are handled by
``_route_via_injection``, never by the per-leg consult -- so ``seg-via`` and
``via-via`` are excluded.  And the *candidate* is always a straight leg
centreline (``is_clear(a, b)`` takes two points and no width of its own), so a
via candidate has no call to make.  That rules out ``via-zone`` for the same
reason it rules out ``seg-via``.

Two rule settings, one consumer (Epic #5509 Phase 3e)
-----------------------------------------------------
Group 10 is switched onto the shared clearance kernel in Phase 3e, so its rows
stop being report-only.  As for group 9, that flip needs the *geometry*
question separated from the *rule-resolution* question:

* :meth:`MeshAdapter.verdicts` -- the table's row.  It drives the consumer at
  the **router's** own numbers (``rules.trace_clearance`` = 0.15 mm here) and
  with **no board-edge rule at all**: ``MeshPathfinder.edge_clearance`` is the
  copper-to-edge floor the owning ``Autorouter`` already resolved
  (``_ensure_mesh_pathfinder`` hands over ``Autorouter._edge_clearance``, the
  same value the grid's ``add_edge_keepout`` and the lattice's
  ``set_escape_boundary`` are given), and the corpus configures none, so it is
  ``0.0``.  Both gaps are rule-selection readings -- the #5398 / #5654 axis --
  and neither is Phase 3e's to fix.
* :meth:`MeshAdapter.verdicts_at_project_rules` -- the **gated** reading.  Same
  unmodified consumer, same entry point, driven at the two rules kicad-cli
  itself applies: ``case.rules.project_clearance`` for copper-to-copper and
  ``case.rules.min_copper_to_edge`` for copper-to-outline (the ``.kicad_pro``
  board rule its ``copper_edge_clearance`` rows are drawn against).  With the
  rule axis pinned to ground truth, a disagreement can only be geometry.

What Phase 3e moved, measured
-----------------------------
Before it, two branches of ``is_clear`` were the consumer's own arithmetic and
both disagreed with kicad-cli in a direction the epic exists to remove:

* a pad entered as ``pad_half_extents`` -- the pad's **bounding box** -- grown
  by the agent radius, so a legal candidate was refused during search (#5410's
  failure mode) for every circle, oval, roundrect or rotated pad;
* the ``outline`` branch was a bare ``point_in_polygon`` containment test on
  the leg's two endpoints, with no clearance term at all, so copper 0.1 mm
  inside ``Edge.Cuts`` read as "inside the board".

Both are now kernel measurements against the real copper, taken through
``router/mesh/kernel_adapter.py``, and this adapter follows production in
passing the pad objects and the edge requirement rather than a pre-inflated
box.  A **committed trace** is still a capsule inflated by a full
``trace_width + clearance`` (``_route_obstacles``' own over-approximation,
square end-caps included): that is unchanged by this phase, so the adapter
reproduces it from that one call site and builds the capsule with the
consumer's own ``_segment_capsule`` rather than restating its vertex
arithmetic.

``fixed_fills`` is left ``None``: group 6 already measures
``FixedFillObstacles`` directly, on both its Python and native halves, and
feeding it here would re-measure that row under this one's name.

The **pours** branch (``seg-zone``) is still a touch test against the pour
outline with no clearance term -- production passes ``pour_outlines``
verbatim -- and is still measured as such.  ``test_corpus`` scores zone pairs
only on its refilled run, which is report-only for every group, so the gate
does not reach that branch.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, KIND_COPPER_EDGE, Verdict
from tests.conformance.adapters._support import (
    BoardEdgeRef,
    layer_indexer,
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    router_segment,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec, ZoneSpec

__all__ = ["LEG_PAIR_KINDS", "MeshAdapter"]

#: The kinds ``is_clear`` can answer: a straight-leg candidate against foreign
#: pad copper, a committed trace's capsule, a pour outline or the board
#: outline.  See the module docstring on why every via *candidate* kind is
#: excluded -- including ``via-zone``.
LEG_PAIR_KINDS = frozenset(
    {
        PairKind.SEG_SEG,
        PairKind.PAD_SEG,
        PairKind.SEG_ZONE,
        PairKind.COPPER_EDGE,
    }
)


class MeshAdapter:
    """Drives ``ObstacleModel.is_clear`` on each close pair's segment leg."""

    name = "mesh"
    group = 10
    pair_kinds = LEG_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        """The consumer at its own resolved rule values (the table's row).

        The board-edge floor is ``0.0`` because that is what production
        resolves here: ``MeshPathfinder.edge_clearance`` defaults to ``0.0``
        and is only raised by an owning ``Autorouter`` that resolved one, which
        the corpus does not configure.  That is why the published row's
        ``copper-edge`` cell is non-zero.
        """
        rules = router_rules(case)
        return self._verdicts(case, rules.trace_clearance, 0.0)

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same consumer, driven at the two rules kicad-cli applies.

        The gated reading (see the module docstring).  ``project_clearance`` is
        the ``Default`` netclass copper-to-copper value and
        ``min_copper_to_edge`` the ``.kicad_pro`` board rule; nothing else about
        the consumer or the obstacle projection changes.
        """
        return self._verdicts(
            case,
            case.rules.project_clearance,
            case.rules.min_copper_to_edge,
        )

    def _verdicts(self, case: CopperCase, clearance: float, edge_clearance: float) -> set[Verdict]:
        from kicad_tools.router.mesh.obstacles import ObstacleModel

        nets = net_ids(case)
        rules = router_rules(case)
        layer_index = layer_indexer(case)
        outline = _board_outline(case)
        capsule_half = rules.trace_width + clearance
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            candidate = context.candidate
            assert isinstance(candidate, SegmentSpec)  # LEG_PAIR_KINDS is seg-candidate only

            pads, pours = _obstacles_for(context.existing, nets, capsule_half)
            seg = router_segment(candidate, nets)
            model = ObstacleModel(
                outline,
                [],
                pours,
                fixed_fills=None,
                layer=layer_index(candidate.layer),
                half=seg.width / 2.0,
                clearance=clearance,
                pads=pads,
                edge_clearance=edge_clearance,
            )

            if not model.is_clear((seg.x1, seg.y1), (seg.x2, seg.y2)):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        _kind_for(context.kind),
                        net_a,
                        net_b,
                        # The predicate answers yes/no and nothing else: the gap
                        # it measures internally (through the kernel) is never
                        # returned, and re-deriving one here would be this
                        # harness's arithmetic wearing the consumer's name.
                        gap_mm=None,
                        required_mm=(
                            edge_clearance if context.kind in PairKind.EDGE else clearance
                        ),
                        zone=context.kind in PairKind.ZONE,
                    )
                )
        return found


def _kind_for(pair_kind: str) -> str:
    """The verdict kind a flagged pair of this pair kind reports.

    Verdict identity is ``(kind, nets)`` and the report compares on nets
    alone, so this only has to be *honest* rather than load-bearing: a
    ``copper-edge`` refusal is a copper-to-outline finding and says so.
    """
    return KIND_COPPER_EDGE if pair_kind in PairKind.EDGE else KIND_CLEARANCE


def _board_outline(case: CopperCase) -> list[tuple[float, float]]:
    """The case's board rectangle, CCW.

    Boards are written with ``center=False`` (``tests/conformance/board.py``),
    so the outline is ``(0, 0)`` to ``(width, height)``.  Passing the real
    outline rather than ``[]`` is what keeps ``is_clear``'s containment and
    copper-to-edge branches live, as they are in production.
    """
    return [(0.0, 0.0), (case.width, 0.0), (case.width, case.height), (0.0, case.height)]


def _obstacles_for(existing, nets: dict[str, int], capsule_half: float):
    """``(pads, pours)`` for the pair's counterpart, as production builds them.

    A pad is passed **verbatim** (``_foreign_pads``, ``mesh/pathfinder.py``):
    since Epic #5509 Phase 3e the model measures the pad's real copper through
    the clearance kernel instead of inflating its bounding box, so there is no
    rectangle for this adapter to reconstruct and no clamp to reproduce.

    A segment becomes one inflated capsule polygon (``_route_obstacles``),
    built by the consumer's own ``_segment_capsule`` so the vertex arithmetic
    is not restated here.

    A **pour** becomes its declared boundary, verbatim and *un*-inflated --
    that is exactly what ``_route_obstacles`` passes (``pour_outlines``), and
    inflating it here would be this adapter inventing a clearance term the
    consumer does not have.  The **board edge** contributes no obstacle at
    all: ``is_clear`` answers it through the ``outline`` and ``edge_clearance``
    arguments, which every call already carries.
    """
    from kicad_tools.router.mesh.pathfinder import _segment_capsule

    pads: list[object] = []
    pours: list[list[tuple[float, float]]] = []

    if isinstance(existing, BoardEdgeRef):
        return pads, pours

    if isinstance(existing, ZoneSpec):
        pours.append([(x, y) for x, y in existing.boundary])
        return pads, pours

    if isinstance(existing, PadSpec):
        pads.append(router_pad(existing, nets))
    else:
        assert isinstance(existing, SegmentSpec)  # a via is not in this model
        seg = router_segment(existing, nets)
        poly = _segment_capsule((seg.x1, seg.y1), (seg.x2, seg.y2), capsule_half)
        if poly is not None:
            pours.append(poly)

    return pads, pours

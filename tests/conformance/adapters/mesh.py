"""Group 10 -- the mesh engine's per-leg obstacle consult.

Epic #5509 section 1's group 10 is ``mesh/obstacles.py:118 is_clear`` -- the
predicate the octilinear 45-fit checks every dogleg leg against before it will
emit a corridor.  The phase spec expected this row to need
``MeshPathfinder.from_board`` and to be limited to seg-vs-zone / seg-vs-edge.
Re-reading the consumer at build time says otherwise on both counts, and the
corrections are what let this row be measured on the existing corpus:

**``ObstacleModel`` is directly constructible**
(``mesh/obstacles.py:99``: ``outline``, ``keepouts``, ``pours``, plus keyword
``fixed_fills`` / ``layer`` / ``half`` / ``clearance``).  The triangulated
navmesh is a *separate* object; ``is_clear`` never touches it.  No board file
and no ``from_board`` are needed, so the curved-outline restriction on that
constructor does not reach this row either.

**Pads and routed copper are both in scope.**  Production builds the model at
``mesh/pathfinder.py:303-313``: ``keepouts`` is every **other-net pad**
inflated by the agent radius (``_keepouts``, ``:540``), and ``pours`` is the
pour outlines *plus* the inflated capsule of every **committed cross-net
trace** (``_route_obstacles``, ``:917``).  So the predicate answers
``pad-seg`` and ``seg-seg``, which is what this row measures.

**Two kinds stay out of scope, and both are properties of the consumer.**  A
via counterpart is not in the model at all -- committed vias are handled by
``_route_via_injection``, never by the per-leg consult -- so ``seg-via`` and
``via-via`` are excluded.  And the *candidate* is always a straight leg
centreline (``is_clear(a, b)`` takes two points and no width), so a via
candidate has no call to make.  That rules out ``via-zone`` for the same
reason it rules out ``seg-via``.

**The `pours` and `outline` branches are measured too (#5644).**  They are the
other half of ``is_clear`` and, until the generator could place a zone and an
edge pair, they were dead code as far as this row was concerned:

* ``seg-zone`` reaches the ``pours`` branch.  Production passes pour
  *outlines* (``_route_obstacles``), so that is what this adapter passes, and
  the predicate is a bare ``segment_intersects_polygon`` -- **no clearance
  term at all**.  A leg that merely comes close to a pour is accepted.
* ``copper-edge`` reaches the ``outline`` branch, which is a
  ``point_in_polygon`` containment test on the leg's two endpoints against the
  *un-inset* board outline -- again with no clearance term.  Copper 0.1 mm
  inside the edge is "inside the board", so the branch accepts it.

Both readings are the consumer's own arithmetic, faithfully driven; the
under-rejection they produce on those kinds is the finding, not a harness
approximation.  In production the mesh engine keeps legs away from the outline
through the *navmesh triangulation* rather than through this predicate -- but
the epic's group 10 citation is ``is_clear``, and what ``is_clear`` does with
a pour and an outline is now measured instead of assumed.

**The inflation radii are the consumer's own, and they are the interesting
part of this row.**  A pad keep-out grows by ``trace_width / 2 + clearance``
(the agent radius), which is exactly right for a centreline query.  A
committed trace's capsule grows by ``trace_width + clearance`` -- a *full*
trace width, not a half -- because the capsule is built to cover "both traces'
half-widths plus the clearance gap on a single-width board"
(``_route_obstacles``' own docstring).  On a board where every trace is the
board-global width that is exact; the capsule's square end-caps then extend
``trace_width + clearance`` past each endpoint rather than the
``half + clearance`` the geometry requires, which is a longitudinal
over-approximation of the same family as group 1/2's square halo.  Both radii
are reproduced here from that one call site, and the capsule itself is built by
importing the consumer's own ``_segment_capsule`` so its vertex arithmetic is
not restated.

``fixed_fills`` is left ``None``: group 6 already measures
``FixedFillObstacles`` directly, on both its Python and native halves, and
feeding it here would re-measure that row under this one's name.
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

#: The kinds ``is_clear`` can answer: a straight-leg candidate against pad
#: keep-outs, a committed trace's capsule, a pour outline or the board
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
        from kicad_tools.router.mesh.obstacles import ObstacleModel

        nets = net_ids(case)
        rules = router_rules(case)
        layer_index = layer_indexer(case)
        outline = _board_outline(case)
        agent_radius = rules.trace_width / 2.0 + rules.trace_clearance
        capsule_half = rules.trace_width + rules.trace_clearance
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            candidate = context.candidate
            assert isinstance(candidate, SegmentSpec)  # LEG_PAIR_KINDS is seg-candidate only

            keepouts, pours = _obstacles_for(
                context.existing, nets, agent_radius, capsule_half, case
            )
            seg = router_segment(candidate, nets)
            model = ObstacleModel(
                outline,
                keepouts,
                pours,
                fixed_fills=None,
                layer=layer_index(candidate.layer),
                half=seg.width / 2.0,
                clearance=rules.trace_clearance,
            )

            if not model.is_clear((seg.x1, seg.y1), (seg.x2, seg.y2)):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        _kind_for(context.kind),
                        net_a,
                        net_b,
                        # A polygon-containment answer, not a distance one.
                        gap_mm=None,
                        required_mm=rules.trace_clearance,
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
    outline rather than ``[]`` keeps ``is_clear``'s containment branch live, as
    it is in production; every corpus object is inside the board by
    construction, so the branch never fires and cannot be mistaken for a
    clearance verdict.
    """
    return [(0.0, 0.0), (case.width, 0.0), (case.width, case.height), (0.0, case.height)]


def _obstacles_for(existing, nets: dict[str, int], agent_radius: float, capsule_half: float, case):
    """``(keepouts, pours)`` for the pair's counterpart, as production builds them.

    A pad becomes one inflated keep-out rectangle (``_keepouts``,
    ``mesh/pathfinder.py:540``, including its clamp to the board bounding box
    minus the 1e-3 poly2tri margin).  A segment becomes one inflated capsule
    polygon (``_route_obstacles``, ``:917``), built by the consumer's own
    ``_segment_capsule`` so the vertex arithmetic is not restated here.

    A **pour** becomes its declared boundary, verbatim and *un*-inflated --
    that is exactly what ``_route_obstacles`` passes (``pour_outlines``), and
    inflating it here would be this adapter inventing a clearance term the
    consumer does not have.  The **board edge** contributes no obstacle at
    all: ``is_clear`` answers it through the ``outline`` argument, which every
    call already carries.
    """
    from kicad_tools.router.mesh.pathfinder import _segment_capsule
    from kicad_tools.router.primitives import pad_half_extents

    keepouts: list[tuple[float, float, float, float]] = []
    pours: list[list[tuple[float, float]]] = []

    if isinstance(existing, BoardEdgeRef):
        return keepouts, pours

    if isinstance(existing, ZoneSpec):
        pours.append([(x, y) for x, y in existing.boundary])
        return keepouts, pours

    if isinstance(existing, PadSpec):
        pad = router_pad(existing, nets)
        half_w, half_h = pad_half_extents(pad)
        hx = half_w + agent_radius
        hy = half_h + agent_radius
        margin = 1e-3
        rect = (
            max(pad.x - hx, margin),
            max(pad.y - hy, margin),
            min(pad.x + hx, case.width - margin),
            min(pad.y + hy, case.height - margin),
        )
        if rect[2] - rect[0] >= margin and rect[3] - rect[1] >= margin:
            keepouts.append(rect)
    else:
        assert isinstance(existing, SegmentSpec)  # a via is not in this model
        seg = router_segment(existing, nets)
        poly = _segment_capsule((seg.x1, seg.y1), (seg.x2, seg.y2), capsule_half)
        if poly is not None:
            pours.append(poly)

    return keepouts, pours

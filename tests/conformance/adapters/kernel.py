"""The Phase 1b clearance kernel, measured as a consumer -- the control row.

Every other row in ``docs/clearance-conformance.md`` answers "how far is this
consumer from kicad-cli?".  This one answers the question the whole epic turns
on: **can a single exact-geometry model agree with kicad-cli on the entire
corpus?**  If it cannot, unifying the consumers onto it would replace nineteen
wrong answers with one, and Epic #5509's plan is wrong.  So this row is a
control, and Phase 1c's acceptance criterion for it is a hard **0 % / 0 %**.

A non-zero cell here is a **Phase 1b bug**, not a Phase 1c finding.  The
disposition is fixed by the epic and is worth restating where the measurement
lives: capture the offending case as a fixture under
``tests/fixtures/conformance/``, and fix the kernel in its own PR through
``tests/router/test_clearance_kernel_parity.py`` (mandatory for every kernel
edit).  Never patch the kernel from inside this phase, and never widen the
comparison to make the cell green.

**Both ports are driven, and both must agree with kicad-cli.**
``router/clearance_kernel.py`` and ``router_cpp``'s ``copper_gap`` /
``hole_gap`` / ``clear`` are one model with two implementations; a pair is
flagged when **either** port flags it, so a port divergence surfaces here as a
disagreement instead of hiding behind whichever side was sampled.  (Their
arithmetic parity is separately and exactly pinned at ``1e-7`` mm by
``test_clearance_kernel_parity.py``; this row is about agreement with KiCad,
not about the two ports.)

**Rule resolution is the caller's job, and here the caller is the oracle's own
project file.**  The kernel knows nothing about rules by design -- Phase 2's
resolver is what will decide ``required_mm`` per pair in production.  So this
adapter resolves it the way the *ground truth* does: ``project_clearance``,
which ``board.manufacturer_rules`` maps onto the ``.kicad_pro`` ``Default``
netclass ``clearance`` that ``kicad-cli pcb drc`` actually applies, and
``min_hole_to_hole`` for the drill-to-drill reading.  That is the one
legitimate difference between this row and the consumer rows: a consumer is
measured with *its own* numbers (that mismatch is the epic's thesis), whereas
the kernel has no numbers of its own to be wrong about.  Its row therefore
isolates the **geometry** question from the **rule-resolution** question, and
the 0 % result is what licenses Phase 2 to treat them separately.

**Shape construction is shared with 1b's parity test**, deliberately: pads go
through ``clearance_kernel.make_pad`` (the ``_pad_polygon`` port, exact via
the Minkowski core), vias carry their drill, and segments carry their layer
index.  Nothing is re-derived here.

The kernel is not one of the epic's nineteen consumer groups -- it is the
thing they are to be unified *onto* -- so it renders in its own section of the
document rather than as a twentieth group row.  ``group`` is
:data:`KERNEL_GROUP` (``0``), a sentinel the report recognises.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, KIND_HOLE_TO_HOLE, Verdict
from tests.conformance.adapters._support import layer_indexer, router_cpp_module
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec, ViaSpec

__all__ = ["KERNEL_GROUP", "KernelAdapter"]

KERNEL_GROUP = 0
"""Sentinel "group" for the kernel: not one of the epic's nineteen consumers."""


class KernelAdapter:
    """Drives both ports of the Phase 1b kernel over every pair in a case."""

    name = "clearance_kernel"
    group = KERNEL_GROUP
    #: Every *copper-to-copper* kind the generator places.  The kernel has no
    #: notion of "which object existed first" -- that absence is the fix for
    #: #5398 -- and no copper shape it cannot express, so there is nothing to
    #: narrow there.
    #:
    #: The #5644 zone and edge kinds are out of scope, and deliberately so.
    #: The kernel *does* carry ``KZonePoly`` / ``KEdge`` shapes (they are
    #: exercised by ``test_clearance_kernel_parity.py``), but this row's job is
    #: to answer one question -- can one exact-geometry model agree with
    #: kicad-cli? -- against *the same copper the consumers are scored on*.
    #: Scoring it on a pour would mean modelling the pour as its declared
    #: boundary while kicad-cli measures the *filled* polygon, so a
    #: disagreement would be the harness's fiction rather than a kernel bug.
    #: Widening this set needs a fill-aware kernel shape first; the control
    #: row's 0 %/0 % criterion is not a licence to guess.
    pair_kinds = frozenset({*PairKind.ROUTING, PairKind.PAD_PAD})

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.router import clearance_kernel as ck

        router_cpp = router_cpp_module()
        layer_index = layer_indexer(case)
        by_net = {obj.net: obj for obj in case.copper_objects}
        required = case.rules.project_clearance
        hole_required = case.rules.min_hole_to_hole
        found: set[Verdict] = set()

        for pair in case.pairs:
            if pair.kind not in self.pair_kinds:
                # A zone or edge pair: one side is not a declared copper
                # object, so there is nothing in ``by_net`` to shape.  Out of
                # this row's scope -- see ``pair_kinds``.
                continue
            a = _kernel_shape(ck, by_net[pair.net_a], layer_index)
            b = _kernel_shape(ck, by_net[pair.net_b], layer_index)

            copper_clear = ck.clear(a, b, required)
            hole = ck.hole_gap(a, b) if _both_drilled(a, b) else ck.NO_INTERACTION
            hole_clear = hole >= hole_required - ck.CLEARANCE_EPSILON_MM

            if router_cpp is not None:
                cpp_a, cpp_b = _cpp_shape(router_cpp, a), _cpp_shape(router_cpp, b)
                copper_clear = copper_clear and router_cpp.clear(cpp_a, cpp_b, required)
                if _both_drilled(a, b):
                    cpp_hole = router_cpp.hole_gap(cpp_a, cpp_b)
                    hole_clear = hole_clear and (
                        cpp_hole >= hole_required - ck.CLEARANCE_EPSILON_MM
                    )

            if not copper_clear:
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        pair.net_a,
                        pair.net_b,
                        gap_mm=_finite(ck.copper_gap(a, b)),
                        required_mm=required,
                    )
                )
            if not hole_clear:
                found.add(
                    Verdict.pair(
                        KIND_HOLE_TO_HOLE,
                        pair.net_a,
                        pair.net_b,
                        gap_mm=_finite(hole),
                        required_mm=hole_required,
                    )
                )
        return found


def _both_drilled(a, b) -> bool:
    """Whether a *drill-to-drill* reading applies to this pair.

    ``hole_gap`` deliberately answers two different questions behind one name
    (``clearance_kernel.py``): drill-to-drill when both sides are drilled, and
    drill-to-**copper** when only one is.  They carry different rule values,
    and only the first is the one ``min_hole_to_hole`` governs.

    Scoring the one-sided reading against ``min_hole_to_hole`` was a real bug
    in the first cut of this adapter: on the ``search-vs-commit-seg-via-max``
    fixture a via drill sits 0.33 mm from a foreign track's copper, which is a
    perfectly legal ``hole_clearance`` distance but looks like a violation
    against a 0.5 mm *hole-to-hole* floor -- so the control row reported a
    kernel disagreement that was entirely the adapter's threshold mistake.
    There is no configured project value for drill-to-copper here at all
    (``board.manufacturer_rules`` sets ``min_hole_to_hole_mm`` and
    ``min_hole_diameter_mm``, not a hole-clearance rule), so the honest answer
    is not to score it rather than to invent a threshold for it.

    Both sides drilled means, in this corpus, a via-via pair: probe pads are
    SMD (``drill`` 0) and segments are never drilled.
    """
    return _drill_of(a) > 0.0 and _drill_of(b) > 0.0


def _drill_of(shape) -> float:
    return float(getattr(shape, "drill", 0.0) or 0.0)


def _kernel_shape(ck, obj, layer_index):
    """One corpus object as a kernel shape.

    Shares ``make_pad`` with ``tests/router/test_clearance_kernel_parity.py``
    ``_corpus_pad``: the probe footprints are SMD (no drill), single-layer,
    and carry their shape keyword, local size and roundrect ratio on
    ``PadSpec.shape``.  ``PadSpec.rotation`` is already the absolute
    board-frame angle (#3902), which is what ``make_pad`` expects.
    """
    if isinstance(obj, SegmentSpec):
        return ck.KSegment(
            x1=obj.start[0],
            y1=obj.start[1],
            x2=obj.end[0],
            y2=obj.end[1],
            width=obj.width,
            layer=layer_index(obj.layer),
        )
    if isinstance(obj, ViaSpec):
        return ck.KVia(x=obj.x, y=obj.y, diameter=obj.diameter, drill=obj.drill)
    assert isinstance(obj, PadSpec)
    shape = obj.shape
    return ck.make_pad(
        shape.shape,
        shape.size[0],
        shape.size[1],
        0.25 if shape.roundrect_rratio is None else shape.roundrect_rratio,
        obj.rotation,
        obj.x,
        obj.y,
        layer_index(obj.layer),
        0.0,
    )


def _cpp_shape(router_cpp, shape):
    """The C++ twin of a Python kernel shape.

    Twins the *already-built* Python shape -- core vertices and all for a pad
    -- rather than rebuilding from parameters, so a divergence inside
    ``make_pad`` cannot hide behind matching gaps.  Same construction
    ``test_clearance_kernel_parity.py`` ``_to_cpp`` uses.
    """
    from kicad_tools.router import clearance_kernel as ck

    if isinstance(shape, ck.KSegment):
        return router_cpp.KSegment(shape.x1, shape.y1, shape.x2, shape.y2, shape.width, shape.layer)
    if isinstance(shape, ck.KVia):
        return router_cpp.KVia(shape.x, shape.y, shape.diameter, shape.drill)
    assert isinstance(shape, ck.KPad)
    return router_cpp.KPad(
        [(x, y) for x, y in shape.core],
        shape.corner_radius,
        shape.cx,
        shape.cy,
        shape.drill,
        shape.layer,
    )


def _finite(value: float) -> float | None:
    import math

    return None if not math.isfinite(value) else float(value)

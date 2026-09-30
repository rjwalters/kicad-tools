"""Group 6 -- the fixed-copper predicate shared by the routing engines.

``FixedFillObstacles`` (``router/fixed_copper.py:45``) is how every engine sees
copper it must route *around* but may never reuse: filled zones and the
board-frame copper of placement-excluded pads.  Two entry points, plus the C++
mirror:

* ``FixedFillObstacles.segment_clear`` (``fixed_copper.py:53``) -- a candidate
  trace (or a degenerate point) against every fill on its layer.
* ``FixedFillObstacles.via_clear`` (``:83``) -- a via, evaluated as a point
  query on each layer it spans.
* ``Grid3D::fixed_fill_clear`` (``grid.cpp:41``, bound at
  ``bindings.cpp:218``) -- the native mirror the C++ search consults, reached
  through ``CppGrid.install_fixed_fills`` -> ``add_fixed_fill``.

Both are driven here, and a pair is flagged when **either** refuses: they are
one model with two implementations (the C++ side exists to keep the A* hot
path off shapely), so a row that showed only one of them would hide a parity
break rather than report it.  A parity break shows up in this row as a
disagreement against kicad-cli on whichever side is wrong.

**What plays the part of the fill.**  The predicate's input contract is a
polygon of real copper plus a clearance; production hands it zone fills and
excluded-pad copper.  This adapter hands it the *exact copper outline of the
pair's pad*, built by ``validate/rules/clearance.py _pad_polygon`` -- the
in-repo reference pad model, the same polygon ``kct check`` measures and the
one that agrees with kicad-cli on the ``roundrect-corner-gap`` fixture.  That
is deliberate: supplying the reference geometry means this row measures group
6's *own* arithmetic (``half + max(clearance, fill.clearance)``, the
``intersects`` short-circuit, the ``1e-4`` slack, the bbox pre-filter) rather
than an approximation introduced by the harness.  A zone pour would have been
the other candidate input, but the generator places no close pair against its
pour (the pour exists to exercise the refill path, and KiCad's filler knocks
it back around every foreign object it meets), so there would have been
nothing to score.

**Pair kinds: pad pairs only** -- ``pad-seg`` and ``pad-via``.  Those are the
kinds whose existing side is static copper, which is the only thing a fixed
fill ever models.  A routed segment or via is *not* fixed copper (it is
rippable, and the engines consult groups 1-5 / 12-13 for it), so feeding one
in as a fill would invent a production configuration that does not exist.

Requires shapely (a hard dependency of ``kct check``, so always present) and,
for the native half, the compiled router extension.  When the extension is
missing only the Python half runs and the row's notes say so -- the row is
still a real measurement, because the Python predicate is the one every
non-native engine uses.
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
    router_pad,
    router_rules,
    router_segment,
    router_via,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.router.rules import DesignRules

__all__ = ["FixedCopperAdapter"]

#: The kinds whose existing side is static placement copper.
STATIC_COPPER_KINDS = frozenset({PairKind.PAD_SEG, PairKind.PAD_VIA})


class FixedCopperAdapter:
    """Drives ``FixedFillObstacles`` and its native ``fixed_fill_clear`` mirror."""

    name = "fixed_copper"
    group = 6
    pair_kinds = STATIC_COPPER_KINDS

    def available(self) -> bool:
        try:
            import shapely  # noqa: F401
        except ImportError:  # pragma: no cover - shapely is a hard dependency
            return False
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        return self._verdicts(case, router_rules(case))

    def verdicts_at_project_rules(self, case: CopperCase) -> set[Verdict]:
        """The same consumer, driven at the clearance kicad-cli applies.

        The **gated** reading (Epic #5509 Phase 3f switched this group, so
        ``test_corpus.test_adapter_agrees_with_kicad_cli`` asserts it hard
        instead of xfailing it).  Only the two copper clearances move onto
        ``project_clearance`` -- the fill's own clearance floor is derived from
        ``trace_clearance`` and follows, while ``min_hole_to_hole`` and the via
        geometry stay the case's own because they feed a different requirement
        this row does not claim.  Nothing about the consumer changes between
        the two readings; the adapter has always chosen which rule values to
        hand ``FixedFillObstacles``, and this is that same choice made twice.
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
        from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles

        router_cpp = router_cpp_module()
        nets = net_ids(case)
        layer_index = layer_indexer(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            existing = context.existing
            assert isinstance(existing, PadSpec)
            geometry = _pad_copper_polygon(case, existing, nets)
            if geometry is None:  # pragma: no cover - probe pads are never degenerate
                continue

            layer = layer_index(existing.layer)
            fill = FixedFill(
                source_net=existing.net,
                source_net_id=nets[existing.net],
                layer=layer,
                clearance=rules.trace_clearance,
                geometry=geometry,
                source_kind="pad",
                source_object_id=existing.reference,
            )
            obstacles = FixedFillObstacles(fills=(fill,))

            candidate = context.candidate
            if isinstance(candidate, SegmentSpec):
                seg = router_segment(candidate, nets)
                clear = obstacles.segment_clear(
                    (seg.x1, seg.y1),
                    (seg.x2, seg.y2),
                    layer_index(candidate.layer),
                    seg.width / 2.0,
                    rules.trace_clearance,
                )
                required = rules.trace_clearance
                native_args = (
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    layer_index(candidate.layer),
                    seg.width / 2.0,
                    rules.trace_clearance,
                )
            else:
                via = router_via(candidate, nets)
                via_layers = tuple(
                    range(
                        min(layer_index(candidate.layers[0]), layer_index(candidate.layers[1])),
                        max(layer_index(candidate.layers[0]), layer_index(candidate.layers[1])) + 1,
                    )
                )
                clear = obstacles.via_clear(
                    (via.x, via.y),
                    via_layers,
                    via.diameter / 2.0,
                    rules.via_clearance,
                )
                required = rules.via_clearance
                # ``fixed_fill_clear`` is a *segment* query, so a via is the
                # degenerate ``a == b`` point form -- exactly how the Python
                # ``via_clear`` expresses it (``fixed_copper.py:89`` calls
                # ``segment_clear(point, point, ...)``).  ``layer`` is the
                # fill's own layer: a through via spans every layer, and only
                # the fill's can match, so one query answers all of them.
                native_args = (
                    via.x,
                    via.y,
                    via.x,
                    via.y,
                    layer,
                    via.diameter / 2.0,
                    rules.via_clearance,
                )

            if clear and router_cpp is not None:
                clear = _native_fixed_fill_clear(router_cpp, case, rules, obstacles, native_args)

            if not clear:
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


def _pad_copper_polygon(case: CopperCase, spec: PadSpec, nets: dict[str, int]):
    """The pad's exact copper outline, via the in-repo reference pad model.

    ``_pad_polygon`` takes a ``schema.pcb`` pad plus its footprint so it can
    resolve a local pad offset into board coordinates.  The probe footprints
    place their single pad at the footprint origin, so a zero-offset pad at
    the footprint's own position reproduces exactly the geometry
    ``tests/conformance/board.py`` wrote into the ``.kicad_pcb``.
    """
    from kicad_tools.schema.pcb import Footprint, Pad
    from kicad_tools.validate.rules.clearance import _pad_polygon

    shape = spec.shape
    router_side = router_pad(spec, nets)
    footprint = Footprint(
        name=shape.name,
        layer=spec.layer,
        position=(spec.x, spec.y),
        rotation=spec.rotation,
        reference=spec.reference,
        value=shape.name,
    )
    pad = Pad(
        number="1",
        type="smd",
        shape=shape.shape,
        position=(0.0, 0.0),
        size=shape.size,
        layers=[spec.layer],
        net_number=router_side.net,
        net_name=spec.net,
        rotation=spec.rotation,
        roundrect_rratio=shape.roundrect_rratio if shape.roundrect_rratio is not None else 0.25,
    )
    del case
    return _pad_polygon(pad, footprint)


def _native_fixed_fill_clear(router_cpp, case, rules, obstacles, args) -> bool:
    """The ``Grid3D::fixed_fill_clear`` mirror, fed through the public path.

    ``CppGrid.install_fixed_fills`` is the production installer; it reads
    ``FixedFillObstacles.native_polygons()`` (exterior ring plus every
    interior ring, one entry per multipolygon lobe) and forwards each to
    ``add_fixed_fill``.  Going through the same generator keeps the two halves
    of group 6 measuring the same copper.
    """
    grid = cpp_grid_for(router_cpp, case, rules)
    for layer, clearance, rings in obstacles.native_polygons():
        grid.add_fixed_fill(layer, clearance, rings)
    return bool(grid.fixed_fill_clear(*args))

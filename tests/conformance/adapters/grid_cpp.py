"""Group 13 -- the C++ commit gate (``Grid3D::validate_route``), unmodified.

``router_cpp.Grid3D`` is driven **directly**, as
``tests/router/test_pairwise_cpp_parity.py`` does, rather than through
``CppPathfinder._validate_route_clearance``.  That wrapper needs a live
pathfinder and layers on fixed-fill handling, off-grid stored segments and
same-component carve-outs, none of which apply to a two-object conformance
case; going through it would measure the wrapper, not the kernel.

The argument order mirrors the production call site
(``cpp_backend.py`` ``_validate_route_clearance``) exactly::

    validate_route(segments, vias, exclude_net, exclude_ref_hashes,
                   trace_clearance, via_clearance, min_drill_clearance,
                   partner_net, intra_pair_clearance, clamp_ref_hashes,
                   min_hole_clearance)

with both hash lists empty (no same-component relaxation is in effect on these
boards) and no diff-pair partner.

**Pads carry no roundrect / oval flag.**  ``Grid3D::add_pad`` takes
``is_circular`` and a rotation, and nothing else: "Oval and roundrect pads
retain conservative rectangle bounds" (``grid.cpp``).  That single missing bit
is the whole mechanism behind the ``roundrect-corner-gap`` fixture -- a pad
whose true outline is 0.22 mm from the track but whose bounding rectangle is
0.1164 mm from it.  The adapter reproduces the call faithfully, flag and all.

Requires the compiled extension (``uv run kct build-native``).  Without it
:meth:`GridCppAdapter.available` returns ``False`` and group 13 renders
``not measured`` rather than a zero-disagreement row.
"""

from __future__ import annotations

import math

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    PairContext,
    layer_of,
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    router_segment,
    router_via,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec

__all__ = ["GridCppAdapter"]


def _cpp():
    """Import the compiled extension, or ``None`` when it is not built."""
    try:
        from kicad_tools.router import router_cpp
    except ImportError:  # pragma: no cover - depends on the build environment
        return None
    return router_cpp


class GridCppAdapter:
    """Drives ``router_cpp.Grid3D::validate_route`` on a bare grid."""

    name = "grid_cpp"
    group = 13
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return _cpp() is not None

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        router_cpp = _cpp()
        if router_cpp is None:  # pragma: no cover - guarded by available()
            return set()

        nets = net_ids(case)
        rules = router_rules(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            result = self._validate(router_cpp, case, context, nets, rules)
            if not result.valid:
                net_a, net_b = context.nets
                required = (
                    rules.trace_clearance
                    if isinstance(context.candidate, SegmentSpec)
                    else rules.via_clearance
                )
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=_finite(result.min_clearance),
                        required_mm=required,
                    )
                )
        return found

    def _validate(self, router_cpp, case: CopperCase, context: PairContext, nets, rules):
        grid = _grid_for(router_cpp, case, rules)
        layer_index = _layer_indexer(case)

        existing = context.existing
        if isinstance(existing, PadSpec):
            pad = router_pad(existing, nets)
            grid.add_pad(
                pad.x,
                pad.y,
                pad.width,
                pad.height,
                pad.net,
                layer_index(existing.layer),
                0,  # ref_hash: no same-component carve-out applies here
                rules.get_clearance_for_component(pad.ref),
                False,  # is_plane_net
                pad.rotation,
                pad.shape == "circle",
            )
        elif isinstance(existing, SegmentSpec):
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

        candidate = context.candidate
        segments = []
        vias = []
        if isinstance(candidate, SegmentSpec):
            seg = router_segment(candidate, nets)
            cpp_seg = router_cpp.Segment()
            cpp_seg.x1, cpp_seg.y1 = seg.x1, seg.y1
            cpp_seg.x2, cpp_seg.y2 = seg.x2, seg.y2
            cpp_seg.width = seg.width
            cpp_seg.layer = layer_index(candidate.layer)
            cpp_seg.net = seg.net
            segments.append(cpp_seg)
            exclude_net = seg.net
        else:
            via = router_via(candidate, nets)
            cpp_via = router_cpp.Via()
            cpp_via.x, cpp_via.y = via.x, via.y
            cpp_via.drill = via.drill
            cpp_via.diameter = via.diameter
            cpp_via.layer_from = layer_index(candidate.layers[0])
            cpp_via.layer_to = layer_index(candidate.layers[1])
            cpp_via.net = via.net
            vias.append(cpp_via)
            exclude_net = via.net

        return grid.validate_route(
            segments,
            vias,
            exclude_net,
            [],
            rules.trace_clearance,
            rules.via_clearance,
            rules.min_drill_clearance,
            -1,
            0.0,
            [],
            rules.min_hole_to_hole,
        )


def _grid_for(router_cpp, case: CopperCase, rules):
    """A bare ``Grid3D`` in the case's coordinate frame.

    ``validate_route`` only consults explicitly registered pads / stored
    segments / stored vias, so no cell ever has to be marked -- the grid's
    raster exists here purely to fix the coordinate frame.
    """
    resolution = rules.grid_resolution
    cols = int(case.width / resolution) + 1
    rows = int(case.height / resolution) + 1
    return router_cpp.Grid3D(cols, rows, case.layers, resolution, 0.0, 0.0)


def _layer_indexer(case: CopperCase):
    """KiCad layer name -> the 0-based stack index ``Grid3D`` speaks."""
    from tests.conformance.adapters._support import layer_stack_for

    stack = layer_stack_for(case)
    by_enum = {layer_of(definition.name): definition.index for definition in stack.layers}

    def index(name: str) -> int:
        return by_enum[layer_of(name)]

    return index


def _finite(value: float) -> float | None:
    return None if not math.isfinite(value) else float(value)

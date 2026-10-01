"""Epic #5509 Phase 4c (#5856): the DRC-nudge repair pass on the kernel.

Consumer group 17 is ``router/drc_nudge.py``'s destination gates.  Three of
them carried their own clearance/overlap arithmetic and now ask the shared
clearance kernel instead:

* ``_post_nudge_introduces_foreign_via_violation`` -- the segment-vs-foreign-via
  half of the nudge destination gate;
* ``_via_drill_overlaps_bbox`` -- the via-in-pad overlap detector, a
  *drill-to-copper* reading;
* ``_via_edge_sweep_clear`` -- the board-outline displacement certificate,
  which also retires the module's two private distance wrappers.

Two properties are pinned per helper, because a consumer migration has to make
two statements at once:

1. **The verdict comes from the kernel**, asserted by observing the kernel
   entry point the helper actually reaches (patching the module-global the
   translation layer resolves at call time, the idiom #5855 established in
   ``tests/test_match_group_clearance_prefilter.py``).
2. **No verdict changed**, asserted against the pre-migration arithmetic
   reproduced verbatim below -- scope guard #2's "no heuristic changes", made
   checkable rather than claimed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.core.board_outline import OutlineSegments
from kicad_tools.router import clearance_shapes, drc_nudge
from kicad_tools.router.clearance_kernel import clear, hole_gap
from kicad_tools.router.clearance_shapes import bbox_shape, segment_shape, via_shape
from kicad_tools.router.drc_nudge import (
    _post_nudge_introduces_foreign_via_violation,
    _router_pad_bbox,
    _via_drill_overlaps_bbox,
    _via_edge_sweep_clear,
)
from kicad_tools.router.geometry import point_to_segment_distance, segment_to_segment_distance
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

TRACE_CLEARANCE = 0.15


@dataclass
class _StubRouter:
    """The two attributes the foreign-via gate reads.

    The same shape ``tests/test_drc_nudge_foreign_via_gate.py`` and the
    conformance adapter (``tests/conformance/adapters/drc_nudge.py``) use: the
    gate consults ``routes`` and ``rules`` and nothing else, so a real
    ``Autorouter`` would only add state that could move a verdict for reasons
    that are not this consumer's arithmetic.
    """

    routes: list = field(default_factory=list)
    rules: DesignRules = field(default_factory=lambda: DesignRules(trace_clearance=TRACE_CLEARANCE))


def _segment(y: float, layer: Layer = Layer.B_CU, net: int = 10) -> Segment:
    return Segment(x1=0.0, y1=y, x2=10.0, y2=y, width=0.2, layer=layer, net=net)


def _via(
    x: float,
    y: float,
    layers: tuple[Layer, Layer] = (Layer.F_CU, Layer.B_CU),
    net: int = 20,
) -> Via:
    return Via(x=x, y=y, drill=0.3, diameter=0.6, layers=layers, net=net)


def _router_with_via(via: Via) -> _StubRouter:
    return _StubRouter(routes=[Route(net=via.net, net_name="FOREIGN", vias=[via])])


# ---------------------------------------------------------------------------
# _post_nudge_introduces_foreign_via_violation
# ---------------------------------------------------------------------------


def test_foreign_via_gate_takes_its_verdict_from_the_kernel() -> None:
    """The gate's answer is ``clearance_kernel.clear``'s, not private arithmetic."""
    seg = _segment(5.0)
    router = _router_with_via(_via(5.0, 5.6))

    # Clean geometry (0.6 mm centre gap vs a 0.15 mm requirement), so a gate
    # that composed its own distance would answer "no violation" here.  Forcing
    # the kernel to refuse must flip the gate -- which it can only do if the
    # kernel is what the gate consults.
    with patch.object(clearance_shapes, "clear", return_value=False) as kernel:
        assert _post_nudge_introduces_foreign_via_violation(seg, router) is True
    assert kernel.call_count == 1
    shape_a, shape_b, required = kernel.call_args.args
    assert shape_a == segment_shape(seg)
    assert shape_b == via_shape(router.routes[0].vias[0])
    assert required == pytest.approx(TRACE_CLEARANCE)


@pytest.mark.parametrize("offset", [0.05 * i for i in range(1, 25)])
def test_foreign_via_gate_agrees_with_the_kernel_at_every_offset(offset: float) -> None:
    """Swept across the threshold, the gate is exactly the kernel's verdict."""
    seg = _segment(5.0)
    via = _via(5.0, 5.0 + offset)
    router = _router_with_via(via)

    expected = not clear(segment_shape(seg), via_shape(via), TRACE_CLEARANCE)
    assert _post_nudge_introduces_foreign_via_violation(seg, router) is expected


def test_foreign_via_gate_keeps_its_same_net_filter() -> None:
    """A same-net via is a chain adjacency, never a clearance finding."""
    seg = _segment(5.0, net=10)
    via = _via(5.0, 5.1, net=10)
    router = _router_with_via(via)

    # The pair is a genuine clearance violation as pure geometry...
    assert not clear(segment_shape(seg), via_shape(via), TRACE_CLEARANCE)
    # ...and the caller-side net filter still suppresses it.
    assert _post_nudge_introduces_foreign_via_violation(seg, router) is False


def test_foreign_via_gate_keeps_its_layer_span_gate() -> None:
    """A barrel that does not reach the segment's layer is out of scope.

    ``KVia`` is all-layer copper by construction, so the migration had to keep
    this gate on the caller's side; losing it would turn every blind via into a
    false finding.
    """
    seg = _segment(5.0, layer=Layer.B_CU)
    blind = _via(5.0, 5.1, layers=(Layer.F_CU, Layer.F_CU))
    assert _post_nudge_introduces_foreign_via_violation(seg, _router_with_via(blind)) is False

    spanning = _via(5.0, 5.1, layers=(Layer.F_CU, Layer.B_CU))
    assert _post_nudge_introduces_foreign_via_violation(seg, _router_with_via(spanning)) is True


def test_foreign_via_gate_never_asks_the_kernel_about_an_out_of_scope_via() -> None:
    """The layer gate runs *before* the kernel call, not after it."""
    seg = _segment(5.0, layer=Layer.B_CU)
    router = _router_with_via(_via(5.0, 5.1, layers=(Layer.F_CU, Layer.F_CU)))
    with patch.object(clearance_shapes, "clear", return_value=False) as kernel:
        assert _post_nudge_introduces_foreign_via_violation(seg, router) is False
    assert kernel.call_count == 0


# ---------------------------------------------------------------------------
# _via_drill_overlaps_bbox
# ---------------------------------------------------------------------------


def _legacy_via_drill_overlaps_bbox(
    via: Via,
    pad_bbox: tuple[float, float, float, float],
    tol: float = 0.005,
) -> bool:
    """The pre-#5856 point-to-box ``hypot``, reproduced verbatim."""
    min_x, min_y, max_x, max_y = pad_bbox
    radius = via.drill / 2.0
    if radius <= tol:
        return False
    dx = max(min_x - via.x, 0.0, via.x - max_x)
    dy = max(min_y - via.y, 0.0, via.y - max_y)
    return math.hypot(dx, dy) < radius - tol


_LAND = (-0.5, -0.65, 0.5, 0.65)  # the 1.0 x 1.3 mm land of issue #5009


@pytest.mark.parametrize("vx", [0.0, 0.4, 0.5, 0.55, 0.6, 0.64, 0.65, 0.7, 1.2])
@pytest.mark.parametrize("vy", [0.0, 0.6, 0.65, 0.78, 0.8, 1.5])
def test_via_drill_overlap_matches_the_pre_migration_detector(vx: float, vy: float) -> None:
    """Same verdict as the retired ``hypot``, on and off the land boundary."""
    via = _via(vx, vy)
    assert _via_drill_overlaps_bbox(via, _LAND) is _legacy_via_drill_overlaps_bbox(via, _LAND)


@pytest.mark.parametrize("vx", [0.0, 0.5, 0.6, 0.64, 0.7])
def test_via_drill_overlap_is_the_kernel_hole_gap(vx: float) -> None:
    """The detector *is* ``hole_gap(land, via) < -tol``."""
    via = _via(vx, 0.0)
    expected = hole_gap(bbox_shape(_LAND), via_shape(via)) < -0.005
    assert _via_drill_overlaps_bbox(via, _LAND) is expected


def test_via_drill_overlap_reads_a_real_pad_through_the_same_box() -> None:
    """The footprint is still ``_router_pad_bbox``'s enclosing box (guard #2)."""
    pad = Pad(
        x=0.0,
        y=0.0,
        width=1.0,
        height=1.3,
        net=5,
        net_name="LAND",
        layer=Layer.F_CU,
        shape="roundrect",
    )
    bbox = _router_pad_bbox(pad)
    clipping = _via(0.6, 0.0)
    assert _via_drill_overlaps_bbox(clipping, bbox) is True
    assert _via_drill_overlaps_bbox(_via(1.2, 0.0), bbox) is False


def test_via_drill_overlap_declines_an_undrilled_via() -> None:
    """A via with no hole cannot wick solder; the tolerance early-out stays."""
    undrilled = Via(x=0.0, y=0.0, drill=0.0, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=20)
    assert _via_drill_overlaps_bbox(undrilled, _LAND) is False


# ---------------------------------------------------------------------------
# _via_edge_sweep_clear
# ---------------------------------------------------------------------------


def _legacy_via_edge_sweep_clear(old_x: float, old_y: float, via: Via, router: object) -> bool:
    """The pre-#5856 centre-distance certificate, reproduced verbatim."""
    clearance = getattr(router, "_edge_clearance", None)
    edges = getattr(router, "_edge_segments", None)
    if clearance is None or clearance <= 0 or not edges:
        return True
    error = getattr(edges, "max_error_mm", 0.0)
    if not math.isfinite(error) or error < 0:
        return False
    required = via.diameter / 2 + clearance
    old_min = swept_min = math.inf
    for (x1, y1), (x2, y2) in edges:
        old_gap = point_to_segment_distance(old_x, old_y, x1, y1, x2, y2)
        swept_gap = segment_to_segment_distance(old_x, old_y, via.x, via.y, x1, y1, x2, y2)
        if swept_gap < min(required, old_gap) - 1e-6:
            return False
        old_min = min(old_min, old_gap)
        swept_min = min(swept_min, swept_gap)
    if error == 0:
        return True
    return max(0.0, swept_min - error) >= min(required, old_min + error) - 1e-6


def _square_outline(half: float = 5.0, max_error_mm: float | None = None):
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    chords = list(zip(corners, corners[1:] + corners[:1], strict=False))
    if max_error_mm is None:
        return chords
    return OutlineSegments(chords, max_error_mm=max_error_mm)


@pytest.mark.parametrize("max_error_mm", [None, 0.0, 1e-5, 2e-3])
@pytest.mark.parametrize("old_y", [4.0, 4.5, 4.6, 4.69, 4.7, 4.75, 4.9])
@pytest.mark.parametrize("new_y", [4.0, 4.55, 4.68, 4.7, 4.72, 4.95])
def test_via_edge_sweep_matches_the_pre_migration_certificate(
    max_error_mm: float | None, old_y: float, new_y: float
) -> None:
    """Every verdict is unchanged: the migration shifted all three terms by one radius."""
    via = Via(x=0.0, y=new_y, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router = SimpleNamespace(
        _edge_segments=_square_outline(max_error_mm=max_error_mm),
        _edge_clearance=0.1,
    )
    assert _via_edge_sweep_clear(0.0, old_y, via, router) is _legacy_via_edge_sweep_clear(
        0.0, old_y, via, router
    )


def test_via_edge_sweep_measures_the_swept_barrel_through_the_kernel() -> None:
    """Two kernel readings per chord: the pre-move barrel and the swept capsule.

    ``drc_nudge`` binds ``copper_gap`` at import, so the observable call site is
    its own binding of the kernel entry point ``clearance_shapes`` re-exports --
    patching it is what proves the certificate reaches the kernel at all rather
    than recomputing a distance of its own.
    """
    via = Via(x=0.0, y=4.5, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router = SimpleNamespace(_edge_segments=_square_outline(), _edge_clearance=0.1)
    with patch.object(drc_nudge, "copper_gap", wraps=clearance_shapes.copper_gap) as kernel_gap:
        assert _via_edge_sweep_clear(0.0, 4.0, via, router) is True
    # Four chords, two readings each -- and nothing else measured the geometry.
    assert kernel_gap.call_count == 8
    measured = {call.args[0] for call in kernel_gap.call_args_list}
    assert clearance_shapes.KSegment(x1=0.0, y1=4.0, x2=0.0, y2=4.5, width=0.4) in measured
    assert clearance_shapes.KVia(x=0.0, y=4.0, diameter=0.4, drill=0.2) in measured
    assert all(
        isinstance(call.args[1], clearance_shapes.KEdge) for call in kernel_gap.call_args_list
    )


def test_via_edge_sweep_requirement_is_the_resolved_edge_clearance() -> None:
    """Rule resolution is untouched: the threshold is ``router._edge_clearance``.

    The via radius moved out of the threshold and into the geometry, so a move
    that is legal at a 0.1 mm edge clearance and illegal at 0.5 mm must still
    flip on that configured value alone (scope guard #1).
    """
    via = Via(x=0.0, y=4.6, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    outline = _square_outline()
    assert _via_edge_sweep_clear(
        0.0, 4.0, via, SimpleNamespace(_edge_segments=outline, _edge_clearance=0.1)
    )
    assert not _via_edge_sweep_clear(
        0.0, 4.0, via, SimpleNamespace(_edge_segments=outline, _edge_clearance=0.5)
    )


def test_via_edge_sweep_is_a_no_op_without_an_outline_or_a_clearance() -> None:
    """Unconfigured board edges still short-circuit before any kernel call."""
    via = Via(x=0.0, y=4.6, drill=0.2, diameter=0.4, layers=(Layer.F_CU, Layer.B_CU), net=1)
    with patch.object(drc_nudge, "copper_gap") as kernel_gap:
        assert _via_edge_sweep_clear(0.0, 4.0, via, SimpleNamespace()) is True
        assert (
            _via_edge_sweep_clear(
                0.0,
                4.0,
                via,
                SimpleNamespace(_edge_segments=_square_outline(), _edge_clearance=0.0),
            )
            is True
        )
    assert kernel_gap.call_count == 0

"""Non-cardinal pad rotation must widen the obstacle-model keep-out AABB (#4910).

``load_pcb_for_routing`` / ``load_pads_for_analysis`` special-cased ONLY
90/270-degree pads (an axis swap); any other rotation (e.g. 30 or 45 degrees)
left the LOCAL (pre-rotation) width/height in place as if the footprint were
still axis-aligned.  ``LatticeObstacleModel.pad_rects`` and the mesh engine's
``_keepouts`` / ``_keepouts_layer`` then built their axis-aligned keep-out
rectangles directly from ``pad.width`` / ``pad.height`` -- for a rotated pad
the TRUE board-space bounding box is wider than either raw side length in
general, so this under-estimated the keep-out extent (a real clearance-
avoidance gap).

This module pins:

1. ``Pad.rotation`` + ``pad_half_extents`` -- the new field and the shared
   trig-AABB helper every obstacle-model consumer must use.
2. ``io._resolve_pad_dims_and_rotation`` -- the parsing-side helper that
   derives ``(width, height, rotation)`` from a pad's absolute board-frame
   angle, preserving the pre-#4910 cardinal-swap ``width``/``height`` values
   byte-for-byte while additionally exposing the residual rotation.
3. ``LatticeObstacleModel.pad_rects`` and the mesh ``MeshPathfinder._keepouts``
   / ``_keepouts_layer`` -- the actual search-time obstacle models -- now
   produce the wider, rotation-correct AABB instead of the under-sized one.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.router.io import (
    _resolve_pad_dims_and_rotation,
    load_pads_for_analysis,
    load_pcb_for_routing,
)
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad, pad_half_extents
from kicad_tools.router.rules import DesignRules

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _true_aabb(width: float, height: float, total_rot_deg: float) -> tuple[float, float]:
    """Reference trig AABB computed directly from RAW dims + full angle."""
    theta = math.radians(total_rot_deg)
    cos_t, sin_t = abs(math.cos(theta)), abs(math.sin(theta))
    return (cos_t * width / 2.0 + sin_t * height / 2.0, sin_t * width / 2.0 + cos_t * height / 2.0)


# -- pad_half_extents --------------------------------------------------------


def test_pad_half_extents_zero_rotation_is_plain_half_dims() -> None:
    """The default ``rotation=0.0`` short-circuits to (width/2, height/2)."""
    pad = Pad(x=0, y=0, width=2.0, height=1.0, net=1, net_name="N1")
    assert pad_half_extents(pad) == (1.0, 0.5)


@pytest.mark.parametrize("angle", [1.0, 15.0, 30.0, 44.9, 45.0, 60.0, 89.0, 90.0, 135.0, 179.0])
def test_pad_half_extents_matches_trig_formula(angle: float) -> None:
    pad = Pad(x=0, y=0, width=1.7, height=0.6, net=1, net_name="N1", rotation=angle)
    got = pad_half_extents(pad)
    expected = _true_aabb(1.7, 0.6, angle)
    assert got == pytest.approx(expected)


def test_pad_half_extents_45_degrees_is_wider_than_either_raw_side() -> None:
    """The regression case: a 45-degree pad's AABB exceeds both raw sides."""
    pad = Pad(x=0, y=0, width=2.0, height=1.0, net=1, net_name="N1", rotation=45.0)
    half_w, half_h = pad_half_extents(pad)
    # Naive (bug) result would just be (1.0, 0.5) -- the raw half-dims.
    assert half_w > 1.0
    assert half_h > 0.5


# -- io._resolve_pad_dims_and_rotation ---------------------------------------


def test_resolve_pad_dims_preserves_cardinal_swap_behavior() -> None:
    """Exactly-cardinal angles keep the pre-#4910 width/height byte-for-byte.

    All four also reduce to a residual of exactly ``0.0``, so they take
    :func:`pad_half_extents`' no-trig fast path and cannot shift a keep-out
    rectangle by even one ulp relative to pre-#4910 behavior.
    """
    assert _resolve_pad_dims_and_rotation(0.0, 1.7, 0.6) == (1.7, 0.6, 0.0)
    assert _resolve_pad_dims_and_rotation(90.0, 1.7, 0.6) == (0.6, 1.7, 0.0)
    assert _resolve_pad_dims_and_rotation(180.0, 1.7, 0.6) == (1.7, 0.6, 0.0)
    assert _resolve_pad_dims_and_rotation(270.0, 1.7, 0.6) == (0.6, 1.7, 0.0)
    assert _resolve_pad_dims_and_rotation(360.0, 1.7, 0.6) == (1.7, 0.6, 0.0)


@pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0, 360.0, -90.0, -180.0, 720.0])
def test_cardinal_angles_are_bit_identical_to_naive_half_dims(angle: float) -> None:
    """A cardinal-rotated pad's AABB is EXACTLY the raw half-dims (no drift)."""
    width, height, residual = _resolve_pad_dims_and_rotation(angle, 1.7, 0.6)
    pad = Pad(x=0, y=0, width=width, height=height, net=1, net_name="N1", rotation=residual)
    assert pad_half_extents(pad) == (width / 2.0, height / 2.0)


@pytest.mark.parametrize(
    "angle",
    [
        0,
        1,
        15,
        30,
        44.9,
        45,
        45.1,
        60,
        89,
        89.5,
        90,
        90.5,
        91,
        135,
        150,
        180,
        200,
        269,
        271,
        315,
        359,
    ],
)
def test_resolve_pad_dims_round_trips_through_pad_half_extents(angle: float) -> None:
    """(swapped width/height, residual rotation) reproduces the TRUE AABB.

    This is the algebraic identity the whole fix relies on: for ANY pad
    angle, feeding the (possibly-swapped) width/height and the residual
    rotation through :func:`pad_half_extents` gives EXACTLY the same result
    as computing the AABB directly from the raw dimensions and the full
    angle -- so existing non-rotation-aware consumers of ``pad.width`` /
    ``pad.height`` see zero behavior change while obstacle models that also
    consult ``pad.rotation`` get the correct wider box.
    """
    width, height = 1.7, 0.6
    w2, h2, residual = _resolve_pad_dims_and_rotation(angle, width, height)
    pad = Pad(x=0, y=0, width=w2, height=h2, net=1, net_name="N1", rotation=residual)
    assert pad_half_extents(pad) == pytest.approx(_true_aabb(width, height, angle))


# -- LatticeObstacleModel.pad_rects -------------------------------------------


def _rules() -> DesignRules:
    return DesignRules()


def test_lattice_pad_rects_widens_for_non_cardinal_rotation() -> None:
    """A 45-degree pad's ``pad_rects`` entry uses the rotation-correct AABB."""
    rules = _rules()
    stack = LayerStack.two_layer()
    routable_pad = Pad(x=2.0, y=10.0, width=0.5, height=0.5, net=1, net_name="N1", ref="A", pin="1")
    other_pad = Pad(x=18.0, y=10.0, width=0.5, height=0.5, net=1, net_name="N1", ref="B", pin="1")
    rotated = Pad(
        x=10.0,
        y=10.0,
        width=2.0,
        height=1.0,
        net=2,
        net_name="N2",
        ref="ROT",
        pin="1",
        rotation=45.0,
    )
    pads = [routable_pad, other_pad, rotated]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    obstacles = pf.obstacles  # builds the lattice + static pad masks

    idx = pads.index(rotated)
    rect = obstacles.pad_rects[idx]
    half_w = (rect[2] - rect[0]) / 2.0
    half_h = (rect[3] - rect[1]) / 2.0

    agent_radius = rules.trace_width / 2.0 + rules.trace_clearance
    naive_half_w = rotated.width / 2.0 + agent_radius
    naive_half_h = rotated.height / 2.0 + agent_radius
    expected_half_w, expected_half_h = pad_half_extents(rotated)
    expected_half_w += agent_radius
    expected_half_h += agent_radius

    assert half_w == pytest.approx(expected_half_w)
    assert half_h == pytest.approx(expected_half_h)
    # The bug this pins: the correct rotation-aware box is strictly WIDER
    # than the naive raw-width/height box on both axes for a 45-degree pad.
    assert half_w > naive_half_w
    assert half_h > naive_half_h


# -- MeshPathfinder._keepouts / _keepouts_layer -------------------------------


def test_mesh_keepouts_widens_for_non_cardinal_rotation() -> None:
    rules = _rules()
    rotated = Pad(
        x=10.0,
        y=10.0,
        width=2.0,
        height=1.0,
        net=2,
        net_name="N2",
        ref="ROT",
        pin="1",
        layer=Layer.F_CU,
        rotation=45.0,
    )
    pf = MeshPathfinder(_OUTLINE, [rotated], rules)
    agent_radius = rules.trace_width / 2.0 + rules.trace_clearance

    keepouts = pf._keepouts(net=1, agent_radius=agent_radius)
    assert len(keepouts) == 1
    rect = keepouts[0]
    half_w = (rect[2] - rect[0]) / 2.0
    half_h = (rect[3] - rect[1]) / 2.0

    expected_half_w, expected_half_h = pad_half_extents(rotated)
    expected_half_w += agent_radius
    expected_half_h += agent_radius
    naive_half_w = rotated.width / 2.0 + agent_radius
    naive_half_h = rotated.height / 2.0 + agent_radius

    assert half_w == pytest.approx(expected_half_w)
    assert half_h == pytest.approx(expected_half_h)
    assert half_w > naive_half_w
    assert half_h > naive_half_h


def test_mesh_keepouts_layer_widens_for_non_cardinal_rotation() -> None:
    rules = _rules()
    stack = LayerStack.two_layer()
    rotated = Pad(
        x=10.0,
        y=10.0,
        width=2.0,
        height=1.0,
        net=2,
        net_name="N2",
        ref="ROT",
        pin="1",
        layer=Layer.F_CU,
        rotation=45.0,
    )
    pf = MeshPathfinder(_OUTLINE, [rotated], rules, layer_stack=stack)
    agent_radius = rules.trace_width / 2.0 + rules.trace_clearance
    layer_idx = stack.layer_enum_to_index(Layer.F_CU)

    keepouts = pf._keepouts_layer(net=1, agent_radius=agent_radius, layer_idx=layer_idx)
    assert len(keepouts) == 1
    rect = keepouts[0]
    half_w = (rect[2] - rect[0]) / 2.0
    half_h = (rect[3] - rect[1]) / 2.0

    naive_half_w = rotated.width / 2.0 + agent_radius
    naive_half_h = rotated.height / 2.0 + agent_radius
    assert half_w > naive_half_w
    assert half_h > naive_half_h


# -- End-to-end: the .kicad_pcb parsers actually populate Pad.rotation --------


def _rotated_pad_board(pad_angle: float) -> str:
    """Minimal router-loadable board with one 30-degree-rotated SMD pad.

    The pad's ``(at ...)`` third token is the ABSOLUTE board-frame angle
    (KiCad already folds the footprint rotation into it -- issue #3902).
    """
    return (
        "(kicad_pcb (version 20240108) (generator test)\n"
        '  (net 0 "")\n'
        '  (net 1 "SIG_A")\n'
        '  (gr_rect (start 100 100) (end 140 140) (layer "Edge.Cuts") (width 0.1))\n'
        '  (footprint "R_0402" (layer "F.Cu") (at 110 110)\n'
        '    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0 {pad_angle}) (size 1.6 0.5) '
        '(layers "F.Cu") (net 1 "SIG_A")))\n'
        '  (footprint "R_0402" (layer "F.Cu") (at 120 110)\n'
        '    (property "Reference" "R2" (at 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 1.6 0.5) '
        '(layers "F.Cu") (net 1 "SIG_A")))\n'
        ")\n"
    )


def test_load_pads_for_analysis_carries_non_cardinal_rotation() -> None:
    """The analysis parser surfaces a 30-degree pad angle on the ``Pad``."""
    pads = load_pads_for_analysis(_rotated_pad_board(30))
    rotated = next(p for p in pads if p.ref == "R1")
    assert rotated.rotation == pytest.approx(30.0)
    # Raw dims are untouched (no cardinal swap fired) ...
    assert rotated.width == pytest.approx(1.6)
    assert rotated.height == pytest.approx(0.5)
    # ... and the derived AABB is correspondingly WIDER than the raw box,
    # which is exactly the keep-out extent the pre-#4910 model dropped.
    half_w, half_h = pad_half_extents(rotated)
    assert half_w == pytest.approx(_true_aabb(1.6, 0.5, 30.0)[0])
    assert half_h == pytest.approx(_true_aabb(1.6, 0.5, 30.0)[1])
    assert half_h > rotated.height / 2.0


def test_load_pcb_for_routing_carries_non_cardinal_rotation(tmp_path: Path) -> None:
    """The routing loader threads the angle all the way onto ``router.pads``."""
    board = tmp_path / "rot.kicad_pcb"
    board.write_text(_rotated_pad_board(30))

    router, _ = load_pcb_for_routing(str(board), validate_drc=False)

    rotated = router.pads[("R1", "1")]
    unrotated = router.pads[("R2", "1")]
    assert rotated.rotation == pytest.approx(30.0)
    assert unrotated.rotation == pytest.approx(0.0)
    assert pad_half_extents(rotated)[1] > pad_half_extents(unrotated)[1]


def test_rotated_pad_blocks_a_corridor_the_naive_aabb_calls_clear() -> None:
    """The issue's headline case, stated as a pass/fail corridor verdict.

    A 45-degree pad's true copper reaches further along Y than its raw
    ``height`` suggests.  A trace corridor threaded through that annulus --
    outside the naive ``height/2 + agent_radius`` box but inside the true
    rotated one -- was reported CLEAR before #4910 and is correctly reported
    BLOCKED now.  The control pad (identical geometry, ``rotation=0.0``)
    reproduces the pre-fix verdict on the very same corridor, so this pins
    the behavior change and not merely the arithmetic.
    """
    rules = _rules()
    stack = LayerStack.two_layer()
    layer_idx = stack.layer_enum_to_index(Layer.F_CU)
    agent_radius = rules.trace_width / 2.0 + rules.trace_clearance

    def _obstacles(rotation: float):
        pads = [
            Pad(x=2.0, y=10.0, width=0.5, height=0.5, net=1, net_name="N1", ref="A", pin="1"),
            Pad(x=18.0, y=10.0, width=0.5, height=0.5, net=1, net_name="N1", ref="B", pin="1"),
            Pad(
                x=10.0,
                y=10.0,
                width=2.0,
                height=1.0,
                net=2,
                net_name="N2",
                ref="ROT",
                pin="1",
                layer=Layer.F_CU,
                rotation=rotation,
            ),
        ]
        return LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack).obstacles

    naive_reach = 1.0 / 2.0 + agent_radius  # raw height/2 + agent radius
    true_reach = _true_aabb(2.0, 1.0, 45.0)[1] + agent_radius
    assert true_reach > naive_reach
    # A corridor squarely between the two reaches.
    corridor_y = 10.0 + (naive_reach + true_reach) / 2.0
    a, b = (6.0, corridor_y), (14.0, corridor_y)

    rotated_obs = _obstacles(45.0)
    control_obs = _obstacles(0.0)

    assert rotated_obs.segment_blocked(a, b, layer_idx, net=1) is True
    # Pre-#4910 behavior (rotation dropped): the same corridor reads clear.
    assert control_obs.segment_blocked(a, b, layer_idx, net=1) is False

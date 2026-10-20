"""Pad keep-outs use the true pad outline, not its bounding square (#7503)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.router.lattice.obstacles import LatticeObstacleModel
from kicad_tools.router.primitives import Pad

AGENT_RADIUS = 0.2  # clearance 0.2 around a zero-width probe


def _model(pad: Pad) -> LatticeObstacleModel:
    lattice = SimpleNamespace(nodes={}, edges=[])
    return LatticeObstacleModel(lattice, [pad], [(0,)], 2, AGENT_RADIUS)  # type: ignore[arg-type]


def _pad(**kw) -> Pad:
    base = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0, "net": 1, "net_name": "A"}
    base.update(kw)
    pad = Pad(**base)
    return pad


def _rr(rratio: float | None, **kw) -> Pad:
    pad = _pad(**kw)
    pad.shape = "roundrect"
    pad.roundrect_rratio = rratio
    return pad


def _hit(model: LatticeObstacleModel, p: tuple[float, float]) -> bool:
    return model.segment_blocked(p, p, 0, net=2)


def test_roundrect_corner_diagonal_accepted_and_near_rejected():
    # 1x1 roundrect, ratio 0.5 -> a circle of radius 0.5.
    model = _model(_rr(0.5))
    # Diagonal at 45 deg: true clearance 0.2 + 0.05 from the circle.
    d_ok = (0.5 + 0.2 + 0.05) / 2**0.5
    d_bad = (0.5 + 0.19) / 2**0.5
    # The bounding square keep-out (0.7) would have rejected both.
    assert not _hit(model, (d_ok, d_ok))
    assert _hit(model, (d_bad, d_bad))


def test_unknown_ratio_stays_rectangular_and_conservative():
    model = _model(_rr(None))
    # Square corner of the rect is at (0.5, 0.5); 0.19 away diagonally is blocked.
    assert _hit(model, (0.5 + 0.19 / 2**0.5, 0.5 + 0.19 / 2**0.5))
    assert not _hit(model, (0.5 + 0.21 / 2**0.5, 0.5 + 0.21 / 2**0.5))


def test_rotated_pad_uses_true_outline():
    # 2x0.4 pad rotated 45 deg: its AABB is ~1.7 square, far wider than the copper.
    pad = _pad(width=2.0, height=0.4, rotation=45.0)
    model = _model(pad)
    # Point on the perpendicular axis, 0.2+0.2+0.05 away from the pad centreline.
    off = (0.2 + 0.2 + 0.05) / 2**0.5
    assert not _hit(model, (off, off))
    off_bad = (0.2 + 0.2 - 0.01) / 2**0.5
    assert _hit(model, (off_bad, off_bad))


def test_same_net_is_not_an_obstacle():
    model = _model(_rr(0.5))
    assert not model.segment_blocked((0.0, 0.0), (0.0, 0.0), 0, net=1)


@pytest.mark.parametrize("shape", ["circle", "oval"])
def test_circle_and_oval(shape):
    pad = _pad(width=1.0, height=1.0)
    pad.shape = shape
    model = _model(pad)
    d_ok = (0.5 + 0.25) / 2**0.5
    assert not _hit(model, (d_ok, d_ok))
    assert _hit(model, (0.69 / 2**0.5, 0.69 / 2**0.5))


# -- issue #7503 follow-up: the exact outline keeps the pad's net class ------
#
# Board 03 at seed 42: JOY_X (Digital, 0.15 mm) was laid 0.166 mm from the
# rounded corner of GND pad Y1.4 (Power, 0.2 mm), inside the old bounding
# square but outside a 0.15 mm exact keep-out, and the post-route authored
# backstop demoted the net.  Y1.4 is a 1.4 x 1.2 roundrect, rratio 0.208333
# (corner radius 0.25), the probe a 0.2 mm track over a 0.15 mm board default.

Y1_HALF_TRACE = 0.1
Y1_BASE_CLEARANCE = 0.15
Y1_CLASS_CLEARANCE = 0.2


def _y1_pad() -> Pad:
    pad = _pad(x=135.4, y=84.65, width=1.4, height=1.2, net=6, net_name="GND")
    pad.shape = "roundrect"
    pad.roundrect_rratio = 0.208333
    return pad


def _y1_probe(copper_gap: float) -> tuple[float, float]:
    """Track centre diagonal to Y1.4's +x/+y rounded corner at ``copper_gap``."""
    r = 1.2 * 0.208333  # KiCad: rratio x min(width, height)
    cx, cy = 135.4 + 0.7 - r, 84.65 + 0.6 - r
    d = (r + copper_gap + Y1_HALF_TRACE) / 2**0.5
    return (cx + d, cy + d)


def _y1_model(class_extra: list[float] | None) -> LatticeObstacleModel:
    lattice = SimpleNamespace(nodes={}, edges=[])
    return LatticeObstacleModel(
        lattice,  # type: ignore[arg-type]
        [_y1_pad()],
        [(0,)],
        2,
        Y1_HALF_TRACE + Y1_BASE_CLEARANCE,
        pad_class_extra=class_extra,
    )


def test_y1_corner_probe_inside_bounding_square():
    # Guard on the fixture: the probe is inside the broad-phase rectangle, so
    # it is the exact test (not the rectangle) that decides.
    model = _y1_model(None)
    x, y = _y1_probe(0.166)
    x0, y0, x1, y1 = model.pad_rects[0]
    assert x0 < x < x1 and y0 < y < y1


def test_y1_corner_rejected_at_pad_class():
    model = _y1_model([Y1_CLASS_CLEARANCE - Y1_BASE_CLEARANCE])
    assert model.segment_blocked(*(_y1_probe(0.166),) * 2, 0, net=12)
    assert model.segment_blocked(*(_y1_probe(0.199),) * 2, 0, net=12)
    # The corner is still released once the class is cleared.
    assert not model.segment_blocked(*(_y1_probe(0.21),) * 2, 0, net=12)


def test_y1_corner_without_class_is_the_base_clearance():
    # No class installed: the exact test is the board default (0.15 mm), the
    # pre-follow-up behaviour, which is what let JOY_X in at 0.166 mm.
    model = _y1_model(None)
    assert not model.segment_blocked(*(_y1_probe(0.166),) * 2, 0, net=12)
    assert model.segment_blocked(*(_y1_probe(0.14),) * 2, 0, net=12)


def test_class_never_grows_the_rectangle():
    # Edge band beyond the rectangle stays with class_pad_repair (#6395): the
    # class may only tighten the exact test, never block more than main did.
    plain = _y1_model(None)
    classed = _y1_model([Y1_CLASS_CLEARANCE - Y1_BASE_CLEARANCE])
    assert plain.pad_rects == classed.pad_rects
    # 0.17 mm straight off the +x edge: outside the 0.15 rectangle.
    p = (135.4 + 0.7 + 0.17 + Y1_HALF_TRACE, 84.65)
    assert not classed.segment_blocked(p, p, 0, net=12)


def test_pathfinder_feeds_pad_class_into_outline_test():
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(trace_width=0.2, trace_clearance=Y1_BASE_CLEARANCE)
    pad = _y1_pad()
    outline = [(130.0, 80.0), (140.0, 80.0), (140.0, 90.0), (130.0, 90.0)]
    pf = LatticePathfinder(outline, [pad], rules)
    probe = _y1_probe(0.166)
    assert not pf.obstacles.segment_blocked(probe, probe, 0, net=12)
    builds = pf.lattice_builds
    pf.set_pad_class_clearances([Y1_CLASS_CLEARANCE])
    assert pf.obstacles.segment_blocked(probe, probe, 0, net=12)
    # Masks rebuilt, lattice not; re-installing the same values is a no-op.
    assert pf.lattice_builds == builds
    obstacles = pf.obstacles
    pf.set_pad_class_clearances([Y1_CLASS_CLEARANCE])
    assert pf.obstacles is obstacles


# Pair legs see the same outline as single-ended legs (#7503 judge, head
# 42493cb): with the single-ended checks on the exact outline but the pair
# checks (``coupled.pads_block_*_grown``) still on ``pad_rects``, the coupled
# search lost the pad-corner corridors the single-ended legs take, and 06rf's
# USB3_RX1/TX1 fell back to uncoupled routes.


@pytest.mark.parametrize("gap", [0.14, 0.166, 0.199, 0.21, 0.3])
@pytest.mark.parametrize("class_extra", [None, [Y1_CLASS_CLEARANCE - Y1_BASE_CLEARANCE]])
def test_pair_checks_agree_with_single_ended_outline(gap, class_extra):
    from kicad_tools.router.lattice.coupled import (
        pads_block_point_grown,
        pads_block_segment_grown,
    )

    model = _y1_model(class_extra)
    p = _y1_probe(gap)
    single = model.segment_blocked(p, p, 0, net=12)
    assert pads_block_segment_grown(model, p, p, 0, {12, 13}, 0.0) is single
    assert pads_block_point_grown(model, p, 0, {12, 13}, 0.0) is single


def test_pair_checks_release_corner_the_rectangle_blocked():
    from kicad_tools.router.lattice.coupled import (
        pads_block_point_grown,
        pads_block_segment_grown,
    )

    model = _y1_model([Y1_CLASS_CLEARANCE - Y1_BASE_CLEARANCE])
    p = _y1_probe(0.21)
    x0, y0, x1, y1 = model.pad_rects[0]
    assert x0 < p[0] < x1 and y0 < p[1] < y1  # the old rectangle blocked it
    assert not pads_block_point_grown(model, p, 0, {12, 13}, 0.0)
    assert not pads_block_segment_grown(model, p, p, 0, {12, 13}, 0.0)
    # The fat half-envelope ``extra`` still widens the exact keep-out.
    assert pads_block_point_grown(model, p, 0, {12, 13}, 0.05)
    # Exempt (centerline) mode and own-net mode still skip the pad.
    assert not pads_block_point_grown(model, _y1_probe(0.1), 0, {6}, 0.0)
    assert not pads_block_point_grown(model, _y1_probe(0.1), 0, set(), 0.0, frozenset({0}))

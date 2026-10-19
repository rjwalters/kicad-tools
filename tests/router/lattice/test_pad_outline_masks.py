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

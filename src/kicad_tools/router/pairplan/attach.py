"""Engine-free attach / anchor-shift candidate generators (issue #6837, 2c-iii).

Lattice pair routing has three places that *generate* geometric candidates and
then *judge* them with engine legality: ``relocate_pair_anchor`` (slide the
P/N anchor, then dogleg each pad to its leg end), ``sandwiched_pad_escape_sites``
(a via site beside a sealed-in pad) and ``LatticePathfinder._pair_attach``
(dogleg a pad to a leg end).  This module owns the generation, in the exact
order the inline loops used; the engines keep the judging, as loop bodies.

Order matters: every caller takes the **first** legal candidate, so changing
the yield order changes the routed copper.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..primitives import Pad
from ..quantize import dogleg_points
from .geometry import Pt

_EPS = 1e-9


def dogleg_attach_candidates(pad_xy: Pt, target: Pt) -> Iterator[list[Pt]]:
    """The two 45-aligned dogleg polylines from ``pad_xy`` to ``target``.

    Yields the ``axis_first=False`` polyline (diagonal leaves the pad), then
    the ``axis_first=True`` one (axis leg leaves the pad).
    """
    for axis_first in (False, True):
        yield dogleg_points(pad_xy[0], pad_xy[1], target[0], target[1], axis_first=axis_first)


def anchor_shift_candidates(
    pad_p: Pad,
    pad_n: Pad,
    toward: Pt,
    *,
    half_pitch: float,
    step: float,
    max_shift: float,
) -> Iterator[tuple[Pt, Pt, Pt]]:
    """Candidate ``(anchor, end_p, end_n)`` for a P/N anchor slid off the midpoint.

    The anchor moves perpendicular to the P-N axis, on the side facing
    ``toward``, by ``k * step`` for ``k = 1 .. int(max_shift / step + 0.5)``;
    ``end_p`` / ``end_n`` are the nominal leg ends ``anchor +/- half_pitch``
    along the axis.  Yields nothing when the P-N axis is degenerate.
    """
    mid = ((pad_p.x + pad_n.x) / 2.0, (pad_p.y + pad_n.y) / 2.0)
    ux, uy = pad_p.x - pad_n.x, pad_p.y - pad_n.y
    axis_len = (ux * ux + uy * uy) ** 0.5
    if axis_len <= _EPS:
        return
    ux, uy = ux / axis_len, uy / axis_len
    nx, ny = -uy, ux  # perpendicular to the P-N axis
    if (toward[0] - mid[0]) * nx + (toward[1] - mid[1]) * ny < 0:
        nx, ny = -nx, -ny
    steps = int(max_shift / step + 0.5)
    for k in range(1, steps + 1):
        shift = k * step
        anchor = (mid[0] + nx * shift, mid[1] + ny * shift)
        end_p = (anchor[0] + ux * half_pitch, anchor[1] + uy * half_pitch)
        end_n = (anchor[0] - ux * half_pitch, anchor[1] - uy * half_pitch)
        yield anchor, end_p, end_n


def sandwich_via_site(pad: Pad, normal: Pt, via_radius: float, reach: float) -> Pt:
    """Via centre just off ``pad``'s outer edge along the outward ``normal``.

    ``reach`` is the pad box's support distance along ``normal``
    (``|nx| * half_w + |ny| * half_h``); the site sits ``via_radius`` past it.
    """
    return (pad.x + normal[0] * (reach + via_radius), pad.y + normal[1] * (reach + via_radius))

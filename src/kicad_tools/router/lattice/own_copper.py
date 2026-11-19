"""Start points on a net's own kept copper (issue #9636).

``kct route --complete`` keeps the copper a board already carries and asks the
lattice engine only for the missing joins.  A join that leaves a pad which is
*already* connected -- a fine-pitch pin with a (narrower than class width)
escape track on it -- used to start from the pad centre again, at the net's
class width, and decline when that width cannot leave the pin even though a
track already does.

This module turns the kept routes of one net into **anchors**: the points of
that copper a new join may start from, with the width the copper already has
there.  :meth:`LatticePathfinder._escape_stubs` falls back to them when the
pad's own escape declines.  Pure geometry, no routing state.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..primitives import Pad, pad_half_extents

Pt = tuple[float, float]

# Endpoints closer than this are the same copper joint (mm).
_JOINT_TOL_MM = 0.01
# Per-pad cap so a heavily routed island cannot multiply the stub scans.
MAX_ANCHORS_PER_PAD = 12


@dataclass(frozen=True)
class KeptAnchor:
    """A point on already-routed copper of the net a new link may start from."""

    layer: int
    point: Pt
    width: float


def pad_key(pad: Pad) -> tuple[str, str]:
    """Identity of a pad across object copies: ``(ref, pin)``."""
    return (pad.ref, pad.pin)


def _on_pad(pad: Pad, point: Pt) -> bool:
    hw, hh = pad_half_extents(pad)
    return (
        abs(point[0] - pad.x) <= hw + _JOINT_TOL_MM and abs(point[1] - pad.y) <= hh + _JOINT_TOL_MM
    )


def _same(a: Pt, b: Pt) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= _JOINT_TOL_MM


def kept_copper_anchors(
    pads: Iterable[Pad],
    routes: Sequence[Any],
    layer_index: Callable[[Any], int | None],
) -> dict[tuple[str, str], list[KeptAnchor]]:
    """Anchors on the copper already attached to each pad of ONE net.

    ``routes`` are the net's kept routes (``segments`` / ``vias``);
    ``layer_index`` maps a segment's layer to the lattice layer index (or
    ``None`` when the layer is outside the routing stack).  For each pad the
    copper reachable from it -- segments chained end to end, hopping layers at
    vias -- is collected and every segment endpoint that is NOT inside the pad
    itself becomes an anchor (an endpoint inside another pad of the net is the
    far end of an existing join, and a legitimate place to start from).  A pad
    with no copper attached has no entry.
    """
    segs: list[tuple[int, Pt, Pt, float]] = []
    vias: list[Pt] = []
    for route in routes:
        for s in getattr(route, "segments", ()) or ():
            layer = layer_index(s.layer)
            if layer is None:
                continue
            a, b = (float(s.x1), float(s.y1)), (float(s.x2), float(s.y2))
            if _same(a, b):
                continue
            segs.append((layer, a, b, float(s.width)))
        for v in getattr(route, "vias", ()) or ():
            vias.append((float(v.x), float(v.y)))
    if not segs:
        return {}

    def joined(i: int, j: int) -> bool:
        li, ai, bi, _ = segs[i]
        lj, aj, bj, _ = segs[j]
        if li == lj and any(_same(p, q) for p in (ai, bi) for q in (aj, bj)):
            return True
        return any(
            any(_same(p, v) for p in (ai, bi)) and any(_same(q, v) for q in (aj, bj)) for v in vias
        )

    out: dict[tuple[str, str], list[KeptAnchor]] = {}
    for pad in pads:
        seeds = [
            i
            for i, (layer, a, b, _w) in enumerate(segs)
            if (pad.through_hole or _pad_on_layer(pad, layer, layer_index))
            and (_on_pad(pad, a) or _on_pad(pad, b))
        ]
        if not seeds:
            continue
        seen = set(seeds)
        stack = list(seeds)
        while stack:
            i = stack.pop()
            for j in range(len(segs)):
                if j not in seen and joined(i, j):
                    seen.add(j)
                    stack.append(j)
        anchors: list[KeptAnchor] = []
        marks: set[tuple[int, int, int]] = set()
        for i in sorted(seen):
            layer, a, b, w = segs[i]
            for p in (a, b):
                if _on_pad(pad, p):
                    continue
                mark = (layer, round(p[0] / _JOINT_TOL_MM), round(p[1] / _JOINT_TOL_MM))
                if mark in marks:
                    continue
                marks.add(mark)
                anchors.append(KeptAnchor(layer, p, w))
        if anchors:
            anchors.sort(
                key=lambda k: (math.hypot(k.point[0] - pad.x, k.point[1] - pad.y), k.layer)
            )
            out[pad_key(pad)] = anchors[:MAX_ANCHORS_PER_PAD]
    return out


def _pad_on_layer(pad: Pad, layer: int, layer_index: Callable[[Any], int | None]) -> bool:
    return layer_index(pad.layer) == layer

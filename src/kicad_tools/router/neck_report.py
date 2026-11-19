"""Post-route neck census: sub-class-width copper, reported not silent.

Audit row 3 of #9592 (issue #9600): power nets came back with narrow necks
running several mm and the run summary said nothing.  The #1018 neck-down
taper is pad-local by construction (its width interpolates from
``min_trace_width`` back to the class width across ``neck_down_distance``),
and the escape-stub necks are pad-local stubs -- but nothing in the run
summary ever said so.  :func:`collect_necked_copper` measures the emitted
copper against each net's class width so ``kct route`` can report every
necked net, its narrowest width and its longest contiguous necked run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable

from .primitives import Route

__all__ = ["NeckedNet", "collect_necked_copper"]

#: Widths below the class width by no more than this are rounding noise,
#: not necks (mm).
NECK_EPSILON_MM = 1e-6


@dataclass
class NeckedNet:
    """Sub-class-width copper observed on one net."""

    net_name: str
    min_width_mm: float
    necked_length_mm: float
    longest_run_mm: float
    segment_count: int


def collect_necked_copper(
    routes: Iterable[Route],
    class_width_of: Callable[[str], float],
) -> dict[str, NeckedNet]:
    """Measure every net's copper against its class width.

    ``class_width_of`` maps a net name to the width that net's copper is
    supposed to carry (net-class trace width, falling back to the global
    rule).  A segment narrower than that width (beyond rounding noise) is
    necked copper; contiguous necked segments along a route -- consecutive
    in the route's emission order and touching end to start within a cell
    -- accumulate into runs.

    Returns one :class:`NeckedNet` per net that has any necked copper;
    nets routed entirely at class width never appear.
    """
    by_net: dict[str, NeckedNet] = {}
    for route in routes:
        resolved_name = route.net_name or f"Net_{route.net}"
        width_limit = class_width_of(resolved_name)
        run = 0.0
        prev_end: tuple[float, float] | None = None
        for seg in route.segments:
            necked = seg.width < width_limit - NECK_EPSILON_MM
            length = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            touching = (
                prev_end is not None
                and math.hypot(seg.x1 - prev_end[0], seg.y1 - prev_end[1]) <= 1e-9
            )
            run = (
                run + length if necked and (touching or run == 0.0) else (length if necked else 0.0)
            )
            prev_end = (seg.x2, seg.y2) if necked else None
            if not necked:
                continue
            entry = by_net.get(resolved_name)
            if entry is None:
                entry = NeckedNet(
                    net_name=resolved_name,
                    min_width_mm=seg.width,
                    necked_length_mm=0.0,
                    longest_run_mm=0.0,
                    segment_count=0,
                )
                by_net[resolved_name] = entry
            entry.min_width_mm = min(entry.min_width_mm, seg.width)
            entry.necked_length_mm += length
            entry.longest_run_mm = max(entry.longest_run_mm, run)
            entry.segment_count += 1
    return by_net

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
from typing import Callable, Iterable, Mapping

from .primitives import Route

__all__ = [
    "NeckedNet",
    "collect_necked_copper",
    "format_neck_report",
    "format_neck_entries",
    "class_width_lookup",
    "necked_net_to_dict",
]

#: Widths below the class width by no more than this are rounding noise,
#: not necks (mm).
NECK_EPSILON_MM = 1e-6

#: Fallback "long neck" threshold (mm) when no rule-derived value is passed;
#: callers that know the design rules pass ``neck_down_distance``-derived
#: thresholds instead (a run longer than the taper is beyond it).
DEFAULT_LONG_NECK_MM = 2.0


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


def class_width_lookup(
    net_class_map: Mapping | None,
    default_width: float,
) -> Callable[[str], float]:
    """Each net's intended trace width: its net class, else the global rule.

    The one class-width rule shared by every neck-census caller (``kct
    route``'s Results section and JSON diagnostics, ``route-auto``'s result
    document) so the three surfaces cannot disagree about what counts as
    necked.
    """

    def width_of(net_name: str) -> float:
        nc = (net_class_map or {}).get(net_name)
        return getattr(nc, "trace_width", None) or default_width

    return width_of


def necked_net_to_dict(
    info: NeckedNet,
    *,
    long_threshold_mm: float = DEFAULT_LONG_NECK_MM,
) -> dict:
    """Serialise one census entry for the JSON surfaces (#9600 row 3).

    ``beyond_taper`` is True when the longest contiguous necked run
    exceeds ``long_threshold_mm`` -- callers that know the design rules
    pass ``neck_down_distance`` (+ a grid cell), so a run longer than the
    taper itself is flagged wherever the entry is rendered.
    """
    return {
        "net_name": info.net_name,
        "min_width_mm": round(info.min_width_mm, 6),
        "necked_length_mm": round(info.necked_length_mm, 6),
        "longest_run_mm": round(info.longest_run_mm, 6),
        "segment_count": info.segment_count,
        "beyond_taper": info.longest_run_mm > long_threshold_mm,
    }


def _neck_header(count: int) -> str:
    return f"  Necked copper:  {count} net(s) below class width"


def _neck_entry_line(
    name: str,
    min_width_mm: float,
    necked_length_mm: float,
    longest_run_mm: float,
    segment_count: int,
    *,
    beyond_taper: bool = False,
) -> str:
    note = " -- long, verify intent" if beyond_taper else ""
    return (
        f"    {name}: {segment_count} segment(s), min width "
        f"{min_width_mm:.3f}mm, necked length {necked_length_mm:.2f}mm "
        f"(longest run {longest_run_mm:.2f}mm){note}"
    )


def format_neck_report(
    necks: dict[str, NeckedNet],
    *,
    long_threshold_mm: float = DEFAULT_LONG_NECK_MM,
) -> str:
    """Render the neck census for the Results section (audit row 3, #9600).

    Empty when no net carries sub-class-width copper -- a clean board adds
    no output.  A necked run longer than ``long_threshold_mm`` (callers
    derive it from ``rules.neck_down_distance``: a contiguous neck longer
    than the taper distance is beyond the taper) is called out as long:
    never silent, exactly what the audit asked for.
    """
    if not necks:
        return ""
    lines = [_neck_header(len(necks))]
    for name in sorted(necks):
        info = necks[name]
        lines.append(
            _neck_entry_line(
                name,
                info.min_width_mm,
                info.necked_length_mm,
                info.longest_run_mm,
                info.segment_count,
                beyond_taper=info.longest_run_mm > long_threshold_mm,
            )
        )
    return "\n".join(lines)


def format_neck_entries(entries: Iterable[dict]) -> str:
    """Render pre-serialised :func:`necked_net_to_dict` entries.

    The ``route-auto`` path renders its result document's ``necked_copper``
    list (the census itself ran inside :func:`route_net_auto`); keep the
    two surfaces byte-identical by sharing the line renderer.  The
    ``beyond_taper`` flag was decided at census time, where the design
    rules live.
    """
    rows = list(entries)
    if not rows:
        return ""
    lines = [_neck_header(len(rows))]
    for row in rows:
        lines.append(
            _neck_entry_line(
                row["net_name"],
                row["min_width_mm"],
                row["necked_length_mm"],
                row["longest_run_mm"],
                row["segment_count"],
                beyond_taper=row["beyond_taper"],
            )
        )
    return "\n".join(lines)

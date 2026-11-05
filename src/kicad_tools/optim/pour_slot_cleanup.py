"""Pour-slot cleanup: spread or pack parallel tracks that slot a pour (Issue #8440).

:class:`~kicad_tools.validate.rules.pour_slot.PourSlotRule` (Issue #8435) flags
different-net parallel tracks whose gap is below ``2 * clearance +
min_thickness``: the strip of pour between them cannot fill and the plane gets
a slot.  This pass resolves such pairs by shifting one run perpendicular to
itself:

* **spread** -- widen the gap to the rule's threshold (the strip then fills);
* **pack** -- opt-in (``pack=True``): tighten the gap to the pair's clearance.
  This removes the slot by removing the ground strip, and the pair is still
  flagged by the advisory, so it is never the default;
* otherwise the pair is **left alone**.

Detection is :func:`iter_pour_slot_pairs`, the same code the advisory uses.
Every edit is refereed: ``referee(pcb)`` returns per-category DRC counts
(including ``"unconnected"``) for the edited board, and an edit that raises any
count, or fails to reduce the number of slot pairs, is reverted.  With no
working referee the pass refuses and edits nothing.

Only the moved run's endpoints are re-pointed: same-net segments that meet it
at an endpoint follow it (their end is stretched to the new point); a run that
ends on a via, a pad or a locked segment is left alone.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from kicad_tools.validate.rules.pour_slot import PourSlotPair, iter_pour_slot_pairs

#: Margin (mm) added past the threshold so a spread pair is not borderline.
SPREAD_MARGIN_MM = 0.005
#: Minimum gap change (mm) worth an edit.
MIN_SHIFT_MM = 0.001
#: Endpoint-coincidence tolerance (mm).
_TOL = 1e-3

#: ``referee(pcb)`` -> ``{category: count}`` or ``None`` when it cannot run.
Referee = Callable[[Any], "Mapping[str, int] | None"]


@dataclass
class PourSlotCleanupResult:
    """Outcome of :func:`cleanup_pour_slots`."""

    spread: int = 0
    packed: int = 0
    left_alone: int = 0
    pairs_before: int = 0
    pairs_after: int = 0
    refused_reason: str | None = None
    #: Reason of the final failed attempt, one entry per left-alone pair.
    left_alone_reasons: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.spread or self.packed)

    @property
    def left_alone_by_reason(self) -> dict[str, int]:
        """``{reason: pairs}``; counts sum to :attr:`left_alone`."""
        counts: dict[str, int] = {}
        for why in self.left_alone_reasons:
            counts[why] = counts.get(why, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "spread": self.spread,
            "packed": self.packed,
            "left_alone": self.left_alone,
            "left_alone_by_reason": self.left_alone_by_reason,
            "pairs_before": self.pairs_before,
            "pairs_after": self.pairs_after,
            "refused_reason": self.refused_reason,
        }


def _dist_pt_seg(p: tuple[float, float], a: Any) -> float:
    ax, ay = a.start
    bx, by = a.end
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _dist_seg_seg(a: Any, b: Any) -> float:
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(a.start, a.end, b.start), cross(a.start, a.end, b.end)
    d3, d4 = cross(b.start, b.end, a.start), cross(b.start, b.end, a.end)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return 0.0
    return min(
        _dist_pt_seg(a.start, b),
        _dist_pt_seg(a.end, b),
        _dist_pt_seg(b.start, a),
        _dist_pt_seg(b.end, a),
    )


def _near(p: tuple[float, float], q: tuple[float, float]) -> bool:
    return math.hypot(p[0] - q[0], p[1] - q[1]) <= _TOL


def _pad_positions(pcb: Any) -> list[tuple[tuple[float, float], float]]:
    out: list[tuple[tuple[float, float], float]] = []
    for fp in getattr(pcb, "footprints", []) or []:
        for pad in getattr(fp, "pads", []) or []:
            try:
                pos = pcb.get_pad_position(fp.reference, pad.number)
            except Exception:
                pos = None
            if pos is not None:
                size = getattr(pad, "size", (0.0, 0.0))
                out.append((pos, max(size) / 2 if size else 0.0))
    return out


def _plan_shift(
    pcb: Any, mover: Any, dx: float, dy: float, pads: list
) -> tuple[list[tuple[Any, tuple, tuple]], str | None]:
    """Plan new endpoints for ``mover`` shifted by ``(dx, dy)``.

    Returns ``([(old_segment, new_start, new_end), ...], reason)``; ``reason``
    is set (and the plan empty) when the run cannot be moved safely.
    """
    if getattr(mover, "locked", False):
        return [], "locked track"
    if not mover.net_name:
        return [], "unnamed net"
    new_ends = {
        0: (mover.start[0] + dx, mover.start[1] + dy),
        1: (mover.end[0] + dx, mover.end[1] + dy),
    }
    edits: dict[int, list] = {id(mover): [mover, mover.start, mover.end]}
    edits[id(mover)][1], edits[id(mover)][2] = new_ends[0], new_ends[1]

    for idx, old_pt in ((0, mover.start), (1, mover.end)):
        for via in pcb.vias:
            if (
                _near(old_pt, via.position)
                or math.hypot(old_pt[0] - via.position[0], old_pt[1] - via.position[1])
                <= via.size / 2
            ):
                return [], "endpoint on a via"
        for pos, radius in pads:
            if math.hypot(old_pt[0] - pos[0], old_pt[1] - pos[1]) <= max(radius, _TOL):
                return [], "endpoint on a pad"
        for other in pcb.segments:
            if other is mover or other.layer != mover.layer:
                continue
            if other.net_number != mover.net_number:
                continue
            for which, pt in ((0, other.start), (1, other.end)):
                if not _near(old_pt, pt):
                    continue
                if getattr(other, "locked", False):
                    return [], "connected to a locked track"
                rec = edits.setdefault(id(other), [other, other.start, other.end])
                rec[1 + which] = new_ends[idx]
    plan = []
    for seg, ns, ne in edits.values():
        if math.hypot(ne[0] - ns[0], ne[1] - ns[1]) < _TOL:
            return [], "move would collapse a connected track"
        plan.append((seg, ns, ne))
    return plan, None


def _apply(pcb: Any, plan: list) -> list[tuple[Any, Any]]:
    """Replace each planned segment; return ``[(old, new), ...]`` for undo."""
    done = []
    for old, ns, ne in plan:
        pcb.remove_segments([old])
        new = pcb.add_trace(
            (round(ns[0], 4), round(ns[1], 4)),
            (round(ne[0], 4), round(ne[1], 4)),
            width=old.width,
            layer=old.layer,
            net=old.net_name,
            dedupe=False,
        )
        done.append((old, new))
    return done


def _undo(pcb: Any, done: list) -> None:
    for old, new in done:
        pcb.remove_segments(list(new))
        pcb.add_trace(
            old.start, old.end, width=old.width, layer=old.layer, net=old.net_name, dedupe=False
        )


def _clear_of_others(pcb: Any, plan: list, clearance: float) -> bool:
    """Cheap pre-check: moved copper keeps ``clearance`` from other-net copper."""
    moved = {id(s) for s, _a, _b in plan}
    for old, ns, ne in plan:
        probe = type("P", (), {"start": ns, "end": ne})()
        for other in pcb.segments:
            if id(other) in moved or other.layer != old.layer:
                continue
            if other.net_number == old.net_number:
                continue
            if _dist_seg_seg(probe, other) - (old.width + other.width) / 2 < clearance - 1e-6:
                return False
        for via in pcb.vias:
            if via.net_number == old.net_number:
                continue
            if _dist_pt_seg(via.position, probe) - (old.width + via.size) / 2 < clearance - 1e-6:
                return False
    return True


def _no_worse(after: Mapping[str, int], baseline: Mapping[str, int]) -> bool:
    return all(after.get(k, 0) <= baseline.get(k, 0) for k in set(after) | set(baseline))


def _slots(pcb: Any, netclass_clearances: Mapping[str, float] | None) -> list[PourSlotPair]:
    """Flagged pairs that still leave an unfillable strip (``gap > 2 x clearance``).

    A pair packed to ``clearance`` is still flagged by the advisory (its gap is
    below the threshold) but has no strip left between the keep-outs, hence no
    slot: the pour is simply absent between the tracks.
    """
    return [
        p for p in iter_pour_slot_pairs(pcb, netclass_clearances) if p.gap > 2 * p.clearance + _TOL
    ]


def _pair_key(pair: PourSlotPair) -> tuple:
    def k(s: Any) -> tuple:
        return (
            round(s.start[0], 3),
            round(s.start[1], 3),
            round(s.end[0], 3),
            round(s.end[1], 3),
            s.layer,
        )

    return tuple(sorted((k(pair.first), k(pair.second))))


def cleanup_pour_slots(
    pcb: Any,
    netclass_clearances: Mapping[str, float] | None = None,
    *,
    referee: Referee,
    pack: bool = False,
) -> PourSlotCleanupResult:
    """Spread (and, with ``pack=True``, pack) pour-slot pairs in place, refereed per edit."""
    result = PourSlotCleanupResult()
    pairs = _slots(pcb, netclass_clearances)
    result.pairs_before = result.pairs_after = len(pairs)
    if not pairs:
        return result

    baseline = referee(pcb)
    if baseline is None:
        result.refused_reason = "referee unavailable (kicad-cli could not run); no edits applied"
        return result
    baseline = dict(baseline)
    pads = _pad_positions(pcb)
    dead: set[tuple] = set()

    while True:
        pairs = [p for p in _slots(pcb, netclass_clearances) if _pair_key(p) not in dead]
        if not pairs:
            break
        pair = pairs[0]
        resolved = False
        last_why = "no accepted edit (clearance or referee rejected it)"
        key = _pair_key(pair)
        a, b = pair.first, pair.second
        la = math.hypot(a.end[0] - a.start[0], a.end[1] - a.start[1])
        lb = math.hypot(b.end[0] - b.start[0], b.end[1] - b.start[1])
        mover_order = (1, 0) if lb <= la else (0, 1)  # move the shorter run first
        for mover_idx in mover_order:
            for kind in ("spread", "pack") if pack else ("spread",):
                # An undone attempt re-adds fresh segment objects, so look the
                # pair up again rather than holding stale references.
                cur = next(
                    (p for p in _slots(pcb, netclass_clearances) if _pair_key(p) == key),
                    None,
                )
                if cur is None:
                    break
                pr = (cur.first, cur.second)
                mover, anchor = pr[mover_idx], pr[1 - mover_idx]
                ux, uy = anchor.end[0] - anchor.start[0], anchor.end[1] - anchor.start[1]
                n = math.hypot(ux, uy)
                nx, ny = -uy / n, ux / n
                mx = (mover.start[0] + mover.end[0]) / 2 - anchor.start[0]
                my = (mover.start[1] + mover.end[1]) / 2 - anchor.start[1]
                sign = 1.0 if mx * nx + my * ny >= 0 else -1.0
                nx, ny = nx * sign, ny * sign  # points anchor -> mover
                shift = (
                    cur.threshold - cur.gap + SPREAD_MARGIN_MM
                    if kind == "spread"
                    else -(cur.gap - cur.clearance)
                )
                if abs(shift) < MIN_SHIFT_MM:
                    continue
                plan, why = _plan_shift(pcb, mover, nx * shift, ny * shift, pads)
                if why:
                    last_why = why
                    continue
                if not _clear_of_others(pcb, plan, cur.clearance):
                    last_why = "no clear room for the shifted run"
                    continue
                before_n = len(_slots(pcb, netclass_clearances))
                done = _apply(pcb, plan)
                after_n = len(_slots(pcb, netclass_clearances))
                score = referee(pcb) if after_n < before_n else None
                if score is not None and _no_worse(score, baseline):
                    baseline = dict(score)
                    if kind == "spread":
                        result.spread += 1
                    else:
                        result.packed += 1
                    resolved = True
                    break
                _undo(pcb, done)
            if resolved:
                break
        if not resolved:
            dead.add(_pair_key(pair))
            result.left_alone += 1
            result.left_alone_reasons.append(last_why)

    result.pairs_after = len(_slots(pcb, netclass_clearances))
    return result

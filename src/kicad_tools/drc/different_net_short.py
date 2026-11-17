"""Grid-independent different-net copper-short verifier + repair post-pass.

Issue #4470 (board-05 Phase 2) -- a *grid-independent* guarantee that the
copper the router emitted is free of different-net shorts.

Motivation
----------
board-05 (a dense fine-pitch BLDC controller) routes on
``--allow-unsafe-grid``: its auto-selected 0.1 mm grid is coarser than
``clearance / 2`` (0.075 mm) because the memory-safe 0.05 mm grid reaches
fewer nets.  At that resolution the C++ A* **occupancy** model cannot
represent sub-cell clearance, so two different-net vias/segments can
quantize into geometrically-overlapping world positions that pass the
grid-occupancy check yet fail geometric DRC.  A fresh regen therefore
emits real ``shorting_items`` (``kicad-cli pcb drc --refill-zones``): a via
shorting ``NRST``/``OSC_IN`` on ``In2.Cu`` and a via shorting
``PWM_CH``/``OSC_OUT`` on ``B.Cu``.

The router's world-coordinate clearance predicates
(:mod:`kicad_tools.router.via_clearance`) already know how to measure
edge-to-edge copper distance, but the router only ever consults them
*within the coarse grid*.  This module **lifts that geometry into a
post-route pass**: after all copper is placed it geometrically checks
every emitted via + segment for different-net overlaps the grid missed,
and relocates the offending via with the clearance-safe candidate-ladder
engine shared with the #4408 hole-to-hole relocation pass.

Two public entry points
-----------------------
* :func:`find_different_net_shorts` -- the grid-independent **verifier**.
  Pure geometry over the placed copper; layer-aware (a via only conflicts
  with copper on a layer its plated barrel actually spans).  Returns a
  :class:`ShortItem` per different-net overlap.  With the default
  ``clearance=0.0`` it flags only *actual* copper overlaps (the
  ``shorting_items`` DRC class); a positive ``clearance`` additionally
  flags sub-clearance near-misses.
* :func:`repair_different_net_shorts` -- the **repair** post-pass.  For
  every short that involves a via it relocates the via to a
  clearance-safe location using the shared
  :func:`kicad_tools.drc.relocate_drill_clearance._try_relocate` engine
  (candidate ladder + connectivity stubs + a boxed-in "leave in place and
  report" fallback).  Because the relocation target is accepted only when
  it clears *all* foreign copper by the manufacturer clearance, moving the
  via there necessarily eliminates the short without introducing a new one
  -- an already-short-free board is never regressed.

Scope note: the repair moves *vias* (a point object with an escape node to
slide along).  A pure segment-vs-segment overlap has no via to relocate;
it is detected and surfaced as ``unresolved`` rather than silently
dropped.  On board-05 both shorts are via shorts, so via relocation covers
them.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    segment_to_segment_distance,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB, Segment, Via

# Only overlaps deeper than this (mm) count as a short.  Grid-quantization
# overlaps on a coarse grid are ~0.025 mm+ (grid 0.1 vs clearance/2 0.075),
# so a sub-micron floor rejects floating-point noise without missing a real
# short.  Mirrors the epsilon convention in :mod:`router.via_clearance`.
_EPS = 1e-4


# ---------------------------------------------------------------------------
# Copper-layer ordering (physical stack, NOT KiCad's internal layer numbers)
# ---------------------------------------------------------------------------
#
# KiCad renumbers copper layers so ``layer.number`` is NOT the physical
# stack order (a 4-layer board can carry In2.Cu=6, B.Cu=2).  The layer NAME
# encodes the true order: ``F.Cu`` is the top, ``In{k}.Cu`` are the inner
# layers in ascending ``k``, and ``B.Cu`` is the bottom.  We map a name to a
# monotonic ordinal so a via's plated barrel span [start..end] can be tested
# for overlap with a segment's single layer.
_BOTTOM_ORDINAL = 10_000


def _copper_ordinal(layer_name: str) -> int | None:
    """Return a monotonic top->bottom ordinal for a copper layer name.

    ``F.Cu`` -> 0, ``In{k}.Cu`` -> k, ``B.Cu`` -> a large sentinel so it
    always sorts last.  Returns ``None`` for a non-copper layer name.
    """
    if layer_name == "F.Cu":
        return 0
    if layer_name == "B.Cu":
        return _BOTTOM_ORDINAL
    if layer_name.startswith("In") and layer_name.endswith(".Cu"):
        try:
            return int(layer_name[2:-3])
        except ValueError:
            return None
    return None


def _via_layer_span(via: Via) -> tuple[int, int]:
    """Return the (top, bottom) copper ordinals a via's barrel spans.

    A standard through-hole via lists ``("F.Cu", "B.Cu")`` and therefore
    spans the whole stack; a blind/buried/micro via lists its true
    endpoints.  A via with no parseable copper layers is treated as
    spanning the full stack (conservative -- never misses a conflict).
    """
    ordinals = [o for name in via.layers if (o := _copper_ordinal(name)) is not None]
    if not ordinals:
        return (0, _BOTTOM_ORDINAL)
    return (min(ordinals), max(ordinals))


def _via_spans_layer(via: Via, layer_name: str) -> bool:
    """True iff the via's plated barrel reaches ``layer_name``."""
    ord_ = _copper_ordinal(layer_name)
    if ord_ is None:
        return False
    lo, hi = _via_layer_span(via)
    return lo <= ord_ <= hi


def _via_spans_overlap(a: Via, b: Via) -> bool:
    """True iff two vias share at least one copper layer."""
    a_lo, a_hi = _via_layer_span(a)
    b_lo, b_hi = _via_layer_span(b)
    return max(a_lo, b_lo) <= min(a_hi, b_hi)


# ---------------------------------------------------------------------------
# Net identity (dialect-robust)
# ---------------------------------------------------------------------------


def _net_identity(pcb: PCB, net_number: int, net_name: str) -> tuple[object, str] | None:
    """Return a ``(key, display_name)`` net identity, or ``None`` if unnetted.

    Prefers the numeric net id (what routed copper always carries); falls
    back to the name for KiCad-10 name-only ``(net "SDA")`` copper whose
    numeric id parses as 0.  Returns ``None`` for genuinely unnetted copper
    (net 0 with no name) so free-floating graphic copper never registers as
    a short participant.
    """
    if net_number != 0:
        name = net_name
        if not name:
            net = pcb._nets.get(net_number)
            name = net.name if net is not None else str(net_number)
        return (net_number, name)
    if net_name:
        return (net_name, net_name)
    return None


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


@dataclass
class ShortItem:
    """A geometric different-net copper overlap the grid model missed.

    Attributes:
        kind: ``"via-via"``, ``"via-segment"`` or ``"segment-segment"``.
        net_a_name / net_b_name: The two shorted net names.
        layer: The copper layer the conflict occurs on (``"?"`` when a via
            barrel spans multiple candidate layers with a segment on a
            single one it is the segment's layer; for via-via it is the
            top shared layer).
        x / y: Approximate world location of the overlap (mm).
        gap: Edge-to-edge copper gap in mm (negative = overlap depth).
    """

    kind: str
    net_a_name: str
    net_b_name: str
    layer: str
    x: float
    y: float
    gap: float
    # Movable via handles for the repair pass (not part of equality/repr).
    via_a: Via | None = field(default=None, compare=False, repr=False)
    via_b: Via | None = field(default=None, compare=False, repr=False)

    @property
    def net_pair(self) -> frozenset[str]:
        """Order-independent {net_a, net_b} name pair."""
        return frozenset({self.net_a_name, self.net_b_name})

    def describe(self) -> str:
        """Human-readable one-line description."""
        return (
            f"{self.kind} short {self.net_a_name}/{self.net_b_name} on {self.layer} "
            f"at ({self.x:.3f}, {self.y:.3f}); gap {self.gap:.3f}mm"
        )


def _seg_bbox(seg: Segment) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box (minx, miny, maxx, maxy) of a segment core."""
    (x1, y1), (x2, y2) = seg.start, seg.end
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def find_different_net_shorts(
    pcb: PCB,
    *,
    clearance: float = 0.0,
) -> list[ShortItem]:
    """Geometrically flag different-net copper overlaps the grid missed.

    Checks every pair among the board's emitted vias + routed segments for
    a different-net edge-to-edge gap below ``clearance`` (with the default
    ``clearance=0.0`` this flags only *actual* copper overlaps -- the
    ``shorting_items`` DRC class).  The check is layer-aware: a via
    conflicts with a segment (or another via) only on a copper layer its
    plated barrel actually spans, so a through-hole via on ``F.Cu`` copper
    does not spuriously "short" an inner-layer trace of another net.

    This is the grid-independent guarantee: unlike the router's coarse
    grid-occupancy model, it operates on raw world coordinates, so it
    catches sub-cell overlaps the occupancy model cannot represent.

    Args:
        pcb: The routed board to inspect (not mutated).
        clearance: Minimum required edge-to-edge gap (mm).  ``0.0`` = flag
            overlaps only; a positive value additionally flags sub-clearance
            near-misses.

    Returns:
        One :class:`ShortItem` per different-net violation, deterministically
        ordered.
    """
    threshold = clearance - _EPS
    shorts: list[ShortItem] = []

    vias = list(pcb.vias)
    segments = list(pcb.segments)

    # Precompute net identities once.
    via_ident: list[tuple[object, str] | None] = [
        _net_identity(pcb, v.net_number, v.net_name) for v in vias
    ]
    seg_ident: list[tuple[object, str] | None] = [
        _net_identity(pcb, s.net_number, s.net_name) for s in segments
    ]

    # --- via vs via -------------------------------------------------------
    for i in range(len(vias)):
        vi = vias[i]
        idi = via_ident[i]
        if idi is None:
            continue
        for j in range(i + 1, len(vias)):
            vj = vias[j]
            idj = via_ident[j]
            if idj is None or idi[0] == idj[0]:
                continue
            if not _via_spans_overlap(vi, vj):
                continue
            d = math.hypot(vi.position[0] - vj.position[0], vi.position[1] - vj.position[1])
            gap = d - vi.size / 2.0 - vj.size / 2.0
            if gap < threshold:
                lo = max(_via_layer_span(vi)[0], _via_layer_span(vj)[0])
                layer = _ordinal_layer_name(pcb, lo)
                shorts.append(
                    ShortItem(
                        kind="via-via",
                        net_a_name=idi[1],
                        net_b_name=idj[1],
                        layer=layer,
                        x=(vi.position[0] + vj.position[0]) / 2.0,
                        y=(vi.position[1] + vj.position[1]) / 2.0,
                        gap=gap,
                        via_a=vi,
                        via_b=vj,
                    )
                )

    # --- via vs segment ---------------------------------------------------
    for i, vi in enumerate(vias):
        idi = via_ident[i]
        if idi is None:
            continue
        vr = vi.size / 2.0
        for k, seg in enumerate(segments):
            idk = seg_ident[k]
            if idk is None or idi[0] == idk[0]:
                continue
            if not _via_spans_layer(vi, seg.layer):
                continue
            d = point_to_segment_distance(
                vi.position[0], vi.position[1], seg.start[0], seg.start[1], seg.end[0], seg.end[1]
            )
            gap = d - vr - seg.width / 2.0
            if gap < threshold:
                shorts.append(
                    ShortItem(
                        kind="via-segment",
                        net_a_name=idi[1],
                        net_b_name=idk[1],
                        layer=seg.layer,
                        x=vi.position[0],
                        y=vi.position[1],
                        gap=gap,
                        via_a=vi,
                        via_b=None,
                    )
                )

    # --- segment vs segment (same layer) ----------------------------------
    for a in range(len(segments)):
        sa = segments[a]
        ida = seg_ident[a]
        if ida is None:
            continue
        ax0, ay0, ax1, ay1 = _seg_bbox(sa)
        pad_a = sa.width / 2.0 + clearance
        for b in range(a + 1, len(segments)):
            sb = segments[b]
            idb = seg_ident[b]
            if idb is None or ida[0] == idb[0] or sa.layer != sb.layer:
                continue
            bx0, by0, bx1, by1 = _seg_bbox(sb)
            pad = pad_a + sb.width / 2.0
            # Cheap AABB reject before the exact segment-segment distance.
            if ax1 + pad < bx0 or bx1 + pad < ax0 or ay1 + pad < by0 or by1 + pad < ay0:
                continue
            d = segment_to_segment_distance(
                sa.start[0],
                sa.start[1],
                sa.end[0],
                sa.end[1],
                sb.start[0],
                sb.start[1],
                sb.end[0],
                sb.end[1],
            )
            gap = d - sa.width / 2.0 - sb.width / 2.0
            if gap < threshold:
                shorts.append(
                    ShortItem(
                        kind="segment-segment",
                        net_a_name=ida[1],
                        net_b_name=idb[1],
                        layer=sa.layer,
                        x=(sa.start[0] + sa.end[0] + sb.start[0] + sb.end[0]) / 4.0,
                        y=(sa.start[1] + sa.end[1] + sb.start[1] + sb.end[1]) / 4.0,
                        gap=gap,
                        via_a=None,
                        via_b=None,
                    )
                )

    shorts.sort(key=lambda s: (s.kind, s.net_a_name, s.net_b_name, round(s.x, 3), round(s.y, 3)))
    return shorts


def _ordinal_layer_name(pcb: PCB, ordinal: int) -> str:
    """Best-effort reverse map from a copper ordinal to a layer name."""
    for layer in pcb.copper_layers:
        if _copper_ordinal(layer.name) == ordinal:
            return layer.name
    if ordinal == 0:
        return "F.Cu"
    if ordinal >= _BOTTOM_ORDINAL:
        return "B.Cu"
    return f"In{ordinal}.Cu"


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


@dataclass
class ShortRepairMove:
    """Record of a via relocated to clear a different-net short."""

    old_x: float
    old_y: float
    new_x: float
    new_y: float
    net_name: str
    uuid: str
    stub_layers: list[str] = field(default_factory=list)


@dataclass
class ShortRepairUnresolved:
    """Record of a short left in place (boxed-in via, or segment-only)."""

    kind: str
    net_a_name: str
    net_b_name: str
    layer: str
    x: float
    y: float
    reason: str


@dataclass
class ShortRepairResult:
    """Aggregate outcome of a different-net short repair pass."""

    moved: list[ShortRepairMove] = field(default_factory=list)
    unresolved: list[ShortRepairUnresolved] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when at least one via was moved."""
        return bool(self.moved)

    def summary(self) -> str:
        """Human-readable multi-line summary."""
        lines = [
            f"Different-net short repair: moved {len(self.moved)} via(s), "
            f"{len(self.unresolved)} unresolved"
        ]
        for m in self.moved:
            lines.append(
                f"  Via {m.uuid[:8] or '?'} (net '{m.net_name}'): "
                f"({m.old_x:.3f}, {m.old_y:.3f}) -> ({m.new_x:.3f}, {m.new_y:.3f}); "
                f"stubs on {', '.join(m.stub_layers) or '(none)'}"
            )
        for u in self.unresolved:
            lines.append(
                f"  UNRESOLVED {u.kind} {u.net_a_name}/{u.net_b_name} on {u.layer} "
                f"at ({u.x:.3f}, {u.y:.3f}): {u.reason}"
            )
        return "\n".join(lines)


def repair_different_net_shorts(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    detect_clearance: float = 0.0,
    dry_run: bool = False,
) -> ShortRepairResult:
    """Relocate offending vias to eliminate different-net copper shorts.

    For each short that involves a via, a new location is found with the
    shared clearance-safe candidate-ladder engine
    (:func:`kicad_tools.drc.relocate_drill_clearance._try_relocate`): it
    prefers a slide onto the via's own routed escape node, else an
    8-direction ladder, and accepts a target only when
    :func:`kicad_tools.cli.relocate_in_pad_vias._check_clearance` passes at
    it -- i.e. the target clears *every* foreign via/segment/pad by
    ``design_rules.min_clearance_mm`` and every drill by
    ``min_hole_to_hole_mm``.  Because that guarantee subsumes "not shorting
    the offended net", moving the via there both eliminates the short and
    introduces no new violation (safety invariant).  A via with no
    clearance-legal location (boxed in) is left in place and reported.  A
    pure segment-vs-segment short (no via to move) is reported as
    unresolved.

    Greedy ordering: the via participating in the most shorts is moved
    first, so one relocation can clear several overlaps.

    Args:
        pcb: The routed board to repair (mutated in place unless ``dry_run``).
        design_rules: Active manufacturer rules driving the clearance-safe
            relocation.
        detect_clearance: Gap threshold (mm) passed to
            :func:`find_different_net_shorts`.  Defaults to ``0.0`` (repair
            actual overlaps only).
        dry_run: When True, compute the report without mutating the board.

    Returns:
        A :class:`ShortRepairResult` listing every moved via and every
        short left in place.
    """
    from kicad_tools.cli.relocate_in_pad_vias import (
        _collect_smd_pads_by_net,
        _collect_tht_pads,
    )
    from kicad_tools.drc.relocate_drill_clearance import _try_relocate

    result = ShortRepairResult()

    min_clearance = design_rules.min_clearance_mm
    min_hole_to_hole = design_rules.min_hole_to_hole_mm

    pads_by_net = _collect_smd_pads_by_net(pcb)
    tht_pads = _collect_tht_pads(pcb)

    # Vias whose relocation was attempted and failed (boxed in): do not retry.
    failed: set[int] = set()

    max_iterations = 4 * max(1, len(pcb.vias))
    for _ in range(max_iterations):
        shorts = find_different_net_shorts(pcb, clearance=detect_clearance)
        via_shorts = [s for s in shorts if s.via_a is not None or s.via_b is not None]
        if not via_shorts:
            break

        # Count via participation (by object identity) and remember handles.
        counts: Counter[int] = Counter()
        via_by_id: dict[int, Via] = {}
        for s in via_shorts:
            for via in (s.via_a, s.via_b):
                if via is None:
                    continue
                counts[id(via)] += 1
                via_by_id[id(via)] = via

        ordered = sorted(
            via_by_id.values(),
            key=lambda v: (-counts[id(v)], round(v.position[0], 4), round(v.position[1], 4)),
        )

        moved_this_pass = False
        for via in ordered:
            if id(via) in failed or via.net_number == 0:
                continue
            outcome = _try_relocate(
                pcb, via, pads_by_net, tht_pads, min_clearance, min_hole_to_hole, dry_run
            )
            if outcome is None:
                failed.add(id(via))
                continue
            result.moved.append(
                ShortRepairMove(
                    old_x=outcome.old_x,
                    old_y=outcome.old_y,
                    new_x=outcome.new_x,
                    new_y=outcome.new_y,
                    net_name=outcome.net_name,
                    uuid=outcome.uuid,
                    stub_layers=list(outcome.stub_layers),
                )
            )
            moved_this_pass = True
            if dry_run:
                # The board is not mutated in dry_run, so the same short
                # would be re-selected forever; report one move and stop.
                failed.add(id(via))
            break

        if not moved_this_pass:
            break

    # Surface any short still present after the greedy loop.
    for s in find_different_net_shorts(pcb, clearance=detect_clearance):
        if s.via_a is None and s.via_b is None:
            reason = "segment-vs-segment overlap (no via to relocate)"
        else:
            reason = "no clearance-legal location for the offending via (boxed in)"
        result.unresolved.append(
            ShortRepairUnresolved(
                kind=s.kind,
                net_a_name=s.net_a_name,
                net_b_name=s.net_b_name,
                layer=s.layer,
                x=s.x,
                y=s.y,
                reason=reason,
            )
        )

    return result


__all__ = [
    "ShortItem",
    "ShortRepairMove",
    "ShortRepairResult",
    "ShortRepairUnresolved",
    "find_different_net_shorts",
    "repair_different_net_shorts",
]

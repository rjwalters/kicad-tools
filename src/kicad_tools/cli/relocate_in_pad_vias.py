#!/usr/bin/env python3
"""Relocate via-in-pad vias off-pad (connectivity-preserving).

Issue #4359 -- Phase 1 (signal-via slide-out).

When a board is routed for a manufacturer that supports via-in-pad
(``jlcpcb-tier1``, ``pcbway``) and then re-targeted to a profile that does
NOT (``jlcpcb``, ``oshpark``, ``seeed``, ``flashpcb``), every via whose drill
sits inside an SMD pad becomes an unmanufacturable via-in-pad violation.

This module implements the **signal-via** relocation pass: for each in-pad via
that has a connected routed track (an "escape track"), the via is slid just
outside the pad boundary along the track's direction, with drill-edge clearance
>= the profile's ``min_clearance_mm``.  Connectivity is preserved by adding
short **stub** segments from the via's original (in-pad) location to its new
location on every layer that had connected copper (the pad's copper layer plus
each connected segment's layer).  No existing copper is mutated -- only the
via's ``(at ...)`` position moves and new stub segments are appended -- so an
already-routed board is never regressed.

Deferred to follow-ups (explicitly out of scope for Phase 1):

* **Phase 2** -- plane-stitch vias (a via tying a power/ground pad to a pour
  with no routed track).  These have no escape direction; relocating them needs
  the ``stitch --avoid-pad-overlap`` candidate-offset engine.  They are reported
  as *unresolvable* here, never left silently in-pad.
* **Phase 3** -- multi-branch fan-out, non-cardinal rotated pads, and
  dense-pitch pads with no clearing location.  Each is reported as
  *unresolvable* / *skipped* rather than mis-placed.

Any via Phase 1 cannot safely handle is counted in the report (skipped or
unresolvable) -- it is never left in-pad without being surfaced.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.validate.rules.via_pad_geometry import (
    is_smd_pad,
    pad_absolute_bbox,
    via_inside_pad,
)

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment, Via

# Coincidence tolerance for "a track endpoint lands on the via center", mirroring
# the 0.001 mm test used by fix_vias_cmd.find_nearby_items.
_COINCIDENT_TOL = 1e-3


@dataclass
class ViaRelocation:
    """Record of an in-pad via that was slid off-pad."""

    old_x: float
    old_y: float
    new_x: float
    new_y: float
    net: int
    net_name: str
    pad_ref: str
    uuid: str
    stub_layers: list[str] = field(default_factory=list)


@dataclass
class ViaRelocationSkip:
    """Record of an in-pad via that could not be relocated in Phase 1.

    ``category`` is ``"skipped"`` (a valid off-pad slide exists in principle but
    would introduce a new clearance / hole-to-hole violation) or
    ``"unresolvable"`` (no Phase-1 escape geometry -- plane-stitch, multi-branch,
    or no clearing location).
    """

    x: float
    y: float
    net: int
    net_name: str
    pad_ref: str
    reason: str
    uuid: str
    category: str = "unresolvable"


@dataclass
class RelocationResult:
    """Aggregate outcome of a relocation pass."""

    moved: list[ViaRelocation] = field(default_factory=list)
    skipped: list[ViaRelocationSkip] = field(default_factory=list)
    unresolvable: list[ViaRelocationSkip] = field(default_factory=list)
    supported_noop: bool = False

    @property
    def changed(self) -> bool:
        """True when at least one via was (or would be) moved."""
        return bool(self.moved)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _ray_aabb_exit_distance(
    px: float, py: float, dx: float, dy: float, bbox: tuple[float, float, float, float]
) -> float:
    """Distance from interior point ``(px, py)`` along unit dir ``(dx, dy)`` to
    the boundary of axis-aligned ``bbox``.

    Assumes ``(px, py)`` lies inside ``bbox`` and ``(dx, dy)`` is a unit vector.
    Returns the smallest positive ``t`` at which the ray exits the box.
    """
    min_x, min_y, max_x, max_y = bbox
    t_candidates: list[float] = []
    if dx > 1e-12:
        t_candidates.append((max_x - px) / dx)
    elif dx < -1e-12:
        t_candidates.append((min_x - px) / dx)
    if dy > 1e-12:
        t_candidates.append((max_y - py) / dy)
    elif dy < -1e-12:
        t_candidates.append((min_y - py) / dy)
    positive = [t for t in t_candidates if t > 0]
    if not positive:
        return 0.0
    return min(positive)


def _dist_point_to_aabb(px: float, py: float, bbox: tuple[float, float, float, float]) -> float:
    """Shortest distance from ``(px, py)`` to axis-aligned ``bbox``.

    Returns 0.0 when the point is inside the box.
    """
    min_x, min_y, max_x, max_y = bbox
    ddx = max(min_x - px, 0.0, px - max_x)
    ddy = max(min_y - py, 0.0, py - max_y)
    return math.hypot(ddx, ddy)


def _dist_point_to_segment(px: float, py: float, seg: Segment) -> float:
    """Shortest distance from ``(px, py)`` to the line segment ``seg``."""
    x1, y1 = seg.start
    x2, y2 = seg.end
    dx = x2 - x1
    dy = y2 - y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = x1 + t * dx
    cy = y1 + t * dy
    return math.hypot(px - cx, py - cy)


def _endpoint_at(seg: Segment, x: float, y: float) -> tuple[float, float] | None:
    """Return the *other* endpoint of ``seg`` if one endpoint is at ``(x, y)``.

    Returns ``None`` when neither endpoint coincides with ``(x, y)``.
    """
    if abs(seg.start[0] - x) < _COINCIDENT_TOL and abs(seg.start[1] - y) < _COINCIDENT_TOL:
        return seg.end
    if abs(seg.end[0] - x) < _COINCIDENT_TOL and abs(seg.end[1] - y) < _COINCIDENT_TOL:
        return seg.start
    return None


def _pad_copper_layer(pad: Pad) -> str:
    """Return the pad's copper layer name (first ``*.Cu``), defaulting F.Cu."""
    for layer in pad.layers:
        if layer.endswith(".Cu") and not layer.startswith("*"):
            return layer
    return "F.Cu"


# ---------------------------------------------------------------------------
# Core relocation pass
# ---------------------------------------------------------------------------


def _collect_smd_pads_by_net(
    pcb: PCB,
) -> dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]]:
    """Group SMD pads (net != 0) by net number with precomputed AABBs."""
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]] = {}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if not is_smd_pad(pad):
                continue
            if pad.net_number == 0:
                continue
            bbox = pad_absolute_bbox(pad, fp)
            pads_by_net.setdefault(pad.net_number, []).append((fp, pad, bbox))
    return pads_by_net


def _check_clearance(
    pcb: PCB,
    via: Via,
    new_x: float,
    new_y: float,
    pads_by_net: dict[int, list[tuple[Footprint, Pad, tuple[float, float, float, float]]]],
    min_clearance: float,
    min_hole_to_hole: float,
) -> str | None:
    """Return a human reason string if placing ``via`` at ``(new_x, new_y)``
    would violate copper clearance or hole-to-hole; ``None`` when clear.

    Same-net copper is exempt (a via may touch its own net).  Only vias, SMD
    pads, and routed segments are considered (Phase 1 scope).
    """
    via_r = via.size / 2.0
    hole_r = via.drill / 2.0

    # Other vias: hole-to-hole (any net) + copper clearance (different net).
    for other in pcb.vias:
        if other is via:
            continue
        d = math.hypot(other.position[0] - new_x, other.position[1] - new_y)
        hole_gap = d - hole_r - other.drill / 2.0
        if hole_gap < min_hole_to_hole - 1e-6:
            return (
                f"hole-to-hole {hole_gap:.3f}mm to via at "
                f"({other.position[0]:.2f}, {other.position[1]:.2f})"
            )
        if other.net_number != via.net_number:
            cu_gap = d - via_r - other.size / 2.0
            if cu_gap < min_clearance - 1e-6:
                return (
                    f"clearance {cu_gap:.3f}mm to via at "
                    f"({other.position[0]:.2f}, {other.position[1]:.2f})"
                )

    # SMD pads on other nets: copper clearance to pad AABB.
    for net_number, entries in pads_by_net.items():
        if net_number == via.net_number:
            continue
        for fp, pad, bbox in entries:
            cu_gap = _dist_point_to_aabb(new_x, new_y, bbox) - via_r
            if cu_gap < min_clearance - 1e-6:
                return f"clearance {cu_gap:.3f}mm to pad {fp.reference}-{pad.number}"

    # Routed segments on other nets: copper clearance.
    for seg in pcb.segments:
        if seg.net_number == via.net_number:
            continue
        cu_gap = _dist_point_to_segment(new_x, new_y, seg) - via_r - seg.width / 2.0
        if cu_gap < min_clearance - 1e-6:
            return f"clearance {cu_gap:.3f}mm to track on net {seg.net_number}"

    return None


def relocate_in_pad_vias(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    nets: set[str] | None = None,
    dry_run: bool = False,
) -> RelocationResult:
    """Slide signal in-pad vias off-pad, preserving connectivity (Phase 1).

    Args:
        pcb: The board to operate on (mutated in place unless ``dry_run``).
        design_rules: Active manufacturer rules.  A no-op when
            ``via_in_pad_supported`` is True.  ``min_clearance_mm`` and
            ``min_hole_to_hole_mm`` gate the off-pad placement.
        nets: Optional set of net *names* to restrict the pass to.  ``None``
            means all nets.
        dry_run: When True, compute the report but do not mutate the board.

    Returns:
        A :class:`RelocationResult` with moved / skipped / unresolvable records.
        Relocation is always clearance-safe: any via whose only off-pad slide
        would introduce a new clearance or hole-to-hole violation is recorded
        as *skipped* and left untouched (an already-routed board is never made
        worse).
    """
    result = RelocationResult()

    # Capability gate: a no-op on profiles that support via-in-pad.
    if getattr(design_rules, "via_in_pad_supported", False):
        result.supported_noop = True
        return result

    min_clearance = design_rules.min_clearance_mm
    min_hole_to_hole = design_rules.min_hole_to_hole_mm

    pads_by_net = _collect_smd_pads_by_net(pcb)

    # Iterate a snapshot: relocate_via/add_trace mutate the underlying lists.
    for via in list(pcb.vias):
        if via.net_number == 0:
            continue

        candidates = pads_by_net.get(via.net_number)
        if not candidates:
            continue

        # First same-net pad whose AABB fully contains the via drill.
        containing = next(
            ((fp, pad, bbox) for fp, pad, bbox in candidates if via_inside_pad(via, bbox)),
            None,
        )
        if containing is None:
            continue

        fp, pad, bbox = containing
        pad_ref = f"{fp.reference}-{pad.number}"
        net_name = via.net_name or pad.net_name or ""

        # Net-name scoping.
        if nets is not None and net_name not in nets:
            continue

        vx, vy = via.position

        # Classify: enumerate routed segments whose endpoint lands on the via.
        connected: list[tuple[Segment, tuple[float, float]]] = []
        for seg in pcb.segments_in_net(via.net_number):
            far = _endpoint_at(seg, vx, vy)
            if far is not None:
                connected.append((seg, far))

        if not connected:
            # No routed track -> plane-stitch or unconnected via (Phase 2).
            result.unresolvable.append(
                ViaRelocationSkip(
                    x=vx,
                    y=vy,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    reason=(
                        "no connected routed track (plane-stitch via -- deferred to "
                        "Phase 2 stitch-engine relocation)"
                    ),
                    uuid=via.uuid,
                    category="unresolvable",
                )
            )
            continue

        pad_center_x = (bbox[0] + bbox[2]) / 2.0
        pad_center_y = (bbox[1] + bbox[3]) / 2.0

        # Choose the escape track: the connected segment whose far endpoint is
        # farthest from the pad center (the one leaving the pad).  Its far
        # endpoint MUST lie outside the pad AABB, otherwise there is no reliable
        # exit direction (multi-branch / fully-internal -> Phase 3).
        escape = max(
            connected,
            key=lambda item: math.hypot(item[1][0] - pad_center_x, item[1][1] - pad_center_y),
        )
        _, far = escape
        if _dist_point_to_aabb(far[0], far[1], bbox) <= 1e-6:
            result.unresolvable.append(
                ViaRelocationSkip(
                    x=vx,
                    y=vy,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    reason=(
                        "no connected track escapes the pad boundary "
                        "(multi-branch / internal -- deferred to Phase 3)"
                    ),
                    uuid=via.uuid,
                    category="unresolvable",
                )
            )
            continue

        # Unit escape direction (from via center toward the far endpoint).
        dir_x = far[0] - vx
        dir_y = far[1] - vy
        dir_len = math.hypot(dir_x, dir_y)
        if dir_len < 1e-9:
            result.unresolvable.append(
                ViaRelocationSkip(
                    x=vx,
                    y=vy,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    reason="degenerate escape-track direction",
                    uuid=via.uuid,
                    category="unresolvable",
                )
            )
            continue
        dir_x /= dir_len
        dir_y /= dir_len

        # Slide along the escape direction until the drill circle clears the pad
        # edge by min_clearance: new_center = via + dir * (t_exit + drill/2 + clr).
        t_exit = _ray_aabb_exit_distance(vx, vy, dir_x, dir_y, bbox)
        slide = t_exit + via.drill / 2.0 + min_clearance
        new_x = vx + dir_x * slide
        new_y = vy + dir_y * slide

        # Clearance gate: never emit a worse board.
        reason = _check_clearance(
            pcb, via, new_x, new_y, pads_by_net, min_clearance, min_hole_to_hole
        )
        if reason is not None:
            result.skipped.append(
                ViaRelocationSkip(
                    x=vx,
                    y=vy,
                    net=via.net_number,
                    net_name=net_name,
                    pad_ref=pad_ref,
                    reason=reason,
                    uuid=via.uuid,
                    category="skipped",
                )
            )
            continue

        # Layers needing a connectivity stub: the pad's copper layer (pad->via
        # path) plus every connected segment's layer (route->via path).
        stub_layers: list[str] = []
        seen_layers: set[str] = set()
        for layer in [_pad_copper_layer(pad), *(seg.layer for seg, _ in connected)]:
            if layer and layer not in seen_layers:
                seen_layers.add(layer)
                stub_layers.append(layer)

        # Stub width: the escape track's width (fall back to a sane default).
        stub_width = escape[0].width if escape[0].width > 0 else 0.2

        if not dry_run:
            # Add stubs from the old (in-pad) location to the new via location
            # on each connected layer BEFORE moving the via, so connectivity is
            # continuous at every step.  No existing copper is mutated.
            for layer in stub_layers:
                pcb.add_trace(
                    (vx, vy),
                    (new_x, new_y),
                    width=stub_width,
                    layer=layer,
                    net=net_name or None,
                )
            moved_ok = pcb.relocate_via(via, (new_x, new_y))
            if not moved_ok:
                # Could not find the backing S-expression node -- record as
                # unresolvable rather than claiming a move that will not persist.
                result.unresolvable.append(
                    ViaRelocationSkip(
                        x=vx,
                        y=vy,
                        net=via.net_number,
                        net_name=net_name,
                        pad_ref=pad_ref,
                        reason="could not locate backing (via ...) node to persist move",
                        uuid=via.uuid,
                        category="unresolvable",
                    )
                )
                continue

        result.moved.append(
            ViaRelocation(
                old_x=vx,
                old_y=vy,
                new_x=new_x,
                new_y=new_y,
                net=via.net_number,
                net_name=net_name,
                pad_ref=pad_ref,
                uuid=via.uuid,
                stub_layers=stub_layers,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_relocation_results(
    result: RelocationResult,
    output_format: str = "text",
    dry_run: bool = False,
    mfr: str | None = None,
) -> None:
    """Print a relocation report in ``text``, ``json``, or ``summary`` form."""
    if output_format == "json":
        data = {
            "manufacturer": mfr,
            "dry_run": dry_run,
            "via_in_pad_supported_noop": result.supported_noop,
            "moved": [
                {
                    "old_x": m.old_x,
                    "old_y": m.old_y,
                    "new_x": m.new_x,
                    "new_y": m.new_y,
                    "net": m.net,
                    "net_name": m.net_name,
                    "pad": m.pad_ref,
                    "uuid": m.uuid,
                    "stub_layers": m.stub_layers,
                }
                for m in result.moved
            ],
            "skipped": [
                {
                    "x": s.x,
                    "y": s.y,
                    "net": s.net,
                    "net_name": s.net_name,
                    "pad": s.pad_ref,
                    "reason": s.reason,
                    "uuid": s.uuid,
                }
                for s in result.skipped
            ],
            "unresolvable": [
                {
                    "x": u.x,
                    "y": u.y,
                    "net": u.net,
                    "net_name": u.net_name,
                    "pad": u.pad_ref,
                    "reason": u.reason,
                    "uuid": u.uuid,
                }
                for u in result.unresolvable
            ],
        }
        print(json.dumps(data, indent=2))
        return

    if result.supported_noop:
        msg = (
            f"Manufacturer profile{f' {mfr}' if mfr else ''} supports via-in-pad; "
            "no relocation needed."
        )
        print(msg)
        return

    if output_format == "summary":
        action = "Would move" if dry_run else "Moved"
        print(f"{action} {len(result.moved)} in-pad via(s) off-pad")
        if result.skipped:
            print(f"  {len(result.skipped)} skipped (clearance/hole-to-hole)")
        if result.unresolvable:
            print(f"  {len(result.unresolvable)} unresolvable (Phase 2/3)")
        return

    # Text output.
    if not result.moved and not result.skipped and not result.unresolvable:
        print("No via-in-pad vias found; nothing to relocate.")
        return

    action = "Would move" if dry_run else "Moved"
    print(f"{action} {len(result.moved)} in-pad via(s) off-pad:")
    for m in result.moved[:10]:
        print(
            f"  Via {m.uuid[:8] or '?'} (net '{m.net_name}') on pad {m.pad_ref}: "
            f"({m.old_x:.3f}, {m.old_y:.3f}) -> ({m.new_x:.3f}, {m.new_y:.3f}); "
            f"stubs on {', '.join(m.stub_layers)}"
        )
    if len(result.moved) > 10:
        print(f"  ... and {len(result.moved) - 10} more")

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} via(s) (would violate clearance):")
        for s in result.skipped[:10]:
            print(f"  Via at ({s.x:.3f}, {s.y:.3f}) on pad {s.pad_ref}: {s.reason}")
        if len(result.skipped) > 10:
            print(f"  ... and {len(result.skipped) - 10} more")

    if result.unresolvable:
        print(
            f"\nUnresolvable {len(result.unresolvable)} via(s) (deferred to Phase 2/3 follow-ups):"
        )
        for u in result.unresolvable[:10]:
            print(f"  Via at ({u.x:.3f}, {u.y:.3f}) on pad {u.pad_ref}: {u.reason}")
        if len(result.unresolvable) > 10:
            print(f"  ... and {len(result.unresolvable) - 10} more")

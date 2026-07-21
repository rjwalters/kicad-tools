"""Generic, manufacturer-floor-driven hole-to-hole via relocation post-pass.

Issue #4408 -- back-port the artifact-only #4017 hole-to-hole fix into a
library-level, ``--mfr``-driven pass so *any* fine-pitch QFP/QFN board routed
at a tier that supports via-in-pad is drill-clearance-clean unattended.

Motivation
----------
When a fine-pitch part (e.g. an LQFP-48 at 0.5 mm pin pitch) is routed with
in-pad micro-vias, adjacent pins' in-pad vias can sit closer than the fab's
hole-to-hole (drill-to-drill) floor even though each via is individually a
legal via-in-pad.  On board-04 three in-pad micro-vias (OSC_OUT / NRST / GND)
stack at the 0.5 mm pitch, so the middle via's drill is only 0.350 mm
edge-to-edge from each neighbour -- below the ``jlcpcb-tier1`` 0.500 mm
``min_hole_to_hole_mm`` floor.  This is **not** a via-in-pad violation
(tier1 supports via-in-pad); it is a hole-to-hole spacing violation between two
otherwise-legal vias.

The existing :mod:`kicad_tools.cli.relocate_in_pad_vias` pass owns the
clearance-safe geometry engine (:func:`_check_clearance` -- hole-to-hole /
THT / copper aware -- plus the 8-direction candidate ladder and the
connectivity-stub logic) but is a **no-op** on profiles that support via-in-pad
and only ever targets a via *inside a same-net pad*.  It never fires on a
hole-to-hole pair of two legal in-pad vias.  This module **bridges that gap**:
it is *driven by* hole-to-hole violations (like
:mod:`kicad_tools.drc.repair_drill_clearance`) but *validated by* the
clearance-safe engine (like :mod:`kicad_tools.cli.relocate_in_pad_vias`) -- one
engine, no second copy of the geometry checks.

Mechanism
---------
For a board and the active manufacturer's :class:`DesignRules`:

1. Read the profile's ``min_hole_to_hole_mm`` (and ``min_clearance_mm``) -- the
   pass is purely floor-driven, so a wider-floor profile relocates more
   aggressively and a via-in-pad-supporting tier still gets hole-to-hole
   relief.
2. Enumerate via/via drill pairs closer than the floor.  Greedily pick the via
   participating in the **most** violations (the "middle" of a stack), so a
   single move can clear multiple pairs.
3. Find a new location for that via using the candidate ladder -- preferring a
   slide onto its own routed escape node (the #4017 pattern, minimal move,
   keeps the via in-pad) then an 8-direction ladder from the via centre -- that
   satisfies the hole-to-hole floor **and** passes
   :func:`_check_clearance` (no new copper / hole-to-hole / THT violation).
4. Preserve connectivity by appending a short stub from the old to the new
   location on every connected copper layer (the pad's copper layer plus each
   connected segment's layer), exactly as the Phase-1 signal-slide does.
5. When no clearance-legal location exists (boxed in), **leave the via in place
   and report it** -- the pass never mis-places a via into a fresh violation
   (safety invariant, mirroring the relocate_in_pad_vias contract).

The pass mutates the board in place (unless ``dry_run``).  It adds only same-net
stub copper and moves via positions -- no existing routed copper on any other
net is touched, so an already-routed board is never regressed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.cli.relocate_in_pad_vias import (
    _PLANE_DIRECTIONS,
    _check_clearance,
    _collect_smd_pads_by_net,
    _collect_tht_pads,
    _endpoint_at,
    _pad_copper_layer,
)
from kicad_tools.validate.rules.via_pad_geometry import via_inside_pad

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB, Segment, Via

# Coincidence tolerance (mm) for "a stub already runs old->new on this layer".
_SEG_COINCIDENT_TOL = 1e-3
# Floating-point slack for the hole-to-hole comparison (mm).
_EPS = 1e-6


@dataclass
class DrillClearanceRelocation:
    """Record of a via moved to satisfy the hole-to-hole floor."""

    old_x: float
    old_y: float
    new_x: float
    new_y: float
    net: int
    net_name: str
    uuid: str
    stub_layers: list[str] = field(default_factory=list)


@dataclass
class DrillClearanceUnresolved:
    """Record of a hole-to-hole violation left in place (boxed in)."""

    x: float
    y: float
    net: int
    net_name: str
    uuid: str
    reason: str


@dataclass
class DrillClearanceRelocationResult:
    """Aggregate outcome of a hole-to-hole relocation pass."""

    moved: list[DrillClearanceRelocation] = field(default_factory=list)
    unresolved: list[DrillClearanceUnresolved] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when at least one via was moved."""
        return bool(self.moved)

    def summary(self) -> str:
        """Human-readable one-line-per-section summary."""
        lines = [f"Hole-to-hole relocation: moved {len(self.moved)} via(s)"]
        for m in self.moved:
            lines.append(
                f"  Via {m.uuid[:8] or '?'} (net '{m.net_name}'): "
                f"({m.old_x:.3f}, {m.old_y:.3f}) -> ({m.new_x:.3f}, {m.new_y:.3f}); "
                f"stubs on {', '.join(m.stub_layers) or '(none)'}"
            )
        if self.unresolved:
            lines.append(f"  {len(self.unresolved)} unresolved (boxed in, left in place):")
            for u in self.unresolved:
                lines.append(f"    Via at ({u.x:.3f}, {u.y:.3f}) net '{u.net_name}': {u.reason}")
        return "\n".join(lines)


def _resolve_net_name(pcb: PCB, via: Via) -> str:
    """Return the via's net name, resolving from the board net table if needed.

    :class:`Via` objects created programmatically (``PCB.add_via``) carry only a
    net *number*; the name lives in ``pcb._nets``.  Loaded boards populate
    ``via.net_name`` directly.  This resolves both.
    """
    if via.net_name:
        return via.net_name
    net = pcb._nets.get(via.net_number)
    return net.name if net is not None else ""


def _hole_gap(a: Via, b: Via) -> float:
    """Edge-to-edge drill gap (mm) between two vias."""
    d = math.hypot(a.position[0] - b.position[0], a.position[1] - b.position[1])
    return d - a.drill / 2.0 - b.drill / 2.0


def _violating_pairs(vias: list[Via], min_hole_to_hole: float) -> list[tuple[Via, Via]]:
    """Return all via/via pairs whose drill gap is below the floor."""
    pairs: list[tuple[Via, Via]] = []
    for i in range(len(vias)):
        for j in range(i + 1, len(vias)):
            if _hole_gap(vias[i], vias[j]) < min_hole_to_hole - _EPS:
                pairs.append((vias[i], vias[j]))
    return pairs


def _segment_exists(
    pcb: PCB, net_number: int, layer: str, a: tuple[float, float], b: tuple[float, float]
) -> bool:
    """True when a same-net segment already runs ``a``->``b`` on ``layer``.

    Used to avoid appending a stub that would exactly duplicate an existing
    routed escape leg (which happens when a via is slid onto its own escape
    node).
    """
    for seg in pcb.segments_in_net(net_number):
        if seg.layer != layer:
            continue
        s, e = seg.start, seg.end
        fwd = (
            abs(s[0] - a[0]) < _SEG_COINCIDENT_TOL
            and abs(s[1] - a[1]) < _SEG_COINCIDENT_TOL
            and abs(e[0] - b[0]) < _SEG_COINCIDENT_TOL
            and abs(e[1] - b[1]) < _SEG_COINCIDENT_TOL
        )
        rev = (
            abs(s[0] - b[0]) < _SEG_COINCIDENT_TOL
            and abs(s[1] - b[1]) < _SEG_COINCIDENT_TOL
            and abs(e[0] - a[0]) < _SEG_COINCIDENT_TOL
            and abs(e[1] - a[1]) < _SEG_COINCIDENT_TOL
        )
        if fwd or rev:
            return True
    return False


def _find_target(
    pcb: PCB,
    via: Via,
    escape_far: tuple[float, float] | None,
    pads_by_net: dict,
    tht_pads: list,
    min_clearance: float,
    min_hole_to_hole: float,
) -> tuple[float, float] | None:
    """Find the first clearance-safe location that clears the hole-to-hole floor.

    Preference order:

    1. The via's routed escape node (``escape_far``) -- a minimal move that
       keeps the via in-pad and lands on existing copper (the #4017 pattern),
       plus a few multiples of the floor along the same direction.
    2. An 8-direction ladder at increasing multiples of the floor from the via
       centre.

    A candidate is accepted only when :func:`_check_clearance` passes at it
    (which enforces the hole-to-hole floor to every other via/THT drill and the
    copper clearance to every other-net pad / track / via).  Returns ``None``
    when every candidate is boxed in.
    """
    vx, vy = via.position
    candidates: list[tuple[float, float]] = []

    if escape_far is not None:
        ex, ey = escape_far
        dist = math.hypot(ex - vx, ey - vy)
        if dist > _EPS:
            candidates.append((ex, ey))
            ux, uy = (ex - vx) / dist, (ey - vy) / dist
            for mult in (1.0, 1.25, 1.5, 2.0):
                candidates.append(
                    (vx + ux * min_hole_to_hole * mult, vy + uy * min_hole_to_hole * mult)
                )

    for mult in (1.0, 1.25, 1.5, 2.0):
        reach = min_hole_to_hole * mult
        for dx, dy in _PLANE_DIRECTIONS:
            candidates.append((vx + dx * reach, vy + dy * reach))

    for nx, ny in candidates:
        if (
            _check_clearance(
                pcb, via, nx, ny, pads_by_net, tht_pads, min_clearance, min_hole_to_hole
            )
            is None
        ):
            return (nx, ny)
    return None


def _try_relocate(
    pcb: PCB,
    via: Via,
    pads_by_net: dict,
    tht_pads: list,
    min_clearance: float,
    min_hole_to_hole: float,
    dry_run: bool,
) -> DrillClearanceRelocation | None:
    """Attempt one clearance-safe relocation of ``via``.

    Returns the :class:`DrillClearanceRelocation` record on success, or ``None``
    when the via is boxed in (no clearance-legal off-position) or the move could
    not be persisted -- in which case the via is left untouched.
    """
    vx, vy = via.position
    net_name = _resolve_net_name(pcb, via)

    # Same-net pad containing the via (for the pad-copper stub bond).
    containing_pad = None
    for _fp, pad, bbox in pads_by_net.get(via.net_number, []):
        if via_inside_pad(via, bbox):
            containing_pad = pad
            break

    # Same-net routed segments whose endpoint lands on the via.
    connected: list[tuple[Segment, tuple[float, float]]] = []
    for seg in pcb.segments_in_net(via.net_number):
        far = _endpoint_at(seg, vx, vy)
        if far is not None:
            connected.append((seg, far))

    escape_far: tuple[float, float] | None = None
    if connected:
        _seg, escape_far = max(
            connected,
            key=lambda item: math.hypot(item[1][0] - vx, item[1][1] - vy),
        )

    target = _find_target(
        pcb, via, escape_far, pads_by_net, tht_pads, min_clearance, min_hole_to_hole
    )
    if target is None:
        return None
    new_x, new_y = target

    # Connectivity stub layers: the pad's copper layer plus every connected
    # segment's layer (deduplicated, order-preserving).
    stub_layers: list[str] = []
    seen: set[str] = set()
    layer_sources: list[str] = []
    if containing_pad is not None:
        layer_sources.append(_pad_copper_layer(containing_pad))
    layer_sources.extend(seg.layer for seg, _ in connected)
    for layer in layer_sources:
        if layer and layer not in seen:
            seen.add(layer)
            stub_layers.append(layer)

    widths = [seg.width for seg, _ in connected if seg.width > 0]
    stub_width = min(widths) if widths else 0.2

    if not dry_run:
        # Append connectivity stubs BEFORE moving the via so the old location
        # stays bonded to the new one on every connected layer.  Skip a layer
        # whose old->new leg already exists (avoids duplicating an escape leg
        # when the via slides onto its own escape node).
        for layer in stub_layers:
            if _segment_exists(pcb, via.net_number, layer, (vx, vy), (new_x, new_y)):
                continue
            pcb.add_trace(
                (vx, vy),
                (new_x, new_y),
                width=stub_width,
                layer=layer,
                net=net_name or None,
            )
        if not pcb.relocate_via(via, (new_x, new_y)):
            return None

    return DrillClearanceRelocation(
        old_x=vx,
        old_y=vy,
        new_x=new_x,
        new_y=new_y,
        net=via.net_number,
        net_name=net_name,
        uuid=via.uuid,
        stub_layers=stub_layers,
    )


def relocate_drill_clearance(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    nets: set[str] | None = None,
    dry_run: bool = False,
) -> DrillClearanceRelocationResult:
    """Relocate vias to satisfy the manufacturer's hole-to-hole floor.

    Args:
        pcb: The board to operate on (mutated in place unless ``dry_run``).
        design_rules: Active manufacturer rules.  ``min_hole_to_hole_mm`` is the
            floor the pass enforces and ``min_clearance_mm`` gates the
            relocated copper.  Unlike :func:`relocate_in_pad_vias`, this pass is
            **not** gated on ``via_in_pad_supported`` -- a hole-to-hole
            violation between two legal in-pad vias must be relieved even on a
            profile that supports via-in-pad.
        nets: Optional set of net *names* to restrict the pass to (``None`` =
            all nets).
        dry_run: When True, compute the report without mutating the board.

    Returns:
        A :class:`DrillClearanceRelocationResult` listing every moved via and
        every violation left in place (boxed in).  Relocation is always
        clearance-safe: a via is moved only to a location that passes
        :func:`_check_clearance`, so the pass never introduces a new violation.
    """
    result = DrillClearanceRelocationResult()

    min_clearance = design_rules.min_clearance_mm
    min_hole_to_hole = design_rules.min_hole_to_hole_mm

    pads_by_net = _collect_smd_pads_by_net(pcb)
    tht_pads = _collect_tht_pads(pcb)

    # Vias whose relocation was attempted and failed (boxed in): do not retry,
    # so the greedy loop falls through to the partner via in the pair.
    failed: set[str] = set()

    # Bound the greedy loop: at most one move per via.
    max_iterations = 4 * max(1, len(pcb.vias))
    for _ in range(max_iterations):
        pairs = _violating_pairs(list(pcb.vias), min_hole_to_hole)
        if not pairs:
            break

        counts: dict[int, int] = {}
        vias_in_pairs: dict[int, Via] = {}
        for a, b in pairs:
            counts[id(a)] = counts.get(id(a), 0) + 1
            counts[id(b)] = counts.get(id(b), 0) + 1
            vias_in_pairs[id(a)] = a
            vias_in_pairs[id(b)] = b

        # Deterministic: most-violating first, then by position.
        ordered = sorted(
            vias_in_pairs.values(),
            key=lambda v: (-counts[id(v)], round(v.position[0], 4), round(v.position[1], 4)),
        )

        moved_this_pass = False
        for via in ordered:
            if via.uuid in failed:
                continue
            if via.net_number == 0:
                continue
            net_name = _resolve_net_name(pcb, via)
            if nets is not None and net_name not in nets:
                continue

            outcome = _try_relocate(
                pcb, via, pads_by_net, tht_pads, min_clearance, min_hole_to_hole, dry_run
            )
            if outcome is None:
                failed.add(via.uuid)
                continue
            result.moved.append(outcome)
            moved_this_pass = True
            # In dry_run mode the board is not mutated, so the same pair would
            # be re-selected forever -- stop after reporting one representative
            # move per violating via.
            if dry_run:
                failed.add(via.uuid)
            break

        if not moved_this_pass:
            break

    # Surface any violation still present after the greedy loop.
    seen_uuids: set[str] = set()
    for a, b in _violating_pairs(list(pcb.vias), min_hole_to_hole):
        for via in (a, b):
            if via.uuid in seen_uuids:
                continue
            seen_uuids.add(via.uuid)
            result.unresolved.append(
                DrillClearanceUnresolved(
                    x=via.position[0],
                    y=via.position[1],
                    net=via.net_number,
                    net_name=_resolve_net_name(pcb, via),
                    uuid=via.uuid,
                    reason="no clearance-legal location satisfies the hole-to-hole floor (boxed in)",
                )
            )

    return result

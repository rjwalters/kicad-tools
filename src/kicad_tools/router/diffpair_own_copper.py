"""Own-copper rule for diff-pair skew bumps (Issue #7891).

The skew tuner's other gates look at *foreign* copper
(:meth:`~kicad_tools.router.core.Autorouter._skew_tune_foreign_clearance_ok`,
which skips every pad of the bump's own net) and at the pair geometry
(:func:`~kicad_tools.router.diffpair_bump_gates.bump_gate_violation`).  None of
them knew where the net's **own** pads and vias are, so a bump could be laid
over its own pad.  This module is that missing check.  It is the one
implementation every skew-tuning path runs: the inline comb, the end-of-recipe
tuner and ``kct retune-lengths`` all reach it through
:func:`~kicad_tools.router.diffpair_length_tuning.tune_diff_pair_skew`'s
``own_copper`` argument, built once by
:meth:`~kicad_tools.router.core.Autorouter._skew_tune_own_copper_rule`.

Why own copper matters
======================

* **Electrically.**  Same-net copper has no clearance rule in KiCad's DRC, so
  nothing else stops a bump from touching its own pad.  Copper that touches
  the pad shorts the bump out: the signal takes the pad, and the length the
  bump adds is not real.
* **For the length both engines read.**  ``kct check`` and kicad-cli measure a
  net pad to pad (:mod:`kicad_tools.router.kicad_length`).  Only a run's two
  ends are trimmed: the stretch from the pad (or via) centre to where the run
  first leaves that copper becomes a straight line.  Copper that sits inside
  pad or via copper *at* a run end is therefore not counted, and copper that
  re-enters a pad further along the run is counted in full.  Issue #7864
  measured this on 06rf: a loop seated partly inside J1.B11 and the J1 via
  added +0.0697 mm by the checker's model against its geometric 0.1045 mm.
  When the bump's centre line stays outside its own pads and vias, the only
  trimmed stretch is the existing pad entry, which the bump does not change,
  so both engines count exactly the copper the bump adds.

The rule
========

Every segment of a candidate bump keeps an edge-to-edge gap of at least
``clearance_mm`` to every pad of its own net on its layer (or through-hole)
and every via of its own route, with two exemptions:

* a segment lying on one of the replaced host segments (the lead-in and
  lead-out pieces are a subset of copper that was already there);
* the **pad entry**: the first or last segment of the bump, when it lies on
  the host's line and its outer end (the route's connection point) is inside
  that pad or via.  It may touch that pad or via only.

The clearance is the board's minimum copper clearance
(``DesignRules.trace_clearance``).  Derivation: a gap between two copper
features only stays open if the fab can etch it, and the smallest gap the
board's own rules ask the fab to etch is that clearance.  A narrower gap
between a bump and its own pad can bridge in fabrication and short the bump,
and KiCad's DRC will not flag it (same net).  So the bump is held to the same
etchable minimum as any two features on the board.  No clearance value is
changed: this applies the board's existing one to a pair of features that had
no rule.

Pads are measured with the router's single segment-vs-pad primitive
(:func:`~kicad_tools.router.grid.pad_segment_edge_clearance`: the rotated
rectangle, or the disc for circular pads), which is conservative for rounded
pads.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .primitives import Pad, Segment, Via

#: Points closer than this to a line are on it (mm).
_ON_LINE_MM = 1e-6


def _point_line_offset(px: float, py: float, seg: Any) -> float:
    """Signed perpendicular distance of ``(px, py)`` from ``seg``'s infinite line."""
    dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return float(math.hypot(px - seg.x1, py - seg.y1))
    return float((dx * (py - seg.y1) - dy * (px - seg.x1)) / length)


def _on_line(seg: Any, host: Any) -> bool:
    """Whether both ends of ``seg`` lie on ``host``'s infinite line."""
    return (
        abs(_point_line_offset(seg.x1, seg.y1, host)) <= _ON_LINE_MM
        and abs(_point_line_offset(seg.x2, seg.y2, host)) <= _ON_LINE_MM
    )


def _within_extent(seg: Any, host: Any) -> bool:
    """Whether both ends of ``seg`` project inside ``host``'s extent."""
    dx, dy = host.x2 - host.x1, host.y2 - host.y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return False
    for px, py in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
        t = ((px - host.x1) * dx + (py - host.y1) * dy) / length_sq
        if t < -1e-9 or t > 1.0 + 1e-9:
            return False
    return True


def on_host(seg: Any, hosts: Sequence[Any]) -> bool:
    """Whether ``seg`` is a piece of one of ``hosts`` (same line, inside its extent)."""
    return any(seg.layer == h.layer and _on_line(seg, h) and _within_extent(seg, h) for h in hosts)


def pad_half_extent(pad: Any, ux: float, uy: float) -> float:
    """How far ``pad``'s copper reaches from its centre along unit ``(ux, uy)`` (mm).

    The rotated rectangle (the disc for circular pads), the same model as
    :func:`~kicad_tools.router.grid.pad_segment_edge_clearance`.
    """
    if getattr(pad, "shape", None) == "circle":
        return float(max(pad.width, pad.height)) / 2.0
    from kicad_tools.core.geometry import rotate_pad_offset

    reach = 0.0
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            cx, cy = rotate_pad_offset(sx * pad.width / 2.0, sy * pad.height / 2.0, pad.rotation)
            reach = max(reach, cx * ux + cy * uy)
    return reach


def _pad_on_layer(pad: Any, layer: Any) -> bool:
    return bool(getattr(pad, "through_hole", False)) or pad.layer == layer


def _point_in_pad(pad: Any, x: float, y: float) -> bool:
    from .grid import pad_segment_edge_clearance

    return bool(pad_segment_edge_clearance(pad, x, y, x, y, 0.0) < 0.0)


def _point_in_via(via: Any, x: float, y: float) -> bool:
    return bool(math.hypot(x - via.x, y - via.y) < via.diameter / 2.0)


def _pad_label(pad: Any) -> str:
    ref, pin = getattr(pad, "ref", "") or "", getattr(pad, "pin", "") or ""
    return f"{ref}.{pin}" if ref or pin else f"({pad.x:.3f}, {pad.y:.3f})"


@dataclass
class OwnCopperRule:
    """The net's own pads plus the clearance a bump keeps to them (Issue #7891).

    ``pads_by_net`` maps a net id to the router pads on it (shape, rotation,
    layer and through-hole flag are what the rule reads).  ``clearance_mm``
    is the board's minimum copper clearance (see the module docs).
    """

    pads_by_net: Mapping[int, Sequence[Pad]] = field(default_factory=dict)
    clearance_mm: float = 0.0

    @classmethod
    def from_pads(cls, pads: Sequence[Any], clearance_mm: float) -> OwnCopperRule:
        """Group ``pads`` by net (net 0 and Steiner points are not pads of a net)."""
        by_net: dict[int, list[Any]] = {}
        for pad in pads:
            if getattr(pad, "net", 0) and not getattr(pad, "steiner_point", False):
                by_net.setdefault(pad.net, []).append(pad)
        return cls(by_net, float(clearance_mm))

    def pads(self, net: int) -> Sequence[Any]:
        return self.pads_by_net.get(net, ())

    def pad_at(self, net: int, x: float, y: float, layer: Any) -> Any | None:
        """The pad of ``net`` on ``layer`` whose copper contains ``(x, y)``, if any."""
        for pad in self.pads(net):
            if _pad_on_layer(pad, layer) and _point_in_pad(pad, x, y):
                return pad
        return None

    def violation(
        self,
        new_segments: Sequence[Segment],
        host_segments: Sequence[Segment],
        vias: Sequence[Via],
        net: int,
    ) -> str | None:
        """Why a bump breaks the own-copper rule, or ``None`` when it keeps it.

        Args:
            new_segments: The bump (lead-in and lead-out included), in route
                order, replacing ``host_segments``.
            host_segments: The segment(s) of the route the bump replaces.
            vias: The vias of the bump's own route.
            net: The bump's net.
        """
        from kicad_tools.core.geometry import point_to_segment_distance

        from .grid import pad_segment_edge_clearance

        if not new_segments:
            return None
        pads = self.pads(net)
        if not pads and not vias:
            return None
        need = self.clearance_mm
        first, last = new_segments[0], new_segments[-1]
        # The pad entry: an end segment on the host line whose outer end
        # (the route's connection point) is inside a pad or via.
        entries: list[tuple[Any, tuple[float, float]]] = []
        if any(first.layer == h.layer and _on_line(first, h) for h in host_segments):
            entries.append((first, (first.x1, first.y1)))
        if any(last.layer == h.layer and _on_line(last, h) for h in host_segments):
            entries.append((last, (last.x2, last.y2)))

        def entry_of(seg: Any, inside: Any) -> bool:
            return any(s is seg and inside(*end) for s, end in entries)

        for seg in new_segments:
            if on_host(seg, host_segments):
                continue
            half = seg.width / 2.0
            for pad in pads:
                if not _pad_on_layer(pad, seg.layer):
                    continue
                if entry_of(seg, lambda x, y, p=pad: _point_in_pad(p, x, y)):
                    continue
                gap = pad_segment_edge_clearance(pad, seg.x1, seg.y1, seg.x2, seg.y2, half)
                if gap + 1e-9 < need:
                    return (
                        f"bump copper {max(gap, 0.0):.3f} mm from its own pad "
                        f"{_pad_label(pad)} (own-copper clearance {need:.3f} mm)"
                    )
            for via in vias:
                if entry_of(seg, lambda x, y, v=via: _point_in_via(v, x, y)):
                    continue
                dist = point_to_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)
                gap = dist - half - via.diameter / 2.0
                if gap + 1e-9 < need:
                    return (
                        f"bump copper {max(gap, 0.0):.3f} mm from its own via at "
                        f"({via.x:.3f}, {via.y:.3f}) (own-copper clearance {need:.3f} mm)"
                    )
        return None


__all__ = ["OwnCopperRule", "on_host", "pad_half_extent"]

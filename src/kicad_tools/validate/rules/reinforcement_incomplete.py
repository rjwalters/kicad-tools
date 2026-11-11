"""Buttress-wire reinforcement completeness DRC rule (Issue #8746).

Slice 4b of #8741. ``kct pcb reinforce`` (slices 1-3) places two anchor holes
per single-layer wire leg, opens the solder mask along the leg and records the
wire in ``<board>.reinforcement.json``. Nothing verified that the result is
buildable, and the manufacturing gate was passed by waiving ``via_dangling`` /
``path_ampacity`` findings "because the wire carries the current". This rule
checks the claim against the board geometry itself.

The rule is **active only when a current-paths file declared at least one
``reinforcement_eligible`` path**. For every leg of every such path (the copper
is re-derived from the board, never read back from the sidecar's own anchor
list, which would be circular) it verifies, per leg:

* **(a) anchors** -- exactly two anchors, one within the end window of each leg
  end. An anchor is a same-net via (or a through-hole pad that is not the
  copper the leg ends on) whose drill matches
  :func:`~kicad_tools.physics.wire_gauge.anchor_drill_for_awg` for the wire's
  gauge (from the sidecar, else 16 AWG). The end window is the distance from
  the leg end to the edge of the same-net copper it lands on, plus the end
  offset (:data:`~kicad_tools.pcb.reinforce.DEFAULT_END_OFFSET_MM`), plus the
  inward nudge allowance and the anchor's own radius.
* **(b) mask run** -- a continuous solder-mask opening (board-level ``gr_line``
  pieces on the leg's ``F.Mask``/``B.Mask``) covering the leg between the
  anchors, to :data:`MASK_TOLERANCE_MM`. A bent leg is the union of its pieces.
* **(c) anchor rings** -- every anchor via is mask-open on both sides (not
  tented).
* **(d) layer change** -- no layer change on the path through a via whose drill
  is smaller than the anchor drill (a wire stays on one layer; the current would
  leave it through the small via).

Plus **stray anchors**: anchor-sized vias on a reinforced net that are at no
leg end.

Severity: a finding is an ``error`` when its net carries an ``ampacity`` /
``path_ampacity`` waiver whose reason matches :data:`REINFORCEMENT_REASON_RE`
(the waiver *claims* a wire carries the current, so the wire must be buildable),
otherwise a ``warning``. The rule's own findings are waivable through
``.kct_waivers.json`` like any other (a waived finding keeps its severity and
does not clear the manufacturing gate by default, ``waivers.py``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.router.current_paths import CurrentPathSpec, PathResolution
    from kicad_tools.schema.pcb import PCB, Segment, Via

#: Waiver ``rule`` ids whose waiver can claim "the wire carries the current".
AMPACITY_WAIVER_RULES = ("ampacity", "path_ampacity")

#: A waiver ``reason`` that cites reinforcement (case-insensitive ``search``).
REINFORCEMENT_REASON_RE = re.compile(r"reinforc|buttress|wire", re.IGNORECASE)

#: Mask-opening coverage tolerance (mm): both the sideways offset of a mask
#: ``gr_line`` from the leg centreline and the largest gap bridged between two
#: collinear pieces.
MASK_TOLERANCE_MM = 0.05

#: Drill match tolerance (mm) between a hole and the gauge's anchor drill.
DRILL_TOLERANCE_MM = 0.02

#: Sideways slack (mm), beyond the track half-width, for "this anchor is on the
#: leg" (anchors sit on the centreline; this absorbs file rounding).
ANCHOR_ON_LEG_SLACK_MM = 0.1

#: Extra slack (mm) on the end window for bends and file rounding.
END_WINDOW_SLACK_MM = 0.5

#: Fallback wire gauge when no sidecar record exists.
DEFAULT_AWG = 16

# Sub-check ids, as named in each finding.
CHECK_ANCHORS = "a"
CHECK_MASK_RUN = "b"
CHECK_ANCHOR_RINGS = "c"
CHECK_LAYER_CHANGE = "d"
CHECK_STRAY = "stray"

_Point = tuple[float, float]


def reinforcement_waived_nets(entries: Iterable[Any]) -> frozenset[str]:
    """Nets whose ampacity waiver cites reinforcement.

    ``entries`` are :class:`~kicad_tools.validate.rules.waivers.Waiver` objects
    (``Waivers.entries``). An entry counts when its ``rule`` is ``ampacity`` or
    ``path_ampacity`` **and** its ``reason`` matches
    :data:`REINFORCEMENT_REASON_RE`. Its nets are the entry's ``nets`` plus, for
    a keyed entry, the net field of the key (the same extraction
    ``export/manufacturing.py`` uses to drop waived ampacity floors).
    """
    nets: set[str] = set()
    for entry in entries:
        if entry.rule not in AMPACITY_WAIVER_RULES:
            continue
        if not REINFORCEMENT_REASON_RE.search(entry.reason or ""):
            continue
        nets.update(entry.nets)
        if entry.key:
            parts = entry.key.split("|")
            if len(parts) > 2:
                nets.update(n for n in parts[2].split(",") if n)
    return frozenset(nets)


@dataclass
class _Anchor:
    """A hole of the anchor drill lying on a leg."""

    pos: _Point
    arc: float
    via: Via | None  # None for a through-hole pad
    radius: float


@dataclass
class _Leg:
    net: str
    layer: str
    points: list[_Point]
    width: float
    gauge: int
    cum: list[float] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.cum[-1]


class ReinforcementIncompleteRule(DRCRule):
    """DRC rule verifying that reinforced runs are buildable (Issue #8746).

    Attributes:
        rule_id: ``"reinforcement_incomplete"``
    """

    rule_id = "reinforcement_incomplete"
    name = "Reinforcement Completeness"
    description = (
        "Verify each wire leg of a reinforcement-eligible current path has two end "
        "anchors, a continuous solder-mask opening, open anchor rings and no layer "
        "change through a via smaller than the anchor drill"
    )

    def __init__(
        self,
        specs: Sequence[CurrentPathSpec] | None = None,
        wires: Sequence[dict[str, Any]] | None = None,
        waived_nets: Iterable[str] | None = None,
        end_offset_mm: float | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            specs: Declared current paths. ``None``/empty makes the rule a
                clean no-op (it is active only with a current-paths file).
            wires: Records from ``<board>.reinforcement.json`` (sheet-absolute
                coordinates). Used only as the wire-gauge source.
            waived_nets: Nets carrying a reinforcement-citing ampacity waiver
                (see :func:`reinforcement_waived_nets`); their findings are
                errors, all others warnings.
            end_offset_mm: End offset; defaults to
                :data:`kicad_tools.pcb.reinforce.DEFAULT_END_OFFSET_MM`.
        """
        self.specs = list(specs) if specs else []
        self.wires = list(wires) if wires else []
        self.waived_nets = frozenset(waived_nets or ())
        self.end_offset_mm = end_offset_mm

    # ------------------------------------------------------------------ API

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        if not self.specs:
            return results

        from kicad_tools.pcb import reinforce as rf
        from kicad_tools.router.current_paths import reinforcement_path_resolutions

        eligible = reinforcement_path_resolutions(pcb, self.specs).eligible
        if not eligible:
            return results

        origin: tuple[float, float] = getattr(pcb, "board_origin", None) or (0.0, 0.0)
        self._ox: float = origin[0]
        self._oy: float = origin[1]
        end_offset = (
            self.end_offset_mm if self.end_offset_mm is not None else rf.DEFAULT_END_OFFSET_MM
        )

        by_net: dict[str, list[PathResolution]] = {}
        for res in eligible:
            by_net.setdefault(res.spec.net_name, []).append(res)

        for net in sorted(by_net):
            self._check_net(pcb, rf, net, by_net[net], end_offset, results)
        return results

    # ------------------------------------------------------------- per net

    def _severity(self, net: str) -> str:
        return "error" if net in self.waived_nets else "warning"

    def _abs(self, p: _Point) -> _Point:
        return (p[0] + self._ox, p[1] + self._oy)

    def _fmt(self, p: _Point) -> str:
        x, y = self._abs(p)
        return f"({x:.3f}, {y:.3f})"

    def _emit(
        self,
        results: DRCResults,
        net: str,
        layer: str,
        check: str,
        where: _Point,
        leg_desc: str,
        what: str,
        actual: float | None = None,
        required: float | None = None,
    ) -> None:
        results.add(
            DRCViolation(
                rule_id=self.rule_id,
                severity=self._severity(net),
                message=(
                    f"Reinforcement incomplete [{check}] net {net!r} on {layer}, "
                    f"{leg_desc} at {self._fmt(where)}: {what}"
                ),
                location=where,
                layer=layer,
                actual_value=actual,
                required_value=required,
                items=(net, f"{check}@{self._fmt(where)}"),
                nets=(net,),
            )
        )

    def _gauge(self, net: str, layer: str, points: list[_Point]) -> int:
        """Wire gauge from the sidecar: end-point match, else net+layer, else 16."""
        ends = [self._abs(points[0]), self._abs(points[-1])]
        fallback: int | None = None
        for w in self.wires:
            try:
                if w["net"] != net or w["layer"] != layer:
                    continue
                awg = int(w["wire_gauge_awg"])
                ws = (w["leg_start"]["x"], w["leg_start"]["y"])
                we = (w["leg_end"]["x"], w["leg_end"]["y"])
            except (KeyError, TypeError, ValueError):
                continue
            fallback = awg if fallback is None else fallback
            for a, b in ((ws, we), (we, ws)):
                if math.dist(a, ends[0]) < 0.05 and math.dist(b, ends[1]) < 0.05:
                    return awg
        if fallback is not None:
            return fallback
        for w in self.wires:
            if isinstance(w, dict) and w.get("net") == net and "wire_gauge_awg" in w:
                try:
                    return int(w["wire_gauge_awg"])
                except (TypeError, ValueError):
                    break
        return DEFAULT_AWG

    def _check_net(
        self,
        pcb: PCB,
        rf: Any,
        net: str,
        resolutions: list[PathResolution],
        end_offset: float,
        results: DRCResults,
    ) -> None:
        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.physics.wire_gauge import anchor_drill_for_awg

        from .silkscreen import _via_is_tented

        # --- eligible copper, by layer (deduplicated across specs) ---------
        seen: set[int] = set()
        by_layer: dict[str, list[Segment]] = {}
        for res in resolutions:
            for seg in res.segments:
                if id(seg) in seen or rf._seg_length(seg) <= 1e-9:
                    continue
                seen.add(id(seg))
                by_layer.setdefault(seg.layer, []).append(seg)
        net_number = next((s.net_number for segs in by_layer.values() for s in segs), None)
        if net_number is None:
            return

        # --- layer-change vias belong to the path, not to the anchors ------
        change_vias: list[_Point] = []
        changes: dict[tuple[float, float], Any] = {}
        for res in resolutions:
            for change in res.layer_changes:
                changes.setdefault(
                    (round(change.position[0], 3), round(change.position[1], 3)), change
                )
                change_vias.extend(v[0] for v in change.vias)

        def is_change_via(p: _Point) -> bool:
            return any(math.dist(p, q) < 0.05 for q in change_vias)

        # --- legs -----------------------------------------------------------
        original_ids: dict[int, int] = {}
        legs: list[_Leg] = []
        for layer in sorted(by_layer):
            for cand in rf._derive_legs(by_layer[layer], layer, original_ids):
                if cand.status is not None:  # unhandled topology: no wire layout implied
                    continue
                pts = rf._run_points(cand.run)
                width = max((s.width for s in cand.run), default=0.0)
                leg = _Leg(net, layer, pts, width, self._gauge(net, layer, pts))
                leg.cum = rf._cumulative_arc(pts)
                legs.append(leg)

        net_gauge = legs[0].gauge if legs else self._gauge(net, "", [])
        # --- candidate anchor holes on this net -----------------------------
        holes_for = self._anchor_holes(pcb, net_number, rotate_pad_offset)

        claimed: set[int] = set()
        drills_seen: set[float] = set()
        for leg in legs:
            drill = anchor_drill_for_awg(leg.gauge)
            drills_seen.add(round(drill, 4))
            self._check_leg(
                pcb,
                rf,
                leg,
                drill,
                holes_for,
                is_change_via,
                end_offset,
                claimed,
                results,
                _via_is_tented,
            )

        # --- (d) layer change through a via smaller than the anchor drill ---
        for change in sorted(changes.values(), key=lambda c: c.position):
            drill = anchor_drill_for_awg(net_gauge)
            if change.drill < drill - DRILL_TOLERANCE_MM:
                self._emit(
                    results,
                    net,
                    change.from_layer,
                    CHECK_LAYER_CHANGE,
                    change.position,
                    f"layer change {change.from_layer} -> {change.to_layer}",
                    f"the path changes layer through a {change.drill:.3f} mm drill via, "
                    f"smaller than the {drill:.3f} mm anchor drill of a {net_gauge} AWG wire "
                    "(a wire stays on one layer: split it into two wires or re-route)",
                    actual=change.drill,
                    required=drill,
                )

        # --- stray anchors ---------------------------------------------------
        for drill in sorted(drills_seen):
            for idx, (via, pos) in enumerate(holes_for["vias"]):
                if abs(via.drill - drill) > DRILL_TOLERANCE_MM or id(via) in claimed:
                    continue
                if is_change_via(pos):
                    continue
                claimed.add(id(via))
                self._emit(
                    results,
                    net,
                    "F.Cu",
                    CHECK_STRAY,
                    pos,
                    "no leg end",
                    f"anchor-sized via (drill {via.drill:.3f} mm) is not at any wire leg end",
                    actual=via.drill,
                    required=drill,
                )

    # ------------------------------------------------------- anchor holes

    @staticmethod
    def _anchor_holes(pcb: PCB, net_number: int, rotate_pad_offset: Any) -> dict[str, list]:
        vias = [(v, v.position) for v in pcb.vias if v.net_number == net_number]
        pads = []
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_number != net_number or pad.type != "thru_hole" or pad.drill <= 0:
                    continue
                off = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
                try:
                    radius = max(float(pad.size[0]), float(pad.size[1])) / 2.0
                except (TypeError, ValueError, IndexError):
                    radius = 0.0
                pads.append((pad, (fp.position[0] + off[0], fp.position[1] + off[1]), radius))
        return {"vias": vias, "pads": pads}

    # ----------------------------------------------------------- per leg

    def _check_leg(
        self,
        pcb: PCB,
        rf: Any,
        leg: _Leg,
        drill: float,
        holes: dict[str, list],
        is_change_via: Any,
        end_offset: float,
        claimed: set[int],
        results: DRCResults,
        via_is_tented: Any,
    ) -> None:
        pts, cum, total = leg.points, leg.cum, leg.total
        if len(pts) < 2 or total <= 1e-9:
            return
        desc = f"leg {self._fmt(pts[0])} -> {self._fmt(pts[-1])}"
        tol = leg.width / 2.0 + ANCHOR_ON_LEG_SLACK_MM

        # --- end windows from the same-net copper each end lands on --------
        pad_guess = next(
            (v.size for v, _ in holes["vias"] if abs(v.drill - drill) <= DRILL_TOLERANCE_MM), 0.0
        )
        features = rf._same_net_end_features(pcb, self._net_number_of(holes), drill, pad_guess)
        extents: list[float] = []
        for ordered in (pts, list(reversed(pts))):
            copper = rf._end_copper(ordered, leg.layer, features, [], leg.width / 2.0)
            if copper is None:  # a pad/via covers the whole leg: nothing to wire
                return
            extents.append(rf._end_extent(ordered, copper))

        # --- anchors on this leg ---------------------------------------------
        anchors: list[_Anchor] = []
        for via, pos in holes["vias"]:
            if abs(via.drill - drill) > DRILL_TOLERANCE_MM or is_change_via(pos):
                continue
            if via.layers and leg.layer not in via.layers and "*.Cu" not in via.layers:
                continue
            if self._dist_to_polyline(pts, pos) <= tol:
                anchors.append(_Anchor(pos, rf._arc_of_point(pts, cum, pos), via, via.size / 2.0))
        for pad, pos, radius in holes["pads"]:
            if abs(pad.drill - drill) > DRILL_TOLERANCE_MM:
                continue
            # A pad the leg ends on is end copper, not a wire hole.
            if min(math.dist(pos, pts[0]), math.dist(pos, pts[-1])) <= radius + 0.05:
                continue
            if self._dist_to_polyline(pts, pos) <= tol:
                anchors.append(_Anchor(pos, rf._arc_of_point(pts, cum, pos), None, radius))
        anchors.sort(key=lambda a: a.arc)

        def window(extent: float, anchor: _Anchor) -> float:
            return float(
                extent + end_offset + rf.END_NUDGE_MAX_MM + anchor.radius + END_WINDOW_SLACK_MM
            )

        near_start = [a for a in anchors if a.arc <= window(extents[0], a)]
        near_end = [a for a in anchors if total - a.arc <= window(extents[1], a)]
        for a in near_start + near_end:
            if a.via is not None:
                claimed.add(id(a.via))

        # (a) exactly two anchors, one at each end
        if len(anchors) != 2:
            self._emit(
                results,
                leg.net,
                leg.layer,
                CHECK_ANCHORS,
                pts[0],
                desc,
                f"found {len(anchors)} anchor hole(s) of {drill:.3f} mm drill "
                f"({leg.gauge} AWG), expected exactly 2 (one at each end)"
                + (f" at {', '.join(self._fmt(a.pos) for a in anchors)}" if anchors else ""),
                actual=float(len(anchors)),
                required=2.0,
            )
        else:
            for label, anchor, ext, dist in (
                ("start", anchors[0], extents[0], anchors[0].arc),
                ("end", anchors[1], extents[1], total - anchors[1].arc),
            ):
                limit = window(ext, anchor)
                if dist > limit:
                    self._emit(
                        results,
                        leg.net,
                        leg.layer,
                        CHECK_ANCHORS,
                        anchor.pos,
                        desc,
                        f"the {label} anchor is {dist:.3f} mm from the leg end, beyond the "
                        f"end window of {limit:.3f} mm (end offset {end_offset:g} mm)",
                        actual=dist,
                        required=limit,
                    )

        mask_layer = rf.MASK_LAYER_FOR_COPPER.get(leg.layer)
        if mask_layer is None:  # inner copper has no solder mask: (b)/(c) do not apply
            return

        # (b) continuous mask opening between the anchors. "Between the anchors"
        # is undefined with exactly one (already an (a) finding); with none the
        # whole leg is expected open.
        if len(anchors) >= 2:
            t0, t1 = anchors[0].arc, anchors[-1].arc
        else:
            t0, t1 = (0.0, total) if not anchors else (0.0, 0.0)
        if t1 - t0 > 1e-6:
            span = rf._sub_polyline(pts, cum, t0, t1)
            lines = [
                (
                    (g.start[0] - self._ox, g.start[1] - self._oy),
                    (g.end[0] - self._ox, g.end[1] - self._oy),
                )
                for g in pcb.graphic_lines
                if g.layer == mask_layer
            ]
            gap = self._first_uncovered(span, lines)
            if gap is not None:
                self._emit(
                    results,
                    leg.net,
                    leg.layer,
                    CHECK_MASK_RUN,
                    gap,
                    desc,
                    f"no continuous {mask_layer} opening along the leg between the anchors "
                    f"(first uncovered point; tolerance {MASK_TOLERANCE_MM:g} mm)",
                )

        # (c) anchor rings open on both sides
        for a in anchors:
            if a.via is None:
                continue
            tented = [
                s
                for s, name in (("F", "front"), ("B", "back"))
                if via_is_tented(a.via, pcb.setup, s)
            ]
            if tented:
                self._emit(
                    results,
                    leg.net,
                    leg.layer,
                    CHECK_ANCHOR_RINGS,
                    a.pos,
                    desc,
                    "anchor via is tented (mask-covered) on the "
                    + " and ".join("front" if s == "F" else "back" for s in tented)
                    + " side; both sides must be mask-open",
                )

    # ------------------------------------------------------------ geometry

    @staticmethod
    def _net_number_of(holes: dict[str, list]) -> int:
        for v, _ in holes["vias"]:
            return int(v.net_number)
        for pad, _, _ in holes["pads"]:
            return int(pad.net_number)
        return -1

    @staticmethod
    def _dist_to_polyline(pts: list[_Point], p: _Point) -> float:
        best = math.inf
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            u = (
                0.0
                if seg2 < 1e-18
                else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / seg2))
            )
            best = min(best, math.hypot(p[0] - (x0 + u * dx), p[1] - (y0 + u * dy)))
        return best

    @staticmethod
    def _first_uncovered(span: list[_Point], lines: list[tuple[_Point, _Point]]) -> _Point | None:
        """First point of ``span`` not covered by the union of collinear ``lines``."""
        tol = MASK_TOLERANCE_MM
        for a, b in zip(span, span[1:], strict=False):
            length = math.dist(a, b)
            if length <= 1e-9:
                continue
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length

            def along_perp(p: _Point) -> tuple[float, float]:
                rx, ry = p[0] - a[0], p[1] - a[1]
                return rx * ux + ry * uy, abs(rx * uy - ry * ux)

            intervals: list[tuple[float, float]] = []
            for p, q in lines:
                tp, dp = along_perp(p)
                tq, dq = along_perp(q)
                if dp <= tol and dq <= tol:
                    intervals.append((min(tp, tq), max(tp, tq)))
            reach = 0.0
            for lo, hi in sorted(intervals):
                if lo <= reach + tol:
                    reach = max(reach, hi)
            if reach < length - tol:
                return (a[0] + ux * reach, a[1] + uy * reach)
        return None

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
    # ``--max-end-shift`` recorded in the sidecar for this leg (None = not recorded).
    max_end_shift_mm: float | None = None

    @property
    def total(self) -> float:
        return self.cum[-1]


@dataclass
class _WirePlan:
    """Declared-eligible paths split by how their wire is found (Issue #8886)."""

    resolved: dict[str, list[PathResolution]] = field(default_factory=dict)
    located: dict[str, dict[str, list[Segment]]] = field(default_factory=dict)
    no_wire: list[PathResolution] = field(default_factory=list)
    skipped: list[PathResolution] = field(default_factory=list)


_FLOAT_RE = re.compile(r"-?\d+\.\d{4,}")


def _round_floats(text: str) -> str:
    """Round float literals with more than 3 decimals to 3 dp (witness coordinates)."""
    return _FLOAT_RE.sub(lambda m: f"{float(m.group()):.3f}", text)


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
        max_end_shift_mm: float | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            specs: Declared current paths. ``None``/empty makes the rule a
                clean no-op (it is active only with a current-paths file).
            wires: Records from ``<board>.reinforcement.json`` (sheet-absolute
                coordinates). Source of each leg's wire gauge and recorded
                ``max_end_shift_mm`` (which overrides ``max_end_shift_mm``).
            waived_nets: Nets carrying a reinforcement-citing ampacity waiver
                (see :func:`reinforcement_waived_nets`); their findings are
                errors, all others warnings.
            end_offset_mm: End offset; defaults to
                :data:`kicad_tools.pcb.reinforce.DEFAULT_END_OFFSET_MM`.
            max_end_shift_mm: The ``kct pcb reinforce --max-end-shift`` the
                board was reinforced with; ``None`` = the placer's default
                bound (:func:`kicad_tools.pcb.reinforce.end_shift_bound`).
        """
        self.specs = list(specs) if specs else []
        self.wires = list(wires) if wires else []
        self.waived_nets = frozenset(waived_nets or ())
        self.end_offset_mm = end_offset_mm
        self.max_end_shift_mm = max_end_shift_mm

    # ------------------------------------------------------------------ API

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        if not self.specs:
            return results

        from kicad_tools.pcb import reinforce as rf
        from kicad_tools.router.current_paths import reinforcement_path_resolutions

        origin: tuple[float, float] = getattr(pcb, "board_origin", None) or (0.0, 0.0)
        self._ox: float = origin[0]
        self._oy: float = origin[1]
        end_offset = (
            self.end_offset_mm if self.end_offset_mm is not None else rf.DEFAULT_END_OFFSET_MM
        )

        plan = self._plan(pcb, rf, reinforcement_path_resolutions(pcb, self.specs))
        for res in plan.no_wire:
            self._emit_no_wire(results, pcb, res)
        for res in plan.skipped:
            self._emit_skipped(results, pcb, res)
        for net in sorted(set(plan.resolved) | set(plan.located)):
            self._check_net(
                pcb,
                rf,
                net,
                plan.resolved.get(net, []),
                end_offset,
                results,
                located=plan.located.get(net),
            )
        return results

    # ------------------------------------------- wire location (Issue #8886)

    def _plan(self, pcb: PCB, rf: Any, partition: Any) -> _WirePlan:
        """Split declared-eligible paths into checkable and non-checkable.

        A path that resolved ``ok`` is checked from its resolved segments, as
        before. A declared-eligible path that did not resolve (ambiguous
        parallel/loop copper, or no continuous path) cannot be located from
        the current split, but the *wire* does not need one: it is where the
        solder-mask strip and the anchor holes are. Such a path is located
        from the board geometry (:meth:`_locate_wire`).
        """
        plan = _WirePlan()
        for res in partition.eligible:
            plan.resolved.setdefault(res.spec.net_name, []).append(res)
        for res in partition.unresolved:
            if not res.spec.reinforcement_eligible:
                continue
            net = res.spec.net_name
            if net in plan.located:
                continue
            if res.source is None or res.sink is None:
                # An endpoint pad is missing or moved off the net: the
                # declaration itself is stale, so say so rather than check.
                plan.skipped.append(res)
                continue
            taken = {id(s) for r in plan.resolved.get(net, []) for s in r.segments}
            located, any_strip, any_anchor = self._locate_wire(pcb, rf, net, taken)
            if located:
                plan.located[net] = located
            elif not any_strip and not any_anchor:
                plan.no_wire.append(res)
            else:
                plan.skipped.append(res)
        # A net located once covers every unresolved spec on it.
        plan.skipped = [
            r
            for r in plan.skipped
            if r.source is None or r.sink is None or r.spec.net_name not in plan.located
        ]
        plan.no_wire = [r for r in plan.no_wire if r.spec.net_name not in plan.located]
        return plan

    def _locate_wire(
        self, pcb: PCB, rf: Any, net: str, taken: set[int]
    ) -> tuple[dict[str, list[Segment]], bool, bool]:
        """Locate the wire copper of ``net`` from its mask strips and anchors.

        Returns ``(segments by layer, any mask strip on the net, any anchor
        hole on the net)``. A same-net segment is wire copper when a mask-layer
        ``gr_line`` of its side lies along it (within :data:`MASK_TOLERANCE_MM`,
        overlapping it). Copper a resolved path already owns is excluded.
        """
        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.physics.wire_gauge import anchor_drill_for_awg

        netobj = pcb.get_net_by_name(net)
        if netobj is None:
            return {}, False, False
        net_number = netobj.number
        mask_lines: dict[str, list[tuple[_Point, _Point]]] = {}
        for g in pcb.graphic_lines:
            if g.layer in ("F.Mask", "B.Mask"):
                mask_lines.setdefault(g.layer, []).append(
                    (
                        (g.start[0] - self._ox, g.start[1] - self._oy),
                        (g.end[0] - self._ox, g.end[1] - self._oy),
                    )
                )
        located: dict[str, list[Segment]] = {}
        any_strip = False
        for seg in pcb.segments:
            if seg.net_number != net_number or id(seg) in taken or rf._seg_length(seg) <= 1e-9:
                continue
            mask_layer = rf.MASK_LAYER_FOR_COPPER.get(seg.layer)
            if mask_layer is None:
                continue
            if any(
                self._strip_along(seg.start, seg.end, a, b)
                for a, b in mask_lines.get(mask_layer, [])
            ):
                any_strip = True
                located.setdefault(seg.layer, []).append(seg)
        drill = anchor_drill_for_awg(self._gauge(net, "", [(0.0, 0.0), (0.0, 0.0)]))
        holes = self._anchor_holes(pcb, net_number, rotate_pad_offset)
        any_anchor = any(
            abs(v.drill - drill) <= DRILL_TOLERANCE_MM for v, _ in holes["vias"]
        ) or any(abs(p.drill - drill) <= DRILL_TOLERANCE_MM for p, _, _ in holes["pads"])
        return located, any_strip, any_anchor

    @staticmethod
    def _strip_along(s0: _Point, s1: _Point, a: _Point, b: _Point) -> bool:
        """True when strip ``a``-``b`` lies along segment ``s0``-``s1`` and overlaps it."""
        length = math.dist(s0, s1)
        if length <= 1e-9:
            return False
        ux, uy = (s1[0] - s0[0]) / length, (s1[1] - s0[1]) / length
        lo: list[float] = []
        for p in (a, b):
            rx, ry = p[0] - s0[0], p[1] - s0[1]
            if abs(rx * uy - ry * ux) > MASK_TOLERANCE_MM:
                return False
            lo.append(rx * ux + ry * uy)
        t0, t1 = min(lo), max(lo)
        return min(t1, length) - max(t0, 0.0) > MASK_TOLERANCE_MM

    def _emit_no_wire(self, results: DRCResults, pcb: PCB, res: PathResolution) -> None:
        net = res.spec.net_name
        where = self._on_path(pcb, res)
        results.add(
            DRCViolation(
                rule_id=self.rule_id,
                severity=self._severity(net),
                message=(
                    f"Reinforcement incomplete [no_wire] net {net!r}: current path "
                    f"{res.spec.name!r} is declared reinforcement_eligible but no wire is "
                    "present (no anchor holes and no solder-mask opening on the net); "
                    "remove the declaration if this is intentionally doubled copper"
                ),
                location=where,
                layer="",
                items=(net, f"no_wire@{res.spec.name}"),
                nets=(net,),
            )
        )

    def _emit_skipped(self, results: DRCResults, pcb: PCB, res: PathResolution) -> None:
        net = res.spec.net_name
        reason = _round_floats(res.reason)
        results.add(
            DRCViolation(
                rule_id=self.rule_id,
                severity="warning",
                message=(
                    f"Reinforcement not checked [skipped] net {net!r}: current path "
                    f"{res.spec.name!r} (declared reinforcement_eligible) is {res.status}"
                    + (f" ({reason})" if reason else "")
                    + "; its anchors and mask openings were not verified"
                ),
                location=self._on_path(pcb, res),
                layer="",
                items=(net, f"skipped@{res.spec.name}"),
                nets=(net,),
            )
        )

    def _on_path(self, pcb: PCB, res: PathResolution) -> _Point:
        """A point on the path in board-relative coordinates, rounded to 3 dp.

        The resolved source/sink pad when there is one, else the start of the
        net's first routed segment; board-relative ``(0, 0)`` -- which
        absolutizes to the board origin -- only for a net with no copper.

        This is a ``DRCViolation.location`` value, so it must NOT absolutize:
        :meth:`~kicad_tools.validate.checker.DRCChecker._absolutize` adds
        ``board_origin`` back exactly once for every rule-backed ``check_*``.
        Calling :meth:`_abs` here shifted ``[no_wire]`` / ``[skipped]``
        locations by one origin too many (Issue #8912). Use :meth:`_fmt` --
        which does absolutize -- for message text instead.
        """
        end = res.source or res.sink
        if end is not None:
            pt: _Point = end.position
        else:
            net = pcb.get_net_by_name(res.spec.net_name)
            seg = next((sg for sg in pcb.segments if net and sg.net_number == net.number), None)
            if seg is None:
                return (0.0, 0.0)
            pt = seg.start
        return (round(pt[0], 3), round(pt[1], 3))

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

    def _recorded_end_shift(self, net: str, layer: str, points: list[_Point]) -> float | None:
        """``max_end_shift_mm`` the sidecar recorded for this exact leg, else None."""
        ends = [self._abs(points[0]), self._abs(points[-1])]
        for w in self.wires:
            try:
                if w["net"] != net or w["layer"] != layer:
                    continue
                raw = w.get("max_end_shift_mm")
                if raw is None:
                    continue
                bound = float(raw)
                ws = (w["leg_start"]["x"], w["leg_start"]["y"])
                we = (w["leg_end"]["x"], w["leg_end"]["y"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if not math.isfinite(bound) or bound < 0:
                continue
            for a, b in ((ws, we), (we, ws)):
                if math.dist(a, ends[0]) < 0.05 and math.dist(b, ends[1]) < 0.05:
                    return bound
        return None

    def _parallel_reinforced(self, pcb: PCB, leg: _Leg) -> bool:
        """True when a placed parallel-copy record covers ``leg`` (Issue #8916).

        The record is not taken on trust: the leg must lie on the recorded
        polyline, on the record's leg layer or copy layer, AND every recorded
        copy segment must still be on the board (same net, copy layer, same
        two ends). A wire leg, a refused copy, or a copy someone deleted is
        checked as a wire as before.
        """
        from kicad_tools.pcb.reinforcement_sidecar import is_parallel

        tol = leg.width / 2.0 + ANCHOR_ON_LEG_SLACK_MM
        for w in self.wires:
            try:
                if w.get("net") != leg.net or not is_parallel(w):
                    continue
                if w.get("copy_status") != "placed" or leg.layer not in (
                    w["layer"],
                    w["copy_layer"],
                ):
                    continue
                poly = [(p["x"] - self._ox, p["y"] - self._oy) for p in w["polyline"]]
                recs = [
                    (
                        (r["start"]["x"] - self._ox, r["start"]["y"] - self._oy),
                        (r["end"]["x"] - self._ox, r["end"]["y"] - self._oy),
                    )
                    for r in w["copy_segments"]
                ]
                copy_layer = w["copy_layer"]
            except (KeyError, TypeError, AttributeError):
                continue
            if len(poly) < 2 or not recs:
                continue
            if any(self._dist_to_polyline(poly, p) > tol for p in leg.points):
                continue
            netobj = pcb.get_net_by_name(leg.net)
            board = [
                (s.start, s.end)
                for s in pcb.segments
                if s.layer == copy_layer
                and (
                    s.net_name == leg.net or (netobj is not None and s.net_number == netobj.number)
                )
            ]
            if all(
                any(
                    (math.dist(a, sa) < 0.05 and math.dist(b, sb) < 0.05)
                    or (math.dist(a, sb) < 0.05 and math.dist(b, sa) < 0.05)
                    for sa, sb in board
                )
                for a, b in recs
            ):
                return True
        return False

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
        located: dict[str, list[Segment]] | None = None,
    ) -> None:
        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.physics.wire_gauge import anchor_drill_for_awg

        from .silkscreen import _via_is_tented

        # --- eligible copper, by layer (deduplicated across specs) ---------
        seen: set[int] = set()
        by_layer: dict[str, list[Segment]] = {}
        for layer, segs in (located or {}).items():
            for seg in segs:
                seen.add(id(seg))
                by_layer.setdefault(layer, []).append(seg)
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
                change_vias.extend(v[0] for v in change.vias)
            # (d) judges only series layer changes: a stitching via inside
            # doubled copper is not where the current leaves a layer (#8916).
            for change in res.series_layer_changes:
                changes.setdefault(
                    (round(change.position[0], 3), round(change.position[1], 3)), change
                )

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
                leg.max_end_shift_mm = self._recorded_end_shift(net, layer, pts)
                legs.append(leg)

        net_gauge = legs[0].gauge if legs else self._gauge(net, "", [])
        # --- candidate anchor holes on this net -----------------------------
        holes_for = self._anchor_holes(pcb, net_number, rotate_pad_offset)

        claimed: set[int] = set()
        drills_seen: set[float] = set()
        for leg in legs:
            # Issue #8916: a leg reinforced by a parallel copy -- the original
            # or the copy itself -- is not a wire: (a)-(c) do not apply to it.
            if self._parallel_reinforced(pcb, leg):
                continue
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

        # The bound the leg was built with (sidecar) wins over the constructor
        # fallback, so `kct check` agrees with a custom `--max-end-shift` (#8803).
        recorded = leg.max_end_shift_mm
        shift_bound = rf.end_shift_bound(
            total, recorded if recorded is not None else self.max_end_shift_mm
        )

        def window(extent: float, anchor: _Anchor) -> float:
            return float(extent + end_offset + shift_bound + anchor.radius + END_WINDOW_SLACK_MM)

        # A same-net via that also lands a track on another copper layer, away
        # from both leg ends, is a layer-change via of the net, not an anchor.
        layer_change = [
            a
            for a in anchors
            if a.via is not None
            and self._joins_other_layer(pcb, a.via, leg.layer)
            and a.arc > window(extents[0], a)
            and total - a.arc > window(extents[1], a)
        ]
        for a in layer_change:
            if a.via is not None:
                claimed.add(id(a.via))
        anchors = [a for a in anchors if a not in layer_change]
        near_start = [a for a in anchors if a.arc <= window(extents[0], a)]
        near_end = [a for a in anchors if total - a.arc <= window(extents[1], a)]
        # With exactly two anchors the (a) window check below reports each
        # one, so a slid anchor is never ALSO reported as a stray (Issue
        # #8797). With another count a mid-leg extra stays a stray too.
        claimable = (
            anchors
            if len(anchors) == 2
            else near_start + [a for a in near_end if a not in near_start]
        )
        for a in claimable:
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
                if via_is_tented(a.via, pcb.setup, s) and not self._ring_mask_open(pcb, a, s)
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

    def _ring_mask_open(self, pcb: PCB, anchor: _Anchor, side: str) -> bool:
        """True when a mask-layer circle graphic covers the anchor's ring.

        The gerber mask is open there whatever the via's ``tenting`` attribute
        says (Issue #8797).
        """
        layer = "F.Mask" if side == "F" else "B.Mask"
        for g in pcb.graphics:
            if g.graphic_type != "circle" or g.layer != layer or g.center is None:
                continue
            centre = (g.center[0] - self._ox, g.center[1] - self._oy)
            if math.dist(centre, anchor.pos) > MASK_TOLERANCE_MM:
                continue
            radius = math.dist(g.center, g.end) + g.stroke_width / 2.0
            if radius >= anchor.radius - MASK_TOLERANCE_MM:
                return True
        return False

    @staticmethod
    def _joins_other_layer(pcb: PCB, via: Via, layer: str) -> bool:
        """True when a same-net track on another layer ends at ``via``."""
        r = via.size / 2.0
        for seg in pcb.segments:
            if seg.net_number != via.net_number or seg.layer == layer:
                continue
            if min(math.dist(seg.start, via.position), math.dist(seg.end, via.position)) <= r:
                return True
        return False

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

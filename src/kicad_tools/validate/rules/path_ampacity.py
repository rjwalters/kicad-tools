"""Branch-scoped current-path ampacity DRC rule (Issue #4980).

Companion to :class:`~kicad_tools.validate.rules.ampacity.AmpacityRule`,
which checks a **whole net** against a single ``target_ampacity``. This
rule instead checks each declared
:class:`~kicad_tools.router.current_paths.CurrentPathSpec` -- a physical
copper *branch* identified by stable ``RefDes.pad`` endpoints -- against
its own declared current, independent of what the rest of the net is
carrying. This is what makes a net with a 15 A trunk and a milliamp sense
tap checkable at all: the trunk's endpoints resolve to a wide-copper path
that must clear a 15 A IPC-2221 floor, and the sense tap's endpoints
resolve to a *different* (usually much shorter/narrower) copper path that
must clear only its own tiny declared current -- never the whole net's
uniform floor.

Every declared path produces exactly one of three outcomes, all visible in
the returned :class:`~kicad_tools.validate.violations.DRCResults` (never a
silent pass):

* **resolved + adequate width** -- no finding.
* **resolved + under-width** -- an ``error`` finding, identical in spirit
  to :class:`AmpacityRule`'s per-segment finding but scoped to the
  declared branch's own current.
* **unresolved** (endpoint moved/removed/off-net, or no continuous copper
  between the endpoints) -- an ``error`` finding. This is the "fails
  closed" behavior the issue requires: a broken endpoint mapping is a
  visible DRC error, not a silent skip.
* **ambiguous** (the endpoints connect, but the net's copper contains a
  loop reachable from them) -- an ``error`` finding: a parallel/alternate
  route means a single resolved path cannot be trusted to represent how
  current actually splits.

A branch that declares a **pulsed** current (``pulsed_a``, optionally with
``duty_cycle`` / ``pulse_duration_s``) is checked two further ways, because
a pulse can destroy copper by a mechanism the steady-state width check
cannot see:

* The IPC-2221 width check runs at the branch's *thermal design current*
  (:meth:`~kicad_tools.router.current_paths.CurrentPathSpec
  .thermal_design_current`) -- the waveform's RMS current when a duty cycle
  was declared, the peak treated as continuous when one was not. The
  violation message always names which, so a width requirement can never be
  mistaken for a different waveform assumption than the one applied.
* When ``pulse_duration_s`` is declared, each segment is additionally
  checked against its Onderdonk adiabatic fusing current: a peak at or
  above the copper's fusing limit is an ``error``, however comfortable the
  RMS check was. When ``pulse_duration_s`` is *not* declared, the fusing
  mode simply cannot be evaluated -- so it is reported as an explicit
  ``warning`` per path rather than passing silently.
* A pulsed branch with no declared duty cycle is sized at its peak. That is
  conservative rather than wrong, so it is an ``info`` finding (never
  failing the gate) -- but it is stated, so nobody reads a pessimistic
  width requirement as a duty-cycle-aware one.

In addition, any net that has at least one declared path but also carries
routed copper NOT covered by any resolved path is flagged with a
``warning``-severity "uncovered copper" finding per orphan segment -- the
issue's "unknown current paths must be visible rather than silently
waived" requirement. This is a warning, not an error: uncovered copper on
a net that also has declared paths is usually deliberate (an unmodeled
low-current branch the designer hasn't gotten around to declaring yet),
not necessarily a defect -- but it must never be silent.

Similarly, a routed arc or a non-keepout same-net zone/pour is real
current-carrying copper :func:`~kicad_tools.router.current_paths
.resolve_current_path`'s graph never sees at all (Issue #5273). Either can
form a parallel return path around a declared branch, so any declared path
on such a net already resolves as ``"ambiguous"`` (an ``error``, via the
branch above). This rule additionally emits a ``warning`` per unmodeled
copper object found -- naming the specific arc/pour responsible, rather
than leaving the reader to infer it from the ambiguous-status error alone.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, NamedTuple

from kicad_tools.physics.ampacity import (
    VIA_PLATING_THICKNESS_MM,
    adiabatic_fusing_current,
    width_for_current,
)
from kicad_tools.physics.constants import copper_thickness_from_oz
from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .reinforcement_incomplete import ANCHOR_ON_LEG_SLACK_MM, WiredLeg

# NOTE: ``kicad_tools.router.current_paths`` is intentionally NOT imported at
# module scope here. ``kicad_tools.router`` (via ``router/core.py`` and
# others) imports from ``kicad_tools.validate`` at import time, so a
# module-level ``validate -> router`` import would be a circular import --
# the exact same constraint that already keeps
# ``kicad_tools.router.rules.NetClassRouting`` behind a ``TYPE_CHECKING``
# guard in ``validate/ampacity_specs.py`` and ``validate/checker.py``. The
# runtime symbols are imported lazily inside :meth:`PathAmpacityRule.check`
# instead, mirroring ``DRCChecker.check_ampacity``'s
# ``from .ampacity_specs import derive_ampacity_specs`` lazy import.
if TYPE_CHECKING:
    from collections.abc import Sequence

    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.router.current_paths import (
        CurrentPathSpec,
        LayerChange,
        ParallelGroup,
        PathResolution,
        ThermalDesignCurrent,
        UnmodeledCopper,
    )
    from kicad_tools.schema.pcb import PCB, Segment

# Matches AmpacityRule / manufacturers/dru_generator.py's external/internal
# copper-layer split exactly, so all three producers of an IPC-2221 width
# floor agree for a given net/copper-weight/layer.
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


def _dist_to_polyline(points: Sequence[tuple[float, float]], p: tuple[float, float]) -> float:
    """Distance from ``p`` to the nearest point of a polyline."""
    best = math.inf
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        u = (
            0.0
            if seg2 < 1e-18
            else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / seg2))
        )
        best = min(best, math.hypot(p[0] - (x0 + u * dx), p[1] - (y0 + u * dy)))
    return best


def _cumulative_arc(points: Sequence[tuple[float, float]]) -> list[float]:
    """Cumulative arc length at each vertex of a polyline.

    Same math as :func:`kicad_tools.pcb.reinforce._cumulative_arc`, kept
    local (like ``_dist_to_polyline`` above) so this module never imports
    the placer at module scope.
    """
    cum: list[float] = [0.0]
    for i in range(1, len(points)):
        (x0, y0), (x1, y1) = points[i - 1], points[i]
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum


def _arc_on_polyline(
    points: Sequence[tuple[float, float]], cum: Sequence[float], p: tuple[float, float]
) -> float:
    """Arc length of the polyline point nearest ``p`` (clamped to the ends)."""
    best_t, best_d = 0.0, float("inf")
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        u = 0.0
        if seg2 > 1e-18:
            u = max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / seg2))
        d = math.hypot(p[0] - (x0 + u * dx), p[1] - (y0 + u * dy))
        if d < best_d:
            best_d, best_t = d, cum[i] + u * math.sqrt(seg2)
    return best_t


def _segment_end(segment: Segment, index: int) -> tuple[float, float]:
    """One endpoint of a segment (``0`` = start, ``1`` = end)."""
    end = getattr(segment, "start" if index == 0 else "end", (0.0, 0.0))
    return (float(end[0]), float(end[1]))


def _segment_length(segment: Segment) -> float:
    return math.dist(_segment_end(segment, 0), _segment_end(segment, 1))


class _CreditSpan(NamedTuple):
    """One judgement stretch of a segment along its own length (Issue #9654).

    ``t0``/``t1`` are arc distances from the segment's start; ``leg`` is the
    wired leg whose wire is credited over the stretch (``None`` = bare
    copper) and ``credit_mm`` the extra width it adds there.
    """

    t0: float
    t1: float
    leg: WiredLeg | None
    credit_mm: float


class PathAmpacityRule(DRCRule):
    """DRC rule verifying declared per-branch current paths.

    Attributes:
        rule_id: ``"path_ampacity"``
        name: ``"Branch Current-Path Ampacity"``
        specs: Declared current-path intents to check.
    """

    rule_id = "path_ampacity"
    name = "Branch Current-Path Ampacity"
    description = (
        "Verify declared branch-specific current paths resolve, are unambiguous, "
        "and meet IPC-2221 width for their own declared current"
    )

    def __init__(
        self,
        specs: Sequence[CurrentPathSpec] | None = None,
        wired_legs: Sequence[WiredLeg] | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            specs: Declared current-path intents. ``None`` or empty means
                no path is checked (clean no-op) -- matching the
                sidecar-optional contract established by ``AmpacityRule``.
            wired_legs: Buttress-wire legs judged fully wired by
                :meth:`~kicad_tools.validate.rules.reinforcement_incomplete
                .ReinforcementIncompleteRule.fully_wired_legs` (Issue
                #9648). A stretch of a segment whose centreline lies on
                such a leg **between its two anchors** is credited the
                wire's copper cross-section as extra trace width in both
                the IPC-2221 and Onderdonk fusing checks; the unwired
                hole-to-pad stubs outside the anchors stay at bare copper
                width (Issue #9654). ``None``/empty keeps every judgement
                at bare copper width (the pre-#9648 behaviour).
        """
        self.specs: list[CurrentPathSpec] = list(specs) if specs else []
        self.wired_legs: list[WiredLeg] = list(wired_legs) if wired_legs else []

    @staticmethod
    def _is_external_layer(layer: str) -> bool:
        return layer in _EXTERNAL_LAYERS

    def _required_width_mm(
        self,
        current_a: float,
        design_rules: DesignRules,
        *,
        external: bool,
    ) -> float:
        if external:
            return width_for_current(
                current_a,
                copper_weight_oz=design_rules.outer_copper_oz,
                layer="external",
            )
        return width_for_current(
            current_a,
            copper_weight_oz=design_rules.inner_copper_oz,
            layer="internal",
        )

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Check declared current paths against the routed board.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile.

        Returns:
            DRCResults with an ``error`` per under-width segment on a
            resolved path, an ``error`` per unresolved/ambiguous path, a
            ``warning`` per segment of a declared net not covered by any
            resolved path, and a ``warning`` per same-net unmodeled arc/pour
            object found on a declared net. Empty when no path is declared.
        """
        results = DRCResults(rules_checked=1)
        if not self.specs:
            return results

        from kicad_tools.router.current_paths import (
            STATUS_AMBIGUOUS,
            STATUS_UNRESOLVED,
            audit_current_paths,
        )

        # Thread the rules through so the packed tie-row grouping bound
        # follows the placer's own spacing rule (#8992) on this audit path.
        audit = audit_current_paths(pcb, self.specs, design_rules=design_rules)
        origin: tuple[float, float] = getattr(pcb, "board_origin", None) or (0.0, 0.0)

        # Buttress-wire credit (Issue #9648): a fully wired leg's wire
        # contributes its copper cross-section as extra trace width at the
        # layer's foil thickness -- the same summed-section reading doubled
        # copper already gets (#8790). A gauge this build cannot size is no
        # credit at all.
        wire_areas: list[tuple[WiredLeg, float]] = []
        for leg in self.wired_legs:
            try:
                diameter_mm = bare_copper_diameter_mm(leg.gauge)
            except ValueError:
                continue
            wire_areas.append((leg, math.pi * diameter_mm * diameter_mm / 4.0))

        for resolution in audit.resolutions:
            if resolution.status in (STATUS_UNRESOLVED, STATUS_AMBIGUOUS):
                results.add(self._unresolved_violation(resolution, origin))
                continue

            spec = resolution.spec
            thermal = spec.thermal_design_current()
            if thermal.assumption:
                results.add(self._assumption_notice(spec, thermal))
            if spec.pulsed_a is not None and spec.pulse_duration_s is None:
                results.add(self._unchecked_fusing_violation(spec))

            # A stitching via inside doubled copper carries no series current
            # (issue #8916); every other layer change is judged as before.
            for change in resolution.series_layer_changes:
                capacity_a = change.current_capacity_a()
                if capacity_a < thermal.current_a:
                    results.add(self._layer_change_violation(spec, thermal, change, capacity_a))

            required_cache: dict[bool, float] = {}
            grouped_ids = {
                id(seg) for group in resolution.parallel_groups for seg in group.segments
            }
            net_has_wired_leg = any(leg.net == spec.net_name for leg, _ in wire_areas)
            for group in resolution.parallel_groups:
                violation = self._parallel_group_violation(
                    spec, thermal, group, design_rules, required_cache
                )
                if violation is not None:
                    results.add(violation)
            for segment in resolution.segments:
                layer = getattr(segment, "layer", "F.Cu")
                external = self._is_external_layer(layer)
                actual_width_mm = getattr(segment, "width", 0.0)

                required_width_mm = required_cache.get(external)
                if required_width_mm is None:
                    required_width_mm = self._required_width_mm(
                        thermal.current_a, design_rules, external=external
                    )
                    required_cache[external] = required_width_mm

                credit_spans = self._segment_credit_spans(
                    spec,
                    segment,
                    layer,
                    external=external,
                    design_rules=design_rules,
                    wire_areas=wire_areas,
                )
                for span in credit_spans:
                    credit_leg = span.leg
                    credit_mm = span.credit_mm
                    effective_width_mm = actual_width_mm + credit_mm
                    location = self._span_midpoint(segment, span.t0, span.t1)

                    # Doubled copper is judged on its summed cross-section
                    # above, not branch by branch (issue #8790).
                    if effective_width_mm < required_width_mm and id(segment) not in grouped_ids:
                        results.add(
                            self._underwidth_violation(
                                spec=spec,
                                thermal=thermal,
                                segment=segment,
                                layer=layer,
                                external=external,
                                actual_width_mm=actual_width_mm,
                                effective_width_mm=effective_width_mm,
                                required_width_mm=required_width_mm,
                                design_rules=design_rules,
                                credit_leg=credit_leg,
                                net_has_wired_leg=net_has_wired_leg,
                                location=location,
                            )
                        )

                    fusing = self._fusing_violation(
                        spec=spec,
                        segment=segment,
                        layer=layer,
                        external=external,
                        actual_width_mm=effective_width_mm,
                        design_rules=design_rules,
                        credit_leg=credit_leg,
                        location=location,
                    )
                    if fusing is not None:
                        results.add(fusing)

        for net_name, segments in audit.uncovered.items():
            for segment in segments:
                results.add(self._uncovered_violation(net_name, segment))

        for net_name, unmodeled_items in audit.unmodeled.items():
            for item in unmodeled_items:
                results.add(self._unmodeled_copper_violation(net_name, item))

        return results

    def _parallel_group_violation(
        self,
        spec: CurrentPathSpec,
        thermal: ThermalDesignCurrent,
        group: ParallelGroup,
        design_rules: DesignRules,
        required_cache: dict[bool, float],
    ) -> DRCViolation | None:
        """Judge doubled copper on its effective cross-section (issues #8790, #9542).

        Each copper layer contributes its :meth:`ParallelGroup.layer_widths`
        share over that layer class's requirement: distinct-layer branches
        each contribute their narrowest width (so a pinch point limits its
        branch), while branches sharing a layer count once at their merged
        union extent -- two overlapping 1.0 mm F.Cu tracks offset 0.6 mm are
        1.6 mm of copper, not 2.0 mm. The group passes when the
        contributions sum to at least 1. Current is not split by resistance.
        """
        capacity = 0.0
        for layer, width in group.layer_widths().items():
            external = self._is_external_layer(layer)
            required = required_cache.get(external)
            if required is None:
                required = self._required_width_mm(
                    thermal.current_a, design_rules, external=external
                )
                required_cache[external] = required
            capacity += width / required if required > 0 else float("inf")
        if capacity >= 1.0:
            return None
        first = group.branches[0][0]
        loc_x, loc_y = self._segment_midpoint(first)
        noun = f"{group.shape} width"
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Current path {spec.name!r} ({spec.source.label()} -> "
                f"{spec.sink.label()}) {group.describe()} too narrow for "
                f"{thermal.description}: {noun} {group.width_mm:.3f}mm provides "
                f"{capacity:.0%} of the IPC-2221 requirement"
            ),
            location=(loc_x, loc_y),
            layer=first.layer,
            actual_value=capacity,
            required_value=1.0,
            items=(spec.name, spec.net_name),
        )

    def _layer_change_violation(
        self,
        spec: CurrentPathSpec,
        thermal: ThermalDesignCurrent,
        change: LayerChange,
        capacity_a: float,
    ) -> DRCViolation:
        """A layer change whose via(s) cannot carry the path's current (#8745).

        Error when the path is ``reinforcement_eligible`` (a force path),
        warning otherwise. Waivable via ``.kct_waivers.json`` like any other
        ``path_ampacity`` finding. The count is broken down into centreline
        and tie vias when the change grew through the #9561 pass, and nearby
        same-net vias it rejected are named with a reason so the reader can
        see why their stitching did not clear the finding.
        """
        via_clause = f"{change.count} via(s)"
        if change.tie_vias:
            via_clause += (
                f" ({change.centreline_count} on the centreline, "
                f"{len(change.tie_vias)} tie vias in the bar)"
            )
        excluded_clause = ""
        if change.excluded_tie_vias:
            detail = ", ".join(f"{n} {reason}" for reason, n in change.excluded_tie_vias)
            total = sum(n for _, n in change.excluded_tie_vias)
            excluded_clause = f"; {total} same-net via(s) not counted: {detail}"
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error" if spec.reinforcement_eligible else "warning",
            message=(
                f"Current path {spec.name!r} ({spec.source.label()} -> "
                f"{spec.sink.label()}) changes layer {change.from_layer} -> "
                f"{change.to_layer} through {via_clause} "
                f"(drill {change.drill:.3f}mm, pad {change.pad_diameter:.3f}mm) with "
                f"combined capacity {capacity_a:.2f}A, below {thermal.description} "
                f"(barrel model: {VIA_PLATING_THICKNESS_MM * 1000:.0f}um plating, "
                f"IPC-2221 external, 10C rise){excluded_clause}"
            ),
            location=change.position,
            layer=change.from_layer,
            actual_value=capacity_a,
            required_value=thermal.current_a,
            items=(spec.name, spec.net_name),
        )

    def _unresolved_violation(
        self, resolution: PathResolution, origin: tuple[float, float] = (0.0, 0.0)
    ) -> DRCViolation:
        """Report a path that could not be checked.

        ``location`` is ``None``, so the message is this finding's only
        coordinate surface: the witness is quoted at its sheet position
        (``origin`` is ``PCB.board_origin``), never board-relative (#8928).
        """
        from kicad_tools.router.current_paths import describe_witness

        spec = resolution.spec
        status = resolution.status
        reason = resolution.reason + describe_witness(resolution.witness, origin)
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Current path {spec.name!r} on net {spec.net_name!r} "
                f"({spec.source.label()} -> {spec.sink.label()}) is {status}: {reason}"
            ),
            location=None,
            layer="",
            actual_value=None,
            required_value=None,
            items=(spec.name, spec.net_name),
        )

    @staticmethod
    def _segment_midpoint(segment: Segment) -> tuple[float, float]:
        start = tuple(getattr(segment, "start", (0.0, 0.0)))
        end = tuple(getattr(segment, "end", (0.0, 0.0)))
        return (
            round((start[0] + end[0]) / 2.0, 3),
            round((start[1] + end[1]) / 2.0, 3),
        )

    @staticmethod
    def _span_midpoint(segment: Segment, t0: float, t1: float) -> tuple[float, float]:
        """Midpoint of the ``t0``..``t1`` arc stretch of ``segment``.

        Identical to :meth:`_segment_midpoint` for a full-length stretch,
        so findings that are not split keep their exact pre-#9654
        locations.
        """
        seg_len = _segment_length(segment)
        if t0 <= 1e-9 and t1 >= seg_len - 1e-9:
            return PathAmpacityRule._segment_midpoint(segment)
        start = _segment_end(segment, 0)
        end = _segment_end(segment, 1)
        f = (t0 + t1) / 2.0 / seg_len if seg_len > 1e-9 else 0.0
        return (
            round(start[0] + (end[0] - start[0]) * f, 3),
            round(start[1] + (end[1] - start[1]) * f, 3),
        )

    def _segment_credit_spans(
        self,
        spec: CurrentPathSpec,
        segment: Segment,
        layer: str,
        *,
        external: bool,
        design_rules: DesignRules,
        wire_areas: Sequence[tuple[WiredLeg, float]],
    ) -> list[_CreditSpan]:
        """The judgement stretches of ``segment`` against the wired legs.

        A segment is covered by a leg when its whole centreline -- start,
        midpoint and end alike -- lies within the leg's corridor
        (:data:`~kicad_tools.validate.rules.reinforcement_incomplete
        .ANCHOR_ON_LEG_SLACK_MM` beyond the leg's half-width) on the same
        layer and net **and** it follows that leg's polyline -- both of its
        endpoints project onto distinct arc positions of it. A segment
        that merely crosses the corridor (a perpendicular or angled branch
        stub whose midpoint is inside the trunk's corridor while its far
        endpoint leaves it) does not follow the trunk and is never
        credited by the trunk's wire: of the legs that match, the one the
        segment runs along -- the smallest off-corridor deviation,
        whatever the order of ``wired_legs`` -- judges it, and bare copper
        does when no leg does. But the wire is only soldered between the
        leg's two anchors, so only the sub-span of the segment projected
        inside the leg's :attr:`WiredLeg.anchor_arc` range is credited;
        the hole-to-pad stubs outside it are judged at bare copper width
        (Issue #9654). Each credited stretch gets the wire's copper
        cross-section expressed as extra trace width at the layer's foil
        thickness, so adding it to the copper width sums the two
        sections -- exactly how ``width_for_current`` derives width from
        required area. A single bare span when no wired leg covers the
        segment.
        """
        seg_len = _segment_length(segment)
        bare = _CreditSpan(0.0, seg_len, None, 0.0)
        start, end = _segment_end(segment, 0), _segment_end(segment, 1)
        midpoint = self._segment_midpoint(segment)
        # Every leg whose corridor holds the segment's whole centreline
        # and whose polyline it follows; the winner is picked below,
        # never by the order of ``wired_legs``.
        candidates: list[tuple[float, WiredLeg, float]] = []
        for leg, area_mm2 in wire_areas:
            if leg.net != spec.net_name or leg.layer != layer:
                continue
            deviation_mm = max(_dist_to_polyline(leg.points, p) for p in (start, midpoint, end))
            if deviation_mm > (leg.width / 2.0 + ANCHOR_ON_LEG_SLACK_MM):
                continue
            if leg.anchor_arc is not None and seg_len > 1e-9:
                cum = _cumulative_arc(leg.points)
                if (
                    abs(
                        _arc_on_polyline(leg.points, cum, end)
                        - _arc_on_polyline(leg.points, cum, start)
                    )
                    <= 1e-6
                ):
                    # Degenerate projection: the segment meets this
                    # polyline side-on (both endpoints project to one
                    # polyline point), so it does not follow this leg --
                    # a branch stub inside the trunk's corridor must be
                    # judged by the leg it follows, never credited by the
                    # trunk's wire.
                    continue
            candidates.append((deviation_mm, leg, area_mm2))
        if not candidates:
            return [bare]
        # The leg the segment actually runs along has the smallest
        # off-corridor deviation (about 0 when it follows the polyline);
        # ties break on the leg's own geometry, so the pick -- and the
        # verdict -- never depends on the order of ``wired_legs``.
        _, leg, area_mm2 = min(
            candidates,
            key=lambda c: (
                c[0],
                c[1].points,
                c[1].anchor_arc or (),
                c[1].width,
                c[1].gauge,
            ),
        )
        thickness_mm = copper_thickness_from_oz(
            self._copper_weight_oz(design_rules, external=external)
        )
        if thickness_mm <= 0:
            return [bare]
        credit_mm = area_mm2 / thickness_mm
        full = _CreditSpan(0.0, seg_len, leg, credit_mm)
        if leg.anchor_arc is None or seg_len <= 1e-9:
            # No anchor range recorded: credit the whole segment, the
            # pre-#9654 reading.
            return [full]
        a0, a1 = sorted(leg.anchor_arc)
        cum = _cumulative_arc(leg.points)
        t_start = _arc_on_polyline(leg.points, cum, start)
        t_end = _arc_on_polyline(leg.points, cum, end)
        lo, hi = (t_start, t_end) if t_start < t_end else (t_end, t_start)
        c0, c1 = max(lo, a0), min(hi, a1)
        if c1 - c0 <= 1e-6:  # no overlap with the wired span: bare copper
            return [bare]
        if lo >= a0 - 1e-6 and hi <= a1 + 1e-6:  # fully between the anchors
            return [full]

        def frac(u: float) -> float:
            return min(1.0, max(0.0, (u - t_start) / (t_end - t_start)))

        spans: list[_CreditSpan] = []
        for u_from, u_to in ((lo, c0), (c1, hi)):
            f0, f1 = sorted((frac(u_from), frac(u_to)))
            if f1 - f0 > 1e-6:
                spans.append(_CreditSpan(f0 * seg_len, f1 * seg_len, None, 0.0))
        spans.append(_CreditSpan(frac(c0) * seg_len, frac(c1) * seg_len, leg, credit_mm))
        spans.sort()
        return spans

    @staticmethod
    def _copper_weight_oz(design_rules: DesignRules, *, external: bool) -> float:
        return design_rules.outer_copper_oz if external else design_rules.inner_copper_oz

    def _underwidth_violation(
        self,
        *,
        spec: CurrentPathSpec,
        thermal: ThermalDesignCurrent,
        segment: Segment,
        layer: str,
        external: bool,
        actual_width_mm: float,
        effective_width_mm: float,
        required_width_mm: float,
        design_rules: DesignRules,
        credit_leg: WiredLeg | None = None,
        net_has_wired_leg: bool = False,
        location: tuple[float, float] | None = None,
    ) -> DRCViolation:
        layer_class = "external" if external else "internal"
        copper_weight_oz = self._copper_weight_oz(design_rules, external=external)
        if location is None:
            location = self._segment_midpoint(segment)
        loc_x, loc_y = location

        if credit_leg is not None:
            width_clause = (
                f"width {actual_width_mm:.3f}mm + {credit_leg.gauge} AWG wire "
                f"= {effective_width_mm:.3f}mm effective"
            )
            actual_value: float | None = effective_width_mm
        else:
            width_clause = f"width {actual_width_mm:.3f}mm"
            actual_value = actual_width_mm
            if net_has_wired_leg:
                # This stretch is judged at bare copper on a net whose wire
                # legs exist elsewhere: say which stretch failed (#9648).
                width_clause += "; this stretch is not covered by a wire leg"

        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Current path {spec.name!r} ({spec.source.label()} -> "
                f"{spec.sink.label()}) trace on {layer} too narrow for "
                f"{thermal.description}: {width_clause}, requires "
                f"{required_width_mm:.3f}mm (IPC-2221, {copper_weight_oz}oz {layer_class})"
            ),
            location=(loc_x, loc_y),
            layer=layer,
            actual_value=actual_value,
            required_value=required_width_mm,
            items=(spec.name, spec.net_name),
        )

    def _fusing_violation(
        self,
        *,
        spec: CurrentPathSpec,
        segment: Segment,
        layer: str,
        external: bool,
        actual_width_mm: float,
        design_rules: DesignRules,
        credit_leg: WiredLeg | None = None,
        location: tuple[float, float] | None = None,
    ) -> DRCViolation | None:
        """Onderdonk adiabatic fusing check for one segment of a pulsed path.

        Returns ``None`` when the path declares no pulse duration (the
        check cannot be performed -- reported once per path by
        :meth:`_unchecked_fusing_violation` instead), or when the segment
        survives the declared pulse. ``actual_width_mm`` is the credited
        effective width when a buttress wire covers the stretch, so the
        fusing current is that of the combined section (Issue #9648).
        """
        if spec.pulsed_a is None or spec.pulse_duration_s is None:
            return None
        if actual_width_mm <= 0:
            return None

        copper_weight_oz = self._copper_weight_oz(design_rules, external=external)
        fusing_a = adiabatic_fusing_current(
            actual_width_mm,
            copper_weight_oz=copper_weight_oz,
            duration_s=spec.pulse_duration_s,
        )
        if spec.pulsed_a < fusing_a:
            return None

        if credit_leg is not None:
            section_clause = (
                f"{actual_width_mm:.3f}mm effective of {copper_weight_oz}oz copper "
                f"+ {credit_leg.gauge} AWG wire"
            )
        else:
            section_clause = f"{actual_width_mm:.3f}mm of {copper_weight_oz}oz copper"

        if location is None:
            location = self._segment_midpoint(segment)
        loc_x, loc_y = location
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Current path {spec.name!r} ({spec.source.label()} -> "
                f"{spec.sink.label()}) trace on {layer} would fuse: declared "
                f"{spec.pulsed_a:.4g}A pulse for {spec.pulse_duration_s:.4g}s meets or "
                f"exceeds the {fusing_a:.4g}A adiabatic fusing current of "
                f"{section_clause} (Onderdonk)"
            ),
            location=(loc_x, loc_y),
            layer=layer,
            actual_value=spec.pulsed_a,
            required_value=fusing_a,
            items=(spec.name, spec.net_name),
        )

    def _unchecked_fusing_violation(self, spec: CurrentPathSpec) -> DRCViolation:
        """A pulsed path with no declared duration leaves fusing unchecked.

        A warning, not an error: the declaration is legal and the thermal
        check still ran. But an unmodeled destruction mode must be visible
        -- the same reason uncovered copper is reported rather than waived.
        """
        return DRCViolation(
            rule_id=self.rule_id,
            severity="warning",
            message=(
                f"Current path {spec.name!r} on net {spec.net_name!r} declares a "
                f"{spec.pulsed_a:.4g}A pulse but no 'pulse_duration_s' -- adiabatic "
                f"fusing survivability was NOT checked for this branch"
            ),
            location=None,
            layer="",
            actual_value=None,
            required_value=None,
            items=(spec.name, spec.net_name),
        )

    def _assumption_notice(
        self, spec: CurrentPathSpec, thermal: ThermalDesignCurrent
    ) -> DRCViolation:
        """Disclose a waveform assumption the declaration did not supply.

        ``info`` severity: the substitution is conservative, so it never
        fails the gate -- but a reader must be able to see *why* a branch
        is being sized at its peak rather than assume a duty cycle was
        taken into account.
        """
        return DRCViolation(
            rule_id=self.rule_id,
            severity="info",
            message=(
                f"Current path {spec.name!r} on net {spec.net_name!r} sized at "
                f"{thermal.description}: {thermal.assumption}"
            ),
            location=None,
            layer="",
            actual_value=None,
            required_value=None,
            items=(spec.name, spec.net_name),
        )

    def _uncovered_violation(self, net_name: str, segment: Segment) -> DRCViolation:
        loc_x, loc_y = self._segment_midpoint(segment)
        layer = getattr(segment, "layer", "F.Cu")

        return DRCViolation(
            rule_id=self.rule_id,
            severity="warning",
            message=(
                f"Net {net_name!r} has declared current path(s) but this segment on "
                f"{layer} is not covered by any resolved path -- unmodeled/unverified "
                f"current-carrying copper"
            ),
            location=(loc_x, loc_y),
            layer=layer,
            actual_value=None,
            required_value=None,
            items=(net_name,),
        )

    def _unmodeled_copper_violation(self, net_name: str, item: UnmodeledCopper) -> DRCViolation:
        """One same-net arc/pour object :func:`resolve_current_path` never models.

        A warning, not an error: the ``ambiguous`` status this copper
        already forces onto every declared path on the net is the error
        (via :meth:`_unresolved_violation`). This finding's job is only to
        name the specific object responsible, since the status alone does
        not say *why* a branch that used to resolve cleanly no longer does.
        """
        return DRCViolation(
            rule_id=self.rule_id,
            severity="warning",
            message=(
                f"Net {net_name!r} has declared current path(s) but carries a same-net "
                f"{item.kind} on {item.layer} that is not modeled by the declared-path "
                "graph -- it may form a parallel return path around a declared branch"
            ),
            location=item.location,
            layer=item.layer,
            actual_value=None,
            required_value=None,
            items=(net_name,),
        )

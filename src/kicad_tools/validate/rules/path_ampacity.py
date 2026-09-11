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

In addition, any net that has at least one declared path but also carries
routed copper NOT covered by any resolved path is flagged with a
``warning``-severity "uncovered copper" finding per orphan segment -- the
issue's "unknown current paths must be visible rather than silently
waived" requirement. This is a warning, not an error: uncovered copper on
a net that also has declared paths is usually deliberate (an unmodeled
low-current branch the designer hasn't gotten around to declaring yet),
not necessarily a defect -- but it must never be silent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.physics.ampacity import width_for_current

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

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
    from kicad_tools.router.current_paths import CurrentPathSpec, PathResolution
    from kicad_tools.schema.pcb import PCB, Segment

# Matches AmpacityRule / manufacturers/dru_generator.py's external/internal
# copper-layer split exactly, so all three producers of an IPC-2221 width
# floor agree for a given net/copper-weight/layer.
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


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

    def __init__(self, specs: Sequence[CurrentPathSpec] | None = None) -> None:
        """Initialize the rule.

        Args:
            specs: Declared current-path intents. ``None`` or empty means
                no path is checked (clean no-op) -- matching the
                sidecar-optional contract established by ``AmpacityRule``.
        """
        self.specs: list[CurrentPathSpec] = list(specs) if specs else []

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
            resolved path, an ``error`` per unresolved/ambiguous path, and
            a ``warning`` per segment of a declared net not covered by any
            resolved path. Empty when no path is declared.
        """
        results = DRCResults(rules_checked=1)
        if not self.specs:
            return results

        from kicad_tools.router.current_paths import (
            STATUS_AMBIGUOUS,
            STATUS_UNRESOLVED,
            audit_current_paths,
        )

        audit = audit_current_paths(pcb, self.specs)

        for resolution in audit.resolutions:
            if resolution.status in (STATUS_UNRESOLVED, STATUS_AMBIGUOUS):
                results.add(self._unresolved_violation(resolution))
                continue

            spec = resolution.spec
            required_cache: dict[bool, float] = {}
            for segment in resolution.segments:
                layer = getattr(segment, "layer", "F.Cu")
                external = self._is_external_layer(layer)
                required_width_mm = required_cache.get(external)
                if required_width_mm is None:
                    required_width_mm = self._required_width_mm(
                        spec.continuous_a, design_rules, external=external
                    )
                    required_cache[external] = required_width_mm

                actual_width_mm = getattr(segment, "width", 0.0)
                if actual_width_mm >= required_width_mm:
                    continue

                results.add(
                    self._underwidth_violation(
                        spec=spec,
                        segment=segment,
                        layer=layer,
                        external=external,
                        actual_width_mm=actual_width_mm,
                        required_width_mm=required_width_mm,
                        design_rules=design_rules,
                    )
                )

        for net_name, segments in audit.uncovered.items():
            for segment in segments:
                results.add(self._uncovered_violation(net_name, segment))

        return results

    def _unresolved_violation(self, resolution: PathResolution) -> DRCViolation:
        spec = resolution.spec
        status = resolution.status
        reason = resolution.reason
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

    def _underwidth_violation(
        self,
        *,
        spec: CurrentPathSpec,
        segment: Segment,
        layer: str,
        external: bool,
        actual_width_mm: float,
        required_width_mm: float,
        design_rules: DesignRules,
    ) -> DRCViolation:
        layer_class = "external" if external else "internal"
        copper_weight_oz = (
            design_rules.outer_copper_oz if external else design_rules.inner_copper_oz
        )
        start = tuple(getattr(segment, "start", (0.0, 0.0)))
        end = tuple(getattr(segment, "end", (0.0, 0.0)))
        loc_x = round((start[0] + end[0]) / 2.0, 3)
        loc_y = round((start[1] + end[1]) / 2.0, 3)

        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"Current path {spec.name!r} ({spec.source.label()} -> "
                f"{spec.sink.label()}) trace on {layer} too narrow for "
                f"{spec.continuous_a:.2f}A: width {actual_width_mm:.3f}mm, requires "
                f"{required_width_mm:.3f}mm (IPC-2221, {copper_weight_oz}oz {layer_class})"
            ),
            location=(loc_x, loc_y),
            layer=layer,
            actual_value=actual_width_mm,
            required_value=required_width_mm,
            items=(spec.name, spec.net_name),
        )

    def _uncovered_violation(self, net_name: str, segment: Segment) -> DRCViolation:
        start = tuple(getattr(segment, "start", (0.0, 0.0)))
        end = tuple(getattr(segment, "end", (0.0, 0.0)))
        loc_x = round((start[0] + end[0]) / 2.0, 3)
        loc_y = round((start[1] + end[1]) / 2.0, 3)
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

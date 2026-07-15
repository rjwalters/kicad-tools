"""Ampacity validation DRC rule.

Issue #4217 (Part 3 of #4215).

Verifies that every routed copper segment of a net whose class declares a
``target_ampacity`` is at least as wide as the IPC-2221-derived minimum
width required to carry that current at the board's copper weight and for
the segment's layer position (external vs internal).

The required-width derivation reuses
:func:`kicad_tools.physics.ampacity.width_for_current` — the *identical*
function (and the identical ``outer_copper_oz`` / ``inner_copper_oz`` +
external/internal-layer split) that
:func:`kicad_tools.manufacturers.dru_generator.generate_dru` uses to emit
net-scoped ``.kicad_dru`` ``track_width`` rules.  Because both paths feed
the same inputs to the same closed-form formula, the Python check and the
emitted KiCad custom rule agree on the required width for a given
net / copper-weight / layer.

Example::

    from kicad_tools.validate.rules.ampacity import AmpacityRule
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.manufacturers import DesignRules

    pcb = PCB.load("board.kicad_pcb")
    design_rules = DesignRules.jlcpcb_2layer()

    rule = AmpacityRule(specs={"VBUS": 15.0})
    results = rule.check(pcb, design_rules)

    for err in results.errors:
        print(f"Ampacity error: {err.message}")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.physics.ampacity import width_for_current

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

# Copper layers that shed heat from a single exposed face.  IPC-2221 uses
# a higher current constant (k=0.048) for these outer layers; everything
# else (In*.Cu) is treated as internal (k=0.024), buried between
# dielectric and therefore needing a wider trace for the same current.
# This binary split matches ``manufacturers/dru_generator.py`` exactly so
# the check and the emitted ``.kicad_dru`` rule agree.
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


class AmpacityRule(DRCRule):
    """DRC rule verifying routed trace widths meet an ampacity target.

    For each net that declares a ``target_ampacity`` (in amps), derives
    the IPC-2221 minimum trace width for the board's copper weight and the
    segment's layer position, then compares each routed segment's actual
    width against that floor.  A per-segment ``error`` finding is emitted
    for every segment narrower than the required width — an under-width
    high-current trace is a real thermal hazard, not a soft tolerance
    band, so there is no warning tier.

    Attributes:
        rule_id: ``"ampacity"``
        name: ``"Ampacity Compliance"``
        specs: ``{net_name: target_ampacity}`` map of nets to check.
    """

    rule_id = "ampacity"
    name = "Ampacity Compliance"
    description = "Verify routed copper width carries a net's target current (IPC-2221)"

    def __init__(self, specs: dict[str, float] | None = None) -> None:
        """Initialize the ampacity rule.

        Args:
            specs: ``{net_name: target_ampacity}`` map (currents in amps).
                Typically produced by
                :func:`kicad_tools.validate.ampacity_specs.derive_ampacity_specs`
                from a ``NetClassRouting`` map.  ``None`` or empty means
                no net is checked (clean no-op) — the standalone
                ``kct check`` contract when no sidecar is supplied.
        """
        self.specs: dict[str, float] = specs or {}

    @staticmethod
    def _is_external_layer(layer: str) -> bool:
        """Return True for outer copper (F.Cu / B.Cu), False for internal.

        Matches the binary external/internal split
        ``manufacturers/dru_generator.py`` uses: any copper layer that is
        not ``F.Cu`` / ``B.Cu`` counts as internal for IPC-2221's k
        constant (there is no third "unknown" bucket).
        """
        return layer in _EXTERNAL_LAYERS

    def _required_width_mm(
        self,
        current_a: float,
        design_rules: DesignRules,
        *,
        external: bool,
    ) -> float:
        """Derive the IPC-2221 minimum trace width (mm) for a target current.

        Deliberately kept as a standalone step, separate from the
        width-comparison loop in :meth:`check` — a future reinforcement
        credit (Unit E of #4218/#4220) needs to intercept the per-segment
        comparison to subtract wire-carried current, which requires these
        two concerns to stay unfused.

        Reuses the exact ``width_for_current`` call shape from
        :func:`kicad_tools.manufacturers.dru_generator.generate_dru`
        (external -> ``outer_copper_oz`` / ``layer="external"``; internal
        -> ``inner_copper_oz`` / ``layer="internal"``; default 10 C rise)
        so the check and the ``.kicad_dru`` generator agree.
        """
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

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check PCB traces against their nets' ampacity targets.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile
                (supplies ``outer_copper_oz`` / ``inner_copper_oz``).

        Returns:
            DRCResults with a per-segment ``error`` for every routed
            segment narrower than its net's IPC-2221 required width.
            Empty when no net declares a target (the standalone-CLI common
            case).
        """
        results = DRCResults(rules_checked=1)

        if not self.specs:
            return results

        # Cache the required width per (net, layer-class) so we call the
        # IPC-2221 solver once per pair rather than once per segment.
        required_cache: dict[tuple[str, bool], float] = {}

        for segment in getattr(pcb, "segments", []):
            net_name = getattr(segment, "net_name", None)
            if not net_name or net_name not in self.specs:
                continue

            current_a = self.specs[net_name]
            layer = getattr(segment, "layer", "F.Cu")
            external = self._is_external_layer(layer)

            cache_key = (net_name, external)
            required_width_mm = required_cache.get(cache_key)
            if required_width_mm is None:
                required_width_mm = self._required_width_mm(
                    current_a, design_rules, external=external
                )
                required_cache[cache_key] = required_width_mm

            actual_width_mm = getattr(segment, "width", 0.0)

            # ``>=``: a segment exactly at the floor passes (meets-or-
            # exceeds, matching DimensionRules and other width-floor rules).
            if actual_width_mm >= required_width_mm:
                continue

            results.add(
                self._create_violation(
                    net_name=net_name,
                    layer=layer,
                    external=external,
                    current_a=current_a,
                    actual_width_mm=actual_width_mm,
                    required_width_mm=required_width_mm,
                    design_rules=design_rules,
                    segment=segment,
                )
            )

        return results

    def _create_violation(
        self,
        *,
        net_name: str,
        layer: str,
        external: bool,
        current_a: float,
        actual_width_mm: float,
        required_width_mm: float,
        design_rules: DesignRules,
        segment: object,
    ) -> DRCViolation:
        """Build a per-segment ampacity violation."""
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
                f"Trace on {layer} too narrow for {current_a:.1f}A: "
                f"width {actual_width_mm:.3f}mm, requires {required_width_mm:.3f}mm "
                f"(IPC-2221, {copper_weight_oz}oz {layer_class})"
            ),
            location=(loc_x, loc_y),
            layer=layer,
            actual_value=actual_width_mm,
            required_value=required_width_mm,
            items=(net_name,),
        )

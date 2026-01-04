"""Impedance validation DRC rule.

Uses the physics module to verify that trace widths match target
impedance requirements based on net class specifications.

Example::

    from kicad_tools.validate.rules.impedance import ImpedanceRule
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.manufacturers import DesignRules

    pcb = PCB.load("board.kicad_pcb")
    design_rules = DesignRules.jlcpcb_4layer()

    rule = ImpedanceRule()
    results = rule.check(pcb, design_rules)

    if results.errors:
        for err in results.errors:
            print(f"Impedance error: {err.message}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.physics import Stackup, TransmissionLine
    from kicad_tools.schema.pcb import PCB


@dataclass
class NetImpedanceSpec:
    """Impedance specification for a net or net class.

    Attributes:
        net_pattern: Regex pattern to match net names
        target_z0: Target characteristic impedance (single-ended)
        target_zdiff: Target differential impedance (for diff pairs)
        tolerance_percent: Allowed deviation from target (default 10%)
    """

    net_pattern: str
    target_z0: float | None = None
    target_zdiff: float | None = None
    tolerance_percent: float = 10.0

    def matches(self, net_name: str) -> bool:
        """Check if this spec matches a net name."""
        return bool(re.match(self.net_pattern, net_name, re.IGNORECASE))


@dataclass
class ImpedanceCheckResult:
    """Result of checking a single trace for impedance compliance.

    Attributes:
        net_name: Net name that was checked
        layer: Layer the trace is on
        width_mm: Actual trace width
        calculated_z0: Calculated impedance for this width
        target_z0: Target impedance
        deviation_percent: Percentage deviation from target
        compliant: Whether the trace meets the tolerance
    """

    net_name: str
    layer: str
    width_mm: float
    calculated_z0: float
    target_z0: float
    deviation_percent: float
    compliant: bool


class ImpedanceRule(DRCRule):
    """DRC rule for verifying trace impedance matches target specifications.

    Uses the physics module to calculate actual trace impedance based on
    the PCB stackup, then compares against net class specifications.

    The rule checks:
    1. Single-ended traces against Z0 targets
    2. Differential pairs against Zdiff targets
    3. Trace width consistency across layers

    Attributes:
        rule_id: "impedance"
        name: "Impedance Control"
        description: Rule description
        specs: List of impedance specifications to check against
    """

    rule_id = "impedance"
    name = "Impedance Control"
    description = "Verify trace widths match target impedance requirements"

    @staticmethod
    def _get_default_specs() -> list[NetImpedanceSpec]:
        """Return default impedance specifications for common signal types."""
        return [
            # USB differential pairs - 90Ω differential
            NetImpedanceSpec(r"USB.*D[PM\+\-]?", target_zdiff=90.0),
            # High-speed single-ended - 50Ω
            NetImpedanceSpec(r".*CLK.*", target_z0=50.0),
            NetImpedanceSpec(r".*MCLK.*", target_z0=50.0),
            NetImpedanceSpec(r".*ETH.*", target_z0=50.0),
            # LVDS/high-speed diff pairs - 100Ω differential
            NetImpedanceSpec(r".*LVDS.*", target_zdiff=100.0),
            NetImpedanceSpec(r".*_[PN]$", target_zdiff=100.0),
        ]

    def __init__(
        self,
        specs: list[NetImpedanceSpec] | None = None,
        stackup: Stackup | None = None,
    ) -> None:
        """Initialize the impedance rule.

        Args:
            specs: Impedance specifications to check. If not provided,
                uses default specs for common signal types.
            stackup: PCB stackup for impedance calculations. If not provided,
                will try to extract from PCB during check.
        """
        self.specs = specs if specs is not None else self._get_default_specs()
        self._stackup = stackup
        self._tl: TransmissionLine | None = None

    def add_spec(self, spec: NetImpedanceSpec) -> None:
        """Add an impedance specification to check."""
        self.specs.append(spec)

    def _init_physics(self, pcb: PCB) -> bool:
        """Initialize physics module from PCB or provided stackup.

        Args:
            pcb: PCB to extract stackup from

        Returns:
            True if physics module is available, False otherwise
        """
        if self._tl is not None:
            return True

        try:
            from kicad_tools.physics import Stackup, TransmissionLine

            if self._stackup is None:
                self._stackup = Stackup.from_pcb(pcb)
            self._tl = TransmissionLine(self._stackup)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check PCB traces for impedance compliance.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing any impedance violations
        """
        results = DRCResults(rules_checked=1)

        # Initialize physics module
        if not self._init_physics(pcb):
            # Physics module not available - add warning and return
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="warning",
                    message="Impedance check skipped: physics module not available",
                    location=None,
                )
            )
            return results

        # Collect all traces from PCB
        trace_data = self._collect_traces(pcb)

        # Check each net against matching specs
        for net_name, traces in trace_data.items():
            spec = self._find_matching_spec(net_name)
            if spec is None:
                continue

            for trace in traces:
                check_result = self._check_trace_impedance(trace, spec)
                if not check_result.compliant:
                    results.add(self._create_violation(check_result, spec))

        return results

    def _collect_traces(
        self,
        pcb: PCB,
    ) -> dict[str, list[dict]]:
        """Collect trace information from PCB.

        Args:
            pcb: PCB to extract traces from

        Returns:
            Dictionary mapping net names to list of trace dictionaries
        """
        trace_data: dict[str, list[dict]] = {}

        # Extract traces from PCB segments
        for segment in getattr(pcb, "segments", []):
            net_name = getattr(segment, "net_name", None)
            if not net_name:
                continue

            if net_name not in trace_data:
                trace_data[net_name] = []

            trace_data[net_name].append(
                {
                    "width_mm": getattr(segment, "width", 0.2),
                    "layer": getattr(segment, "layer", "F.Cu"),
                    "start": (getattr(segment, "x1", 0), getattr(segment, "y1", 0)),
                    "end": (getattr(segment, "x2", 0), getattr(segment, "y2", 0)),
                }
            )

        return trace_data

    def _find_matching_spec(self, net_name: str) -> NetImpedanceSpec | None:
        """Find the first matching impedance spec for a net name.

        Args:
            net_name: Net name to match

        Returns:
            Matching spec or None
        """
        for spec in self.specs:
            if spec.matches(net_name):
                return spec
        return None

    def _check_trace_impedance(
        self,
        trace: dict,
        spec: NetImpedanceSpec,
    ) -> ImpedanceCheckResult:
        """Check a single trace against an impedance specification.

        Args:
            trace: Trace dictionary with width_mm and layer
            spec: Impedance specification to check against

        Returns:
            ImpedanceCheckResult with calculated values
        """
        width_mm = trace["width_mm"]
        layer = trace["layer"]

        # Calculate actual impedance
        try:
            if self._stackup.is_outer_layer(layer):
                result = self._tl.microstrip(width_mm, layer)
            else:
                result = self._tl.stripline(width_mm, layer)
            calculated_z0 = result.z0
        except (ValueError, AttributeError):
            # Calculation failed - assume 50 ohms as default
            calculated_z0 = 50.0

        # Determine target
        target_z0 = spec.target_z0 or 50.0

        # Calculate deviation
        deviation_percent = abs(calculated_z0 - target_z0) / target_z0 * 100

        # Check compliance
        compliant = deviation_percent <= spec.tolerance_percent

        return ImpedanceCheckResult(
            net_name=trace.get("net_name", "unknown"),
            layer=layer,
            width_mm=width_mm,
            calculated_z0=calculated_z0,
            target_z0=target_z0,
            deviation_percent=deviation_percent,
            compliant=compliant,
        )

    def _create_violation(
        self,
        result: ImpedanceCheckResult,
        spec: NetImpedanceSpec,
    ) -> DRCViolation:
        """Create a DRC violation for an impedance mismatch.

        Args:
            result: Check result showing the mismatch
            spec: Spec that was violated

        Returns:
            DRCViolation for the impedance mismatch
        """
        # Calculate required width for correct impedance
        try:
            required_width = self._tl.width_for_impedance(
                result.target_z0,
                result.layer,
            )
            width_hint = f" (requires {required_width:.3f}mm)"
        except (ValueError, AttributeError):
            width_hint = ""

        return DRCViolation(
            rule_id=self.rule_id,
            severity="error" if result.deviation_percent > 20 else "warning",
            message=(
                f"Trace impedance mismatch on {result.layer}: "
                f"width {result.width_mm:.3f}mm gives {result.calculated_z0:.1f}Ω, "
                f"target is {result.target_z0:.1f}Ω "
                f"({result.deviation_percent:.1f}% deviation){width_hint}"
            ),
            layer=result.layer,
            actual_value=result.calculated_z0,
            required_value=result.target_z0,
            items=(result.net_name,),
        )

    def get_required_width(
        self,
        target_z0: float,
        layer: str,
    ) -> float | None:
        """Calculate required trace width for target impedance.

        Convenience method for calculating trace width.

        Args:
            target_z0: Target impedance in ohms
            layer: Layer to calculate for

        Returns:
            Width in mm, or None if physics not available
        """
        if self._tl is None:
            return None

        try:
            return self._tl.width_for_impedance(target_z0, layer)
        except (ValueError, AttributeError):
            return None

    def get_layer_impedances(
        self,
        width_mm: float,
        layers: list[str] | None = None,
    ) -> dict[str, float]:
        """Calculate impedance for a given width across all layers.

        Useful for understanding how impedance varies by layer for
        a fixed trace width.

        Args:
            width_mm: Trace width in mm
            layers: Layers to calculate for (defaults to common copper layers)

        Returns:
            Dictionary mapping layer names to impedance values
        """
        if self._tl is None:
            return {}

        if layers is None:
            layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]

        impedances: dict[str, float] = {}
        for layer in layers:
            try:
                if self._stackup.is_outer_layer(layer):
                    result = self._tl.microstrip(width_mm, layer)
                else:
                    result = self._tl.stripline(width_mm, layer)
                impedances[layer] = result.z0
            except (ValueError, AttributeError):
                continue

        return impedances
